"""Repository configuration: `.daedalus.yml` -> effective policy.

The user-facing file is deliberately small:

    enabled: true          # gate on/off for harness hooks (not part of policy)
    mode: gate             # default mode for new runs: gate | review | council
    adapter: claude-code   # agent harness for `daedalus run` / `daedalus review`
    checks:                # trusted checks; replaces the default list
      tests: python -m pytest -q
      typecheck: {command: [mypy, src], mandatory: true}
    cache: {checks: true, reviews: true, max_age_s: 86400}  # reuse verified evidence across runs (default on)
    harness:
      auto_finish: true    # `daedalus verify` records acceptance when every condition holds
    coordination: true     # launched agents coordinate over a message bus (default on); or
                           # {enabled: true, bus_dir: path} to share a bus with other tools
    preset: solo           # solo | standard | strict: a named overlay on the default policy
    policy: {...}          # optional overrides of any default-policy key (they win over the preset)
    policy_file: path.yaml # or: a complete policy file instead of the default

Everything that is not UX (enabled/mode/adapter/harness/coordination) becomes the policy
whose hash every run pins. With no `.daedalus.yml`, Daedalus uses the default
policy and detects a test command, so `daedalus run "..."` works zero-config.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from daedalus.core.contracts import MODES
from daedalus.core.paths import MAKEFILE_NAMES
from daedalus.core.errors import PolicyError
from daedalus.core.policy import (
    DEFAULT_MAX_AGE_S,
    Policy,
    cache_flag,
    cache_max_age,
    default_policy_text,
    policy_from_dict,
)

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
    cache_checks: bool = True  # reuse PASS check results from earlier runs on a byte-identical tree
    cache_reviews: bool = True  # reuse approving review verdicts likewise
    cache_max_age_s: int = DEFAULT_MAX_AGE_S  # a source older than this is not reused
    preset: str = ""  # the named policy preset in effect, if any
    # Agents launched by `daedalus run` coordinate over a message bus (docs/coordination.md). Not policy:
    # the bus never decides acceptance, so turning it off changes no run's outcome rules.
    coordination: bool = True
    bus_dir: str | None = None  # bus root, relative to the repository; default .daedalus/bus

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


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _has_python_tests(root: Path) -> bool:
    """A `tests/` tree holding Python files, or a `test_*.py` next to the code. Looks only for
    files, so a JavaScript repo's `tests/` directory does not read as pytest."""
    tests = root / "tests"
    if tests.is_dir() and (any(tests.rglob("test_*.py")) or any(tests.rglob("*_test.py"))):
        return True
    return any(root.glob("test_*.py"))


def _node_runner(root: Path) -> list[str]:
    """How this package manager runs a script, from the lockfile that says which one the project uses."""
    if (root / "pnpm-lock.yaml").exists():
        return ["pnpm"]
    if (root / "yarn.lock").exists():
        return ["yarn"]
    if (root / "bun.lockb").exists() or (root / "bun.lock").exists():
        return ["bun", "run"]
    return ["npm"]


def _package_json(root: Path) -> dict[str, Any]:
    try:
        data = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


# `npm init` writes this placeholder as the test script; running it only fails.
_NPM_PLACEHOLDER_TEST = "no test specified"


def _detect_tests(root: Path) -> dict[str, Any] | None:
    py_signals = (
        any((root / f).exists() for f in ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini"))
        or _has_python_tests(root)
    )
    if py_signals:
        # uv owns the environment when it has a lockfile; `uv run` is then the only command that finds pytest.
        cmd = ["uv", "run", "pytest", "-q"] if (root / "uv.lock").exists() else ["python", "-m", "pytest", "-q"]
        return {"command": cmd, "timeout_s": 1800}
    if (root / "tox.ini").exists():
        return {"command": ["tox"], "timeout_s": 3600}
    for name in MAKEFILE_NAMES:
        if re.search(r"^test\s*:(?![:=])", _read(root / name), re.MULTILINE):
            return {"command": ["make", "test"], "timeout_s": 1800}
    scripts = _package_json(root).get("scripts")
    test = scripts.get("test") if isinstance(scripts, dict) else None
    if isinstance(test, str) and _NPM_PLACEHOLDER_TEST not in test:
        runner = _node_runner(root)
        cmd = ["npm", "test", "--silent"] if runner == ["npm"] else [*runner, "test"]
        return {"command": cmd, "timeout_s": 1800}
    if (root / "Cargo.toml").exists():
        return {"command": ["cargo", "test", "--quiet"], "timeout_s": 3600}
    if (root / "go.mod").exists():
        return {"command": ["go", "test", "./..."], "timeout_s": 1800}
    return None


def _detect_quality(root: Path) -> dict[str, dict[str, Any]]:
    """Lint and type checks, only for tools the repository itself configures, and never mandatory:
    a style failure should be visible, not a reason to refuse the work. Conservative on purpose."""
    pyproject = _read(root / "pyproject.toml")
    py_prefix = ["uv", "run"] if (root / "uv.lock").exists() else ["python", "-m"]
    out: dict[str, dict[str, Any]] = {}

    def add(cid: str, command: list[str]) -> None:
        out.setdefault(cid, {"command": command, "timeout_s": 900, "mandatory": False})

    if "[tool.ruff" in pyproject or (root / "ruff.toml").exists() or (root / ".ruff.toml").exists():
        add("lint", [*py_prefix, "ruff", "check", "."] if py_prefix[0] == "uv" else ["ruff", "check", "."])
    if "[tool.mypy" in pyproject or (root / "mypy.ini").exists() or (root / ".mypy.ini").exists():
        add("typecheck", [*py_prefix, "mypy", "."])
    elif "[tool.pyright" in pyproject or (root / "pyrightconfig.json").exists():
        add("typecheck", ["uv", "run", "pyright"] if py_prefix[0] == "uv" else ["pyright"])
    pkg = _package_json(root)
    deps = {
        **(pkg["dependencies"] if isinstance(pkg.get("dependencies"), dict) else {}),
        **(pkg["devDependencies"] if isinstance(pkg.get("devDependencies"), dict) else {}),
    }
    if (root / "tsconfig.json").exists() and "typescript" in deps:
        runner = _node_runner(root)
        exec_ = {"npm": ["npx", "--no-install"], "pnpm": ["pnpm", "exec"], "yarn": ["yarn"], "bun": ["bunx"]}
        add("typecheck", [*exec_[runner[0]], "tsc", "--noEmit"])
    return out


def detect_checks(root: Path, *, quality: bool = True) -> dict[str, dict[str, Any]]:
    """Best-effort checks for zero-config use. Never guesses silently:
    `daedalus status` shows what was detected, and `daedalus init` writes it down.

    The test command is the only mandatory check. Lint and type checks join it, optional, when the
    repository configures those tools. Without a recognised test command nothing is returned,
    because a gate with only advisory checks would accept anything."""
    tests = _detect_tests(root)
    if tests is None:
        return {}
    return {"tests": tests, **(_detect_quality(root) if quality else {})}


PRESETS = ("solo", "standard", "strict")
_PRESET_KEYS = {"mode", "cache", "harness", "policy"}


def load_preset(name: str) -> dict[str, Any]:
    """A preset is read from the installed package each time, so upgrading Daedalus updates it.
    It has the shape of a small `.daedalus.yml`: `mode`, `cache`, `harness` and `policy`."""
    if name not in PRESETS:
        raise PolicyError(f"unknown preset {name!r}: choose one of {', '.join(PRESETS)}")
    path = Path(__file__).resolve().parent.parent / "data" / "presets" / f"{name}.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise PolicyError(f"cannot read preset {name!r}: {exc}") from exc
    if not isinstance(data, dict) or set(data) - _PRESET_KEYS:
        raise PolicyError(f"preset {name!r} may only set {sorted(_PRESET_KEYS)}")
    return data


def _overlay(raw: dict[str, Any], overlay: dict[str, Any]) -> None:
    """Apply a preset's policy keys. A mapping such as `tier_requirements` merges one level deep, so a
    preset can restate only the tiers it changes; anything else is replaced."""
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(raw.get(key), dict):
            raw[key] = {**raw[key], **value}
        else:
            raw[key] = value


def load_config(root: str | Path, *, preset: str | None = None) -> RepoConfig:
    """`preset` overrides the file's `preset:`; `daedalus policy --preset X` uses it to show what a
    preset would change."""
    root = Path(root)
    default_raw = yaml.safe_load(default_policy_text())
    path = root / CONFIG_FILE
    notes: list[str] = []
    if not path.exists():
        raw = dict(default_raw)
        # Lint and type checks are for `daedalus init` to write down; an unconfigured run does not
        # start running them (up to 900s each) on every verify.
        raw["checks"] = detect_checks(root, quality=False)
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
    preset = preset if preset is not None else (str(cfg["preset"]) if cfg.get("preset") else "")
    layer = load_preset(preset) if preset else {}
    if layer:
        if cfg.get("policy_file"):
            raise PolicyError("`preset:` and `policy_file:` cannot be combined: put the preset's keys in your policy file")
        _overlay(raw, layer.get("policy") or {})
    overrides = cfg.get("policy") or {}
    if not isinstance(overrides, dict):
        raise PolicyError("`policy:` must be a mapping of policy keys")
    raw.update(overrides)  # the repository's own keys always win over a preset

    if "checks" in cfg:
        raw["checks"] = {cid: _check_entry(cid, v) for cid, v in (cfg["checks"] or {}).items()}
    if cfg.get("required_checks") is not None:
        required = set(cfg["required_checks"])
        unknown = required - set(raw["checks"])
        if unknown:
            raise PolicyError(f"required_checks names undefined checks: {sorted(unknown)}")
        for cid, entry in raw["checks"].items():
            entry["mandatory"] = cid in required

    mode = str(cfg.get("mode", layer.get("mode", "gate")))
    if mode not in MODES:
        raise PolicyError(f"mode must be one of {MODES}")
    cache = cfg.get("cache")
    if cache is None:
        cache = {}
    elif isinstance(cache, bool):
        cache = {"checks": cache, "reviews": cache}
    if isinstance(cache, dict) and layer.get("cache"):
        cache = {**layer["cache"], **cache}
    if not isinstance(cache, dict) or set(cache) - {"checks", "reviews", "max_age_s"}:
        raise PolicyError("`cache:` must be a mapping with only `checks`, `reviews` (booleans) and `max_age_s`")
    adapter = cfg.get("adapter") or {}
    if isinstance(adapter, str):
        adapter = {"name": adapter}
    policy_from_dict(raw)  # validate now so errors point at the config file
    harness = {**(layer.get("harness") or {}), **dict(cfg.get("harness") or {})}
    if "auto_finish" in harness and not isinstance(harness["auto_finish"], bool):
        raise PolicyError("`harness: auto_finish` must be true or false")
    coordination, bus_dir = _coordination(cfg.get("coordination", True))
    if bus_dir is not None:
        where, top = (root / bus_dir).resolve(), root.resolve()
        state = top / STATE_DIR
        if (where == top or top in where.parents) and where != state and state not in where.parents:
            # Its files would be part of every candidate: each message would change the tree under review.
            raise PolicyError(f"`coordination: bus_dir` must be under {STATE_DIR}/ or outside the repository")
    return RepoConfig(
        root=root,
        enabled=bool(cfg.get("enabled", True)),
        mode=mode,
        adapter=dict(adapter),
        harness=harness,
        policy_raw=raw,
        source="file",
        notes=tuple(notes),
        cache_checks=cache_flag(cache.get("checks"), "`cache: checks`"),
        cache_reviews=cache_flag(cache.get("reviews"), "`cache: reviews`"),
        cache_max_age_s=cache_max_age(cache.get("max_age_s"), "`cache: max_age_s`", DEFAULT_MAX_AGE_S),
        preset=preset,
        coordination=coordination,
        bus_dir=bus_dir,
    )


def _coordination(v: Any) -> tuple[bool, str | None]:
    """`coordination: true|false`, or `{enabled: bool, bus_dir: path}`."""
    if isinstance(v, bool):
        return v, None
    if not isinstance(v, dict) or set(v) - {"enabled", "bus_dir"}:
        raise PolicyError("`coordination:` must be true, false, or a mapping with `enabled` and `bus_dir`")
    enabled = v.get("enabled", True)
    if not isinstance(enabled, bool):
        raise PolicyError("`coordination: enabled` must be true or false")
    bus_dir = v.get("bus_dir")
    if bus_dir is not None and (not isinstance(bus_dir, str) or not bus_dir.strip()):
        raise PolicyError("`coordination: bus_dir` must be a path")
    return enabled, bus_dir


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


# The timeout a check gets when it sets none (CheckDefinition.timeout_s).
DEFAULT_CHECK_TIMEOUT_S = 600


def render_config(checks: dict[str, dict[str, Any]], adapter: str | None = None, preset: str | None = None) -> str:
    lines = [
        "# Daedalus gate configuration (see the Daedalus README).",
        "# Turn the gate off for harness hooks with `enabled: false`.",
        "enabled: true",
        "",
    ]
    if preset:
        # No `mode:` line: a key written here would win over the preset's own.
        lines += [
            "# Policy preset, loaded from Daedalus itself so upgrades update it (`daedalus policy --preset X`).",
            "# Keys you write here (mode, cache, harness, policy) win over the preset.",
            f"preset: {preset}",
            "",
        ]
    else:
        lines += [
            "# Default mode for new runs: gate (checks only) | review (+1 reviewer) | council (4 perspectives).",
            "# Risk floors in the policy can require reviews and approvals in any mode.",
            "mode: gate",
            "",
        ]
    if adapter:
        lines += ["# Agent harness used by `daedalus run` and `daedalus review`.", f"adapter: {adapter}", ""]
    lines += ["# Trusted checks. Every check here is mandatory unless `mandatory: false`.", "checks:"]
    if not checks:  # no entries at all: loads as "no checks", and doctor says what to add
        lines.append("  # tests: python -m pytest -q")
    for cid, entry in checks.items():
        mandatory = entry.get("mandatory", True)
        timeout = entry.get("timeout_s")
        if mandatory and timeout in (None, DEFAULT_CHECK_TIMEOUT_S):
            lines.append(f"  {cid}: " + " ".join(shlex.quote(c) for c in entry["command"]))
        else:  # a flow mapping keeps the command an exact list, whatever it contains
            parts = [f"command: {json.dumps(entry['command'])}"]
            if timeout not in (None, DEFAULT_CHECK_TIMEOUT_S):
                parts.append(f"timeout_s: {json.dumps(timeout)}")
            if not mandatory:
                parts.append("mandatory: false")
            lines.append(f"  {cid}: {{{', '.join(parts)}}}")
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
