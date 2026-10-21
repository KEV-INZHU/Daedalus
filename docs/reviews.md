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
