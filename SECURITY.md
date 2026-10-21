# Security policy

## Reporting a vulnerability

Please report privately through GitHub: **Security** tab of
[KEV-INZHU/Daedalus](https://github.com/KEV-INZHU/Daedalus) > **Report a vulnerability**
(private vulnerability reporting). Do not open a public issue or pull request for a vulnerability.

Include what you did, what you expected and what happened, plus a minimal repository or commands that
reproduce it. You can expect an acknowledgement within a few days. Fixes ship in a patch release and are
credited in the [changelog](CHANGELOG.md) unless you prefer otherwise.

Supported versions: the latest release.

## What counts

Daedalus is a gate against a **cooperative but fallible** agent. The full trust model, including known
limits, is in [docs/security-model.md](docs/security-model.md). In short:

- The agent is not trusted to decide acceptance. Disposition is derived from a hash-chained event log;
  human-only decisions (approvals, attestations, amendments, risk exceptions) refuse to run inside an
  agent session or without a terminal.
- Evidence is bound to the exact candidate (a content hash of the working tree). Any edit makes earlier
  evidence stale, and untrusted verifiers, timeouts and crashes are never a pass.
- Reviewers are read-only and independent of the builder. Check commands come from committed policy;
  in CI the policy comes from the base branch, so a pull request cannot weaken the gate that judges it.
- The gate is **not** a sandbox against an adversarial agent with shell access to the same machine.

In scope: a way for an agent or a pull request to get work accepted without the required evidence,
forge or alter the audit trail, perform a human-only action, escape the read-only reviewer boundary, or
run code through Daedalus's own tooling (hooks, CI workflow, GitHub Action) with more privilege than
documented. Out of scope: attacks that need an adversarial agent already running arbitrary commands as
you on your machine, which the security model states are not defended against.
