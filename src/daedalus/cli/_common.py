"""Helpers every command module shares: who is acting, which run, how a decision reads.

Nothing here parses arguments for a particular command; that lives next to the command.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from daedalus.adapters.base import AGENT_MARKER
from daedalus.core.acceptance import Decision
from daedalus.core.config import find_root
from daedalus.core.contracts import bind_text_criteria
from daedalus.core.errors import AuthorizationError, DaedalusError
from daedalus.core.names import DISPLAY_NAMES as DISPLAY_NAMES  # re-exported for callers
from daedalus.core.names import display_name
from daedalus.core.state_machine import Disposition

SEV_ABBR = {"BLOCKER": "B", "MAJOR": "M", "MINOR": "m", "ADVISORY": "A"}
EXIT = {Disposition.ACCEPTED: 0, Disposition.BLOCKED: 1, Disposition.REJECTED: 3, Disposition.CANCELLED: 4}


# ------------------------------------------------------------------ identity
def in_agent_session() -> bool:
    return bool(os.environ.get("CLAUDECODE") or os.environ.get(AGENT_MARKER))


def human_name() -> str:
    name = os.environ.get("DAEDALUS_USER")
    if not name:
        try:
            r = subprocess.run(["git", "config", "user.email"], capture_output=True, text=True, check=False)
            name = r.stdout.strip()
        except FileNotFoundError:
            name = ""
    name = name or getpass.getuser()
    return re.sub(r"[^A-Za-z0-9@._+-]", "_", name)


def human_actor(what: str, *, assume_yes: bool = False) -> str:
    """The acting human, after checking a human is plausibly at the keyboard."""
    if in_agent_session():
        raise AuthorizationError(f"`{what}` is a human decision; it cannot run inside an agent session")
    from daedalus import cli  # looked up at call time so tests can patch `cli._interactive`

    if not cli._interactive():
        raise AuthorizationError(f"`{what}` is a human decision and needs an interactive terminal")
    actor = f"human:{human_name()}"
    if not assume_yes:
        ans = input(f"{actor}: confirm {what}? [y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            raise DaedalusError("not confirmed; nothing recorded")
    return actor


def starter_actor() -> str:
    """Starting a run is not a privileged decision; record who asked honestly."""
    return "agent:harness" if in_agent_session() else f"human:{human_name()}"


def _ari(args: argparse.Namespace, *, adapter: bool = False):
    from daedalus.adapters import make_adapter
    from daedalus.orchestration.ariadne import Ariadne

    root = find_root(args.dir)
    ad = None
    if adapter:
        from daedalus.core.config import load_config

        cfg = dict(load_config(root).adapter)
        if getattr(args, "adapter", None) and args.adapter != cfg.get("name"):
            cfg = {"name": args.adapter}  # the repository's adapter keys belong to its own adapter
        ad = make_adapter(cfg)
    return Ariadne(root, adapter=ad)


def _run_id(ari, args: argparse.Namespace, *, any_run: bool = False) -> str:
    if getattr(args, "run", None):
        return ari.store.resolve_run_id(args.run)
    rid = ari.active_run_id()
    if rid:
        return rid
    if any_run:
        ids = ari.store.run_ids()
        if ids:
            return ids[-1]
    return ari.require_active()


def _print(s: str = "") -> None:
    print(s, flush=True)


def _spec_from_args(args: argparse.Namespace, policy: Any = None) -> dict[str, Any]:
    """The contract spec from flags and `--contract`. Criteria given with `-c` are proved by the
    required checks (bound here, when `policy` is given); `--attest` criteria need a person; bare
    strings in a contract file keep their meaning (a person attests them)."""
    spec: dict[str, Any] = {}
    if getattr(args, "contract", None):
        loaded = yaml.safe_load(Path(args.contract).read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise DaedalusError("contract file must be a mapping")
        spec.update(loaded)
    if args.goal:
        spec["objective"] = args.goal
    crits: list[Any] = list(args.criterion or [])
    if policy is not None and crits:
        merged = list(spec.get("required_checks") or []) + list(args.check or [])  # file + --check
        crits = bind_text_criteria({"acceptance_criteria": crits, "required_checks": merged}, policy)[
            "acceptance_criteria"
        ]
    crits += [{"description": a, "checks": []} for a in getattr(args, "attest", None) or []]
    if crits:  # flags add to the contract file's criteria rather than replacing them
        spec["acceptance_criteria"] = list(spec.get("acceptance_criteria") or []) + crits
    if args.scope:
        spec["scope"] = list(args.scope)
    if args.risk:
        spec["risk_tier"] = args.risk
    if args.check:
        spec["required_checks"] = list(spec.get("required_checks", [])) + list(args.check)
    return spec


def hidden_checks(state: Any) -> frozenset[str]:
    """Holdout check ids, when the reader is an agent session: those it must not be shown."""
    from daedalus.core.policy import holdout_ids

    return holdout_ids(state.policy) if in_agent_session() else frozenset()


def render_decision(
    d: Decision, *, verbose: bool = True, root: Path | None = None, hidden: frozenset[str] = frozenset()
) -> list[str]:
    lines = [f"Candidate: {d.candidate_id[:12]} · risk {d.risk.explain()}"]
    if d.check_states:
        lines.append("Checks:")
        shown = {("(held out)" if cid in hidden else cid): v for cid, v in d.check_states.items()}
        w = max(len(k) for k in shown)
        for cid, (st, detail) in shown.items():
            lines.append(f"  {cid.ljust(w)}  {st.value:<7} {detail if verbose else ''}".rstrip())
    if d.required_reviews:
        lines.append("Required reviews: " + ", ".join(display_name(r) for r in d.required_reviews))
    if d.required_approvals:
        lines.append("Required approvals: " + ", ".join(d.required_approvals))
    lines += disposition_lines(d, root)
    return lines


def is_ready(d: Decision) -> bool:
    """Every condition holds and only the recording is left. The core calls this BLOCKED
    (`not_finished`); to a person it is the opposite of blocked, so say so."""
    return d.disposition is Disposition.BLOCKED and d.acceptable and not d.terminal


def disposition_lines(d: Decision, root: Path | None = None) -> list[str]:
    """The 'Disposition' line and the reasons under it. Presentation only: the decision itself
    is untouched."""
    if is_ready(d):
        return ["Disposition: READY - every condition holds; `daedalus finish` records the acceptance"]
    lines = [f"Disposition: {d.disposition.value}" + (" (final)" if d.terminal else "")]
    for r in d.reasons:
        lines.append(f"  [{r.fixable_by}] {r.message}")
    # Check logs and the like are named by absolute path; show them from the repository root.
    return [shorten(root, ln) for ln in lines] if root is not None else lines


def rel(root: Path, path: str | Path) -> str:
    """`path` relative to the repository root when it lives inside it (forward slashes), else as given."""
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except (ValueError, OSError):
        return str(path)


def shorten(root: Path, text: str) -> str:
    """`text` with the repository root cut off wherever it prefixes a path (output from code that
    prints absolute paths)."""
    for prefix in {str(root), Path(root).resolve().as_posix(), str(Path(root).resolve())}:
        for sep in ("/", os.sep):
            text = text.replace(prefix + sep, "")
    return text


def add_command(
    sub: Any, name: str, fn: Any, help_: str, *, run: bool = False, human: bool = False
) -> argparse.ArgumentParser:
    """Register one subcommand. `run` adds --run; `human` adds -y (a person still needs a TTY)."""
    sp = sub.add_parser(name, help=help_, description=help_)
    sp.set_defaults(fn=fn)
    if run:
        sp.add_argument("--run", help="run id or prefix (default: the open run)")
    if human:
        sp.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt (still needs a TTY)")
    return sp


def contract_args(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("goal", nargs="?", help="the objective, in plain words")
    sp.add_argument("--contract", help="YAML file with contract fields")
    sp.add_argument("-c", "--criterion", action="append", help="acceptance criterion, proved by the required checks (repeatable)")
    sp.add_argument("--attest", action="append", metavar="CRITERION",
                    help="acceptance criterion only a person can judge; needs `daedalus attest` (repeatable)")
    sp.add_argument("--scope", action="append", help="path glob the change may touch (repeatable)")
    sp.add_argument("--risk", help="declared risk tier (can only raise the computed tier)")
    sp.add_argument("--check", action="append", help="extra required check from policy (repeatable)")
    sp.add_argument("--mode", choices=("gate", "review", "council"))
