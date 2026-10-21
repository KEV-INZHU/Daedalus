# Pi

[Pi](https://github.com/badlogic/pi-mono) (`@mariozechner/pi-coding-agent`, CLI `pi`) is a minimal coding agent with
**no MCP and no settings-file hooks**. Its extension point is a TypeScript extension. Daedalus ships a ~100 line
one that turns Pi events into `daedalus hook <name>` calls (the same payloads Claude Code sends), so the gate logic
lives in one place. Verified against `@mariozechner/pi-coding-agent` 0.73.1 (docs and `dist` source), and run for
real with Pi's faux model provider.

## Setup

`daedalus install pi` (or the `pi` entry of `daedalus init`) writes, idempotently, and you commit them:

- `.pi/extensions/daedalus-gate.ts`: the shim. Pi auto-discovers `.pi/extensions/*.ts` and loads TypeScript
  without a build step. Re-install overwrites it; a file of that name that is not ours is refused, never replaced.
  `harness: {bootstrap: ...}` from `.daedalus.yml` is baked in and run when `daedalus` is missing.
- `.pi/skills/daedalus-gate/SKILL.md`: the gate skill (Pi implements the Agent Skills standard).
- The harness-agnostic git hooks (prepare-commit-msg stamp, pre-push check), unless someone else's hook is in the way.

Other extensions and `.pi/settings.json` are never touched. The shim is a no-op when `daedalus` is not on PATH
(it says how to install it once, at session start), inside sessions Daedalus launched itself (`DAEDALUS_AGENT`),
and on any error.

## What is enforced

| Gate step | Pi event | Status |
|---|---|---|
| `prompt` | `input` (skipped when `source == "extension"`, so the gate's own follow-ups never become the request) | enforced |
| `pre-edit` | `tool_call` for `edit` / `write` (payload `file_path` = Pi's `path`) | enforced; never blocks the edit |
| `session-start` | `session_start`, then `before_agent_start` | enforced; context is appended to the system prompt of every turn (not stored in the session) |
| `stop` | `agent_end` | **degraded**, see below |

Shell edits (`bash`) open the run at stop time, exactly as in Claude Code.

## What degrades, and why

Pi cannot veto the end of a turn: `agent_end` is notification-only, and a message queued from inside the handler is
never picked up because the loop has already finished (verified in `agent.js` / `agent-session.js`). So the
equivalent of `decision: block` is: the shim awaits `daedalus hook stop` (Pi awaits `agent_end` listeners, so a
long run of tests and reviews is not cut off; timeout 3600s), then once the agent is idle it sends the gate's
reason as a new user message with `pi.sendUserMessage(reason, {deliverAs: "followUp"})`, which starts a fresh turn.
Accepted or human-only outcomes are shown with `ctx.ui.notify`.

- Interactive and RPC mode: works end to end (observed: a faux-model session wrote a bad file, the gate sent it back,
  the second turn fixed it, and the next stop printed `ACCEPTED`).
- Print / JSON mode (`pi -p`, `--mode json`): the process exits when the prompt finishes, so a follow-up cannot run.
  The reason and summary go to stderr instead, and the agent is not sent back. Daedalus-launched agents are never
  gated this way (`DAEDALUS_AGENT`), so this only affects a person running `pi -p` directly.
- Aborted turns (Esc) and errored turns (Pi retries) are not treated as finish attempts.
- `tool_call` errors block the tool in Pi, so the shim catches everything and returns nothing.

## Adapter preset `pi`

`adapter: pi` in `.daedalus.yml` launches `pi -p --mode json --no-session`, prompt on stdin (Pi merges piped
stdin into the prompt), and parses the JSON event stream (`message_end` of assistant messages: final text, summed
`usage.cost.total`, tokens; a `stopReason` of `error`/`aborted` is a failure). Reviewers add
`--tools read,grep,find,ls --no-extensions --no-skills --no-prompt-templates --no-context-files`: an allowlist with
no `bash`/`edit`/`write` (observed: a `write` call from the model answers "Tool write not found"), and nothing a
builder wrote under `.pi/` or `AGENTS.md` is loaded. Builders get Pi's default tools. Cost is marked `cash` when
non-zero, because Pi does not say whether a provider is billed or a subscription. Marked an experimental preset: Pi
does not report rate limits, and sessions are not resumable. Pi has no sandbox of its own; `--tools` is the only
read-only enforcement.

## Sources

- Extensions (locations, events, `tool_call`, `input`, `pi.sendUserMessage`, `pi.sendMessage`, modes):
  https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/extensions.md
- Skills: https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/skills.md
- CLI flags and "No MCP": https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/README.md
- JSON event stream: https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/json.md
- RPC mode: https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/rpc.md
