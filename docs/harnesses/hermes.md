# Hermes Agent (Nous Research)

Module `src/daedalus/harness/hermes.py`, harness name `hermes`, adapter preset `hermes`.
The gate is a small Python plugin that shells out to `daedalus hook`.

## Setup

```
daedalus install hermes        # writes .hermes/plugins/daedalus/ and the git provenance hooks
```

Hermes loads plugins only from `~/.hermes/plugins/` (or pip entry points), and only after you
enable them, so `install` stays inside the repository and prints the one command to run:

```
mkdir -p ~/.hermes/plugins && ln -sfn "<repo>/.hermes/plugins/daedalus" ~/.hermes/plugins/daedalus && hermes plugins enable daedalus
```

The symlink keeps the user-level plugin tracking the committed copy; re-running `install` updates
it in place. The plugin is a no-op in repositories that are not gated and where `daedalus` is not
on PATH. Gates that take longer than 30 seconds need Hermes to wait for them:

```
hermes config set plugins.hook_callback_timeout 600
```

Adapter: `adapter: hermes` in `.daedalus.yml` launches `hermes chat --oneshot -Q --source daedalus
--query-file -` (prompt on stdin). Experimental: it is built from the documented CLI and not
exercised against a live model.

## Event mapping

| Gate need | Hermes event | Enforced? |
|---|---|---|
| Prompt submitted | `pre_llm_call` (once per turn) -> `daedalus hook prompt` | yes |
| Before a file edit | `pre_tool_call` on `write_file`, `patch` -> `pre-edit` | yes; never blocks the edit |
| Agent finishing | `pre_verify` -> `stop`; block+reason becomes `{"action": "continue", "message"}`, which Hermes appends as a user turn | yes, with limits below |
| Session start / inject context | first `pre_llm_call` of a session -> `session-start`, returned as `{"context": ...}` | yes |
| Skills | not installed (Hermes skills are user-level) | instructions arrive as session context instead |

`pre_tool_call` can block (`{"action": "block", "message"}`) but the gate never needs it.
Project-scoped hooks do not exist: plugins, `hooks:` in `config.yaml` and `~/.hermes/hooks/` are all
profile-level. `.hermes/plugins/` and `HERMES_ENABLE_PROJECT_PLUGINS` are explicitly not loaded by the
gateway that runs sessions. Context files (`.hermes.md`, `AGENTS.md`) are not touched.

## What degrades, and why

- **pre_verify only fires after Hermes saw a file-edit tool land**, so work done purely through
  `terminal` is not stopped. It is still gated at `daedalus finish` and by the pre-push provenance hook.
- **At most `agent.max_verify_nudges` (default 3) continues per turn.** The gate's own cap
  (`harness.max_stop_blocks`, default 8) is therefore not reachable; raise `agent.max_verify_nudges`
  to match. When Hermes stops nudging the run stays open and unaccepted.
- **Hermes abandons a `pre_verify` callback after `plugins.hook_callback_timeout`** (default 30s, max
  600; `pre_tool_call` fails closed, `pre_verify` fails open). The shim waits at most that long minus
  a margin; if the gate is slower, Hermes finishes the turn (never as acceptance) and the verdict is
  posted to the user and injected into the next turn.
- **`pre_tool_call` is capped at 30s and fails closed**, so the shim bounds `pre-edit` to 20s and
  returns no directive on any error.
- **The `DAEDALUS_AGENT` marker does not reach plugins** (sessions run in a shared gateway whose
  client-env allowlist excludes it), so launched sessions are tagged `--source daedalus` and the
  shim checks `HERMES_SESSION_SOURCE` as well as the env var.
- **Reviewers have no file access.** Hermes's `file` toolset bundles `write_file` and `patch`, and
  there is no read-only file toolset or deny flag. Review launches use `--toolsets todo --ignore-rules`
  (no file, shell, web or MCP tools; no AGENTS.md or memory a Builder could have written) and judge from
  the diff in the prompt. This is enforced by the tool list, not by a sandbox.
- Token usage is available from `--format stream-json` but there is no cost, so the preset parses text
  and does not report cost.

## Sources

- https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/hooks.md
  (`pre_tool_call`, `pre_llm_call`, `pre_verify`, `on_session_start`, return shapes, timeouts)
- https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/plugins.md
  (plugin layout, opt-in `plugins.enabled`, project-local plugins not loaded)
- https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/context-files.md
- https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/cli-commands.md
  (`hermes chat -q/--oneshot/-Q/--query-file/--toolsets/--source/--ignore-rules`)
- https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/toolsets-reference.md
- Source read at commit 40cc180: `hermes_cli/plugins.py` (`VALID_HOOKS`, `get_pre_verify_continue_message`),
  `agent/turn_stop_gates.py`, `hermes_cli/plugins_dispatch.py` (timeout-bounded hooks),
  `agent/session_client_env.py`.
