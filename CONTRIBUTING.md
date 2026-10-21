# Contributing to Daedalus

Thanks for helping. Bug reports, harness integrations and test cases that break the gate are all welcome.

## Development setup

```bash
git clone https://github.com/KEV-INZHU/Daedalus
cd Daedalus
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Python 3.10 or newer. The only runtime dependency is PyYAML.

## Tests and lint

```bash
ruff check                          # lint (must be clean)
python -m pytest -q                 # the full suite (it spawns git and subprocesses, so it takes a few minutes)
```

CI runs the full suite on Linux, macOS and Windows. A gate bug gets a regression test that fails without the fix.

## This repository is gated by Daedalus

Daedalus is developed under its own gate: `.daedalus.yml` holds the mandatory checks, and the risk floors
treat changes to the gate's own core, policy and CI as higher tier. If you use Claude Code here, the hooks
are already configured, so a run opens when you edit and `daedalus status` tells you what is blocking
acceptance. Run `daedalus doctor` if something looks off. You do not need the gate to contribute; CI
re-runs the checks on every pull request either way.

## Adding a harness

Harness integrations are thin adapters over the core. Read [docs/extending.md](docs/extending.md) for the
walkthrough and [docs/adapter-contract.md](docs/adapter-contract.md) for the contract an adapter must
honour (what it may and may not decide, read-only reviewers, how usage is reported).

## Pull requests

- Keep a change to one concern; explain why in the description.
- Match the existing style: dataclasses, small functions, comments that say why, plain user-facing wording.
- Never weaken a check, risk floor or the trust model without saying so in the PR
  ([docs/security-model.md](docs/security-model.md)).
- Update docs and [CHANGELOG.md](CHANGELOG.md) when users would notice.

Security issues: do not open a public issue. See [SECURITY.md](SECURITY.md).

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
