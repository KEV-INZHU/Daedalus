# Roadmap

Sorted by expected impact on the quality of accepted work, then on how easy Daedalus is to use. Ticked
items have shipped, with tests. Items marked *blocked* need something outside this repository; each
says what.

Sources: the open items of [history/PLAN.md](history/PLAN.md), and gaps found while building
coordination over the bus (October 2026).

## Quality of what gets accepted

1. [x] **Proposed contracts.** At a run's first edit the objective is the raw prompt (truncated) and the
   criteria are the generic "required checks pass", so the gate cannot hold the work to what was asked.
   The agent proposes a short objective and testable criteria, and a person confirms them with one
   command. *Shipped:* `daedalus propose` / `daedalus confirm` ([gate-workflow.md](gate-workflow.md)).
2. [x] **Findings as a worklist.** Review findings reach the agent as one block of hook text. List them
   with `daedalus findings`, and let the agent answer each one: fixed (with a note the re-review checks)
   or disputed (with evidence, which goes to arbitration). *Shipped:* `daedalus findings`.
3. [x] **Holdout checks.** Criteria verified by checks the Builder never sees, so passing the visible
   tests is not enough. *Shipped:* `holdout: true` on a check ([configuration.md](configuration.md)).
4. [x] **Live coordination delivery.** New bus messages reach a running session: a harness hook reads
   the agent's unread messages and adds them to its context after each tool call. *Shipped* for launched
   `claude-code` builders ([coordination.md](coordination.md)).
5. [x] **Escaped-defect tracking.** A later commit with a `Fixes-Run:` (or `Fixes-Order:`) trailer links
   a regression back to the run that accepted it, so `daedalus metrics` can count escaped defects.
   *Shipped:* `Fixes-Run:` trailers ([gate-workflow.md](gate-workflow.md)).
6. [x] **Test-strength check.** The gate is only as good as the tests. An optional check mutates changed
   lines and reports the mutants the tests did not catch. *Shipped:* `daedalus mutate`, Python only
   ([configuration.md](configuration.md)).
7. [x] **Plan review before coding.** An optional, cheap review of the Builder's plan before it writes
   code, for the design choices that are expensive to change after review. *Shipped:* `plan_review: true`
   and `daedalus plan-review` ([reviews.md](reviews.md)).

## Ease of use

8. [x] **`daedalus next`.** One line saying what the run needs now, and from whom. *Shipped.*
9. [x] **Plain names.** Show roles by what they do (quality review, builder, arbiter) everywhere a
   person reads them. The classical names stay as internal ids. *Shipped* (`daedalus.core.names`).
10. [ ] **Fresh containers get the test tools.** This repository's `harness.bootstrap` installs only the
    package, so its own checks cannot run until pytest is installed by hand. *Partly shipped:* a check that
    fails for a missing module now says so, and the docs show the right bootstrap. **Needs you:** this
    repository's own `.daedalus.yml` should read `bootstrap: python -m pip install -q -e ".[dev]"`. An agent
    must not edit that file (it is gate configuration, and changing it makes the run critical).

## Measurement

11. [x] **Cost per finding** in `daedalus metrics`: what review spend bought, by perspective. *Shipped.*
12. [x] **Parallelism savings vs integration overhead** in `daedalus metrics`. *Shipped.*

## Factory and integrations

13. [x] **Optional ship step** for the factory line: push the integration branch and open a pull request.
    *Shipped:* `factory: {ship: ...}` ([factory.md](factory.md)).
14. [ ] **A shared bus lock for the shared `bus.py`**, so both can write one bus safely. *Partly
    shipped:* the lock is now one standalone, copyable file (`coordination/buslock.py`). **Needs you:**
    `bus.py` lives outside this repository; copy the file next to it and wrap its writes
    ([coordination.md](coordination.md) has the snippet).
15. [ ] **Exercise the experimental `codex` / `aider` presets** against the real CLIs. *Blocked:* needs
    machines with those CLIs installed and their own API keys; neither is in the cloud container this
    roadmap was worked in.
16. [ ] **A live factory line run** with the `claude-code` adapter on a multi-order backlog. *Blocked:*
    every order launches paid sessions; it needs the owner's go-ahead and a budget (`factory: {budget: ...}`).

## Deliberately out of scope until usage justifies it

Server, multi-user control plane, plugin marketplace, distributed scheduler, public remote API.
