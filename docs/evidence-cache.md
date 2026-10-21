# Evidence cache: verify and review an identical tree once

A new run, a run reopened after an abandon or a revert, or a factory order re-run often arrives at a working tree that is byte-identical to one Daedalus already verified.
Instead of re-running the suite and re-launching billed reviewers, it reuses what the audit log already
proves:

- **Checks.** A PASS that Daedalus itself ran is reused when the candidate hash, check id, exact argv,
  timeout, declared `env_inputs`, executable and OS fingerprint, policy hash, Daedalus version and the
  contents of its `cache_inputs` all match. FAIL, ERROR and TIMEOUT are never reused. A non-deterministic check opts out with
  `checks: {flaky: {command: ..., cache: false}}`.
- **Reviews.** An approving verdict (no blocking finding) that Daedalus launched, unexposed and full-scope,
  is reused when the candidate, perspective, policy, contract (objective, criteria, scope, invariants,
  constraints), review prompt text and adapter + model all match. Only first-pass reviews count: a
  re-review that was shown earlier findings is never a source. An adapter with no configured `model` /
  `review_model` has no reuse, since the key could not say what ran.
- **Recorded, not assumed.** A reuse is a new evidence or review event carrying `reused_from`
  (`run_id`, event `seq` and `hash`), bound to the current candidate, so acceptance is derived exactly as
  before. `daedalus verify` prints `reused from run ... event ...`; `daedalus metrics` reports hits and
  the seconds and dollars saved, taken from the source event's recorded duration and cost.

```yaml
cache: {checks: true, reviews: true, max_age_s: 86400}   # the defaults; false always runs for real
checks:
  tests: {command: pytest -q, cache_inputs: [uv.lock, .venv/pyvenv.cfg]}
```

**What the key cannot see.** The candidate hash covers the repository's files, not what is installed
around them (site-packages, node_modules, a system toolchain). Two knobs bound that risk. `max_age_s`
(default one day, measured by the run's clock) refuses any source older than that. `cache_inputs` on a
check lists paths or globs inside the repository (a lockfile, `.venv/pyvenv.cfg`); their contents are
hashed into that check's key, so changing a dependency pin or recreating the environment is a miss.
Neither makes reuse exact: an environment changed in place and not listed is still invisible, so use
`cache: false` on such a check, or `--no-cache`.

`--no-cache` on `daedalus verify`, `run` and `review` (and the Stop-hook settle path) forces a fresh
run. `daedalus ci` never reuses evidence: it is the independent re-verification on a fresh checkout, and
an audit database that arrives with the pull request is input the author controls, so CI runs every
mandatory check itself.
