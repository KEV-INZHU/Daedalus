# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/),
and versions follow [Semantic Versioning](https://semver.org/).

## Unreleased

### Upgrading
- Runs record new event types (`agent.registered`, `agent.retired`, `contract.proposed`,
  `contract.proposal_rejected`, `finding.answered`). Earlier
  versions do not know these events and refuse to replay such a run, so read it with this version or later.

### Added
- `daedalus/coordination/buslock.py`: the bus's writer lock as one standard-library-only file, so the shared
  `bus.py` can copy it and take the same lock as Daedalus (docs/coordination.md shows the two-line change).
- Factory ship step: `factory: {ship: {push: origin, pull_request: true, base: main}}` pushes the integration
  branch after a line ships orders and opens a pull request with `gh` (logged as `line.pushed`). Off by default; a
  failure is reported and never undoes what shipped.
- `daedalus metrics`: review spend by perspective (cost, findings, cost per finding), and, for fan-out runs, time
  saved by running packages in parallel against integration time and failures (`review_spend`,
  `parallel_saved_s`, `integration_s`, `integration_failures` in `--json`).
- `daedalus next`: one line saying what the open run needs now and from whom: the agent's own fixes first, then
  what Daedalus runs itself, then decisions only a person can make (`--json` too).
- Plan review before coding: `plan_review: true` makes `daedalus run` have the Builder plan first (read-only) and an
  independent reviewer critique the plan; its concerns become round 1's "Before you build" feedback.
  `daedalus plan-review "plan"` (or `--file`) does the same in an interactive session. Advice only: no review or
  finding is recorded, only the sessions' cost.
- Test strength: `daedalus mutate -- <test command>` mutates the changed Python lines (comparisons, boolean and
  arithmetic operators, `not`, returns) in a throwaway copy and reports the mutants the tests let through. Fit to
  run as a non-mandatory check. Every check now gets `DAEDALUS_BASE_REVISION` (the run's base).
- Escaped-defect tracking: a commit with a `Fixes-Run: <run id>` trailer marks that accepted run as having let a
  defect through; `daedalus metrics` reports escaped defects and the escape rate (`escaped_by` per run in `--json`).
- Live coordination delivery: a launched `claude-code` builder that coordinates gets a `PostToolUse` hook
  (`daedalus hook bus`, passed with `--settings`) that adds its new bus messages to the session's context after
  each tool call, fenced as peer data.
- Holdout checks: `holdout: true` on a check keeps it out of every builder, worker and planner prompt and
  out of the builder's command grants, and its failures reach the builder without name, output or log path. It
  still gates acceptance like any required check ([docs/configuration.md](docs/configuration.md)).
- Findings as a worklist: `daedalus findings` lists a run's findings (`--all`, `--json`), and
  `daedalus findings answer F1 --fixed "note"` or `--dispute "evidence"` answers one. A fix note
  (`finding.answered`) resolves nothing: the next review of that perspective sees it, quoted, and resolves the
  finding only if the change bears it out. Gate feedback now points at the worklist.
- Proposed contracts: `daedalus propose "objective" -c criterion` (agents may run it) records a suggested
  objective and criteria (`contract.proposed`), and `daedalus confirm` (human only) applies it as an amendment, or
  rejects it with `--reject`. A run the harness opens asks the agent to propose one; a pending proposal shows in
  `daedalus status` and the stop hook's "Needs you" list.
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
- A check that fails because a Python module is not installed says so in its reason ("`pytest` is not installed
  where the check ran, so this is not a test result"), with the bootstrap fix for fresh containers.
- Messages people read name roles by what they do: "a quality review (Aristotle)", "the arbiter (Plato)", in gate
  reasons, the stop hook and `daedalus run` progress. Commands, reason codes and the audit log keep the ids
  (`daedalus review aristotle`, `review:aristotle`).
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
