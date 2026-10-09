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

## Rules for adapter authors

1. Never report `COMPLETED` for a session that didn't finish. When unsure, report `FAILED`.
2. A read-only request must not modify the working tree. The lifecycle captures
   the candidate before and after each review and discards any review that changed it.
3. Don't return results for sessions you didn't launch. If `status()` can't know, return `None`.
4. Declare limitations honestly. A missing capability is fine, but a
   capability claimed and not delivered breaks the gate.
