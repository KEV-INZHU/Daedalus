"""The HTML renderer: one self-contained file, no scripts, no external requests.

Every value goes through `h` (`html.escape`) on its way in, including values Daedalus itself produced:
the rule is simple enough to audit, and a finding's text is hostile until proven otherwise.
"""

from __future__ import annotations

import html

from daedalus.report.model import FOOTER, Report, headline_parts, money, risk_line, strip_controls, utc

BANNER = {"ACCEPTED": "ok", "READY": "ok", "BLOCKED": "warn", "REJECTED": "bad", "CANCELLED": "muted"}

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c2330;--muted:#5d6878;--line:#dfe3ea;--ok:#1a7f4b;--warn:#a15c00;--bad:#b42318;
--okbg:#e5f5ec;--warnbg:#fff3df;--badbg:#fde8e6;--mutedbg:#eceef2;--code:#eef0f4}
@media (prefers-color-scheme:dark){:root{--bg:#12161d;--card:#1a2029;--fg:#e6e9ef;--muted:#9aa5b5;--line:#2c3440;
--ok:#59c98d;--warn:#f0b35a;--bad:#ff8b80;--okbg:#14301f;--warnbg:#33260e;--badbg:#391816;--mutedbg:#242b36;--code:#252c38}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 48px}
.banner{border-radius:10px;padding:18px 20px;margin-bottom:20px;border:1px solid var(--line)}
.banner.ok{background:var(--okbg);color:var(--ok)}.banner.warn{background:var(--warnbg);color:var(--warn)}
.banner.bad{background:var(--badbg);color:var(--bad)}.banner.muted{background:var(--mutedbg);color:var(--muted)}
.banner h1{margin:0 0 4px;font-size:1.5rem}.banner p{margin:0;color:var(--fg)}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 20px;margin-bottom:16px}
h2{margin:0 0 10px;font-size:1.1rem}
table{border-collapse:collapse;width:100%;font-size:.9rem;display:block;overflow-x:auto}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;white-space:nowrap}
code{background:var(--code);padding:1px 5px;border-radius:4px;font:.85em ui-monospace,Menlo,Consolas,monospace;word-break:break-all}
dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;margin:0}dt{color:var(--muted)}dd{margin:0}
.pill{display:inline-block;padding:0 8px;border-radius:99px;font-size:.8rem;font-weight:600;background:var(--mutedbg)}
.pill.ok{background:var(--okbg);color:var(--ok)}.pill.warn{background:var(--warnbg);color:var(--warn)}
.pill.bad{background:var(--badbg);color:var(--bad)}
ol.timeline{list-style:none;margin:0;padding:0 0 0 14px;border-left:2px solid var(--line)}
ol.timeline li{margin:0 0 8px;padding-left:10px;position:relative}
ol.timeline li::before{content:"";position:absolute;left:-19px;top:8px;width:8px;height:8px;border-radius:50%;background:var(--muted)}
.t{color:var(--muted);font-size:.8rem}
footer{color:var(--muted);font-size:.85rem;margin-top:24px}
@media (max-width:600px){dl{grid-template-columns:1fr}}
""".strip()


def h(x: object) -> str:
    return html.escape(strip_controls(str(x)), quote=True)


def _code(x: object) -> str:
    return f"<code>{h(x)}</code>"


def _pill(text: str, kind: str = "") -> str:
    return f'<span class="pill {kind}">{h(text)}</span>'


def _table(head: list[str], rows: list[list[str]], empty: str) -> str:
    """Rows are already-escaped HTML fragments."""
    if not rows:
        return f"<p>{h(empty)}</p>"
    th = "".join(f"<th>{h(c)}</th>" for c in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"


def _src(d: dict | None) -> str:
    return f"reused from run {h(d.get('run_id', '?'))}, event {h(d.get('seq', '?'))}" if d else "ran"


def _kind(result: str) -> str:
    return "ok" if result == "PASS" else "bad" if result in ("FAIL", "ERROR") else "warn"


def render_html(r: Report) -> str:
    """The report as one HTML document."""
    parts = headline_parts(r)
    sections = [
        _verdict(r), _criteria(r), _checks(r), _reviews(r), _findings(r), _tamper(r), _humans(r), _cost(r),
        _audit(r), _timeline(r),
    ]
    footer = h(FOOTER).replace("`.daedalus/daedalus.db`", "<code>.daedalus/daedalus.db</code>")
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Daedalus report {h(r.run_id)}</title><style>{CSS}</style></head><body><main>"
        f'<div class="banner {BANNER.get(r.disposition, "muted")}"><h1>{h(r.disposition)}'
        f"{'' if r.final else ' (not yet finished)'}</h1><p>{h(' · '.join(parts[1:]))}</p></div>"
        + "".join(s for s in sections if s)
        + f"<footer>{footer}</footer></main></body></html>\n"
    )


def _sec(title: str, body: str) -> str:
    return f"<section><h2>{h(title)}</h2>{body}</section>"


def _verdict(r: Report) -> str:
    facts = [
        ("Objective", h(r.objective)), ("Run", _code(r.run_id)), ("Mode", h(r.mode)),
        ("Contract", f"v{r.contract_version}"), ("Scope", h(", ".join(r.scope) or "whole repository")),
        ("Candidate", _code(r.candidate_id or "-")), ("Base revision", _code(r.base_revision or "-")),
        ("Risk", h(risk_line(r))), ("Generated", h(utc(r.generated_at))),
    ]
    dl = "<dl>" + "".join(f"<dt>{h(k)}</dt><dd>{v}</dd>" for k, v in facts) + "</dl>"
    why = "".join(f"<li>{_pill(x.fixable_by or 'system')} {h(x.message)}</li>" for x in r.reasons)
    return _sec("Verdict", dl + (f"<h3>Reasons</h3><ul>{why}</ul>" if why else ""))


def _criteria(r: Report) -> str:
    rows = [[_code(c.criterion_id), h(c.description), h(", ".join(c.checks) or "needs a human")] for c in r.criteria]
    return _sec("Acceptance criteria", _table(["id", "criterion", "proved by"], rows, "None.")) if rows else ""


def _checks(r: Report) -> str:
    rows = [[
        _code(c.check_id), _code(" ".join(c.command)), _pill(c.result, _kind(c.result)),
        _pill(c.state, _kind(c.state)) if c.state else "superseded", f"{c.duration_s:.1f}s", _src(c.reused_from),
    ] for c in r.checks]
    return _sec("Checks", _table(["check", "command", "result", "now", "time", "source"], rows, "No check has run."))


def _reviews(r: Report) -> str:
    rows = [[
        h(v.review_id), h(v.perspective), h(v.scope), _src(v.reused_from) if v.reused else "launched",
        h(v.model or "unknown"), h(", ".join(f"{n} {k.lower()}" for k, n in v.counts.items()) or "none"),
    ] for v in r.reviews]
    return _sec("Reviews", _table(["review", "perspective", "scope", "source", "model", "findings"], rows,
                                  "No review ran."))


def _findings(r: Report) -> str:
    rows = [[
        h(f.finding_id), h(f.severity), _pill(f.status, "ok" if f.status == "resolved" else "warn" if f.status != "open" else "bad"),
        h(f.title), h(f.resolution) or "-",
    ] for f in r.findings]
    return _sec("Findings", _table(["id", "severity", "status", "title", "resolution"], rows, "No findings."))


def _tamper(r: Report) -> str:
    if not r.tamper_signals:
        if not r.tamper_known:
            return _sec("Test-tamper signals", "<p>Not recorded: the audit log does not store the scan, and "
                        "the working tree is no longer this candidate. Run <code>daedalus ci</code> to rescan.</p>")
        return ""
    rows = [[h(s.kind), _code(s.path), h(s.detail)] for s in r.tamper_signals]
    return _sec("Test-tamper signals", _table(["kind", "file", "detail"], rows, ""))


def _humans(r: Report) -> str:
    if not r.human_acts:
        return ""
    rows = [[h(utc(a.at)), h(a.actor), h(a.kind), h(a.detail)] for a in r.human_acts]
    return _sec("Human acts", _table(["when (UTC)", "who", "act", "detail"], rows, ""))


def _cost(r: Report) -> str:
    c = r.cost
    facts = [
        ("Cash", money(c.cash) + (" (at least; a billed session did not report its cost)" if c.cash_unknown else "")),
        ("API-equivalent estimate", money(c.api_equivalent) + " (not billed)"),
        ("Total", f"{money(c.total)} over {c.sessions} session(s)"),
        ("Tokens", f"{c.input_tokens:,} in / {c.output_tokens:,} out"),
        ("Cache savings", f"{c.cache_hits} hit(s): {money(c.saved_total)} (cash {money(c.saved_cash)}), "
                          f"{c.saved_seconds / 60:.1f} min"),
    ]
    return _sec("Cost", "<dl>" + "".join(f"<dt>{h(k)}</dt><dd>{h(v)}</dd>" for k, v in facts) + "</dl>")


def _audit(r: Report) -> str:
    a = r.audit
    state = _pill("verified", "ok") if a.verified else _pill("NOT verified", "bad") + " " + h(a.error)
    return _sec("Audit chain", f"<dl><dt>Hash chain</dt><dd>{state}</dd><dt>Head hash</dt><dd>{_code(a.head_hash or '-')}</dd>"
                               f"<dt>Events</dt><dd>{a.events} in the log, {a.run_events} in this run</dd></dl>")


def _timeline(r: Report) -> str:
    items = "".join(
        f'<li><span class="t">{h(utc(e.at))}</span> {_code(e.type)} {h(e.actor)}'
        + (f" · {h(e.summary)}" if e.summary else "") + "</li>"
        for e in r.timeline
    )
    return _sec("Timeline", f'<ol class="timeline">{items}</ol>')
