# The factory

A software factory is a production line for code. Specs go in, verified changes come out, the line runs
without someone steering each step, and you measure it like a line: throughput, yield, cost per unit
shipped. Daedalus already has the hard part of that, a quality gate that an agent can't talk its way
past. The factory adds the line around it, and keeps it small:

```
 factory/orders/*.yml          one committed YAML file per order: a contract plus routing
        │  daedalus factory run
        ▼
 pick the next ready order ──► branch factory/<id> ──► Builder → checks → reviews   (the gate, unchanged)
        ▲                                                       │
        │                       ACCEPTED ──► one commit on the integration branch (fast-forward)
        │                       agents out of rounds ──► parked: work kept on its branch, line moves on
        └──── next order ◄───── needs a human ──► the line halts on it; decide, then run again
```

There's no server, queue service or scheduler. The orders are files, the line is one command, and the
factory's own state is an append-only ledger in `.daedalus/factory/`. Each order still runs as an
ordinary Daedalus run, with its own hash-chained audit trail.

## Quickstart

```bash
daedalus factory plan "rate limiting for the public API"    # optional: a planner drafts orders
daedalus factory add "Return 429 after 100 requests a minute" \
    -c "the 101st request in a minute gets HTTP 429" --scope "src/api/**" --scope "tests/api/**"
git add factory/orders && git commit -m "Factory orders"    # the line builds committed orders only

daedalus factory run          # build ready orders one after another; ship the accepted ones
daedalus factory status       # the board: what shipped, what's parked and why, yield, cost
```

`factory run` needs an adapter that can launch agents (`adapter: claude-code` in `.daedalus.yml`).

## Work orders

An order is a file in `factory/orders/`, and its name is its id (which also names its branch). An
order is a Daedalus contract with a few routing keys added:

```yaml
# factory/orders/rate-limit.yml
title: Rate-limit the public API   # optional: the commit subject and board label
objective: Add rate limiting to the public API
acceptance_criteria:
  - Requests over 100 a minute from one key get HTTP 429
  - The limit is configurable per key
scope: ["src/api/**", "tests/api/**"]
depends_on: [api-keys]   # ship these first
priority: 10             # higher runs first among ready orders (default 0)
mode: review             # gate | review | council (default: the factory's mode, then the repo's)
rounds: 3                # build/verify/review rounds before the order is parked
```

Every other contract field (`invariants`, `hard_constraints`, `design_decisions`, `risk_tier`,
`required_checks`, `budget`, `verification_plan`) passes through unchanged and is validated by the
same code a run uses. An invalid order, an order that depends on an unknown id, and every order on a
dependency cycle are reported and skipped. They never stop the line.

**Titles.** `title` is one line of at most 72 characters; a longer one, or one with a line break or
another control character, makes the order invalid. Surrounding whitespace is stripped and a tab
becomes a space. It becomes the subject of the order's shipped commit and its label in `factory
status`. Without a title, the objective's first sentence (cut to 72 characters at a word) is used.
`daedalus factory add --title "..."` sets it, and the planner proposes one for every order it drafts.

**Criteria are proved by tests.** A plain-text criterion is bound to the order's required checks, and
the contract's verification plan says each criterion must be demonstrated by a test those checks run.
That keeps the line lights-out. In `review` mode a reviewer also judges whether the tests really prove
the criteria. If a criterion genuinely needs a person, write it as a mapping with `checks: []`. The
order will then wait for a human attestation.

**Orders are trusted input, like policy.** The line refuses a dirty working tree, so everything it
builds was committed and could be reviewed. `factory plan` writes drafts and stops. A human reads them,
edits them, and commits the ones worth building.

## The line

`daedalus factory run` takes ready orders (queued, with every dependency shipped) by priority and then
by id, and for each one:

1. **Branch.** Cut `factory/<id>` from the integration branch. That's the branch you're on, or
   `--into`. If the order was parked before, its branch is reused and brought up to date, so the
   Builder starts from the earlier work.
2. **Build.** Open a run with the order's contract and drive it exactly as `daedalus run` does.
3. **Settle.**
   - **ACCEPTED:** commit exactly the accepted candidate as one commit on top of the integration
     branch, fast-forward the integration branch onto it, and delete the order branch. The commit
     message carries `Daedalus-Order`, `Daedalus-Run`, `Daedalus-Candidate` and `Daedalus-Risk` trailers,
     so every shipped commit leads back to its audit trail.
   - **The agents ran out of rounds:** cancel the run, commit the work in progress on the order branch,
     and move on. The order is `parked`.
   - **Only a human can unblock it** (an approval the risk tier requires, an attestation): leave the run
     open on its branch and halt the line (`waiting`). Decide with the usual commands, such as
     `daedalus approve merge` or `daedalus attest C1 pass --evidence ...`, then run `daedalus factory run`.
     It ships the order and carries on. To set it aside instead, run `daedalus factory park <id>`.
   - **REJECTED / CANCELLED:** keep the work on the branch and record the outcome.

The line stops when nothing is ready, at `--max-orders`, when it has spent `--budget`, on
`daedalus factory stop` (between orders; `daedalus cancel` stops the current one), when an order needs a
human, or when the **andon** cord pulls: `andon` orders in a row (default 3) did not ship. That many
failures in a row usually means the line itself is broken (a failing check, a bad environment, a
broken adapter), and the line stops before it spends more.

If a line is interrupted mid-order, the next `factory run` resumes that order from its open run. It
recovers crashed sessions without presuming they succeeded. Only one line runs at a time.

## What it guarantees

- **Only accepted work ships.** The integration branch moves only by fast-forward onto a commit whose
  tree is exactly an ACCEPTED candidate. If the working tree changed after acceptance, or the
  integration branch moved while the order was waiting, nothing ships and the order is parked.
- **Commits match what was verified.** Factory commits skip repository hooks (`--no-verify`). A
  hook that reformats files would otherwise commit something other than what was checked.
- **No work is thrown away.** Parked, rejected and cancelled orders keep their work in a commit on
  their branch, and `daedalus factory retry <id>` builds on top of it.
- **An empty change does not count.** An order that is accepted but changes nothing is parked, not
  shipped. Otherwise passing pre-existing checks would hide a Builder that did no work, and inflate the
  yield.
- **The gate is unchanged.** Risk floors, required reviews, approvals and budgets apply to every order
  exactly as to any run. The factory decides *which* contract runs next and what happens to the working
  tree afterwards, never whether a run is accepted.

## Measuring the line

`daedalus factory status` (or `--json`) shows the board and the line's numbers:

| Measure          | Meaning                                                              |
|------------------|----------------------------------------------------------------------|
| yield            | shipped / orders that reached an outcome                             |
| first-pass yield | shipped on the first attempt, in the first round / orders attempted  |
| cost per shipped | all spend on the orders (failures included) / shipped orders         |
| median cycle     | first start to ship, for shipped orders                              |
| escape rate      | shipped orders later named by a `Fixes-Order:` trailer / shipped     |

A commit on the integration branch made after an order shipped, whose message has a
`Fixes-Order: <order-id>` trailer, marks that order as escaped. Trailers naming orders that never
shipped are ignored. Escapes are derived from git history each time, not stored in the ledger.

`daedalus metrics` still reports each underlying run.

## Configuration

All optional, in `.daedalus.yml`:

```yaml
factory:
  orders: factory/orders   # where order files live
  branch_prefix: factory/  # order branches
  mode: review             # default mode for orders (else the repository's `mode`)
  rounds: 3                # default rounds per order
  andon: 3                 # consecutive failed orders that stop the line
  budget: 20.0             # spend cap for one `factory run` (also --budget)
  ship:                    # optional ship step, off by default
    push: origin           # after the line ships orders, push the integration branch here
    pull_request: true     # and open a pull request (needs the `gh` CLI)
    base: main             # the pull request's base branch
```

The ship step runs once per `factory run`, after the line has shipped at least one order. A failed
push or pull request is reported in the line's notes. It never undoes what shipped, because the
orders are already committed on the integration branch. A successful push is logged as
`line.pushed` in the factory ledger.

## Limits, on purpose

- **One order at a time.** The core allows one open run per repository. Because of that,
  integration is a fast-forward and never a merge. Running in parallel would need isolated
  checkouts per order and real merge handling, and nothing measured yet shows that's worth the cost.
- **Local unless you ask.** The factory commits to a local branch. Pushing and opening a pull request
  happen only with `factory: {ship: ...}`. Merging is still yours (or a `merge` action in policy).
- **A human gate halts the line.** An order waiting on an approval keeps the repository's only open
  run, so the line can't continue past it. Keep human-gated work small, or park it and come back.
