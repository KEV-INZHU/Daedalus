# Modes, risk and configuration

## Modes

| Mode            | Adds                                    | When                                   |
|-----------------|-----------------------------------------|----------------------------------------|
| `gate` (default)| Builder + trusted checks + predicate    | Most tasks                             |
| `review`        | + one focused reviewer (Aristotle)      | When you want a second look            |
| `council`       | + Socrates, Mozi, Aristotle, James      | High-stakes work                       |

Parallel workers are off by default. To let Brunel split a task into
independent packages that build concurrently in isolated git worktrees, raise
the cap in policy, for example `policy: {default_budget: {max_workers: 2, ...}}`,
and set `budget: {max_workers: 2}` in the run's contract file (`daedalus run --contract ...`).

Risk floors apply in every mode. Touching `**/auth/**`, migrations, CI or
lockfiles raises the tier, and the tier sets the reviews and approvals that are
mandatory. A mode can add requirements but can never remove them.

By default the `high` tier requires three independent reviews and no human approval; `critical` also
requires a `merge` approval. To put a human on high-tier merges, add `approvals: [merge]` to the `high` entry of
`tier_requirements` in your policy.

## Configuration

`.daedalus.yml` is deliberately small:

```yaml
enabled: true
mode: gate
adapter:                    # used by `daedalus run` / `daedalus review`
  name: claude-code          # lean sessions: no MCP servers or skills; reviewers are read-only
  review_model: sonnet       # optional per-role models (`model:` for the builder)
checks:
  tests: python -m pytest -q
  typecheck: {command: [mypy, src]}
coordination: true          # launched agents coordinate over a message bus (default on; docs/coordination.md)
policy:                     # optional: override any key of the default policy
  path_floors:
    - {name: billing, patterns: ["src/billing/**"], tier: high}
```

`daedalus policy --default` prints the full default policy.

### Test strength (mutation)

The gate is only as good as the tests it runs. `daedalus mutate` checks them: it breaks the changed
Python lines on purpose, one small break at a time (`==` to `!=`, `and` to `or`, `+` to `-`,
`return x` to `return None`, and so on), and runs your tests against each break in a throwaway
copy of the tree. A break the tests do not notice "survives", and is listed with its file and line:

```yaml
checks:
  tests: python -m pytest -q
  mutation: {command: [daedalus, mutate, --, python, -m, pytest, -q, -x], mandatory: false, cache: false}
```

Changed lines are those since the run's base: Daedalus gives every check `DAEDALUS_BASE_REVISION`.
`cache: false` matters, because the result depends on the base, not only on the tree. The check
fails below `--min-score` (default 0.8), and exits 2 if the tests fail before any break. Use
`--max` (default 30 mutants) and `--timeout` (seconds per test run) to bound the cost. Test files
are never mutated, and the working tree is never touched.

### Holdout checks

A check with `holdout: true` gates acceptance like any other, but the builder never sees it:

```yaml
checks:
  tests: python -m pytest -q
  acceptance: {command: [python, -m, pytest, -q, ../holdout-tests], holdout: true}
```

Builder, worker and planner prompts leave it out of the required checks, and say only that a
criterion is "verified by a held-out check". It is never granted as a command the builder may
run. When it fails, the builder is told that a holdout check failed, without its name, output or
log path.

Inside an agent session (`DAEDALUS_AGENT` or `CLAUDECODE` set):
- `daedalus status` shows its row as `(held out)` (and `held-out` in `--json`);
- `daedalus log` shows its events as "a held-out check";
- `daedalus log --json` and `daedalus report` refuse, because they would show the pinned policy
  and the check by name.

You see everything outside an agent session. Its logs are written **outside the repository**,
under `~/.daedalus/holdout/` (or `$DAEDALUS_HOLDOUT_DIR`), not next to the other check logs.

This hides the holdout from what Daedalus shows the builder. It is not a sandbox. The builder
runs as you, and can read anything you can: `.daedalus.yml` (which holds the check's command),
the held-out tests if they sit in the repository, and the log directory above. Launched
reviewers are confined to their working directory, but launched builders are not. So keep the
held-out tests **outside the repository**, somewhere only the check's command points to (as
above). Treat a holdout as a check the builder is not *told* about, not one it *cannot* find.

Budgets keep two currencies apart. `max_cost` caps everything a run is charged, including
API-equivalent estimates from subscription sessions; `max_cash` caps money actually billed and
defaults to 0. Like `max_cost`, an exhausted cash budget turns a run that still has unmet
conditions into REJECTED; a run whose conditions all hold is not undone by it. A billed session
that ends without reporting its cost (a timeout, a cancellation) counts as exhausting `max_cash`,
since the spend cannot be shown to fit. The budget records spend after the fact; routing
is what declines a paid route before it is called.

## Presets

`daedalus init --preset solo|standard|strict` writes `preset: <name>` into `.daedalus.yml`. The preset
is read from Daedalus itself each time the config loads, so upgrading Daedalus updates it, and the
run's pinned policy hash covers the resolved policy: the preset's `policy` keys, after your overrides.

The hash does **not** cover the other keys a preset can carry: `mode`, `cache` and
`harness` (for example `strict`'s `require_provenance_on_push`). They come from the installed package
each time the config loads, so a Daedalus upgrade can change them without changing a run's policy hash.
To pin one of them, write it in `.daedalus.yml` yourself: keys you write always win over the preset.

| Preset | What it sets |
|---|---|
| `solo` | One developer plus agents. `mode: review`; medium needs one reviewer; high needs three and no human approval; the evidence cache is on. Critical still needs a human merge approval. |
| `standard` | The built-in defaults, unchanged. |
| `strict` | High and critical both need the full four-perspective council and a `merge` approval, and `harness: {require_provenance_on_push: true}` refuses pushes of commits the gate did not accept. |

Keys you write yourself (`mode`, `cache`, `harness`, `policy`) always win over the preset, so a preset
never silently lowers a floor you set. A `policy:` key replaces the preset's value for that key
(`tier_requirements` is replaced as a whole). A preset cannot be combined with `policy_file`.
`daedalus policy --preset strict --diff` shows what a preset would change in your effective policy.

## What it guarantees and what it doesn't

- Every mandatory check must PASS on the **current** candidate. A candidate is
  the content hash of the whole working tree relative to its base, including
  untracked files. Any edit makes earlier evidence STALE.
- Untrusted verifiers, timeouts, crashes and missing checks are never PASS.
- Disposition (ACCEPTED / BLOCKED / REJECTED / CANCELLED) is derived from the
  event log, and nobody sets it directly.
- The gate constrains a cooperative but fallible agent. It is **not** a sandbox
  against an adversarial agent with shell access to the same machine. See
  [security-model.md](security-model.md).
