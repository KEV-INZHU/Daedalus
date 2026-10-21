# OpenCode

Daedalus supports [OpenCode](https://opencode.ai) (sst/opencode) in two ways: an ambient gate (a plugin in the
repository) and an adapter preset (`opencode run`) for agents Daedalus launches itself.

## Setup

```
daedalus init            # once the harness registry calls INTEGRATION.install; until then, call opencode.install(root)
git add .opencode && git commit
```

`install` copies one file, `.opencode/plugins/daedalus-gate.js`, which OpenCode loads at startup from
`{plugin,plugins}/*.{js,ts}`. Nothing is added to `opencode.json`, so user config and other plugins are untouched.
The `daedalus-gate` skill goes to `.opencode/skills/` unless `.claude/skills/` already has it (OpenCode reads that
too). Re-installing overwrites our file in place. A copy left in the older singular `plugin/` directory is removed so
the shim never loads twice. `uninstall` removes only these files.

To launch reviewers and builders through OpenCode, set the adapter in `.daedalus.yml`:

```yaml
adapter:
  name: opencode
```

The shim is a no-op where `daedalus` is not on PATH (spawn `ENOENT`), and inside sessions Daedalus launched itself
(`DAEDALUS_AGENT` is set). Errors in the shim never block the user; they only ever let the agent stop with the run
still open and unaccepted.

## Lifecycle mapping

| Daedalus hook | OpenCode event | Enforced? |
| --- | --- | --- |
| `prompt` | `chat.message` hook (user message received) | Yes. |
| `pre-edit` | `tool.execute.before` for `edit`, `write`, `patch`, `multiedit`, `apply_patch` | Yes. It opens the run and never blocks the edit. For `apply_patch` the target path is not in the args, so only "a run is open" is guaranteed. |
| `stop` | `event` with `session.idle` | Degraded: see below. |
| `session-start` | `experimental.chat.system.transform` | Yes, once per session, as an extra system prompt block. The hook is marked experimental in the plugin types; if a release drops it, only the context injection is lost. |
| context for the agent | same | Via the system prompt. |

### Stop: continue-with-feedback

OpenCode has no hook that can veto the agent going idle. The plugin reacts to `session.idle` instead: it runs
`daedalus hook stop`, and on `{"decision": "block", "reason": ...}` it calls
`client.session.prompt({path: {id}, body: {parts: [{type: "text", text: "Stop hook feedback:\n" + reason}]}})`,
which starts a new agent turn carrying the reason. That turn ends in another `session.idle`, which runs the gate again.
This cannot loop forever: the gate itself counts blocks (`harness.max_stop_blocks`, default 8) and stops answering
`block` after that. The prompt is deliberately not awaited, so a new idle event is never starved by a still-running
reply. The `Stop hook feedback:` prefix makes the `prompt` hook ignore this message as a user request. A
`systemMessage` answer (accepted, rejected, needs a human) is shown as a TUI toast.

What this degrades, compared with Claude Code's Stop:

- The turn has already ended when the gate runs; the agent is called back, not held. Visible to the user as one more
  assistant turn.
- Interrupted sessions (`session.error` with `MessageAbortedError`) are skipped once, so Esc stops the agent.
- Subagent (child) sessions are not gated; only the primary session is.
- `opencode run` (headless, one message) exits as soon as the session is idle and does not wait for the plugin, so
  the gate cannot continue the agent there (observed on 1.19.3: the process exits before the stop hook finishes).
  Use the TUI, `opencode web`, or `opencode serve` plus `opencode run --attach <url>`, where the server stays alive;
  that path was run end to end (block, feedback turn, fix, ACCEPTED). For anything that ends with a bare
  `opencode run`, `daedalus verify` / `daedalus finish` are authoritative.
- Toasts exist only in the TUI/web UI. Without one, the user message is dropped (the run state is still recorded;
  see `daedalus status`).

## Adapter preset `opencode`

`opencode run --format json`, prompt on stdin (a positional prompt is re-quoted by OpenCode, so stdin is used), output
parsed from the JSON event stream (`text` parts for the answer; `step_finish` for tokens and cost; `error` events
fail the session).

Builders need no extra flag: `opencode run` permits edits and shell by default, and anything that would ask (outside
the directory, doom loops) is refused when headless. `--auto` (approve everything not explicitly denied) is never used.

### Read-only reviewers

OpenCode has **no read-only CLI flag**. Read-only is a permission configuration, and what we verified is:

- `--agent <name>` with a name that does not exist silently falls back to the default (write-capable) agent, so an
  agent-based restriction fails open.
- An agent-level permission (for example a project's `agent.build.permission`) outranks the global `permission`, and a
  project `opencode.json` can re-grant `bash`/`edit`/`write` over a plain global deny, even from the inline config.
- The inline config (`OPENCODE_CONFIG_CONTENT`) plus `OPENCODE_DISABLE_PROJECT_CONFIG=1` held: with a hostile project
  config trying to allow everything, the model was offered only `read`, `grep` and `glob`, for any `--agent`.

So the preset does not run `opencode` directly. Its argv is `python -m daedalus.harness.opencode run --format json`
and a review adds `--daedalus-read-only`. That launcher, for a review only:

1. generates the config `{"permission": {"*": "deny", <write tools>: "deny", read/grep/glob/list: "allow"}}`, sets the
   same map under `agent.build`, `agent.plan` and `agent.general` (an agent-level permission outranks the global one),
   and passes it as `OPENCODE_CONFIG_CONTENT` (keeping only `provider`/`model` keys from any inline config the user had);
2. sets `OPENCODE_DISABLE_PROJECT_CONFIG=1`, so the repo's `opencode.json`, `.opencode/` agents, plugins and commands,
   which a Builder session can write, cannot widen it (the same reason `claude --restricted` is used);
3. points `OPENCODE_CONFIG`, `OPENCODE_CONFIG_DIR` and `XDG_CONFIG_HOME` at a fresh empty temporary directory (removed
   afterwards), so the user's global config (`~/.config/opencode`), whose agent-level permissions could widen the
   reviewer, is not read and only the inline config applies;
4. pins `--agent build` (replacing any other `--agent`), drops `OPENCODE_PERMISSION`, and execs `opencode` with the
   `--daedalus-read-only` flag removed. If `opencode` is not on PATH it exits 127 and nothing runs; the flag is never
   ignored.

What a reviewer can and cannot see: it sees the working tree through `read`/`grep`/`glob`/`list`, the `provider`,
`model`, `small_model`, `enabled_providers` and `disabled_providers` of the inline config, and the provider credentials
in `auth.json`, which OpenCode keeps under `XDG_DATA_HOME` (default `~/.local/share/opencode`); `XDG_DATA_HOME` is
left as it was, so `opencode auth login` credentials and provider environment variables (`ANTHROPIC_API_KEY`, ...)
keep working. It does not see the global or project `opencode.json`, global or project agents, plugins, commands and MCP
servers, so a provider or API key defined only in a config file is not available to reviewers. A provider defined only in
the repository's `opencode.json` is not visible either. The launcher also sets `$PWD` to the working directory, because
OpenCode takes its project directory from `$PWD` and a parent that only sets the child's cwd would otherwise have the
session run in the parent's directory. Not checked against the opencode source here (opencode is not installed in this
environment): `~/.opencode` is not redirected, only the config variables above.

The preset is marked `experimental preset`: it is verified against OpenCode 1.19.3 with a mock OpenAI-compatible model,
not against a real provider.

## Verified how

Checked against the published `opencode-ai@1.19.3` binary and `@opencode-ai/plugin` / `@opencode-ai/sdk` types, and by
running the real binary against a mock model server (plugin load, event order, the block-and-continue loop, system
prompt injection, the offered tool list under the read-only launcher). opencode.ai itself was not reachable from the
build sandbox, so the doc pages below are cited for the APIs but the behaviour above comes from the package and runs.

## References

- Plugins and hooks: https://opencode.ai/docs/plugins/
- Config and precedence: https://opencode.ai/docs/config/
- Permissions: https://opencode.ai/docs/permissions/
- Agents: https://opencode.ai/docs/agents/
- CLI (`opencode run`, `serve`, `--attach`): https://opencode.ai/docs/cli/
- Skills: https://opencode.ai/docs/skills/
- SDK: https://opencode.ai/docs/sdk/
- Plugin types: https://github.com/sst/opencode/blob/dev/packages/plugin/src/index.ts
