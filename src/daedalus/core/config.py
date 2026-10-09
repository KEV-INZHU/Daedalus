"""Repository configuration: `.daedalus.yml` -> effective policy.

The user-facing file is deliberately small:

    enabled: true          # gate on/off for harness hooks (not part of policy)
    mode: gate             # default mode for new runs: gate | review | council
    adapter: claude-code   # agent harness for `daedalus run` / `daedalus review`
    checks:                # trusted checks; replaces the default list
      tests: python -m pytest -q
      typecheck: {command: [mypy, src], mandatory: true}
    policy: {...}          # optional overrides of any default-policy key
    policy_file: path.yaml # or: a complete policy file instead of the default

Everything that is not UX (enabled/mode/adapter/harness) becomes the policy
whose hash every run pins. With no `.daedalus.yml`, Daedalus uses the default
policy and detects a test command, so `daedalus run "..."` works zero-config.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from daedalus.core.contracts import MODES
from daedalus.core.errors import PolicyError
from daedalus.core.policy import Policy, default_policy_text, policy_from_dict

CONFIG_FILE = ".daedalus.yml"
STATE_DIR = ".daedalus"


@dataclass(frozen=True)
class RepoConfig:
    root: Path
    enabled: bool
    mode: str
    adapter: dict[str, Any]
    harness: dict[str, Any]
    policy_raw: dict[str, Any]
    source: str  # file | default
    notes: tuple[str, ...] = field(default=())

    @property
    def policy(self) -> Policy:
        return policy_from_dict(self.policy_raw)


def find_root(start: str | Path) -> Path:
    """The git toplevel, else the nearest directory holding .daedalus.yml, else start."""
    start = Path(start).resolve()
    try:
        r = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"], capture_output=True, text=True
        )
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip()).resolve()
    except FileNotFoundError:
        pass
    for d in (start, *start.parents):
        if (d / CONFIG_FILE).exists():
            return d
    return start


def _check_entry(cid: str, value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return {"command": shlex.split(value)}
    if isinstance(value, list):
        return {"command": [str(x) for x in value]}
    if isinstance(value, dict):
        entry = dict(value)
        if isinstance(entry.get("command"), str):
            entry["command"] = shlex.split(entry["command"])
        return entry
    raise PolicyError(f"check {cid!r} must be a command string, list, or mapping")


def detect_checks(root: Path) -> dict[str, dict[str, Any]]:
    """Best-effort test command for zero-config use. Never guesses silently:
    `daedalus status` shows what was detected, and `daedalus init` writes it down."""
    if (root / "pyproject.toml").exists() or (root / "setup.py").exists() or (root / "pytest.ini").exists():
        return {"tests": {"command": ["python", "-m", "pytest", "-q"], "timeout_s": 1800}}
    pkg = root / "package.json"
    if pkg.exists():
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
        except (ValueError, OSError):
            scripts = {}
        if "test" in scripts:
            return {"tests": {"command": ["npm", "test", "--silent"], "timeout_s": 1800}}
    if (root / "Cargo.toml").exists():
        return {"tests": {"command": ["cargo", "test", "--quiet"], "timeout_s": 3600}}
    if (root / "go.mod").exists():
        return {"tests": {"command": ["go", "test", "./..."], "timeout_s": 1800}}
    return {}


def load_config(root: str | Path) -> RepoConfig:
    root = Path(root)
    default_raw = yaml.safe_load(default_policy_text())
    path = root / CONFIG_FILE
    notes: list[str] = []
    if not path.exists():
        raw = dict(default_raw)
        raw["checks"] = detect_checks(root)
        notes.append(
            "no .daedalus.yml: using the default policy with detected checks "
            f"{sorted(raw['checks']) or 'NONE'}"
        )
        return RepoConfig(root, True, "gate", {}, {}, raw, "default", tuple(notes))

    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise PolicyError(f"{CONFIG_FILE} is not valid YAML: {exc}") from exc
    if not isinstance(cfg, dict):
        raise PolicyError(f"{CONFIG_FILE} must be a mapping")

    if cfg.get("policy_file"):
        pf = (root / str(cfg["policy_file"])).resolve()
        if root.resolve() not in pf.parents:
            raise PolicyError("policy_file must live inside the repository")
        try:
            raw = yaml.safe_load(pf.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PolicyError(f"cannot read policy_file {pf}: {exc}") from exc
    else:
        raw = dict(default_raw)
    overrides = cfg.get("policy") or {}
    if not isinstance(overrides, dict):
        raise PolicyError("`policy:` must be a mapping of policy keys")
    raw.update(overrides)

    if "checks" in cfg:
        raw["checks"] = {cid: _check_entry(cid, v) for cid, v in (cfg["checks"] or {}).items()}
    if cfg.get("required_checks") is not None:
        required = set(cfg["required_checks"])
        unknown = required - set(raw["checks"])
        if unknown:
            raise PolicyError(f"required_checks names undefined checks: {sorted(unknown)}")
        for cid, entry in raw["checks"].items():
            entry["mandatory"] = cid in required

    mode = str(cfg.get("mode", "gate"))
    if mode not in MODES:
        raise PolicyError(f"mode must be one of {MODES}")
    adapter = cfg.get("adapter") or {}
    if isinstance(adapter, str):
        adapter = {"name": adapter}
    policy_from_dict(raw)  # validate now so errors point at the config file
    return RepoConfig(
        root=root,
        enabled=bool(cfg.get("enabled", True)),
        mode=mode,
        adapter=dict(adapter),
        harness=dict(cfg.get("harness") or {}),
        policy_raw=raw,
        source="file",
        notes=tuple(notes),
    )


TOGGLE_FILE = "enabled"


def gate_enabled(root: Path) -> bool:
    """`daedalus on|off` writes a local override in .daedalus/ (never part of a
    candidate); otherwise `enabled:` from .daedalus.yml decides."""
    try:
        return (root / STATE_DIR / TOGGLE_FILE).read_text(encoding="utf-8").strip() != "off"
    except OSError:
        pass
    try:
        return load_config(root).enabled
    except PolicyError:
        return True  # a broken config must not silently switch the gate off


def set_gate(root: Path, on: bool) -> None:
    d = root / STATE_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / TOGGLE_FILE).write_text("on\n" if on else "off\n", encoding="utf-8")


def uncommitted_policy_files(root: Path) -> list[str]:
    """Policy is trusted repository configuration: it must be committed before a
    run pins it, or the run would be certified by a policy nobody reviewed."""
    paths = [CONFIG_FILE]
    try:
        cfg = yaml.safe_load((root / CONFIG_FILE).read_text(encoding="utf-8")) or {}
        if isinstance(cfg, dict) and cfg.get("policy_file"):
            paths.append(str(cfg["policy_file"]))
    except (OSError, yaml.YAMLError):
        pass
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--", *paths], capture_output=True, text=True
        )
    except FileNotFoundError:
        return []
    if r.returncode != 0:
        return []  # not a git repository: nothing to compare against
    return [line[3:] for line in r.stdout.splitlines() if line.strip()]


def render_config(checks: dict[str, dict[str, Any]], adapter: str | None = None) -> str:
    lines = [
        "# Daedalus gate configuration (see the Daedalus README).",
        "# Turn the gate off for harness hooks with `enabled: false`.",
        "enabled: true",
        "",
        "# Default mode for new runs: gate (checks only) | review (+1 reviewer) | council (4 perspectives).",
        "# Risk floors in the policy can require reviews and approvals in any mode.",
        "mode: gate",
        "",
    ]
    if adapter:
        lines += ["# Agent harness used by `daedalus run` and `daedalus review`.", f"adapter: {adapter}", ""]
    lines += ["# Trusted checks. Every check here is mandatory unless `mandatory: false`.", "checks:"]
    if not checks:
        lines += ["  # tests: python -m pytest -q", "  {}"]
    for cid, entry in checks.items():
        cmd = " ".join(shlex.quote(c) for c in entry["command"])
        lines.append(f"  {cid}: {cmd}")
    lines += [
        "",
        "# Override any key of the default policy (risk floors, tier requirements,",
        "# authorities, budgets). See `daedalus policy --default` for the full file.",
        "# policy:",
        "#   path_floors:",
        '#     - {name: billing, patterns: ["src/billing/**"], tier: high}',
        "",
    ]
    return "\n".join(lines)
