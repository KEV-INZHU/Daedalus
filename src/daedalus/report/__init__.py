"""A shareable verdict report for one run, rendered from the audit log.

`build_report` folds the recorded events (plus the live decision and the metrics) into a `Report`;
`render_markdown` and `render_html` turn it into text. Nothing here writes to the log or re-runs a
check. A report is a claim by the machine that produced it: see `FOOTER`.
"""

from daedalus.report.markdown import render_markdown
from daedalus.report.model import FOOTER, Report, build_report
from daedalus.report.page import render_html

__all__ = ["FOOTER", "Report", "build_report", "render_html", "render_markdown"]
