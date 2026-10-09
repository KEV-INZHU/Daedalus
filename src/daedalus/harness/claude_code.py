"""Claude Code shim: make the gate feel like a mode of the harness.

Two hooks and one skill:

- **Stop** — when the agent tries to end its turn while a run is open, Daedalus
  verifies stale or missing evidence and evaluates the acceptance predicate. If
  agent-fixable conditions remain, the stop is blocked and the agent receives
  the exact reasons. If only human decisions remain, the stop is allowed and the
  user is told which commands to run. If the predicate holds, the run is
  finished and the disposition recorded. The agent never declares acceptance.
- **SessionStart** — injects the open run's contract summary so a new session
  knows it is working under the gate.
- **daedalus-gate skill** — instructions for participating. Instructions only:
  the hooks and the core enforce whether or not the agent follows them.

Hooks fail open on internal errors (the user is never locked out of their
harness) but never fail open into acceptance: an error only ever lets the
agent stop with the run still open and unaccepted.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

from daedalus.core.acceptance import Decision
from daedalus.core.config import find_root, gate_enabled, load_config
from daedalus.core.errors import DaedalusError
from daedalus.core.state_machine import CheckState, Disposition, ExecutionState
from daedalus.orchestration.ariadne import Ariadne
from daedalus.orchestration.prompts import ROLE_SKILLS, SKILLS_DIR

DEFAULT_MAX_STOP_BLOCKS = 8
STOP_HOOK_TIMEOUT_S = 1800


def _hook_command(sub: str) -> str:
    return f'"{Path(sys.executable).as_posix()}" -m daedalus hook {sub}'


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


# ----------------------------------------------------------------------- stop
def stop_hook(payload: dict[str, Any], *, ari: Ariadne | None = None) -> dict[str, Any]:
    root = find_root(payload.get("cwd") or ".")
    if not gate_enabled(root):
        return {}
    own = ari is None
    ari = ari or Ariadne(root)
    try:
        run_id = ari.active_run_id()
        if run_id is None:
            return {}
        state = ari.state(run_id)
        if state.execution is not ExecutionState.RUNNING or state.cancel_requested:
            return {}
        if ari.needs_recovery(run_id):
            ari.recover(run_id)
        decision = ari.evaluate(run_id)
        if any(st in (CheckState.NOT_RUN, CheckState.STALE) for st, _ in decision.check_states.values()):
            ari.verify(run_id)
            decision = ari.evaluate(run_id)
        if decision.acceptable:
            final = ari.finish(run_id)
            return {"systemMessage": summary_line(final)}

        if decision.disposition is Disposition.REJECTED:  # nothing the agent does can change it
            final = ari.finish(run_id)
            return {"systemMessage": summary_line(final) + "\n" + "\n".join(render_reasons(final))}
        agent = render_reasons(decision, "agent")
        cap = int(load_config(root).harness.get("max_stop_blocks", DEFAULT_MAX_STOP_BLOCKS))
        if agent and state.stop_blocks < cap:
            ari.note_stop_blocked(run_id, agent)
            return {
                "decision": "block",
                "reason": (
                    "Daedalus gate: this run is not accepted yet. Resolve these before stopping "
                    f"(attempt {state.stop_blocks + 1}/{cap}):\n- "
                    + "\n- ".join(agent)
                    + "\nRun `daedalus status` for detail. Do not edit policy or checks to get past the gate."
                ),
            }
        lines = [summary_line(decision)]
        if agent:
            lines.append(f"The agent stopped after {cap} gate blocks with agent-fixable items still open:")
            lines += agent
        human = render_reasons(decision, "human") + render_reasons(decision, "system")
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
    root = find_root(payload.get("cwd") or ".")
    if not gate_enabled(root):
        return {}
    own = ari is None
    ari = ari or Ariadne(root)
    try:
        run_id = ari.active_run_id()
        if run_id is None:
            return {}
        state = ari.state(run_id)
        c = state.contract
        crit = "; ".join(f"{x.criterion_id}: {x.description}" for x in c.acceptance_criteria)
        context = (
            f"A Daedalus run ({run_id}, mode {state.mode}) is open in this repository and the acceptance "
            f"gate is ON. Objective: {c.objective}. Acceptance criteria: {crit}. Scope: {', '.join(c.scope)}. "
            "Follow the daedalus-gate skill: work inside scope, run `daedalus verify` and `daedalus status`, "
            "and fix every [agent] blocker. You cannot declare the work accepted; the gate decides when you stop."
        )
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}
    except DaedalusError:
        return {}
    finally:
        if own:
            ari.close()


def run_hook(name: str, stdin_text: str) -> str:
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except ValueError:
        payload = {}
    handler = {"stop": stop_hook, "session-start": session_start_hook}.get(name)
    if handler is None:
        raise DaedalusError(f"unknown hook {name!r}")
    try:
        out = handler(payload)
    except Exception as exc:  # fail open for the harness, never into acceptance
        out = {"systemMessage": f"Daedalus hook {name} failed (run not accepted): {exc}"}
    return json.dumps(out) if out else ""


# -------------------------------------------------------------------- install
def install(root: Path, *, skills: tuple[str, ...] = ("gate",)) -> list[str]:
    """Merge Daedalus hooks into .claude/settings.json and install skills. Idempotent."""
    done: list[str] = []
    settings_path = root / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    except ValueError as exc:
        raise DaedalusError(f"{settings_path} is not valid JSON; fix it before installing") from exc
    hooks = settings.setdefault("hooks", {})
    wanted = {
        "Stop": {"type": "command", "command": _hook_command("stop"), "timeout": STOP_HOOK_TIMEOUT_S},
        "SessionStart": {"type": "command", "command": _hook_command("session-start")},
    }
    for event, hook in wanted.items():
        groups = hooks.setdefault(event, [])
        for g in groups:  # drop any older Daedalus hook so re-install updates the interpreter path
            g["hooks"] = [h for h in g.get("hooks", []) if "-m daedalus hook" not in h.get("command", "")]
        groups[:] = [g for g in groups if g.get("hooks")]
        groups.append({"hooks": [hook]})
        done.append(f"hook {event} -> {hook['command']}")
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    for role in skills:
        name = ROLE_SKILLS[role]
        dest = root / ".claude" / "skills" / name
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SKILLS_DIR / name / "SKILL.md", dest / "SKILL.md")
        done.append(f"skill {name} -> {dest.relative_to(root).as_posix()}")
    return done
