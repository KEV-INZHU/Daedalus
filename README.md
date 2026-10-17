# Daedalus

**An acceptance gate for AI coding agents, and a small software factory built on it.**
Run your agent normally, or run it under the gate: the agent still does the work, but "done" is decided by a
deterministic predicate over trusted, revision-bound evidence. The agent can't
make that decision for itself.

> Agents propose; the control plane enforces.

Daedalus is harness-agnostic. The core owns contracts, lifecycle state, risk
policy, evidence, authorization and the acceptance decision. Thin adapters
connect it to Claude Code, Codex, other CLIs, or a plain terminal.

## Quickstart: set it up once, then just work

```bash
pip install "git+https://github.com/KEV-INZHU/Daedalus"
cd your-repo
daedalus init          # .daedalus.yml (detected test command), Claude Code hooks, git hook
git add .daedalus.yml .claude .gitignore && git commit -m "Gate this repository with Daedalus"
```

That's all the setup there is. Everyone who opens the repository in Claude Code now works under the
gate, with nothing to remember:

1. **You ask for something.** Questions open nothing. The agent's first file edit opens a run, and
   the run's objective is your request.
2. **The agent works as usual.** The status line shows the run and what it's waiting on.
3. **The agent tries to stop.** Daedalus runs the trusted checks on the exact working tree. Once
   they pass, it launches every independent review the risk tier or mode requires (read-only, and
   never by the agent that wrote the code). Anything the agent can fix goes back to it with exact
   reasons. Anything only you can decide (an approval, a sign-off, a budget) is put to you. When the
   predicate holds, the run is recorded **ACCEPTED**.
4. **You commit.** A commit that is exactly the accepted work gets `Daedalus-Run` trailers, so the
   log and the PR show what passed the gate.

Without Claude Code, the same loop is three commands: `daedalus start "goal"`, then work, then
`daedalus verify` (runs the checks and launches the required reviews). `daedalus status` always says
what blocks acceptance and who can fix it. To have Daedalus drive the agent itself:

```bash
daedalus run "fix the flaky retry in the uploader" --mode review
```

In CI, `daedalus ci` re-runs the committed checks on every pull request (see below).

For fresh containers (cloud sessions), set `harness: {bootstrap: "pip install ..."}` in `.daedalus.yml`
and rerun `daedalus install claude-code`. The SessionStart hook runs that command whenever Daedalus
is missing, unprompted, on every machine that opens the repository. It's committed configuration
like the rest of `.daedalus.yml`, so changing it raises the run's tier to critical.

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
daedalus attest C2 pass --evidence "checked the UI manually"   # only for --attest criteria
daedalus amend --set scope='["src/api/**"]' --rationale "narrowed"
daedalus risk-exception low --rationale "docs-only change under auth/"
```

Human-only commands refuse to run inside an agent session or without a terminal.

If a review finding looks wrong, dispute it rather than arguing in circles:
`daedalus dispute F2 --reason "..."`, then `daedalus arbitrate F2` has Plato rule on it.
By default the ruling is advice for a human. Set `arbitration_resolves_findings: true` under
`policy:` to let an overrule resolve the finding.

## The factory: a queue of orders through the gate

One run turns one goal into one accepted change. The factory puts a line around that station: a
backlog of committed **work orders** goes in, and each one is built on its own branch under the gate.
Accepted work ships as one commit on your branch, and anything that doesn't pass is parked with its
work kept.

```bash
daedalus factory plan "rate limiting for the public API"   # a planner drafts orders (optional)
daedalus factory add "Return 429 after 100 requests a minute" -c "the 101st request gets HTTP 429"
git add factory/orders && git commit -m "Factory orders"   # the line builds committed orders only
daedalus factory run                                       # build, verify, review, ship; repeat
daedalus factory status                                    # board, yield, cost per shipped order
```

Orders can depend on each other and carry priorities. The line halts when an order needs a human
decision, and stops itself (the andon cord) when several orders in a row fail. See
[docs/factory.md](docs/factory.md).

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

Budgets keep two currencies apart. `max_cost` caps everything a run is charged, including
API-equivalent estimates from subscription sessions; `max_cash` caps money actually billed and
defaults to 0. Like `max_cost`, an exhausted cash budget turns a run that still has unmet
conditions into REJECTED; a run whose conditions all hold is not undone by it. A billed session
that ends without reporting its cost (a timeout, a cancellation) counts as exhausting `max_cash`,
since the spend cannot be shown to fit. The budget records spend after the fact; routing
is what declines a paid route before it is called.

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
- [docs/factory.md](docs/factory.md): the factory line, work orders and what it guarantees
- [docs/architecture.md](docs/architecture.md): layers and responsibilities
- [docs/state-machine.md](docs/state-machine.md): states, transitions, disposition precedence
- [docs/adapter-contract.md](docs/adapter-contract.md): writing a harness adapter
- [docs/security-model.md](docs/security-model.md): trust boundaries and known limits

## Development

```bash
pip install -e ".[dev]"
python -m pytest
```
