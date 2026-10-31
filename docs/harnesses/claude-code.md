# Claude Code

Module `src/daedalus/harness/claude_code.py`, harness name `claude-code`, adapter preset `claude-code`.
This is the reference integration: the other harnesses reuse its `daedalus hook` handlers.

## Setup

```
daedalus init            # installs Claude Code's integration (always) plus any other detected harness
git add .daedalus.yml .claude .gitignore && git commit -m "Gate this repository with Daedalus"
```

`daedalus install claude-code` does just this harness. `install` writes, idempotently and merging with
what is there:

- `.claude/settings.json`: Stop, SessionStart, UserPromptSubmit and PreToolUse (file-edit tools) hooks,
  each `if command -v daedalus ...; then daedalus hook <name>; fi`, so they are no-ops on machines
  without Daedalus (SessionStart says how to install it). Your own hooks are kept; older Daedalus hooks
  are replaced in place. A `statusLine` entry is added only if you have none.
- `.claude/skills/daedalus-gate/SKILL.md`: the gate skill, telling the agent how to work under the gate.
- The harness-agnostic git hooks (a commit-msg stamp and a pre-push check), unless someone else's hook
  is in the way.

Commit these files. Changing them raises a run's risk to critical, because they decide whether a session
is gated at all. `.claude/settings.local.json` stays local and is git-ignored.

## What is enforced, per event

| Event | Claude Code hook | Daedalus | Status |
|---|---|---|---|
| prompt submitted | `UserPromptSubmit` | `hook prompt` records the request | Enforced |
| before a file edit | `PreToolUse` (edit and write tools) | `hook pre-edit` opens the run | Enforced; never blocks the edit |
| turn end | `Stop` | `hook stop` runs the checks and the required reviews | Enforced: a block sends precise reasons back to the agent |
| session start | `SessionStart` | `hook session-start` | Enforced: shows the open run, or how to install Daedalus |

The Stop hook can run for minutes (tests, then reviews), so it is installed with a long timeout.
Edits made through the shell open the run at stop time, by comparing the tree with the prompt snapshot.

## The status line

`daedalus statusline` prints one line for the status bar, such as
`Daedalus · run 3fa9c1 · tests pass · 1 blocking finding(s)`. It reads recorded state only, never hashes
the tree and never raises, because the bar refreshes constantly. It prints `Daedalus off` when the gate
is off.

## Reviews and the adapter

`adapter: claude-code` in `.daedalus.yml` lets Daedalus launch reviewers and builders with
`claude -p`. Reviewers are read-only (no write or shell tools, no settings files, file access confined
to the repository). Sessions are lean: no MCP servers or skills. `daedalus init` sets the adapter when it
finds the `claude` binary. Details and the cost model: [../adapter-contract.md](../adapter-contract.md).

## Failure behaviour

Hooks fail open on internal errors, so you are never locked out of Claude Code, but never into
acceptance: an error only lets the agent stop with the run still open and unaccepted. Hooks do nothing
inside sessions Daedalus launched itself (`DAEDALUS_AGENT`), since the lifecycle already governs those.

## Cloud and fresh containers

Set `harness: {bootstrap: "pip install ..."}` in `.daedalus.yml` and rerun
`daedalus install claude-code`. SessionStart then runs that command whenever Daedalus is missing. It is
committed configuration, so changing it raises the run's tier to critical.

Install what the **checks** need too, not only Daedalus: with `pip install -e .` alone, a fresh
container has Daedalus but no pytest, and every check fails. For a Python project with test
extras, use `pip install -q -e ".[dev]"` (or your project's equivalent). A check that fails because a
Python module is missing now says so ("`pytest` is not installed where the check ran, so this is
not a test result"), rather than reading as a failed test.

See also [../gate-workflow.md](../gate-workflow.md) and [../troubleshooting.md](../troubleshooting.md).
