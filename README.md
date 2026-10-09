# Daedalus

**An acceptance gate for AI coding agents.** Run your agent normally, or run it
under the gate: the agent still does the work, but "done" is decided by a
deterministic predicate over trusted, revision-bound evidence. The agent can't
make that decision for itself.

> Agents propose; the control plane enforces.

Daedalus is harness-agnostic. The core owns contracts, lifecycle state, risk
policy, evidence, authorization and the acceptance decision. Thin adapters
connect it to Claude Code, Codex, other CLIs, or a plain terminal.

## Quickstart

```bash
pip install -e .                       # from this repository
cd your-repo
daedalus init --claude-code            # writes .daedalus.yml (detected test command) + Claude Code hooks
git add .daedalus.yml && git commit -m "Add Daedalus gate"   # runs only trust committed policy

daedalus start "add rate limiting to the API"   # open a run (a contract)
# ... work with your agent as usual ...
daedalus status                                  # what blocks acceptance, and who can fix it
```

With the Claude Code hooks installed, an agent that tries to stop while
agent-fixable blockers remain is sent back with the exact reasons. When only
human decisions remain, it is allowed to stop and you're told which command to
run. When the predicate holds, the run is recorded **ACCEPTED**.

To have Daedalus drive the agent itself (Builder → verify → review → finish):

```bash
daedalus run "fix the flaky retry in the uploader" --mode review
```

## The three things you touch

| Surface  | Command                                   | What runs underneath                                    |
|----------|-------------------------------------------|---------------------------------------------------------|
| Toggle   | `daedalus on` / `daedalus off`, `init`    | Hooks consult the gate; the core pins policy per run    |
| Contract | `daedalus start "goal" -c "criterion"`    | Versioned contract, scope, required checks, budget      |
| Status   | `daedalus status`                         | Full state machine, evidence, risk floors, authorization |

Everything else stays out of the way until it matters: `daedalus log` and
`daedalus audit` for the hash-chained audit trail, `daedalus metrics` for
acceptance rate, rework, cost and latency across runs, plus a few human-only decisions:

```bash
daedalus approve merge          # scoped to this candidate + contract version, expiring, single-use
daedalus attest C2 pass --evidence "checked the UI manually"
daedalus amend --set scope='["src/api/**"]' --rationale "narrowed"
daedalus risk-exception low --rationale "docs-only change under auth/"
```

Human-only commands refuse to run inside an agent session or without a terminal.

If a review finding looks wrong, dispute it rather than arguing in circles:
`daedalus dispute F2 --reason "..."`, then `daedalus arbitrate F2` has Plato rule on it.
By default the ruling is advice for a human. Set `arbitration_resolves_findings: true` under
`policy:` to let an overrule resolve the finding.

## Modes

| Mode            | Adds                                    | When                                   |
|-----------------|-----------------------------------------|----------------------------------------|
| `gate` (default)| Builder + trusted checks + predicate    | Most tasks                             |
| `review`        | + one focused reviewer (Aristotle)      | When you want a second look            |
| `council`       | + Socrates, Mozi, Aristotle, James      | High-stakes work                       |

Parallel workers are off by default. To let Brunel split a task into
independent packages that build concurrently in isolated git worktrees, raise
the cap in policy, for example `policy: {default_budget: {max_workers: 2, ...}}`,
and set `budget: {max_workers: 2}` in the run's contract file (`daedalus run --contract ...`).

Risk floors apply in every mode. Touching `**/auth/**`, migrations, CI or
lockfiles raises the tier, and the tier sets the reviews and approvals that are
mandatory. A mode can add requirements but can never remove them.

## Configuration

`.daedalus.yml` is deliberately small:

```yaml
enabled: true
mode: gate
adapter:                    # used by `daedalus run` / `daedalus review`
  name: claude-code          # lean sessions: no MCP servers or skills; reviewers are read-only
  review_model: sonnet       # optional per-role models (`model:` for the builder)
checks:
  tests: python -m pytest -q
  typecheck: {command: [mypy, src]}
policy:                     # optional: override any key of the default policy
  path_floors:
    - {name: billing, patterns: ["src/billing/**"], tier: high}
```

`daedalus policy --default` prints the full default policy.

## What it guarantees and what it doesn't

- Every mandatory check must PASS on the **current** candidate. A candidate is
  the content hash of the whole working tree relative to its base, including
  untracked files. Any edit makes earlier evidence STALE.
- Untrusted verifiers, timeouts, crashes and missing checks are never PASS.
- Disposition (ACCEPTED / BLOCKED / REJECTED / CANCELLED) is derived from the
  event log, and nobody sets it directly.
- The gate constrains a cooperative but fallible agent. It is **not** a sandbox
  against an adversarial agent with shell access to the same machine. See
  [docs/security-model.md](docs/security-model.md).

## Documentation

- [docs/PLAN.md](docs/PLAN.md): build plan and progress
- [docs/architecture.md](docs/architecture.md): layers and responsibilities
- [docs/state-machine.md](docs/state-machine.md): states, transitions, disposition precedence
- [docs/adapter-contract.md](docs/adapter-contract.md): writing a harness adapter
- [docs/security-model.md](docs/security-model.md): trust boundaries and known limits

## Development

```bash
pip install -e ".[dev]"
python -m pytest
```
