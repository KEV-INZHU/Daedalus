---
name: daedalus-gate
description: Work under the Daedalus acceptance gate. Use whenever a Daedalus run is open in this repository (`daedalus status` shows one) or the user asks to work "under the gate". The gate, not you, decides when work is accepted.
---

# Working under the Daedalus gate

A Daedalus run is a contract plus a deterministic acceptance decision. You do the
engineering work; the control plane decides whether it is accepted. You cannot
declare the task done, waive a check, or approve anything on a human's behalf.

## Loop

1. Read the contract: `daedalus status` (objective, scope, acceptance criteria, required checks).
2. Make changes **inside the contract scope only**. Changes outside it are recorded as violations.
3. Run `daedalus verify`. Evidence binds to the exact working tree, so any later edit makes it STALE.
4. Run `daedalus status` and work through every blocker marked `[agent]`.
5. When every agent-fixable blocker is gone, stop. The stop hook (or `daedalus finish`) records the
   disposition. Blockers marked `[human]` (approvals, attestations, risk exceptions) are for the user:
   tell them exactly which command to run, then stop.

## Rules

- Never edit `.daedalus.yml`, policy files, `.daedalus/`, CI configuration, or test infrastructure
  to make a check pass. Those changes escalate risk and are reviewed as such.
- Never run `daedalus approve`, `deny`, `attest`, `amend`, `risk-exception`, `off`, or `abandon`. They need
  a human at an interactive terminal; attempting them from a tool call fails.
- A passing check is evidence about this candidate, not proof of correctness. Still reason about the change.
- If the contract itself is wrong (impossible criterion, missing scope), say so plainly and ask the user to
  amend it rather than working around it.
