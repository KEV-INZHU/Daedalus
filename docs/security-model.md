# Security model

Daedalus is built to stop a **cooperative but fallible** agent from declaring
work done when it isn't. It does that reliably. It isn't a sandbox against an
**adversarial** agent with shell access to the same machine and user account.
This document says where the line falls.

## What is enforced

| Property                                   | Mechanism                                                        |
|--------------------------------------------|------------------------------------------------------------------|
| Agents can't accept their own work         | Disposition is derived by `evaluate`; no event sets it from agent input |
| Agents hold no authority                   | `Policy.has_authority` returns False for `agent:*`; policies granting agents authority fail to load |
| Evidence matches the code                  | Candidate = content hash of the working tree vs base (incl. untracked); evidence binds to it |
| Weakening policy mid-run doesn't help      | The policy hash is pinned at start, and any drift makes acceptance impossible |
| Editing the contract file doesn't help     | The contract file hash is pinned; drift is a violation          |
| Policy must be reviewable                  | Runs refuse to start with uncommitted `.daedalus.yml`/`policy_file` |
| Verification infra changes are visible     | Change floors raise CI, test-config, lockfile and policy edits to high/critical |
| Untrusted verifiers fail closed            | Only `trusted_verifiers` produce evidence that can PASS        |
| Checks don't see secrets by default        | Credential-like env vars are scrubbed unless listed in `env_inputs` |
| Crashes never presume success              | Intents are written before effects; recovery turns them into ERROR / FAILED / unknown |
| Restricted actions aren't blindly retried  | An unknown outcome blocks every action until a human reconciles |
| Tampering with the log is detected         | Hash chain over all events; `daedalus audit` verifies it        |
| Reviewers can't write, or see what the diff doesn't show | `claude-code` reviews launch with `--restricted --tools Read,Grep,Glob`: no write or shell tools, no settings files, file access confined to the repository, no `.claude/CLAUDE.md` or auto-memory (verified live in run 10) |
| Launched sessions don't outlive supervision | Cancellation is polled while a session runs; timeouts and orchestrator errors kill it; on Windows a kill-on-close job object ends the session's whole process tree, including when the orchestrator itself is killed |

## Human identity on the local CLI

Human-only commands (`approve`, `deny`, `attest`, `amend`, `risk-exception`,
`resolve`, `abandon`, `off`, `reconcile --action`) refuse to run when
`CLAUDECODE` or `DAEDALUS_AGENT` is set, or when stdin isn't a terminal, and
they ask for confirmation. Agent tool calls typically fail all three checks.

That is **defence in depth, not authentication.** A process running as the
same user can:

- forge a TTY (e.g. with `script`) and claim any `human:` identity,
- edit or delete `.daedalus/` (detected for edits by the hash chain, but not for wholesale replacement),
- remove the hooks from `.claude/settings.json`,
- turn the gate off by writing `.daedalus/enabled`,
- start the harness with `DAEDALUS_AGENT` set, which makes the hooks stand down. They do this on purpose
  inside sessions Daedalus launched itself, whose lifecycle Ariadne already governs.

## Recommendations by threat level

- **Fallible agent (default).** Local gate plus Claude Code hooks. Committed policy.
- **You need an audit trail you can trust.** Copy `.daedalus/daedalus.db`, or
  export `daedalus log --json`, to storage the agent can't write. Re-verify with `daedalus audit`.
- **Adversarial or high-stakes.** Re-run the Proof Gate in CI on a clean
  checkout, with `trusted_verifiers` limited to the CI identity, and grant
  `merge`/`deploy` only through your code-review system. The local gate then
  serves as a fast pre-check, and the real gate sits outside the agent's reach.

## Known limitations

- The candidate excludes gitignored files. A check that depends on an ignored,
  generated file isn't fully bound to the candidate.
- Environment fingerprints cover the check executable, OS and declared env
  inputs, not every library on the machine.
- Risk floors are path-based. No classifier catches every risky semantic
  change, so add `behavioral_floors` for your critical areas.
- One orchestrator per repository. Locks on other hosts are presumed alive.
- The Builder is not sandboxed. A `claude-code` Builder launch loads the user's
  settings, so their permission allow-rules decide which shell commands it may run
  (in run 10 it ran `pytest` through PowerShell). Its session can also write the project's
  auto-memory and `.claude/`, which later Builder rounds load. Reviews are protected from this;
  builders are not.
- Off Windows, a session whose orchestrator is killed outright (not cancelled, timed
  out or interrupted) keeps running until it finishes, and a killed session's children
  are not reaped. Whatever such a session leaves behind must still pass verification and any
  required review on the final candidate.
- A criterion bound to a check is only as strong as that check. In `gate` mode,
  a Builder that changes nothing is accepted if the existing checks already pass;
  `review` mode puts a reviewer in front of that diff.
