# Verdict report

`daedalus report` renders one run from the audit log: what was asked, what was proven, by whom, and at what cost. It reads the log; it never re-runs a check, launches a reviewer or writes an event.

```
daedalus report                          # Markdown for the open run (else the latest), to the terminal
daedalus report --run 3fa9 --format html -o report.html
daedalus report --format json            # the Report as JSON
```

| Format | For |
|---|---|
| `md` (default) | A PR comment. One headline line, then collapsible `<details>` sections. Kept under 60,000 characters; if it would be longer, every list (findings, checks, reviews, signals, human acts) is cut with an "N more" note, and as a last resort the text is cut at a line with its sections closed. |
| `html` | One self-contained file: inline CSS, no scripts, no external requests, light and dark mode. Adds a timeline of the run's events. |
| `json` | The full `Report`, for tools. |

The headline looks like this:

```
✅ ACCEPTED · tier high · tests PASS · 3 reviews (0 blocking) · $0.41 · chain verified
```

## What is in it

- **Run:** objective, acceptance criteria, scope, mode, risk tier and the floor that set it, base revision, candidate hash.
- **Verdict:** ACCEPTED, READY (every condition holds, not yet finished), BLOCKED, REJECTED or CANCELLED, with the reasons.
- **Checks:** command, result, duration, and whether the result was reused from the evidence cache (with the run and event it came from).
- **Reviews:** perspective, full or delta, launched or reused, the reviewer's model when the session reported one, and findings by severity.
- **Findings:** title, severity, status (open, resolved, disputed, moot) and the resolution note.
- **Test-tamper signals:** what the `test_integrity` scan flagged. The scan is not stored in the log, so it is shown only for an open run still on the reported candidate; for a finished run (or after the tree has moved) the report says the signals are not recorded, and the risk line shows only the recorded tier.
- **Human acts:** approvals, attestations, risk exceptions and finding resolutions, with who and when.
- **Cost:** cash against API-equivalent estimate, and what the evidence cache saved.
- **Audit chain:** whether the hash chain verified, and its head hash.

## What it does and does not prove

A local report is a claim by the machine that produced it. It says what that machine's audit log records. The footer says so: "Generated locally from `.daedalus/daedalus.db`; CI re-verifies checks independently." A reader who needs proof should rely on `daedalus ci`, which runs the checks again on a fresh checkout. "Chain verified" means the log has not been altered since it was written, not that the machine that wrote it was honest.

## Untrusted text

Findings, resolution notes and attestation evidence are written by agents and reviewers. The HTML renderer escapes every value with `html.escape`. The Markdown renderer turns `<`, `>`, `&`, backticks, `|`, brackets, `*`, `_` and `@` into inert text and puts each value on one line, so a finding cannot inject HTML, close a `<details>` block, break a table row or ping a user. Control characters (such as terminal escape sequences) are dropped by both renderers, and a bare `https://…`, `www.` or `mailto:` is broken with a zero-width space so GitHub does not turn it into a link.

## From code

```python
from daedalus.report import build_report, render_markdown, render_html, Report

report = build_report(ari, run_id)      # reads only
text = render_markdown(report)
same = Report.from_dict(report.to_dict())
```
