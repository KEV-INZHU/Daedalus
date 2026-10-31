# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/),
and versions follow [Semantic Versioning](https://semver.org/).

## Unreleased

### Upgrading
- A run that registers coordination agents records `agent.registered` and `agent.retired` events. Earlier
  versions do not know these events and refuse to replay such a run, so read it with this version or later.

### Added
- Agent coordination over the shared filesystem bus, **on by default for `daedalus run`**
  (`daedalus.coordination`, `daedalus bus`, [docs/coordination.md](docs/coordination.md)). `Channel.spawn` mints
  an `agent:<role>:<uuid>` name and has Ariadne register it against the run, task, role and principal.
  `surface.attach` puts the run's context and unread messages into a launch request, fenced as peer data. The
  per-run channel has threaded messages, a context bootstrap for late joiners, restart recovery with message-id
  deduplication, and published/delivered/seen/acted receipts. Reviewers do not see each other's findings on a
  candidate before submitting their own review of it. Launched builders and parallel workers get a registered
  name, the run's context in their prompt, and a separate grant to run `daedalus bus`. Launched reviewers get a
  registered name but no peer messages, and their findings are posted after they are recorded. Turn it off with
  `coordination: false`, or share a bus with `coordination: {bus_dir: path}`. A coordination failure never
  fails a run.
- `AgentRequest.env` and `AgentRequest.bus_commands`: extra environment and coordination-command grants for a
  launched session (the command adapter passes both through; read-only sessions never get the grant).

### Changed
- Every authorship check (review, arbitration, acceptance, review reuse) follows a name's registered
  principal, so a new alias does not let a builder review its own candidate. So does resolving a finding or a
  blocker: any alias of the principal that raised it counts as the raiser.
- `-C` defaults to unset rather than `.`, so a command can tell an explicit `-C .` from none (the
  effective directory is unchanged).
- Launched builders, workers and reviewers are recorded under their generated names (for example
  `agent:brunel:<uuid>` rather than `agent:brunel`) while coordination is on.

## 0.2.0

### Upgrading
- Finish or cancel open runs before you upgrade. A run pins the effective policy hash when it starts, and
  0.2.0 changes the built-in policy, so a run opened on 0.1.0 reports `policy_drift` under 0.2.0 and cannot
  be certified. Start a new run after upgrading.

### Notes on the tamper detector and release process
- `daedalus risk-exception` now acknowledges the test-integrity signals it was shown (it prints them and
  records a digest of each). The `test_integrity` reason names the tier every other floor yields, so the
  exception drops that floor and waives no other; an exception granted without seeing the signals does not
  clear them. A run records `policy_features` at start, so a `policy_file` that omits `test_integrity:` is
  scanned in new runs exactly as `daedalus ci` reports; only runs pinned before the feature stay off.
- The version lives in `daedalus.__version__` alone (pyproject reads it). The release workflow runs lint and
  the tests, and refuses a tag that differs from the built version.
- `daedalus init --force` keeps its backup git-ignored and never overwrites an earlier one;
  `init --commit` commits only the harness files that init wrote and leaves a user's other `.gitignore`
  edits alone. `daedalus demo` ignores the user's git configuration. Runner-config signals look only at
  the test-related part of `setup.cfg`, `tox.ini`, `package.json` (`jest`, `test*` scripts) and `Makefile`
  (`test` target); `tests.py`, `test.py` and `*_tests.py` count as test files. Zero-config runs no longer
  add lint and type checks (`daedalus init` writes them); `harness.auto_finish` must be a boolean.

### Added
- **Evidence cache** ([docs/evidence-cache.md](docs/evidence-cache.md)). A check PASS that Daedalus ran,
  or an approving full review, is reused when the candidate, policy, check or contract, toolchain and
  Daedalus version all match. Reuse is read from the hash-chained audit log (nothing else stores it) and
  recorded as a new event with `reused_from`. `daedalus metrics` reports hits and time and money saved.
  FAIL, ERROR and TIMEOUT are never reused. Review reuse needs an explicit reviewer `model` /
  `review_model`, since the key cannot otherwise say what ran. `daedalus ci` never reuses evidence: it
  re-runs every mandatory check itself. `--no-cache` and `cache:` / `cache_inputs` in `.daedalus.yml`
  control it.
- **Plugin registry** (`daedalus/plugins.py`). Harness integrations (entry-point group
  `daedalus.harnesses`) and adapters (`daedalus.adapters`) register without touching `cli.py`; built-in
  harnesses load lazily and a broken one is listed as unavailable, not a crash. `daedalus plugins` lists
  them. See [docs/extending.md](docs/extending.md).
- **Codex, OpenCode, Pi and Hermes integrations**, alongside Claude Code:
  `daedalus install <name>` or `daedalus init --harness <name>`; `daedalus init` installs into every
  detected harness. Notes in `docs/harnesses/`.
- **Tamper detector** (`verification/test_integrity.py`, deterministic, no LLM). For every changed test
  file the gate compares the base with the candidate and flags removed or emptied tests, dropped
  assertions, newly added skip/focus markers, trivially true assertions (`assert True`) and a changed
  expected literal. Python is analysed with `ast`, JS/TS, Go and Rust by heuristic. A changed test file it
  cannot analyse is itself a signal. A signal never decides alone: it raises the risk tier to the
  `test_integrity` floor (by default the lowest tier that requires an independent review, `high`), which
  adds those reviews, and puts a `[test_integrity]` reason on the decision. A human can acknowledge a
  deliberate test change with `daedalus risk-exception`. Configure with `test_integrity:`
  (`enabled`, `test_globs`, `tier`) in the policy.
- **`daedalus demo [--fast] [--keep]`.** A throwaway repository and a scripted agent, no API key. In
  scene 1 the agent replaces a failing test with `assert True`: the checks pass, yet the gate refuses
  with a `test_integrity` reason. In scene 2 the agent fixes the code and is accepted, with a receipt
  and a verified audit chain.
- **Packaging and CI.** PyPI metadata and a trusted-publishing release workflow, repository CI (the full suite on Linux,
  macOS and Windows, in parallel), and a composite GitHub Action (`action.yml`) that runs `daedalus ci`.
- Community files: contributing guide, security policy, code of conduct, issue and pull request templates.
- `docs/index.md`; the long README sections now live in `docs/` pages.

### Changed
- High-tier changes no longer need a human merge approval by default; the three independent
  Daedalus-launched reviews plus the trusted checks gate them. `critical` still requires `merge`. A team that
  wants a human on high-tier merges adds `approvals: [merge]` to the `high` tier in its policy.
- First-run fixes so `daedalus init` and `daedalus doctor` work on a fresh repository:
  `init` detects more test setups (pytest by tests/ layout, `uv run`, tox, `make test`, pnpm/yarn/bun,
  not the `npm init` placeholder); `init --commit` commits every file it and the selected harness
  integrations wrote, and without it prints the exact commit command; `init --force` keeps the old
  `.daedalus.yml` as `.daedalus.yml.bak`; the decision reads `READY` rather than `BLOCKED` when only the
  recording is left; `daedalus verify` records acceptance itself when every condition holds
  (`--no-finish` or `harness.auto_finish: false` to opt out); `daedalus --help` lists commands in groups.
- README rewritten to be short; the detail moved to `docs/`.
- `ruff` is part of the dev extras.

## 0.1.0

- Initial release: lifecycle state machine, revision-bound evidence, derived acceptance, risk floors,
  independent read-only reviews, hash-chained audit log, Claude Code hooks, `daedalus ci` and the factory.
