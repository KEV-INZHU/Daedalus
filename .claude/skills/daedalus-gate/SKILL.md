---
name: daedalus-gate
description: Work under the Daedalus acceptance gate. Use whenever a Daedalus run is open in this repository (`daedalus status` shows one) or the user asks to work "under the gate". The gate, not you, decides when work is accepted.
---

# Working under the Daedalus gate

A Daedalus run is a contract plus a deterministic acceptance decision. You do the
engineering work; the control plane decides whether it is accepted. You cannot
declare the task done, waive a check, or approve anything on a human's behalf.

## Loop

1. Read the contract: `daedalus status` (objective, scope, acceptance criteria, required checks). If no
   run is open yet, your first file edit opens one for the user's request.
2. Make changes **inside the contract scope only**. Changes outside it are recorded as violations.
3. Prove each acceptance criterion with a test the required checks run. Evidence binds to the exact
   working tree, so any later edit makes it STALE.
4. Stop when you believe the work is done. The gate then runs the checks and launches the required
   independent reviews itself. If it sends you back, fix every `[agent]` item it lists (failing checks,
   review findings) and stop again. You never launch reviews of your own work.
5. Items for the user (`[human]`: approvals, attestations, budgets) are not yours to resolve: tell them
   exactly which command to run, then stop.

## Rules

- Never edit `.daedalus.yml`, policy files, `.daedalus/`, CI configuration, or test infrastructure
  to make a check pass. Those changes escalate risk and are reviewed as such.
- Never run `daedalus approve`, `deny`, `attest`, `amend`, `risk-exception`, `off`, or `abandon`. They need
  a human at an interactive terminal; attempting them from a tool call fails.
- A passing check is evidence about this candidate, not proof of correctness. Still reason about the change.
- If the contract itself is wrong (impossible criterion, missing scope), say so plainly and ask the user to
  amend it rather than working around it.
