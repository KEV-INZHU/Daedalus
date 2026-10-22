# In CI: the gate on every pull request

```bash
daedalus init --ci --ci-install 'pip install -e ".[dev]"'   # writes .github/workflows/daedalus.yml
```

On each pull request, `daedalus ci` uses only what is committed, so no contributor's machine has to be
trusted:

- **Checks.** It re-runs every mandatory check in the committed policy on the head. That's fresh
  evidence, not anyone's local claim.
- **Risk.** It classifies the whole diff with the same floors a run uses, and lists the reviews and
  approvals that tier requires.
- **Provenance.** It reports which commits carry `Daedalus-Run` trailers (were accepted by the gate where
  they were written). `--require-provenance` makes a missing trailer fail the job.

Pin what the workflow installs with `--ci-source 'git+https://github.com/KEV-INZHU/Daedalus@<tag>'`,
so the tool judging pull requests doesn't change under them. It writes the report to the job summary
and fails when a check fails. Trailers are claims anyone can
type, which is why the checks are always re-run.

## The GitHub Action

The repository root holds a composite action (`action.yml`) that installs Daedalus and runs
`daedalus ci`, the same thing `init --ci` writes out as steps:

```yaml
name: Daedalus
on:
  pull_request:
permissions:
  contents: read
  pull-requests: write    # for the sticky comment; drop it and set `comment: "false"` to skip the comment
jobs:
  gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0          # the gate needs the base branch's history
      - uses: KEV-INZHU/Daedalus@main               # pin a release tag (for example @v0.2.0) once one is published
        with:
          python-version: "3.12"
          install-command: pip install -e ".[dev]"   # whatever your checks need
          # daedalus-source: git+https://github.com/KEV-INZHU/Daedalus@<commit-or-tag>   # pin the tool that judges pull requests
          #   (after the first PyPI release, `daedalus==<version>` works too)
          # require-provenance: "true"
          # extra-args: ""
```

| Input | Default | Meaning |
|---|---|---|
| `python-version` | `3.12` | Python used for Daedalus and your checks |
| `daedalus-source` | `git+https://github.com/KEV-INZHU/Daedalus` | What `pip install` receives; pin a commit or tag |
| `install-command` | none | Shell command that installs what your checks need |
| `base` | `origin/<PR base branch>` | Ref to compare against |
| `require-provenance` | `false` | Fail when a commit lacks a `Daedalus-Run` trailer |
| `extra-args` | none | Extra `daedalus ci` flags, space-separated |
| `comment` | `true` | Post the verdict as one pull request comment, updated on each push |
| `annotations` | `true` | Show test-integrity signals and failing checks inline on the diff |
| `fail-on` | `blocked` | `blocked` fails the job when the gate rejects the change; `never` only reports it |

A copy-paste workflow with least privileges is in `examples/workflows/daedalus.yml`.

### The pull request comment

On `pull_request` events the action posts one comment and updates it on every push. The first line is
the verdict, for example `Daedalus CI: ✅ passed · tier high (ci floor) · tests PASS · tamper scan clean`.
Collapsible sections follow: checks with durations, the risk tier and why, test-integrity signals,
required reviews, and the provenance trailers seen. Required reviews are listed, not verified: CI cannot
see local review records, so a reviewer confirms them. Anything taken from the repository (paths,
commit subjects, signal details) is escaped, so a hostile file name cannot inject markup.

The headline names the commit it judged (`Daedalus CI: ✅ passed @ \`abc1234567\` · ...`), so a stale
comment is visible. The action takes the outcome from a small JSON file that `daedalus ci --result-out FILE`
writes (`{"verdict": "passed" | "blocked" | "error", "head_sha": ..., "detail": ...}`), never from the exit
code alone: a crash also exits 1. A missing or unreadable file, or a verdict that disagrees with the exit
code, counts as `error`. An `error` always fails the job (whatever `fail-on` says) and replaces the sticky
comment with `error running the gate @ <sha>`, so an earlier pass is never left standing.

The comment carries a hidden `<!-- daedalus-ci -->` marker. The action finds its own comment by that
marker (only one posted by `github-actions[bot]`) and edits it instead of adding another.

The job needs `permissions: pull-requests: write` for this. On pull requests from forks GitHub gives the
token read-only access: the action detects that (the head repository differs from this one, or a 403 from the API), prints a notice,
and skips the comment. It never fails the job over the comment. The report is still in the job summary.

Without the action, `daedalus ci --markdown-out FILE` writes the same body to a file, and
`daedalus ci --annotations` prints GitHub workflow commands (`::warning file=...::` for test-integrity
signals, `::error::` for failing checks) so they show on the diff. Their text is escaped by GitHub's
rules (`%`, CR, LF, and `:` and `,` in properties).

### Least privilege

- Give the job `permissions: contents: read`, plus `pull-requests: write` only if you want the comment.
  Nothing else. The checks are your own commands and run in
  this job, so a narrow token limits what a malicious pull request can do. Only the comment step is given the token.
- Trigger on `pull_request`, never `pull_request_target`. `pull_request_target` runs with the base
  repository's secrets, and the gate executes the check commands.
- The action passes every input through environment variables, not into the shell script text.
  `install-command` is run as a shell command by design, so set it in your own workflow file, not from
  untrusted input.
- The policy that judges a pull request comes from the base branch, so a pull request cannot weaken
  the gate that judges it.
