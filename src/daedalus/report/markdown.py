"""The Markdown renderer: compact enough for a PR comment.

Everything an agent or reviewer wrote is passed through `esc` before it reaches the output, so a finding
cannot inject HTML, close a `<details>` block, break a table row or start a heading.
"""

from __future__ import annotations

import re

from daedalus.report.model import FOOTER, ICONS, Report, headline_parts, money, risk_line, strip_controls, utc

MAX_CHARS = 60_000  # GitHub rejects comments over 65,536; stay well inside
ZWSP = "​"


_AUTOLINK = re.compile(r"(?i)(?:://|\bwww\.|\b(?:mailto|xmpp):)")


def esc(text: object, limit: int = 300) -> str:
    """Make arbitrary text inert in Markdown and in the HTML Markdown allows: no tags, no code spans,
    no table or heading syntax, no links, no @mentions. One line, cut at `limit` characters."""
    s = " ".join(strip_controls(str(text)).split())  # newlines would end a table row; ESC would replay in a terminal
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("`", "&#96;")
    s = s.replace("|", "&#124;").replace("[", "&#91;").replace("]", "&#93;").replace("@", "@" + ZWSP)
    s = s.replace("*", "&#42;").replace("_", "&#95;").replace("~", "&#126;")
    s = _AUTOLINK.sub(lambda m: m.group(0) + ZWSP, s)  # GFM would turn a bare URL or www. host into a link
    if s[:1] == "#":
        s = ZWSP + s  # belt and braces: callers never start a line with this text, but a heading must not form
    return s


def code(text: object, limit: int = 300) -> str:
    """Inline code that cannot be closed from inside: HTML <code>, with the text escaped."""
    return f"<code>{esc(text, limit)}</code>"


def _short(h: str, n: int = 12) -> str:
    return h[:n] if h else "-"


def _table(head: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return out


def _section(title: str, body: list[str], *, open_: bool = False) -> list[str]:
    # Blank lines around the body are what make GitHub render Markdown inside <details>.
    return [f"<details{' open' if open_ else ''}><summary>{esc(title)}</summary>", "", *body, "", "</details>", ""]


def _src(d: dict | None) -> str:
    if not d:
        return "-"
    return f"run {esc(d.get('run_id', '?'), 24)} event {esc(d.get('seq', '?'), 12)}"


def _counts(c: dict[str, int]) -> str:
    return ", ".join(f"{n} {sev.lower()}" for sev, n in c.items()) or "none"


def render_markdown(report: Report, *, max_chars: int = MAX_CHARS) -> str:
    """The report as Markdown. If it is too long, every list is cut (with an 'N more' note) until it
    fits; as a last resort the text is cut on a line boundary with its open sections closed."""
    cap = max(len(report.findings), len(report.checks), len(report.reviews), len(report.criteria),
              len(report.tamper_signals), len(report.human_acts), 1)
    while True:
        text = _render(report, cap)
        if len(text) <= max_chars:
            return text
        if cap == 0:
            return _hard_cut(text, max_chars)
        cap //= 2


def _hard_cut(text: str, limit: int) -> str:
    tail = f"\n\n(report cut to fit the size limit; see `daedalus report --format json`)\n\n---\n<sub>{esc_footer()}</sub>\n"
    cut = text[: max(0, limit - len(tail) - 40)]
    cut = cut[: cut.rfind("\n")] if "\n" in cut else ""
    close = "\n</details>" * max(0, cut.count("<details") - cut.count("</details>"))
    return cut + close + tail


def _more(n: int, what: str) -> list[str]:
    return ["", f"{n} more {what} left out to keep this comment short; see `daedalus report --format json`."]


def _render(r: Report, cap: int) -> str:
    parts = headline_parts(r)
    out = [f"### {ICONS.get(r.disposition, '')} " + " · ".join([parts[0]] + [esc(p) for p in parts[1:]]), ""]
    out += [f"**Objective:** {esc(r.objective, 600)}", ""]
    out += [
        f"Run {code(r.run_id)} · mode {esc(r.mode)} · contract v{r.contract_version} · candidate "
        f"{code(_short(r.candidate_id))} · base {code(_short(r.base_revision, 10))}",
        "",
    ]
    out += _section("Verdict and risk", _verdict(r), open_=True)
    if r.criteria:
        out += _section(f"Acceptance criteria ({len(r.criteria)})", [
            f"- {code(c.criterion_id)} {esc(c.description, 400)}" + (f" (proved by {esc(', '.join(c.checks))})" if c.checks else " (needs a human)")
            for c in r.criteria[:cap]
        ] + (_more(len(r.criteria) - cap, "criteria") if len(r.criteria) > cap else []))
    out += _section(f"Checks ({len(r.checks)})", _checks(r, cap))
    out += _section(f"Reviews ({len(r.reviews)})", _reviews(r, cap))
    out += _section(f"Findings ({len(r.findings)})", _findings(r, cap), open_=bool(r.blocking_open))
    if r.tamper_signals:
        out += _section(f"Test-tamper signals ({len(r.tamper_signals)})", [
            f"- {esc(s.kind)} in {code(s.path)}: {esc(s.detail)}" for s in r.tamper_signals[:cap]
        ] + (_more(len(r.tamper_signals) - cap, "signal(s)") if len(r.tamper_signals) > cap else []), open_=True)
    elif not r.tamper_known:
        out += _section("Test-tamper signals", [
            "Not recorded: the audit log does not store the scan, and the working tree is no longer this "
            "candidate. Run `daedalus ci` to rescan."])
    if r.human_acts:
        out += _section(f"Human acts ({len(r.human_acts)})", _table(
            ["when (UTC)", "who", "act", "detail"],
            [[utc(a.at), esc(a.actor), esc(a.kind), esc(a.detail)] for a in r.human_acts[:cap]],
        ) + (_more(len(r.human_acts) - cap, "act(s)") if len(r.human_acts) > cap else []))
    out += _section("Cost and cache savings", _cost(r))
    out += _section("Audit chain", _audit(r))
    out += ["---", f"<sub>{esc_footer()}</sub>", ""]
    return "\n".join(out)


def esc_footer() -> str:
    return FOOTER.replace("`.daedalus/daedalus.db`", "<code>.daedalus/daedalus.db</code>")


def _verdict(r: Report) -> list[str]:
    lines = [f"**{r.disposition}**" + ("" if r.final else " (not yet finished)"), ""]
    lines += [f"- Risk: {esc(risk_line(r), 400)}"]
    for x in r.reasons:
        lines.append(f"- {esc(x.fixable_by or 'system')}: {esc(x.message, 400)}")
    return lines


def _checks(r: Report, cap: int) -> list[str]:
    if not r.checks:
        return ["No check has run."]
    rows = [[
        code(c.check_id), code(" ".join(c.command), 120), esc(c.result), esc(c.state or "superseded"),
        f"{c.duration_s:.1f}s", ("reused: " + _src(c.reused_from)) if c.reused else "ran",
    ] for c in r.checks[:cap]]
    out = _table(["check", "command", "result", "now", "time", "source"], rows)
    return out + (_more(len(r.checks) - cap, "check run(s)") if len(r.checks) > cap else [])


def _reviews(r: Report, cap: int) -> list[str]:
    if not r.reviews:
        return ["No review ran."]
    rows = [[
        esc(v.review_id), esc(v.perspective), esc(v.scope), ("reused: " + _src(v.reused_from)) if v.reused else "launched",
        esc(v.model or "unknown"), esc(_counts(v.counts)),
    ] for v in r.reviews[:cap]]
    out = _table(["review", "perspective", "scope", "source", "model", "findings"], rows)
    return out + (_more(len(r.reviews) - cap, "review(s)") if len(r.reviews) > cap else [])


def _findings(r: Report, shown: int) -> list[str]:
    if not r.findings:
        return ["No findings."]
    rows = [[
        esc(f.finding_id), esc(f.severity), esc(f.status), esc(f.title, 200),
        esc(f.resolution, 200) or "-",
    ] for f in r.findings[:shown]]
    out = _table(["id", "severity", "status", "title", "resolution"], rows)
    if shown < len(r.findings):
        out += _more(len(r.findings) - shown, "finding(s)")
    return out


def _cost(r: Report) -> list[str]:
    c = r.cost
    lines = [
        f"- Cash: {money(c.cash)}" + (" (at least; a billed session did not report its cost)" if c.cash_unknown else ""),
        f"- API-equivalent estimate (not billed): {money(c.api_equivalent)}",
        f"- Total: {money(c.total)} over {c.sessions} session(s), {c.input_tokens:,} input / {c.output_tokens:,} output tokens",
        f"- Evidence cache: {c.cache_hits} hit(s), saved {money(c.saved_total)} "
        f"(cash {money(c.saved_cash)}) and {c.saved_seconds / 60:.1f} min",
    ]
    return lines


def _audit(r: Report) -> list[str]:
    a = r.audit
    state = "verified" if a.verified else "NOT verified: " + esc(a.error, 200)
    return [f"- Hash chain {state} ({a.events} events in the log, {a.run_events} in this run)",
            f"- Head hash: {code(a.head_hash or '-', 80)}"]
