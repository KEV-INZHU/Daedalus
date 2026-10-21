# Daedalus

**Your agent says it's done. Daedalus checks.**

An acceptance gate for AI coding agents. The agent does the work; "done" is decided by a deterministic
predicate over trusted, revision-bound evidence, so the agent can't grade its own homework.

```text
$ daedalus demo --fast        # trimmed; the rest of the output is in the demo itself
== Scene 1: the agent that cheats ==
  agent> Fixing the failing test...
  agent> Done! All tests pass.
  gate> checks: tests PASS
  gate> NOT ACCEPTED (BLOCKED)
  gate>   refused [test_integrity]: tests weakened: tests/test_calc.py: 'test_add' lost 1 assertion and now asserts `True`; restore the assertion or fix the code. [...]
== Scene 2: the agent that does the work ==
  gate> checks: tests PASS
  gate> ACCEPTED
```

> Agents propose; the control plane enforces.

## Install

```bash
pipx install git+https://github.com/KEV-INZHU/Daedalus
```

After the first PyPI release, `pipx install daedalus` works too.

## See it work

```bash
daedalus demo      # a throwaway repo, a cheating agent, and the gate catching it
```

## Gate your repository

```bash
cd your-repo
daedalus init      # .daedalus.yml (detected test command), hooks for your agent
daedalus doctor    # checks the setup; each problem comes with a one-line fix
```

Commit what `init` wrote. From then on the agent's first edit opens a run, and when it tries to stop,
Daedalus re-runs your trusted checks on the exact working tree, launches the independent read-only
reviews the risk tier requires, and either sends precise reasons back to the agent or records the run
**ACCEPTED**. Commits that are exactly the accepted work carry `Daedalus-Run` trailers.
See [docs/gate-workflow.md](docs/gate-workflow.md).

## Supported harnesses

Daedalus is harness-agnostic: the core owns contracts, lifecycle state, risk policy, evidence and the
acceptance decision, and thin integrations connect it to your agent. `daedalus init` installs the gate
into every harness it detects (Claude Code always); `daedalus install <name>` or
`daedalus init --harness <name>` picks one, and `daedalus plugins` shows what is registered and detected.

| Harness     | Install                        | Notes                                                |
|-------------|--------------------------------|------------------------------------------------------|
| Claude Code | `daedalus install claude-code` | Reference integration: hooks, status line, gate skill |
| Codex       | `daedalus install codex`       | [docs/harnesses/codex.md](docs/harnesses/codex.md)     |
| OpenCode    | `daedalus install opencode`    | [docs/harnesses/opencode.md](docs/harnesses/opencode.md) |
| Pi          | `daedalus install pi`          | [docs/harnesses/pi.md](docs/harnesses/pi.md)           |
| Hermes      | `daedalus install hermes`      | [docs/harnesses/hermes.md](docs/harnesses/hermes.md)   |
| Any CLI     | `daedalus start` / `daedalus verify` | Or a command adapter                            |

Anything else is one Python package with an entry point: see [docs/extending.md](docs/extending.md).

## In CI

```yaml
- uses: actions/checkout@v4
  with: {fetch-depth: 0}
- uses: KEV-INZHU/Daedalus@main   # pin a release tag (for example @v0.2.0) once one is published
  with: {install-command: 'pip install -e ".[dev]"'}
```

Every pull request gets its mandatory checks re-run from committed policy, a risk tier for the whole
diff, and a provenance report. See [docs/ci.md](docs/ci.md).

## What it guarantees and what it doesn't

- Every mandatory check must PASS on the **current** candidate, the content hash of the whole working
  tree. Any edit makes earlier evidence STALE.
- Untrusted verifiers, timeouts, crashes and missing checks are never PASS.
- Disposition (ACCEPTED / BLOCKED / REJECTED / CANCELLED) is derived from a hash-chained event log;
  nobody sets it directly.
- The gate constrains a cooperative but fallible agent. It is **not** a sandbox against an adversarial
  agent with shell access to the same machine. See [docs/security-model.md](docs/security-model.md).

## Documentation

Everything else lives in [docs/](docs/index.md):

- [Working under the gate](docs/gate-workflow.md): the daily loop and the commands you touch
- [Modes, risk and configuration](docs/configuration.md): `.daedalus.yml`, budgets, policy
- [Reviews](docs/reviews.md): one thorough pass, then only what changed
- [Evidence cache](docs/evidence-cache.md): verify and review an identical tree once
- [The factory](docs/factory.md): a queue of work orders through the gate
- [Architecture](docs/architecture.md), [state machine](docs/state-machine.md), [security model](docs/security-model.md)

## Development

```bash
pip install -e ".[dev]"
python -m pytest -q
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Released under the MIT license.
