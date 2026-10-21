# Working under the gate

Set it up once, then just work.

```bash
pipx install git+https://github.com/KEV-INZHU/Daedalus   # `pipx install daedalus` after the first PyPI release
cd your-repo
daedalus init          # .daedalus.yml (detected test command), Claude Code hooks, git hook
git add .daedalus.yml .claude .gitignore && git commit -m "Gate this repository with Daedalus"
daedalus doctor        # checks the setup; each problem comes with a one-line fix
```

`daedalus doctor` checks that `.daedalus.yml` is committed and valid with at least one mandatory
check, the Claude Code hooks and git hooks are installed (and `.claude/settings.json` committed),
`daedalus` is on PATH where the hooks run, a launching adapter is configured and installed for
reviews, and, when reviews would be billed in cash, `max_cash` is above zero. It changes nothing
and exits non-zero when any check fails.

That's all the setup there is. Everyone who opens the repository in Claude Code now works under the
gate, with nothing to remember:

1. **You ask for something.** Questions open nothing. The agent's first file edit opens a run, and
   the run's objective is your request.
2. **The agent works as usual.** The status line shows the run and what it's waiting on.
3. **The agent tries to stop.** Daedalus runs the trusted checks on the exact working tree. Once
   they pass, it launches every independent review the risk tier or mode requires (read-only, and
   never by the agent that wrote the code). Anything the agent can fix goes back to it with exact
   reasons. Anything only you can decide (an approval, a sign-off, a budget) is put to you. When the
   predicate holds, the run is recorded **ACCEPTED**.
4. **You commit.** A commit that is exactly the accepted work gets `Daedalus-Run` trailers, so the
   log and the PR show what passed the gate. When you push, a `pre-push` hook lists outgoing commits
   without a trailer (merge commits and commits the remote already has are skipped). By default it
   only warns; `harness: {require_provenance_on_push: true}` in `.daedalus.yml` makes it refuse the
   push (a failure of the check itself never does). It is silent with the gate off. Like the commit
   hook, it never replaces a hook you already have.

Without Claude Code, the same loop is three commands: `daedalus start "goal"`, then work, then
`daedalus verify` (runs the checks and launches the required reviews). `daedalus status` always says
what blocks acceptance and who can fix it. To have Daedalus drive the agent itself:

```bash
daedalus run "fix the flaky retry in the uploader" --mode review
```

In CI, `daedalus ci` re-runs the committed checks on every pull request (see [ci.md](ci.md)).

For fresh containers (cloud sessions), set `harness: {bootstrap: "pip install ..."}` in `.daedalus.yml`
and rerun `daedalus install claude-code`. The SessionStart hook runs that command whenever Daedalus
is missing, unprompted, on every machine that opens the repository. It's committed configuration
like the rest of `.daedalus.yml`, so changing it raises the run's tier to critical.

An auto-opened run's objective is your prompt (up to 600 characters). It is stored with the run in
`.daedalus/` (local, never committed) and appears in `daedalus status`, so keep secrets out of prompts
as you would out of commit messages. Set `harness: {auto_start: false}` to open runs only by hand.

## The three things you touch

| Surface  | Command                                   | What runs underneath                                    |
|----------|-------------------------------------------|---------------------------------------------------------|
| Toggle   | `daedalus on` / `daedalus off`, `init`    | Hooks consult the gate; the core pins policy per run    |
| Contract | `daedalus start "goal" -c "criterion"`    | Versioned contract, scope, required checks, budget      |
| Status   | `daedalus status`                         | Full state machine, evidence, risk floors, authorization |

Everything else stays out of the way until it matters: `daedalus log` and
`daedalus audit` for the hash-chained audit trail, `daedalus metrics` for
acceptance rate, rework, cost and latency across runs, plus a few human-only decisions:

```bash
daedalus approve merge          # scoped to this candidate + contract version, expiring, single-use
daedalus attest C2 pass --evidence "checked the UI manually"   # only for --attest criteria
daedalus amend --set scope='["src/api/**"]' --rationale "narrowed"
daedalus risk-exception low --rationale "docs-only change under auth/"
```

Human-only commands refuse to run inside an agent session or without a terminal.

If a review finding looks wrong, dispute it rather than arguing in circles:
`daedalus dispute F2 --reason "..."`, then `daedalus arbitrate F2` has Plato rule on it.
By default the ruling is advice for a human. Set `arbitration_resolves_findings: true` under
`policy:` to let an overrule resolve the finding.
