# Troubleshooting

Start with `daedalus doctor`: it checks the setup and gives a one-line fix for each problem. Then
`daedalus status` says what blocks the current run and who can fix it. The quoted messages below are
what Daedalus prints.

## 1. "uncommitted policy configuration ... commit it before starting a run"

A run may only be certified by policy that was committed and so could be reviewed. Commit
`.daedalus.yml` (and any `policy_file`):

```bash
git add .daedalus.yml && git commit -m "Daedalus config"
```

## 2. "the effective policy changed after the run started"

The run pinned the policy's hash when it began and the policy is different now. Either you edited
`.daedalus.yml` mid-run (revert it, or start a new run), or you upgraded Daedalus, which can change the
built-in defaults and presets. Cancel the run (`daedalus cancel`) and start a new one. To see what a
preset would change before adopting it: `daedalus policy --preset strict --diff`.

## 3. "would be billed in cash, and this run's cash budget cannot cover it"

Reviews launched through an API key cost money and `max_cash` defaults to 0. Either use a subscription
login (reviews then cost nothing in cash), or raise the cap and start a new run:

```yaml
policy: {default_budget: {max_cash: 5}}
```

## 4. A review is required but nothing can launch it

Typical messages: "no adapter here can launch one" and "the inline adapter cannot launch agents".
Set `adapter: claude-code` in `.daedalus.yml` and make sure the `claude` binary is on `PATH`
(`daedalus doctor` checks both). Or run reviews yourself with `daedalus review`. See
[adapter-contract.md](adapter-contract.md).

## 5. Evidence is STALE, or "ran under a different policy or check definition"

Evidence belongs to one exact tree and one check definition. After any edit, or a change to the
check's command, earlier results are stale. Run `daedalus verify` again. Edits made by the gate's own
follow-up round are covered the same way.

## 6. A mandatory check keeps failing, or the gate says "tests weakened"

The `test_integrity` rule fires when a candidate deletes tests, drops assertions or adds
`assert True`; it raises the risk tier and the reasons name the test. Restore the assertion or fix the
code. A person who really intends the change records it with `daedalus risk-exception`.

## 7. The agent's edits do not open a run

Check that the gate is on (`daedalus status` shows ON or OFF; `daedalus on` turns it on), that the
hooks are installed and committed (`daedalus doctor`; reinstall with `daedalus install <harness>`),
and that `daedalus` is on `PATH` where the hooks run. For Codex, the project and its hooks must also be
trusted in Codex itself; see [harnesses/codex.md](harnesses/codex.md).

## 8. "run ... is still open; finish, cancel or abandon it first"

Only one run can be open per repository. `daedalus status` shows it. Finish it, or `daedalus cancel`
it if it is not wanted.

## 9. "is a human decision; it cannot run inside an agent session" or "lacks ... authority"

Merge, deploy, approvals and risk exceptions are human decisions. They cannot run inside an agent
session or without an interactive terminal. Run the command yourself in a normal terminal (for
example `daedalus approve merge`).

## 10. "not a git repository" or a git hook was not installed

Daedalus needs a git repository to identify candidates and to install the commit and push hooks. Run
`git init` and commit once, then `daedalus init` again. A hook you already have is never replaced; the
message says how to chain ours into it. If a push is refused for a missing `Daedalus-Run` trailer,
that is `harness: {require_provenance_on_push: true}` (set by the `strict` preset); commit the
accepted work as is, or turn the key off.
