"""Start here and daily use: init, on/off, start, run, status, verify, finish, cancel, restore, runs."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from daedalus.cli._common import (
    EXIT,
    _ari,
    _print,
    _run_id,
    _spec_from_args,
    add_command,
    contract_args,
    disposition_lines,
    human_actor,
    is_ready,
    rel,
    render_decision,
    shorten,
    starter_actor,
)
from daedalus.core.config import (
    CONFIG_FILE,
    PRESETS,
    STATE_DIR,
    detect_checks,
    find_root,
    gate_enabled,
    render_config,
    set_gate,
)
from daedalus.cli.integrations import HARNESS_PATHS, _init_harnesses, _install_harness, snapshot_paths
from daedalus.core.errors import DaedalusError


def cmd_init(args: argparse.Namespace) -> int:
    if (args.ci_install or args.require_provenance or args.ci_source) and not args.ci:
        raise DaedalusError("--ci-install, --ci-source and --require-provenance configure the workflow: add --ci")
    if args.harness and args.no_hooks:
        raise DaedalusError("--harness installs harness integrations, which --no-hooks forbids: drop one of them")
    workflow = None
    if args.ci:  # render (and so validate) before writing anything
        from daedalus.ci import DEFAULT_SOURCE, render_workflow

        workflow = render_workflow(args.ci_source or DEFAULT_SOURCE, args.ci_install, args.require_provenance)
    root = find_root(args.dir)
    cfg = root / CONFIG_FILE
    written: list[str] = []  # repository-relative paths this run created or changed
    if cfg.exists() and (args.ci or not args.force):  # with --ci, --force refreshes only the workflow
        _print(f"{CONFIG_FILE} already exists" + (" (kept; --ci --force refreshes only the workflow)" if args.ci
                                                  else " (use --force to overwrite)"))
        if args.preset:
            _print(f"preset {args.preset!r} was NOT applied: the existing {CONFIG_FILE} is kept. Add `preset: "
                   f"{args.preset}` to it yourself (see what it changes: daedalus policy --preset {args.preset} "
                   f"--diff), or use --force to rewrite the file (the old one is saved as a backup)")
    else:
        if cfg.exists():  # --force replaces a file someone may have edited by hand: keep it
            bak = cfg.with_name(CONFIG_FILE + ".bak")
            n = 0
            while bak.exists():  # never replace an earlier backup
                n += 1
                bak = cfg.with_name(f"{CONFIG_FILE}.bak.{n}")
            shutil.copyfile(cfg, bak)
            _print(f"kept the old {CONFIG_FILE} as {bak.name} (git-ignored; delete it when you no longer need it)")
            if _ensure_ignored(root, BACKUP_IGNORE):
                _print(f"added {BACKUP_IGNORE} to .gitignore")
                if ".gitignore" not in written:
                    written.append(".gitignore")
        checks = detect_checks(root)
        adapter = args.adapter or _default_adapter(root, args)
        if adapter and not args.adapter:
            _print(f"adapter: {adapter} (found the `claude` binary; independent reviews can launch)")
        cfg.write_text(render_config(checks, adapter, args.preset), encoding="utf-8")
        _print(f"wrote {CONFIG_FILE} with checks: {', '.join(checks) or 'NONE — add one before starting a run'}")
        if args.preset:
            _print(f"preset: {args.preset} (see what it changes: daedalus policy --preset {args.preset} --diff)")
        written.append(CONFIG_FILE)
    if _ensure_ignored(root, f"{STATE_DIR}/"):
        _print(f"added {STATE_DIR}/ to .gitignore")
        written.append(".gitignore")
    chosen: list[str] = []
    if not args.no_hooks:
        from daedalus.harness.provenance import install_git_hook

        chosen = _init_harnesses(root, args.harness)
        watched = [p for n in chosen for p in HARNESS_PATHS.get(n, [])]
        before = snapshot_paths(root, watched)
        for name in chosen:
            for line in _install_harness(name, root):
                _print(line)
        try:
            _print(shorten(root, install_git_hook(root)))
        except DaedalusError as exc:
            _print(f"note: {exc}")
        if "claude-code" in chosen and _ensure_ignored(root, ".claude/settings.local.json") and ".gitignore" not in written:
            written.append(".gitignore")
        after = snapshot_paths(root, watched)  # exactly the files the installers created or changed
        written += [p for p in sorted(after) if after[p] != before.get(p) and p not in written
                    and not _git_ignored(root, p)]
        if any(n not in HARNESS_PATHS for n in chosen):
            _print("note: " + ", ".join(n for n in chosen if n not in HARNESS_PATHS)
                   + " writes files init cannot name: commit the files listed above yourself")
    if workflow is not None:
        wf = root / ".github" / "workflows" / "daedalus.yml"
        if wf.exists() and not args.force:
            _print(f"{wf.relative_to(root).as_posix()} already exists (use --force to overwrite)")
        else:
            wf.parent.mkdir(parents=True, exist_ok=True)
            wf.write_text(workflow, encoding="utf-8")
            _print(f"wrote {wf.relative_to(root).as_posix()}: pull requests now run `daedalus ci`")
            written.append(wf.relative_to(root).as_posix())
    # A policy must be committed before a run pins it, so the last step of setup is a commit.
    written = [w for w in written if (root / w).exists()]
    if ".gitignore" in written and not _gitignore_only_init_entries(root):
        # init appended to a .gitignore that also holds the user's own uncommitted edits.
        written.remove(".gitignore")
        _print(".gitignore has uncommitted changes besides init's entries: not committing it; "
               "review and commit it yourself")
    if ".gitignore" not in written and _uncommitted(root, ".gitignore"):  # an earlier init's entries
        if _gitignore_only_init_entries(root):
            written.append(".gitignore")
        else:
            _print(".gitignore has uncommitted changes besides init's entries: not committing it; "
                   "review and commit it yourself")
    if written and args.commit:
        _print(_commit_setup(root, written))
    elif written:
        _print("Next: review the files above, then commit them (a run only starts from a committed policy):")
        _print("  " + _commit_command(written, root))
    else:
        _print(f"Next: make sure {CONFIG_FILE} is committed (a run only starts from a committed policy).")
    if not chosen or "claude-code" in chosen:
        _print("Then just work: in Claude Code the first edit opens a run and the gate decides when it is done.")
    else:
        _print("Then just work: the first edit in your agent opens a run and the gate decides when it is done.")
    return 0


def _default_adapter(root: Path, args: argparse.Namespace) -> str | None:
    """`claude-code` when init installs that harness and its binary is on PATH, so the reviews a
    high-tier change needs can be launched. Not when reviews would be billed in cash (then a person
    sets a budget first)."""
    if args.no_hooks or "claude-code" not in _init_harnesses(root, args.harness) or not shutil.which("claude"):
        return None
    try:
        from daedalus.adapters import make_adapter

        if make_adapter({"name": "claude-code"}).expected_cost_basis() == "cash":
            return None
    except DaedalusError:
        return None
    return "claude-code"


COMMIT_MESSAGE = "Configure Daedalus"
BACKUP_IGNORE = f"{CONFIG_FILE}.bak*"


def _uncommitted(root: Path, path: str) -> bool:
    """Whether `path` is new or changed relative to HEAD (False outside a repository)."""
    try:
        r = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", path],
                           capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


INIT_IGNORE_ENTRIES = {f"{STATE_DIR}/", ".claude/settings.local.json", BACKUP_IGNORE}


def _git_ignored(root: Path, path: str) -> bool:
    try:
        r = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", "--", path], capture_output=True, check=False)
    except FileNotFoundError:
        return False
    return r.returncode == 0


def _gitignore_only_init_entries(root: Path) -> bool:
    """Every line of .gitignore that HEAD lacks is one `daedalus init` adds itself."""
    try:
        r = subprocess.run(["git", "-C", str(root), "show", "HEAD:.gitignore"], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return False
    head = set(r.stdout.split()) if r.returncode == 0 else set()
    current = set((root / ".gitignore").read_text(encoding="utf-8").split())
    return (current - head) <= INIT_IGNORE_ENTRIES


def _commit_command(paths: list[str], root: Path) -> str:
    """The one line that commits exactly what init wrote. `git -C <root>` unless cwd is the root,
    so it works from a subdirectory too (the paths are relative to the root)."""
    prefix = "git" if root.resolve() == Path.cwd().resolve() else f"git -C {shlex.quote(str(root))}"
    quoted = " ".join(shlex.quote(p) for p in paths)
    return f"{prefix} add {quoted} && {prefix} commit -m {shlex.quote(COMMIT_MESSAGE)}"


def _commit_setup(root: Path, paths: list[str]) -> str:
    """`init --commit`: commit just the files init wrote, as whoever git is configured to be.
    No trailers and no attribution are added; the message is the whole commit."""

    def run_git(*argv: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(["git", "-C", str(root), *argv], capture_output=True, text=True, check=False)
        except FileNotFoundError as exc:
            raise DaedalusError("--commit needs git on PATH") from exc

    if run_git("rev-parse", "--is-inside-work-tree").returncode != 0:
        raise DaedalusError("--commit: this is not a git repository (run `git init`, then `daedalus init --commit`)")
    added = run_git("add", "--", *paths)
    if added.returncode != 0:
        raise DaedalusError(f"git add failed: {added.stderr.strip() or added.stdout.strip()}")
    # `--` limits the commit to these paths, so anything else already staged is left alone.
    done = run_git("commit", "-m", COMMIT_MESSAGE, "--", *paths)
    if done.returncode != 0:
        out = done.stderr.strip() or done.stdout.strip()
        if "nothing to commit" in out or "no changes added" in out:
            return "nothing to commit: the setup files are already committed"
        raise DaedalusError(f"git commit failed (set user.name and user.email if git asks): {out}")
    return f'committed {", ".join(paths)} as "{COMMIT_MESSAGE}"'


def _ensure_ignored(root: Path, entry: str) -> bool:
    """Add `entry` to .gitignore unless present. Returns True when it was added."""
    gi = root / ".gitignore"
    existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
    if entry in existing.split():
        return False
    gi.write_text(existing + ("" if existing.endswith("\n") or not existing else "\n") + entry + "\n")
    return True


def cmd_toggle(args: argparse.Namespace) -> int:
    root = find_root(args.dir)
    if args.cmd == "off":
        human_actor("turning the gate off", assume_yes=args.yes)
    set_gate(root, args.cmd == "on")
    where = "this repository" if root == Path.cwd().resolve() else str(root)
    _print(f"Daedalus gate {'ON' if args.cmd == 'on' else 'OFF'} for {where} (local setting in {STATE_DIR}/)")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    ari = _ari(args)
    try:
        spec = _spec_from_args(args, ari.config().policy)
        rid = ari.start(spec, actor=starter_actor(), mode=args.mode)
        state = ari.state(rid)
        _print(f"run {rid} started (mode {state.mode}, contract v{state.contract.contract_version})")
        _print(f"contract: {rel(ari.root, state.contract_file)}")
        for c in state.contract.acceptance_criteria:
            _print(f"  {c.criterion_id}: {c.description}")
        _print("Work, then `daedalus verify` and `daedalus status`.")
        return 0
    finally:
        ari.close()


def cmd_run(args: argparse.Namespace) -> int:
    from daedalus.orchestration.lifecycle import run_task

    ari = _ari(args, adapter=True)
    ari.no_cache = args.no_cache
    try:
        report = run_task(
            ari,
            _spec_from_args(args, ari.config().policy),
            actor=starter_actor(),
            adapter=ari.adapter,
            mode=args.mode,
            max_rounds=args.rounds,
            notify=lambda m: _print(f"· {m}"),
        )
        _print()
        _print(f"run {report.run_id} after {report.rounds} round(s)")
        for line in render_decision(report.decision, root=ari.root):
            _print(line)
        for n in report.notes:
            _print(f"note: {n}")
        return EXIT[report.decision.disposition]
    finally:
        ari.close()


def cmd_status(args: argparse.Namespace) -> int:
    root = find_root(args.dir)
    ari = _ari(args)
    try:
        enabled = gate_enabled(root)
        try:
            rid = _run_id(ari, args, any_run=True)
        except DaedalusError:
            if args.json:
                _print(json.dumps({"gate": enabled, "run": None}))
            else:
                _print(f"Daedalus gate: {'ON' if enabled else 'OFF'} · no runs yet")
                for note in ari.config().notes:
                    _print(f"note: {note}")
            return 0
        state = ari.state(rid)
        if ari.needs_recovery(rid):
            _print("warning: the last orchestrator stopped mid-operation; run `daedalus reconcile`")
        d = ari.evaluate(rid)
        if args.json:
            _print(
                json.dumps(
                    {
                        "gate": enabled,
                        "run": rid,
                        "execution": state.execution.value,
                        "mode": state.mode,
                        "contract": state.contract.to_dict(),
                        "decision": d.to_dict(),
                    },
                    indent=2,
                )
            )
            return 0
        _print(
            f"Daedalus gate: {'ON' if enabled else 'OFF'} · run {rid} ({state.mode} mode) · {state.execution.value}"
        )
        _print(f"Objective: {state.contract.objective}  [contract v{state.contract.contract_version}]")
        for line in render_decision(d, verbose=not args.brief, root=ari.root):
            _print(line)
        return 0
    finally:
        ari.close()


def cmd_verify(args: argparse.Namespace) -> int:
    ari = _ari(args)
    try:
        rid = _run_id(ari, args)
        ari.no_cache = args.no_cache
        recs = ari.verify(rid, only=args.only or None)
        reused = {e.payload["record"]["evidence_id"]: e.payload["reused_from"]
                  for e in ari.store.events(rid) if e.type == "evidence.recorded" and e.payload.get("reused_from")}
        for r in recs:
            extra = f" ({r.error})" if r.error else (f" exit {r.exit_code}" if r.result == "FAIL" else "")
            src = reused.get(r.evidence_id)
            if src:
                extra += f" (reused from run {src['run_id']} event {src['seq']})"
            _print(f"{r.check_id}: {r.result} on {r.candidate_id[:12]}{extra}")
        passed = all(r.result == "PASS" for r in recs if r.mandatory)
        from daedalus.harness.claude_code import launched_by_daedalus

        if passed and not args.no_review and not args.only and not launched_by_daedalus():
            from daedalus.harness.claude_code import settle

            d, notes, _ = settle(ari, rid, cache=not args.no_cache, notify=lambda m: _print(f"· {m}"))
            for n in notes:
                _print(f"note: {n}")
            for f in ari.state(rid).open_findings:
                _print(f"  {f.finding_id} [{f.severity.value}] {f.title}: {f.required_change}"[:300])
        d = ari.evaluate(rid)
        # Everything holds and only the recording is left: do it, as the Stop hook does, so success
        # does not need a third command. Partial runs (--only) and the gate's own agents never finish.
        if (is_ready(d) and not args.no_finish and not args.only and not launched_by_daedalus()
                and ari.config().harness.get("auto_finish", True)):
            d = ari.finish(rid)
        for line in disposition_lines(d, ari.root):
            _print(line)
        return 0 if passed else 1
    finally:
        ari.close()


def cmd_finish(args: argparse.Namespace) -> int:
    ari = _ari(args)
    try:
        d = ari.finish(_run_id(ari, args))
        for line in render_decision(d, root=ari.root):
            _print(line)
        return EXIT[d.disposition]
    finally:
        ari.close()


def cmd_cancel(args: argparse.Namespace) -> int:
    ari = _ari(args)
    try:
        state = ari.cancel(_run_id(ari, args), actor=starter_actor(), reason=args.reason or "")
        _print(f"run {state.run_id}: {state.execution.value}" + (f" · {state.disposition.value}" if state.disposition else ""))
        return 0
    finally:
        ari.close()


def cmd_restore(args: argparse.Namespace) -> int:
    from daedalus.orchestration.lifecycle import restore_followup

    ari = _ari(args)
    try:
        rid = _run_id(ari, args)
        for n in restore_followup(ari, rid, notify=lambda m: _print(f"· {m}")):
            _print(n)
        for line in disposition_lines(ari.evaluate(rid), ari.root):
            _print(line)
        return 0
    finally:
        ari.close()


def cmd_runs(args: argparse.Namespace) -> int:
    ari = _ari(args)
    try:
        for rid in ari.store.run_ids():
            s = ari.state(rid)
            disp = s.disposition.value if s.disposition else "open"
            _print(f"{rid}  {s.execution.value:<10} {disp:<9} {s.contract.objective[:70]}")
        return 0
    finally:
        ari.close()


def register(sub: Any) -> None:
    sp = add_command(sub, "init", cmd_init, "write .daedalus.yml with detected checks")
    sp.add_argument("--adapter", help="agent harness for `daedalus run` (e.g. claude-code)")
    sp.add_argument("--claude-code", action="store_true", help=argparse.SUPPRESS)  # the default now
    sp.add_argument("--preset", choices=PRESETS,
                    help="policy preset: solo (one developer, cost-conscious) | standard (the defaults) | "
                         "strict (full council and human approval on high risk)")
    sp.add_argument("--no-hooks", action="store_true", help="do not install the harness hooks and git hook")
    sp.add_argument("--harness", action="append", metavar="NAME",
                    help="harness to install (repeatable; default: claude-code plus every detected harness)")
    sp.add_argument("--ci", action="store_true", help="also write .github/workflows/daedalus.yml")
    sp.add_argument("--ci-install", metavar="CMD", help="command the workflow runs to install what your checks need")
    sp.add_argument("--ci-source", metavar="SPEC",
                    help="what the workflow pip-installs as Daedalus; pin it, e.g. 'git+https://...@<tag>'")
    sp.add_argument("--require-provenance", action="store_true",
                    help="the workflow fails pull requests whose commits were not accepted by the gate")
    sp.add_argument("--force", action="store_true", help="overwrite .daedalus.yml (the old one is kept as .daedalus.yml.bak)")
    sp.add_argument("--commit", action="store_true",
                    help="commit the files init wrote, with your configured git identity")
    add_command(sub, "on", cmd_toggle, "turn the gate on for this repository")
    add_command(sub, "off", cmd_toggle, "turn the gate off for this repository (human only)", human=True)

    contract_args(add_command(sub, "start", cmd_start, "open a run; you or your harness agent do the work"))
    sp = add_command(sub, "run", cmd_run, "open a run and drive it with the configured agent adapter")
    contract_args(sp)
    sp.add_argument("--rounds", type=int, default=3, help="max build/verify/review rounds (default 3)")
    sp.add_argument("--adapter", help="override the adapter from .daedalus.yml")
    sp.add_argument("--no-cache", action="store_true",
                    help="verify and review for real; do not reuse results verified in earlier runs")

    sp = add_command(sub, "status", cmd_status, "show the gate, the run and what blocks acceptance", run=True)
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--brief", action="store_true")
    sp = add_command(sub, "verify", cmd_verify, "run trusted checks against the current working tree", run=True)
    sp.add_argument("--only", action="append", help="run only this check (repeatable)")
    sp.add_argument("--no-review", action="store_true",
                    help="do not launch the reviews the run requires once checks pass")
    sp.add_argument("--no-cache", action="store_true",
                    help="run checks and reviews for real; do not reuse results verified in earlier runs")
    sp.add_argument("--no-finish", action="store_true",
                    help="do not record acceptance when every condition holds (config: harness.auto_finish)")
    add_command(sub, "finish", cmd_finish, "derive and record the final disposition if it is decided", run=True)
    sp = add_command(sub, "cancel", cmd_cancel, "cancel the run", run=True)
    sp.add_argument("--reason")
    add_command(sub, "restore", cmd_restore,
                "undo a follow-up round: put back the candidate that was acceptable before it", run=True)
    add_command(sub, "runs", cmd_runs, "list runs")
