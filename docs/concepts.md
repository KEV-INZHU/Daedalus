# Concepts

The words Daedalus uses, in plain language. Where a reviewer has a name, the plain description comes
first and the name in brackets is what you will also see in logs and the audit trail.

- **Run.** One unit of gated work, from the agent's first edit to a final disposition. A run pins its
  contract and policy when it starts, and everything that happens in it is written to a hash-chained log.
- **Contract.** What the run is supposed to achieve: the objective, acceptance criteria, scope and budget.
  It can change only through `daedalus amend`, which leaves a record.
- **Candidate.** The exact content of your working tree right now, identified by a hash. Daedalus judges
  candidates, not intentions: edit one file and you have a new candidate.
- **Tier.** How risky a change is: low, medium, high or critical. The tier decides which reviews and
  approvals are required.
- **Floor.** A rule that sets the lowest tier a change can have. Touching `auth/`, a lockfile, CI config
  or the gate's own policy raises the floor, whatever the agent says about the change.
- **Evidence.** A recorded result of a trusted check (for example your tests) on one specific candidate.
  Evidence for an earlier candidate is **stale** and does not count.
- **Review / perspective.** An independent, read-only look at the candidate by a separate agent that
  Daedalus launches; the agent that wrote the code never reviews it. Each perspective asks one question:
  quality review (Aristotle), validation review (Socrates), functionality review (James), simplicity
  review (Mozi). An arbiter (Plato) rules only when reviewers disagree on a finding.
- **Finding.** One problem a review reports, with a severity (BLOCKER, MAJOR, MINOR, ADVISORY). By default
  only blockers stop acceptance; the rest are offered for one follow-up round.
- **Disposition.** The outcome of a run: ACCEPTED, BLOCKED (not yet; the reasons say what to fix),
  REJECTED (a violation, such as a changed policy) or CANCELLED. It is derived from the log; nobody sets it.
- **Follow-up round.** One extra pass, after the gate is otherwise satisfied, where the builder fixes
  the non-blocking findings. A round that leaves things worse is undone.
- **Factory.** A queue of work orders run through the gate one after another, with the board
  (`daedalus factory status`) showing what shipped and what is parked.
- **Order.** One work item in the factory: a committed file under `factory/orders/` holding a small contract.
- **Line.** The factory as a process: pick the next ready order, build it on its own branch, gate it, ship
  or park it, move on.
- **Adapter.** The piece that lets Daedalus launch an agent itself (the builder, or a reviewer), for
  example the `claude-code` adapter. Without one, Daedalus can gate your work but cannot run reviews.
- **Harness.** The agent tool you work in (Claude Code, Codex, OpenCode, Pi, Hermes). Its integration
  installs hooks so the first edit opens a run and stopping triggers the gate. A harness is where you
  work; an adapter is how Daedalus launches agents.

Related: [gate-workflow.md](gate-workflow.md) for the daily loop, [configuration.md](configuration.md)
for tiers and floors, [reviews.md](reviews.md) for how reviews are kept cheap.
