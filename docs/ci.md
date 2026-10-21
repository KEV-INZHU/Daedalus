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

### Least privilege

- Give the job `permissions: contents: read` and nothing else. The checks are your own commands and run
  with whatever token the job has; a read-only token limits what a malicious pull request can do.
- Trigger on `pull_request`, never `pull_request_target`. `pull_request_target` runs with the base
  repository's secrets, and the gate executes the check commands.
- The action passes every input through environment variables, not into the shell script text.
  `install-command` is run as a shell command by design, so set it in your own workflow file, not from
  untrusted input.
- The policy that judges a pull request comes from the base branch, so a pull request cannot weaken
  the gate that judges it.

### Not yet

A sticky pull request comment (post once, update on each push) is planned. Until then the report is in
the job summary only.
