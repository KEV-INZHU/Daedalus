# Reviews: one thorough pass, then only what changed

Reviews cost time and money, so the gate aims for one round:

- **The first review of a candidate is full and exhaustive.** The reviewer is told it is the only full
  review the change gets, and works through a required edge-case sweep (edge inputs, failure paths, state
  and ordering, interfaces, trust and security, tests). It reports MINOR edge cases now, not in round three.
- **Re-reviews are delta reviews.** Daedalus snapshots exactly what each review saw. After a fix, the
  next review of that perspective gets only the inter-diff plus its open findings. A delta review counts
  only when it chains back to a full review in the same contract version, and anything that can't be proven
  from hash-checked snapshots falls back to a full review. (`policy: {rereview: full}` turns this off.)
- **Non-blocking findings get one fix round.** When a candidate would be accepted but reviewers raised
  MAJOR or MINOR findings (`policy: {followup_severities: [...]}`), the agent gets exactly one pass to fix
  them, delta-reviewed. What's left open never blocks acceptance.

Builders may run the run's check commands themselves (for example `python -m pytest`), so they hand in
tested work. A check that would grant a bare interpreter or shell is never granted, and
`policy: {builder_runs_checks: false}` grants nothing (see [security-model.md](security-model.md)).

`daedalus metrics` reports reviews per accepted change, delta reviews, and **late findings**: every
finding raised by a *full* re-review, meaning a full review of a perspective after its first one. Those are
what one thorough pass plus delta reviews should have made unnecessary, so that's the number this design
drives toward zero. A follow-up round that ends worse than it started is undone: the acceptable candidate
is restored and re-verified (`daedalus restore` does the same by hand). Findings a *delta* review raised
against the undone change no longer count, since that code is gone. A *full* review's findings always
count, because they may concern code that is still there.

## What reviews cost, and what they bought

`daedalus metrics` prints review spend by perspective: what its launched sessions were charged,
how many findings it raised, and the cost per finding. A perspective that keeps costing money and
finding nothing is a candidate to drop from your mode or tier. In `--json`, see `review_spend` per
run and in `aggregate`. For fan-out runs it also prints how much time running packages side by
side saved compared with running them one by one, how long integrating their results took, and
how many results failed to integrate. The saving can be negative: when packages wait on each other,
the round spans longer than the work itself, and the metric shows that rather than hiding it.

## Plan review: before any code

Design mistakes are the expensive findings: a review of finished code that says "wrong approach"
costs a rebuild. A plan review catches them first, for the price of two short read-only sessions.

- `plan_review: true` in `.daedalus.yml` makes `daedalus run` start by having the Builder write a
  plan (read-only). An independent reviewer then critiques it, and its concerns become round 1's
  "Before you build" section. Off by default.
- `daedalus plan-review "the plan"` (or `--file PLAN.md`) does the same for an interactive
  session. An agent may ask for one of its own plan.

A plan review is advice. It records no review and no finding, so it never satisfies or blocks
acceptance. Only its session's cost is charged to the run. The plan reaches the reviewer quoted as
data.
