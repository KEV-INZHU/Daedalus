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

## Evidence cache

Reusing a result from an earlier run is sound only if the earlier result cannot be forged and answers the
same question. The cache is built to that:

- **No store of its own.** The source of truth is the hash-chained audit log in `.daedalus/daedalus.db`,
  which only Ariadne writes. There is no cache file an agent could edit. A lookup first verifies the whole
  chain; a broken chain, an unreadable log or an unloadable config disables reuse and the checks and
  reviews simply run (fail closed). The chain stops naive edits, not an actor who rewrites the whole
  database (see Known limitations); against that actor `cache: {checks: false, reviews: false}` or
  `--no-cache` is the setting.
- **Same question.** The key for a check is the candidate content hash, check id, exact argv, timeout,
  `env_inputs` (by value hash), executable content and OS fingerprint, policy hash, Daedalus version and the
  contents of the files its `cache_inputs` name.
  The key for a review is the candidate, perspective, policy hash, contract text, review prompt hash,
  adapter + effective review model, version and `first_pass`; an adapter with no configured model is never
  keyed, and only a review shown no earlier findings is; a review event stores these inputs and the lookup recomputes the key from the
  event's own candidate id and inputs, never read back from it, and requires that candidate to be the current
  one, so a mismatch in any input is a miss. A review is keyed to the candidate it was submitted against; if
  the tree moved between key computation and submission it stores no key and is never a source.
- **Limit: the installed environment.** The candidate hash covers repository files, not site-packages,
  node_modules or the system toolchain, so a dependency upgrade does not change a check's key by itself.
  Two knobs narrow this and neither closes it. `cache: {max_age_s: N}` (default 86400, one day, by
  the orchestrator's clock) refuses any source older than N seconds. A check's `cache_inputs` (paths or
  globs inside the repository, such as a lockfile or `.venv/pyvenv.cfg`) hash those files into its key. An
  environment changed in place and not listed stays invisible; use `cache: false` on that check, or
  `--no-cache`.
- **Never in CI.** `daedalus ci` does not read the audit log at all. A pull request can ship its own
  `.daedalus/daedalus.db`, so CI reusing a PASS from it would let the author supply the verdict CI exists to
  check independently. CI always runs every mandatory check on the fresh checkout.
- **Same kind of answer.** Only PASS evidence from a trusted verifier that Daedalus ran (not a submitted
  claim) and approving reviews that Daedalus launched with no blocking finding and no exposure to other
  reviewers' findings qualify. FAIL, ERROR, timeouts and failed reviews are never reused; a check with
  `cache: false` never is. Reviews with prior open findings of the same perspective are launched, since
  only a launched review can say which findings the new candidate resolves.
- **Still evidence.** A reuse is a new `evidence.recorded` / `review.recorded` event with `reused_from`
  naming the source run, event sequence and event hash. It binds to the current candidate, contract
  version and policy hash and passes the same staleness checks as any record, so the acceptance predicate
  is unchanged and the audit trail shows exactly what was not re-run.

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
  (in run 10 it ran `pytest` through PowerShell). Daedalus itself also grants each Builder
  the run's check commands (`--allowedTools Bash(<prefix>:*)`, e.g. `python -m pytest`), so
  it can test its work before handing it in; without that, headless Builders could run
  nothing and a quarter of factory rounds failed checks they never saw. That is code
  execution: the tests a Builder writes run in its session's environment, which holds the
  agent CLI's own credentials and is **not** scrubbed the way the gate's checks are.
  Checks whose prefix would be a bare interpreter, shell or runner (`python -c ...`) are
  never granted. Set `policy: {builder_runs_checks: false}` to grant nothing.
  The Builder's session can also write the project's
  auto-memory and `.claude/`, which later Builder rounds load. Reviews are protected from this;
  builders are not.
- Off Windows, a session whose orchestrator is killed outright (not cancelled, timed
  out or interrupted) keeps running until it finishes, and a killed session's children
  are not reaped. Whatever such a session leaves behind must still pass verification and any
  required review on the final candidate.
- A criterion bound to a check is only as strong as that check. In `gate` mode,
  a Builder that changes nothing is accepted if the existing checks already pass;
  `review` mode puts a reviewer in front of that diff.
