# Codex CLI

Module `src/daedalus/harness/codex.py`, harness name `codex`, adapter preset `codex`.
Verified against codex-cli 0.163.0 (installed with npm, driven against a mock
Responses server; no model account was available, so a real model session was not run).

## Setup

```
daedalus init            # or: the harness installer for `codex`
git add .codex/hooks.json AGENTS.md && git commit
```

`install` writes, idempotently and merging with what is there:

- `.codex/hooks.json`: Stop, SessionStart, UserPromptSubmit and PreToolUse (matcher
  `apply_patch|Edit|Write`) hooks, each `if command -v daedalus ...; then daedalus hook <name>; fi`,
  so they are no-ops on machines without Daedalus (SessionStart says how to install it).
  Existing user hooks are kept; older Daedalus hooks are replaced in place.
- `AGENTS.md`: a managed block (`<!-- daedalus:begin ... -->` to `<!-- daedalus:end -->`)
  holding the daedalus-gate instructions. Text outside the block is never touched.

## Trust: hooks do not run until the user approves them

Codex hooks are enabled by default (`[features] hooks`), but a repository cannot switch
its own hooks on:

1. The project must be trusted (`[projects."<path>"] trust_level = "trusted"` in
   `~/.codex/config.toml`; Codex asks on first open). Untrusted project: `.codex/hooks.json` is ignored.
2. Every non-managed command hook must be reviewed in the TUI's `/hooks` screen. Approval is stored
   as a hash of the hook definition (`hooks.state."<key>".trusted_hash` in the user config), so any
   change to the hook command, including a Daedalus upgrade that changes it, needs a new review.
   Unreviewed hooks are skipped silently (`codex exec` says "hooks need review").
3. `codex exec --dangerously-bypass-hook-trust` runs enabled hooks without review. Intended only for
   automation that vets the hook source. Observed: with a trusted project but no review and no
   flag, no hook ran; with the project trusted and the flag, all four fired.

Until a user has done this, Codex sessions in the repository are not gated by hooks. The git
provenance hook (commit-msg, pre-push) and `daedalus` commands are unaffected.

## What is enforced, per event

| Event | Codex | Daedalus | Status |
|---|---|---|---|
| prompt submitted | `UserPromptSubmit` (stdin: `session_id`, `cwd`, `prompt`, `turn_id`, ...) | `hook prompt` | Enforced |
| before a file edit | `PreToolUse`, tool `apply_patch` (`Edit`/`Write` are matcher aliases; `tool_name` stays `apply_patch`) | `hook pre-edit` opens the run | Enforced for apply_patch. Shell-made edits do not fire it; Stop opens the run then by comparing the tree to the prompt snapshot |
| turn end | `Stop` (stdin has `stop_hook_active`, `last_assistant_message`) | `hook stop` | Enforced. `{"decision":"block","reason":...}` on stdout makes Codex continue; `reason` reaches the model as a hook prompt (observed: a second model request carried it). `systemMessage` is shown to the user |
| session start | `SessionStart` (`source`: startup, resume, clear, compact) | `hook session-start` | Enforced. `hookSpecificOutput.additionalContext` becomes a developer message in the model context (observed) |

The payloads are Claude-compatible, so no translating shim exists: the same
`daedalus hook` handlers (`harness/claude_code.py`) serve both harnesses.

What degrades and why:

- The Stop hook can run for minutes; the hook is installed with `timeout: 3600` (seconds; Codex's default is 600).
- Codex documents `PreToolUse` as a guardrail, not a complete boundary: commands run through the shell
  tool are not seen as file edits. Daedalus relies on Stop for these, as it does in Claude Code.
- Hooks do nothing in sessions Daedalus launches (`DAEDALUS_AGENT`), same as for Claude Code.
- Project hooks are skipped until reviewed (above); there is no repository-side way around that.
- `codex exec` Stop-block loops: `stop_hook_active` is true on the continuation turn; Daedalus caps its
  own blocks (`harness.max_stop_blocks`).

## Adapter preset (`adapter: {name: codex}`)

Launch: `codex exec --json --color never <flags>`, prompt on stdin (Codex reads stdin when no prompt argument is given).

- Builder: `--sandbox workspace-write` (writes confined to the workspace, no network; `.git` is read-only
  inside it, so commits are made by Daedalus, not the agent).
- Reviewer: `--sandbox read-only --ephemeral --disable hooks`. `read-only` is Codex's default for `exec`,
  and is passed explicitly; `--ephemeral` writes no session files; `--disable hooks` was checked to
  suppress even trusted and bypassed hooks. Reads are probably not confined to the workspace (Codex's read-only
  sandbox is documented as read-everything; not tested here), unlike the claude-code reviewer.
- `--full-auto` is gone: codex-cli 0.163.0 rejects it ("unexpected argument"). The previous preset used it
  and would have failed to launch. The replacement is the explicit sandbox flag.
- Output: JSONL events. Parsed: `item.completed` with `item.type == "agent_message"` (last one is the
  answer), `turn.completed.usage` (`input_tokens`, `cached_input_tokens`, `output_tokens`,
  `reasoning_output_tokens`, summed across turns) and `turn.failed` / `error`. `item.completed` items of type
  `error` are warnings. No cost is reported, so cost stays 0 and only tokens are recorded.
- Still marked "experimental preset": it was run against a mock Responses server, not a real model.

## Sources

- https://developers.openai.com/codex/hooks (events, hooks.json, trust, block semantics; the page was not reachable from the sandbox, so its content was cross-checked with a mirror and with the installed binary's behaviour)
- https://developers.openai.com/codex/guides/agents-md
- https://developers.openai.com/codex/noninteractive
- https://developers.openai.com/codex/security
- https://github.com/openai/codex (`codex exec --help`, `codex features list`)
