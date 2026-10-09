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
timeout and a `read_only` flag. `AgentResult` returns a status
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
user's full setup, about 5k lean.

Review launches get an **allowlist** of tools, `--tools Read,Grep,Glob`, as
the final arguments. A new editing or shell tool in a later harness release
stays excluded by default. The launch line is **locked**. For `claude-code`,
`.daedalus.yml` may set only `model`, `review_model` and `args`:

- `args` is itself an allowlist (`--verbose`, `--max-turns N`, `--fallback-model M`), and
  values are validated. These args are appended to **builder** launches only and never
  reach a review.
- Model names are validated and passed as one `--model=<name>` argument.
- Anything else (`argv`, `read_only_args`, `prompt_via`, MCP, plugin, settings or permission
  flags) fails closed with a configuration error.

For the other presets (`codex`, `aider`), the read-only profile can't be
overridden either, and `model`/`review_model` are refused. For a **custom**
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

## Rules for adapter authors

1. Never report `COMPLETED` for a session that didn't finish. When unsure, report `FAILED`.
2. A read-only request must not modify the working tree. The lifecycle captures
   the candidate before and after each review and discards any review that changed it.
3. Don't return results for sessions you didn't launch. If `status()` can't know, return `None`.
4. Declare limitations honestly. A missing capability is fine, but a
   capability claimed and not delivered breaks the gate.
