"""Claude Code shim: the gate as an ambient mode of the harness, not a command to remember.

Once `daedalus init` has run and its files are committed, everyone who opens the
repository in Claude Code works under the gate without typing anything:

- **UserPromptSubmit** remembers what was asked and a snapshot of the tree.
- **PreToolUse** (file edits): the first edit with no run open opens one. Its
  objective is the prompt; its base is the commit the prompt was given on, so
  work committed along the way is still covered. Questions never open runs.
- **Stop**: when the agent tries to end its turn, Daedalus verifies stale or
  missing evidence, launches every review the risk tier or mode requires
  (through the configured adapter, read-only and independent), and evaluates the
  acceptance predicate. Agent-fixable reasons send the agent back with exact
  instructions; human-only reasons let it stop and tell the user what to run; a
  holding predicate records ACCEPTED. If the agent changed files without any
  edit tool (a shell command), the run is opened here instead.
- **SessionStart** injects the open run's contract, or a one-line note that the
  repository is gated, and installs the git hook that stamps accepted commits.
- **daedalus-gate skill**: instructions only; the hooks and the core enforce.

The hook commands are portable (`daedalus hook <name>`, run only when Daedalus is
installed), so `.claude/settings.json` can be committed. Without Daedalus on
PATH, SessionStart says how to install it and every other hook does nothing.

Hooks do nothing inside sessions Daedalus launched itself (`DAEDALUS_AGENT`
is set by the command adapter): the lifecycle already governs those sessions.

Hooks fail open on internal errors (the user is never locked out of their
harness) but never fail open into acceptance: an error only ever lets the
agent stop with the run still open and unaccepted.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from daedalus.adapters.base import AGENT_MARKER
from daedalus.core.acceptance import Decision
from daedalus.core.config import STATE_DIR, find_root, gate_enabled, load_config
from daedalus.core.errors import DaedalusError
from daedalus.core.names import review_name, with_article
from daedalus.core.state_machine import CheckState, Disposition, ExecutionState
from daedalus.orchestration.ariadne import Ariadne
from daedalus.orchestration.lifecycle import WORKLIST_HINT
from daedalus.orchestration.prompts import ROLE_SKILLS, SKILLS_DIR
from daedalus.plugins import HarnessIntegration

DEFAULT_MAX_STOP_BLOCKS = 8
STOP_HOOK_TIMEOUT_S = 3600
EDIT_TOOLS = "Edit|Write|MultiEdit|NotebookEdit"
INSTALL_HINT = 'pip install "git+https://github.com/KEV-INZHU/Daedalus"'
PROMPT_FILE = "last_prompt.json"
HOOK_MARK = "daedalus hook "
AUTO_ACTOR = "agent:harness"


def _hook_command(sub: str, bootstrap: str | None = None) -> str:
    """Portable: runs wherever `daedalus` is on PATH, and is a no-op where it is not.

    With `bootstrap` (from `harness: {bootstrap: ...}` in .daedalus.yml), SessionStart
    first runs that command when Daedalus is missing, e.g. `pip install -e .` in a
    fresh cloud container, so the gate is on from the first prompt."""
    run = f"if command -v daedalus >/dev/null 2>&1; then daedalus hook {sub}; "
    if sub == "session-start" and bootstrap:
        run = f"command -v daedalus >/dev/null 2>&1 || ( {bootstrap} ) >/dev/null 2>&1; " + run
    if sub == "session-start":
        hint = json.dumps(
            {"systemMessage": f"This repository is gated by Daedalus, which is not installed here: {INSTALL_HINT}"}
        )
        return run + f"else echo '{hint}'; fi"
    return run + "fi"


STATUSLINE_COMMAND = "if command -v daedalus >/dev/null 2>&1; then daedalus statusline; fi"


def launched_by_daedalus() -> bool:
    return bool(os.environ.get(AGENT_MARKER))


def _harness(root: Path) -> dict[str, Any]:
    try:
        return load_config(root).harness
    except DaedalusError:
        return {}


# --------------------------------------------------------------------- render
def render_reasons(decision: Decision, who: str | None = None) -> list[str]:
    return [
        f"[{r.fixable_by}] {r.message}"
        for r in decision.reasons
        if (who is None or r.fixable_by == who) and r.code != "not_finished"
    ]


def summary_line(decision: Decision) -> str:
    return (
        f"Daedalus: {decision.disposition.value} — candidate {decision.candidate_id[:12]}, "
        f"{decision.risk.explain()}"
    )


# ------------------------------------------------------------ auto-opening runs
def _snapshot(ari: Ariadne) -> dict[str, Any]:
    """Where the user's prompt was given: HEAD plus a cheap fingerprint of uncommitted work
    (status, sizes and mtimes; no file is hashed). Runs on every prompt, so it must be quick."""
    import hashlib
    import subprocess

    from daedalus.repository.candidate import git_head

    head = git_head(ari.root)
    status = subprocess.run(
        ["git", "-C", str(ari.root), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        capture_output=True, check=False,
    ).stdout
    h = hashlib.sha256(status)
    for entry in status.split(b"\0"):
        if len(entry) > 3:
            try:
                st = (ari.root / entry[3:].decode("utf-8", "surrogateescape")).stat()
                h.update(f"{st.st_size}:{st.st_mtime_ns};".encode())
            except OSError:
                h.update(b"-;")
    return {"head": head, "tree": h.hexdigest()}


def _prompt_path(root: Path) -> Path:
    return root / STATE_DIR / PROMPT_FILE


def _read_prompt(root: Path) -> dict[str, Any]:
    try:
        data = json.loads(_prompt_path(root).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _objective(prompt: dict[str, Any], session_id: str | None) -> str:
    text = " ".join(str(prompt.get("prompt") or "").split())
    if not text or (session_id and prompt.get("session_id") not in (None, session_id)):
        return "Changes made in a Claude Code session"
    return text if len(text) <= 600 else text[:597].rsplit(" ", 1)[0] + "..."


def auto_open(ari: Ariadne, session_id: str | None) -> str | None:
    """Open a run for the work in this session. Returns the run id, or None when the
    repository opted out (`harness: {auto_start: false}`) or a run is already open."""
    if not _harness(ari.root).get("auto_start", True) or ari.active_run_id():
        return None
    from daedalus.repository.candidate import is_ancestor

    prompt = _read_prompt(ari.root)
    base = prompt.get("head")
    same_session = bool(session_id) and prompt.get("session_id") == session_id
    if not (same_session and isinstance(base, str) and base and is_ancestor(ari.root, base)):
        base = None  # another session's prompt says nothing about where this work began
    return ari.start({"objective": _objective(prompt, session_id)}, actor=AUTO_ACTOR, base_revision=base)


def _is_accepted_state(ari: Ariadne, recent: int = 5) -> bool:
    """The working tree is exactly a candidate a recent run accepted (committed or not)."""
    from daedalus.repository.candidate import capture

    try:
        exclude = load_config(ari.root).policy.candidate_exclude
    except DaedalusError:
        return False
    for rid in reversed(ari.store.run_ids()[-recent:]):
        try:
            state = ari.state(rid)
        except DaedalusError:
            continue
        base = state.contract.base_revision
        if state.disposition is not Disposition.ACCEPTED or not state.accepted_candidate_id or not base:
            continue
        try:
            if capture(ari.root, base, exclude).candidate_id == state.accepted_candidate_id:
                return True
        except (subprocess.CalledProcessError, OSError):
            continue  # e.g. the base no longer exists after a rebase: not a match, keep gating
    return False


def opened_message(ari: Ariadne, rid: str) -> str:
    from daedalus.repository.candidate import git_head

    state = ari.state(rid)
    msg = f"Daedalus opened run {rid}: the gate decides when this work is done."
    base, head = state.contract.base_revision, git_head(ari.root)
    if base and head and base != head:
        msg += f" It covers everything since {base[:10]}, including commits made after your request."
    if state.current_candidate_id and ari.capture(state).changed_paths:
        msg += " Uncommitted changes that were already in the tree are part of it."
    if state.created_by == AUTO_ACTOR:
        msg += (
            " Its objective is the request as given, with one generic criterion: propose a short objective and"
            ' testable criteria now with `daedalus propose "<objective>" -c "<criterion>"`, for the user to confirm.'
        )
    return msg


def proposal_note(state: Any) -> str | None:
    p = state.pending_proposal
    if p is None:
        return None
    return (
        f"[human] {p['proposed_by']} proposed a contract ({p['proposal_id']}): \"{p['objective']}\" with "
        f"{len(p['criteria']) + len(p['attest'])} criteria. Review it with `daedalus status`, then "
        f"`daedalus confirm` (or `daedalus confirm --reject`)."
    )


# Turns the harness submits that are not the user's words. They never name a run.
# `<agent-message` is a report from another session (e.g. a subagent hand-back), not the user.
HARNESS_PROMPTS = ("<task-notification>", "<system-reminder>", "<agent-message", "Stop hook feedback:")


def is_harness_prompt(text: str) -> bool:
    return str(text or "").lstrip().startswith(HARNESS_PROMPTS)


def prompt_hook(payload: dict[str, Any], *, ari: Ariadne | None = None) -> dict[str, Any]:
    if launched_by_daedalus():
        return {}
    root = find_root(payload.get("cwd") or ".")
    if not gate_enabled(root):
        return {}
    own = ari is None
    ari = ari or Ariadne(root)
    try:
        if ari.active_run_id() is None:  # an open run keeps its own objective and base
            harness = is_harness_prompt(payload.get("prompt", ""))
            if harness and _read_prompt(root):
                return {}  # keep the user's last request (and where it was given) exactly as recorded
            record = {"prompt": "" if harness else payload.get("prompt", ""),
                      "session_id": payload.get("session_id"), "at": time.time()}
            record.update(_snapshot(ari))
            p = _prompt_path(root)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(record), encoding="utf-8")
        return {}
    except DaedalusError:
        return {}
    finally:
        if own:
            ari.close()


def pre_edit_hook(payload: dict[str, Any], *, ari: Ariadne | None = None) -> dict[str, Any]:
    """Never blocks the edit: it only makes sure a run is open before the first one."""
    if launched_by_daedalus():
        return {}
    root = find_root(payload.get("cwd") or ".")
    if not gate_enabled(root):
        return {}
    tool_input = payload.get("tool_input") or {}
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    if target:
        p = Path(target)
        p = p if p.is_absolute() else root / p
        try:
            rel = p.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return {}  # outside the repository: not part of any candidate
        if rel == STATE_DIR or rel.startswith(STATE_DIR + "/"):
            return {}
    own = ari is None
    ari = ari or Ariadne(root)
    try:
        rid = auto_open(ari, payload.get("session_id"))
        if rid is None:
            return {}
        return {"systemMessage": opened_message(ari, rid)}
    except DaedalusError as exc:
        return {"systemMessage": f"Daedalus could not open a run, so this work is not gated: {exc}"}
    finally:
        if own:
            ari.close()


# ----------------------------------------------------------------------- stop
def _review_adapter(root: Path):
    """The configured adapter, if it can launch independent reviewers; else None."""
    from daedalus.adapters import make_adapter

    try:
        adapter = make_adapter(load_config(root).adapter)
    except DaedalusError:
        return None
    caps = adapter.capabilities
    if not (caps.launch_agent and caps.structured_results and caps.identity == "launched"):
        return None
    available = getattr(adapter, "available", None)
    return adapter if available is None or available() else None


def settle(ari: Ariadne, run_id: str, *, reviews: bool = True, cache: bool = True, notify=lambda _m: None) -> tuple[Decision, list[str], bool]:
    """Bring a run as close to a decision as the system can on its own: verify stale or
    missing evidence, then (once checks pass) launch every review the run still needs.

    Returns the decision, notes for the user, and whether reviews are the system's job
    here (an adapter can launch them), in which case a missing review is never put to
    the agent as something it must do."""
    from daedalus.orchestration.lifecycle import launch_required_reviews

    notes: list[str] = []
    if not cache:
        ari.no_cache = True  # `--no-cache`: verify and review for real, whatever earlier runs proved
    if ari.needs_recovery(run_id):
        ari.recover(run_id)
    decision = ari.evaluate(run_id)
    if any(st in (CheckState.NOT_RUN, CheckState.STALE) for st, _ in decision.check_states.values()):
        ari.verify(run_id)
        decision = ari.evaluate(run_id)
    adapter = _review_adapter(ari.root) if reviews and _harness(ari.root).get("auto_review", True) else None
    checks_ok = not any(r.code.startswith("check:") for r in decision.reasons)
    wants_review = any(r.code.startswith("review:") for r in decision.reasons)
    if adapter is not None and checks_ok and wants_review:
        try:
            launch_required_reviews(ari, run_id, adapter, notify)
        except DaedalusError as exc:  # e.g. a billed review the cash budget cannot cover
            notes.append(str(exc))
        decision = ari.evaluate(run_id)
    return decision, notes, adapter is not None


def _agent_items(decision: Decision) -> list[str]:
    """What the agent itself can fix. A review is never the author's job: the gate launches it,
    or a person arranges one."""
    return [
        f"[agent] {r.message}"
        for r in decision.reasons
        if r.fixable_by == "agent" and r.code != "not_finished" and not r.code.startswith("review:")
    ]


def stop_hook(payload: dict[str, Any], *, ari: Ariadne | None = None) -> dict[str, Any]:
    if launched_by_daedalus():
        return {}
    root = find_root(payload.get("cwd") or ".")
    if not gate_enabled(root):
        return {}
    own = ari is None
    ari = ari or Ariadne(root)
    try:
        run_id = ari.active_run_id()
        if run_id is None:
            # Changes made without an edit tool (a shell command) still get gated.
            prompt = _read_prompt(root)
            if not prompt or not prompt.get("tree") or _snapshot(ari) == {"head": prompt.get("head"), "tree": prompt.get("tree")}:
                return {}
            if _is_accepted_state(ari):
                return {}  # e.g. accepted work committed since: there is nothing new to gate
            run_id = auto_open(ari, payload.get("session_id"))
            if run_id is None:
                return {}
        state = ari.state(run_id)
        if state.execution is not ExecutionState.RUNNING or state.cancel_requested:
            return {}
        decision, notes, system_reviews = settle(ari, run_id)
        if decision.acceptable:
            followup = ari.followup_candidates(run_id)
            cand = ari.capture(ari.state(run_id)) if followup else None
            from daedalus.repository import snapshot

            if followup and snapshot.save(ari.root, ari.state_dir / "runs" / run_id, cand.candidate_id, cand.manifest):
                # Once per run: non-blocking findings get one fix pass, then acceptance. The acceptable
                # candidate is kept, so `daedalus restore` can always undo the pass.
                from daedalus.orchestration.lifecycle import followup_feedback

                ari.request_followup(run_id, [f.finding_id for f in followup], cand.candidate_id)
                return {"decision": "block", "reason": "Daedalus gate:\n- " + "\n- ".join(followup_feedback(followup))
                        + "\nThen stop again; the gate reviews only what you change."}
            final = ari.finish(run_id)
            note = proposal_note(ari.state(run_id))
            if note:  # accepted against the contract as it stood; the proposal was never applied
                return {"systemMessage": summary_line(final) + "\n" + note.replace("[human] ", "Unconfirmed: ")}
            return {"systemMessage": summary_line(final)}

        if decision.disposition is Disposition.REJECTED:  # nothing the agent does can change it
            final = ari.finish(run_id)
            return {"systemMessage": summary_line(final) + "\n" + "\n".join(render_reasons(final))}
        pending_reviews = [r.code.split(":", 1)[1] for r in decision.reasons if r.code.startswith("review:")]
        agent = _agent_items(decision)
        cap = int(_harness(root).get("max_stop_blocks", DEFAULT_MAX_STOP_BLOCKS))
        if agent and state.followup_from:
            agent.append(
                "[agent] the candidate before your follow-up pass was acceptable: if these are not quick to fix, "
                "run `daedalus restore` to undo the pass, then stop"
            )
        if state.followup_from and (not agent or state.stop_blocks >= cap):
            # About to stop blocking with the follow-up worse off than where it began: undo it.
            from daedalus.orchestration.lifecycle import restore_followup

            try:
                notes += restore_followup(ari, run_id)
            except DaedalusError as exc:
                notes.append(str(exc))
            decision = ari.evaluate(run_id)
            if decision.acceptable:
                final = ari.finish(run_id)
                return {"systemMessage": summary_line(final) + "\n" + "\n".join(notes)}
            agent = _agent_items(decision)
            pending_reviews = [r.code.split(":", 1)[1] for r in decision.reasons if r.code.startswith("review:")]
        if agent and state.stop_blocks < cap:
            ari.note_stop_blocked(run_id, agent)
            return {
                "decision": "block",
                "reason": (
                    "Daedalus gate: this work is not accepted yet. Resolve these before stopping "
                    f"(attempt {state.stop_blocks + 1}/{cap}):\n- "
                    + "\n- ".join(agent)
                    + ("\n" + WORKLIST_HINT if ari.state(run_id).open_findings else "")
                    + "\nRun `daedalus status` for detail. Do not edit policy or checks to get past the gate."
                ),
            }
        lines = [summary_line(decision)]
        if agent:
            lines.append(f"The agent stopped after {cap} gate blocks with agent-fixable items still open:")
            lines += agent
        human = render_reasons(decision, "human") + [
            f"[system] {r.message}" for r in decision.reasons
            if r.fixable_by == "system" and not r.code.startswith("review:")  # reviews: one line each, below
        ]
        for p in pending_reviews:
            if system_reviews:
                human.append(f"[system] the {review_name(p)} has not passed on this candidate (see notes above, or rerun "
                             f"`daedalus verify`)")
            else:
                human.append(f"[human] {with_article(review_name(p))} is required, and no adapter here can launch one: set "
                             f"`adapter: claude-code` in .daedalus.yml, or run `daedalus review {p}` with one")
        human += [f"[human] {n}" for n in notes]
        if (note := proposal_note(ari.state(run_id))) is not None:
            human.append(note)
        if human:
            lines.append("Needs you:")
            lines += human
        lines.append("Then run `daedalus finish` (or keep working with the agent).")
        return {"systemMessage": "\n".join(lines)}
    except DaedalusError as exc:
        return {"systemMessage": f"Daedalus gate error (run left open, not accepted): {exc}"}
    finally:
        if own:
            ari.close()


# --------------------------------------------------------------- session start
def session_start_hook(payload: dict[str, Any], *, ari: Ariadne | None = None) -> dict[str, Any]:
    if launched_by_daedalus():
        return {}
    root = find_root(payload.get("cwd") or ".")
    if not gate_enabled(root):
        return {}
    from daedalus.harness.provenance import install_git_hook

    try:
        install_git_hook(root, only_if_absent=True)
    except DaedalusError:
        pass
    own = ari is None
    ari = ari or Ariadne(root)
    try:
        run_id = ari.active_run_id()
        if run_id is None:
            context = (
                "This repository works under the Daedalus acceptance gate. Your first file edit opens a run "
                "whose objective is the user's request; when you stop, Daedalus runs the trusted checks and "
                "any required reviews and decides whether the work is accepted. Follow the daedalus-gate skill."
            )
        else:
            state = ari.state(run_id)
            c = state.contract
            crit = "; ".join(f"{x.criterion_id}: {x.description}" for x in c.acceptance_criteria)
            context = (
                f"A Daedalus run ({run_id}, mode {state.mode}) is open in this repository and the acceptance "
                f"gate is ON. Objective: {c.objective}. Acceptance criteria: {crit}. Scope: {', '.join(c.scope)}. "
                "Follow the daedalus-gate skill: work inside scope and fix every [agent] blocker. You cannot "
                "declare the work accepted; the gate decides when you stop."
            )
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}
    except DaedalusError:
        return {}
    finally:
        if own:
            ari.close()


# ---------------------------------------------------------------- status line
def statusline(payload: dict[str, Any]) -> str:
    """One line for Claude Code's status bar, from recorded state only: it never hashes the
    tree or runs anything, because the bar refreshes constantly. Never raises."""
    try:
        ws = payload.get("workspace") or {}
        root = find_root(ws.get("current_dir") or payload.get("cwd") or ".")
        if not (root / ".daedalus.yml").exists():
            return ""
        if not gate_enabled(root):
            return "Daedalus off"
        ari = Ariadne(root)
        try:
            rid = ari.active_run_id()
            if rid is None:
                return "Daedalus on · no open run"
            state = ari.state(rid)
            last: dict[str, str] = {}
            for e in state.evidence:  # a result for an older candidate is not this tree's result
                fresh = e.candidate_id == state.current_candidate_id
                last[e.check_id] = e.result if fresh else "stale"
            checks = " ".join(f"{c} {last.get(c, 'not run')}" for c in state.contract.required_checks)
            blocking = [f for f in state.open_findings if f.severity.value in state.policy.blocking_severities]
            parts = [f"Daedalus · run {rid[:6]}", checks or "no checks"]
            if blocking:
                parts.append(f"{len(blocking)} blocking finding(s)")
            return " · ".join(parts)
        finally:
            ari.close()
    except Exception:  # noqa: BLE001 — a status bar must never break the harness
        return ""


def bus_hook(payload: dict[str, Any], *, ari: Ariadne | None = None) -> dict[str, Any]:
    """PostToolUse in a launched, coordinating session: hand the agent the bus messages that arrived
    since it last looked, as added context. The only hook that runs inside a launched session (the
    adapter adds it there with `--settings`); anywhere without an agent name it does nothing."""
    from daedalus.coordination.bus import FileBus
    from daedalus.coordination.channel import Channel, peer_block
    from daedalus.coordination.launch import ENV_REPO
    from daedalus.coordination.surface import ENV_AGENT, ENV_BUS_DIR, ENV_RUN

    name, run_id, where, repo = (os.environ.get(k) for k in (ENV_AGENT, ENV_RUN, ENV_BUS_DIR, ENV_REPO))
    if not (name and run_id and where and repo):
        return {}
    own = ari is None
    try:
        ari = ari or Ariadne(Path(repo))
        msgs = Channel(ari, run_id, FileBus(Path(where))).read(name)
    except (DaedalusError, OSError, ValueError):
        return {}  # coordination never gets in the way of the work
    finally:
        if own and ari is not None:
            ari.close()
    if not msgs:
        return {}
    text = peer_block(msgs, f"New coordination messages ({len(msgs)}):")
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}


def session_hooks() -> dict[str, Any]:
    """`--settings` for a launched, coordinating builder: the live-delivery hook, nothing else."""
    return {"hooks": {"PostToolUse": [{"matcher": "*", "hooks": [{"type": "command",
                                                                   "command": _hook_command("bus")}]}]}}


def run_hook(name: str, stdin_text: str, args: list[str] | None = None) -> str:
    if name == "commit-msg":
        from daedalus.harness.provenance import commit_msg_hook

        commit_msg_hook(args or [])
        return ""
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except ValueError:
        payload = {}
    handlers = {
        "stop": stop_hook,
        "session-start": session_start_hook,
        "prompt": prompt_hook,
        "pre-edit": pre_edit_hook,
        "bus": bus_hook,
    }
    handler = handlers.get(name)
    if handler is None:
        raise DaedalusError(f"unknown hook {name!r}")
    try:
        out = handler(payload)
    except Exception as exc:  # fail open for the harness, never into acceptance
        out = {"systemMessage": f"Daedalus hook {name} failed (run not accepted): {exc}"}
    return json.dumps(out) if out else ""


# -------------------------------------------------------------------- install
def _ours(h: dict[str, Any]) -> bool:
    cmd = h.get("command", "")
    return HOOK_MARK in cmd or "-m daedalus hook" in cmd


def install(root: Path, *, skills: tuple[str, ...] = ("gate",)) -> list[str]:
    """Merge Daedalus hooks into .claude/settings.json and install skills. Idempotent;
    the result is meant to be committed so every session gets the gate."""
    done: list[str] = []
    settings_path = root / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    except ValueError as exc:
        raise DaedalusError(f"{settings_path} is not valid JSON; fix it before installing") from exc
    hooks = settings.setdefault("hooks", {})
    bootstrap = _harness(root).get("bootstrap")
    bootstrap = bootstrap if isinstance(bootstrap, str) and bootstrap.strip() else None
    wanted: dict[str, dict[str, Any]] = {
        "Stop": {"hooks": [{"type": "command", "command": _hook_command("stop"), "timeout": STOP_HOOK_TIMEOUT_S}]},
        "SessionStart": {"hooks": [{"type": "command", "command": _hook_command("session-start", bootstrap)}]},
        "UserPromptSubmit": {"hooks": [{"type": "command", "command": _hook_command("prompt")}]},
        "PreToolUse": {"matcher": EDIT_TOOLS, "hooks": [{"type": "command", "command": _hook_command("pre-edit")}]},
    }
    for event, group in wanted.items():
        groups = hooks.setdefault(event, [])
        for g in groups:  # drop older Daedalus hooks so re-install updates them
            g["hooks"] = [h for h in g.get("hooks", []) if not _ours(h)]
        groups[:] = [g for g in groups if g.get("hooks")]
        groups.append(group)
        done.append(f"hook {event}")
    if "statusLine" not in settings:
        settings["statusLine"] = {"type": "command", "command": STATUSLINE_COMMAND}
        done.append("status line")
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    for role in skills:
        name = ROLE_SKILLS[role]
        dest = root / ".claude" / "skills" / name
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SKILLS_DIR / name / "SKILL.md", dest / "SKILL.md")
        done.append(f"skill {name} -> {dest.relative_to(root).as_posix()}")
    return done


def _detect(root: Path) -> bool:
    return (root / ".claude").is_dir() or shutil.which("claude") is not None


INTEGRATION = HarnessIntegration(
    name="claude-code",
    description="Claude Code: hooks, status line and the gate skill in .claude/",
    install=lambda root: install(root),
    detect=_detect,
    adapter_preset="claude-code",
)
