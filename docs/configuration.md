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
policy:                     # optional: override any key of the default policy
  path_floors:
    - {name: billing, patterns: ["src/billing/**"], tier: high}
```

`daedalus policy --default` prints the full default policy.

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
