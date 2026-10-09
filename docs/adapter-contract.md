# Adapter contract

An adapter translates Daedalus requests into a harness's operations and
**declares** what it can guarantee. The core never assumes a capability. When
a run needs one the adapter lacks, it fails closed, unless the policy lists
that shortfall under `allowed_degradations`.

## Interface (`src/daedalus/adapters/base.py`)

```python
class Adapter(ABC):
    name: str
    capabilities: Capabilities

    def run_agent(self, request: AgentRequest) -> AgentResult: ...
    def cancel(self, session_id: str) -> bool: ...          # optional
    def status(self, session_id: str) -> AgentResult | None: ...  # optional, used by recovery
```

`AgentRequest` carries the run and task ids, the role (`brunel`, `socrates`,
...), a rendered prompt (skill + contract + context), the working directory, a
timeout, a `read_only` flag and an optional `should_stop` check. The lifecycle
sets `should_stop` to "this run has been cancelled or has a final disposition";
an adapter that owns the session polls it and stops the session when it turns true. `AgentResult` returns a status
(`COMPLETED | FAILED | TIMED_OUT | CANCELLED`), the raw output, an optional
structured result, cost, and a session id.

## Capabilities

| Field               | Values                          | Needed for                                     |
|---------------------|---------------------------------|------------------------------------------------|
| `launch_agent`      | bool                            | `daedalus run`, launched reviews               |
| `cancellation`      | `none < cooperative < hard`     | cancelling in-flight work                      |
| `isolation`         | `none < worktree < sandbox`     | fan-out (Phase 2)                              |
| `identity`          | `launched` / `self_reported`    | reviews that count as independent              |
| `structured_results`| bool                            | reviews (findings must parse)                  |
| `cost_reporting`    | bool                            | cost budgets                                   |
| `status_query`      | bool                            | reattaching to sessions after a crash          |
| `limitations`       | free text                       | shown to users; never relied on                |

## Shipped adapters

| Adapter      | Launch | Cancel      | Identity        | Notes                                    |
|--------------|--------|-------------|-----------------|------------------------------------------|
| `inline`     | no     | none        | self_reported   | The harness agent *is* the executor (hooks + skill) |
| `claude-code`| yes    | hard        | launched        | `claude -p --output-format json`; reports cost |
| `codex`      | yes    | hard        | launched        | Experimental preset, untested            |
| `aider`      | yes    | hard        | launched        | Experimental preset, untested            |
| `simulated`  | yes    | cooperative | launched        | Scripted sessions for tests and drills   |

Custom CLI harnesses can be configured without code:

```yaml
adapter:
  name: my-agent
  argv: [my-agent, --non-interactive]
  prompt_via: stdin        # stdin | arg | message
  read_only_args: [--no-write]
```

## Launch environment

Every command adapter preset (`claude-code`, `codex`, `aider`, custom `argv`) starts each agent as a
**fresh session**. Harness session
markers (`CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_CODE_SSE_PORT`) are
removed and `DAEDALUS_AGENT=1` is set, so a launched agent can't pass for its
parent session, and Daedalus's human-only commands refuse inside it. The
`claude-code` preset accepts both shapes of `claude -p --output-format json`:
a single result object (older releases) or the array of session messages
(2.1.x), whose last `type: result` entry is the outcome. A result whose `subtype` is not
`success` (e.g. `error_max_turns`), `is_error: true`, or a non-zero exit is FAILED; reported cost is kept.

## Launch profile and plan usage

Each launch is a full harness session, so its fixed startup cost is paid per
builder round and per review. The `claude-code` preset launches **lean** sessions:
`--strict-mcp-config` (no MCP servers) and `--disable-slash-commands` (no skills).
Measured on Claude Code 2.1: about 37k input tokens before any work with the
user's full setup, about 5k lean. Run 10 measured the first request of each live
launch (prompt included, Claude Code 2.1.296): about 18.4k for a Builder, which
keeps the full built-in tool set, and about 7k for a restricted reviewer with a
small diff.

Review launches end with `--restricted --tools Read,Grep,Glob`. The tool
**allowlist** keeps a new editing or shell tool in a later harness release
excluded by default. `--restricted` makes the profile independent of the
machine's Claude Code configuration: user, project and local settings files
are ignored (no settings-granted permissions or hooks), and file tools are
confined to the working directory. Run 10 found both gaps live, without it. A
reviewer read `C:/Windows/win.ini` under the user's `defaultMode: auto`, and
loaded a `.claude/CLAUDE.md` and the project's auto-memory. `.claude/**` is
excluded from the candidate and auto-memory lives outside the repository, so
both are channels a Builder session can write that never appear in the diff
under review. With `--restricted` (verified on Claude Code 2.1.296) neither
loads. An older CLI that lacks the flag fails the review launch, which fails
closed. The launch line is **locked**. For `claude-code`,
`.daedalus.yml` may set only `model`, `review_model` and `args`:

- `args` is itself an allowlist (`--verbose`, `--max-turns N`, `--fallback-model M`), and
  values are validated. These args are appended to **builder** launches only and never
  reach a review.
- Model names are validated and passed as one `--model=<name>` argument.
- Anything else (`argv`, `read_only_args`, `prompt_via`, MCP, plugin, settings or permission
  flags) fails closed with a configuration error.

Every built-in preset splits its launch line in three parts: shared flags for
every launch, **builder-only** flags (write-enabling ones such as claude's
`--permission-mode acceptEdits` and codex's `--full-auto`), and read-only flags
for reviews. A review launch is exactly the preset's command, its shared flags,
the validated `--model=` (claude-code only) and its read-only flags, then the
prompt. For the other built-in presets (`codex`, `aider`), config may set only
`name` and `args`. Those args can be any list of strings, since these presets
are experimental and their flags aren't allowlisted, and they're appended to
builder launches only. `daedalus run/review --adapter X` builds a *different*
adapter X from its own defaults. Naming the configured adapter keeps the
repository's settings. For a **custom**
`argv` adapter, read-only enforcement is whatever its `read_only_args` provide.
Daedalus can't vouch for an unknown CLI's flags. The lifecycle still captures
the candidate before and after every review and discards any review that
changed the working tree.

Models are chosen per role:

```yaml
adapter:
  name: claude-code
  model: opus          # builder launches (default: the CLI's default model)
  review_model: sonnet # review launches (default: `model`)
```

With a claude.ai subscription login, launched sessions draw on the plan's usage
limits. The reported `total_cost_usd` is an API-equivalent estimate, not a charge.
A session stopped by a timeout or cancellation reports no cost, so its usage is
not charged to the run's budget.

## Session lifetime

A launched session never outlives the orchestrator that supervises it:

- **Cancellation.** While a session runs, the command adapter polls `should_stop`
  (every second, through its own read-only connection to the audit store). A
  `daedalus cancel` from another terminal stops the session and records it CANCELLED.
  Before run 10, the Builder kept editing the tree for about 20 seconds after a cancel.
- **Timeouts and interrupts.** A timeout, Ctrl-C or any orchestrator error kills the session
  before the error propagates.
- **Process trees (Windows).** Each session runs in a kill-on-close job object. Killing the
  session also ends the shells and test runs it started, and if the orchestrator process
  dies, the OS ends the whole tree. Before run 10, a killed orchestrator left the Builder
  editing the tree for about 22 seconds, and a timed-out session whose child held the
  output pipe hung the orchestrator. Processes that start in the instant before the
  session joins the job escape it.
- **Elsewhere** only the session process itself is killed. Its children, and a session
  whose orchestrator was killed outright, are not contained (untested off Windows).

## Rules for adapter authors

1. Never report `COMPLETED` for a session that didn't finish. When unsure, report `FAILED`.
2. A read-only request must not modify the working tree. The lifecycle captures
   the candidate before and after each review and discards any review that changed it.
3. Don't return results for sessions you didn't launch. If `status()` can't know, return `None`.
4. Declare limitations honestly. A missing capability is fine, but a
   capability claimed and not delivered breaks the gate.
