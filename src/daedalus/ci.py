"""`daedalus ci`: the gate, run where a team already looks: the pull request.

CI is the one place every change passes through, whoever (or whatever) wrote
it. `daedalus ci` checks a branch against its base using only what is
committed, so a contributor's machine never has to be trusted:

1. **Checks.** Runs every mandatory check on the checked-out head. CI is the
   verifier here: this is fresh evidence, not a replay of anyone's local result.
   The checks, risk floors and requirements come from the **base** branch's
   policy: a pull request cannot weaken the gate that judges it. A PR that
   changes the policy is flagged (and its diff is critical-tier anyway); the new
   policy takes effect once it is merged.
2. **Risk.** Classifies the whole diff (base...HEAD) with the same floors a
   run uses, and lists what that tier requires (reviews, approvals). CI cannot
   see local review records, so it reports them for reviewers to confirm.
3. **Provenance.** Lists which commits carry `Daedalus-Run` trailers, meaning
   they were accepted by the gate where they were written. `--require-provenance`
   fails the job when any non-merge commit lacks one. Trailers are claims, not
   proof; the checks in step 1 are the proof.

It writes a Markdown report to `$GITHUB_STEP_SUMMARY` when that is set (and a compact pull request
comment body to `--markdown-out`), and exits non-zero when a check fails or required provenance is missing.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from daedalus.core.config import load_config
from daedalus.core.errors import DaedalusError
from daedalus.core.risk import assess
from daedalus.repository.candidate import file_sha256
from daedalus.verification.check_runner import run_check
from daedalus.verification.test_integrity import analyze as analyze_tests
from daedalus.verification.test_integrity import check_runner_specs


@dataclass
class CIReport:
    base: str
    changed: list[str]
    tier: str
    risk: str
    reviews: tuple[str, ...]
    approvals: tuple[str, ...]
    checks: dict[str, tuple[str, str]] = field(default_factory=dict)  # id -> (result, detail)
    commits: list[tuple[str, str, str | None]] = field(default_factory=list)  # sha, subject, run id
    require_provenance: bool = False
    policy_source: str = "base"  # base | head (the base has no Daedalus config yet)
    policy_changed: list[str] = field(default_factory=list)
    test_integrity: list[str] = field(default_factory=list)  # signals from the test-tampering scan
    signals: list[tuple[str, str, str]] = field(default_factory=list)  # kind, path, detail (same scan, structured)
    durations: dict[str, float] = field(default_factory=dict)  # check id -> seconds
    tier_source: str = ""  # what set the tier: "path", "behavioral", ...
    head_sha: str = ""  # the commit that was judged; shown in the headline so a stale comment is visible

    @property
    def checks_ok(self) -> bool:
        return bool(self.checks) and all(r == "PASS" for r, _ in self.checks.values())

    @property
    def unattested(self) -> list[tuple[str, str, str | None]]:
        return [c for c in self.commits if not c[2]]

    @property
    def ok(self) -> bool:
        return self.checks_ok and not (self.require_provenance and self.unattested)

    def headline(self) -> str:
        """One line for the top of the PR comment."""
        if self.ok:
            state = "✅ passed"
        elif not self.checks_ok:
            state = "❌ failed"
        else:
            state = "❌ failed (provenance missing)"
        if not self.tier_source:
            where = ""
        elif self.tier_source in _FLOOR_SOURCES:
            where = f" ({_cell(self.tier_source)} floor)"
        else:
            where = f" ({_cell(self.tier_source)})"
        tier = f"tier {_cell(self.tier)}{where}"
        if not self.checks:
            tests = "no checks"
        elif len(self.checks) <= 3:
            tests = " · ".join(f"{_cell(cid)} {res}" for cid, (res, _) in self.checks.items())
        else:
            good = sum(1 for r, _ in self.checks.values() if r == "PASS")
            tests = f"{good}/{len(self.checks)} checks PASS"
        scan = "tamper scan clean" if not self.signals else f"tamper scan: {len(self.signals)} signal(s)"
        sha = f" @ `{_cell(self.head_sha[:10])}`" if self.head_sha else ""
        return f"Daedalus CI: {state}{sha} · {tier} · {tests} · {scan}"

    def pr_comment(self) -> str:
        """The sticky pull request comment: a headline, then collapsible details. Everything that
        comes from the repository goes through `_cell`, so a hostile path or commit message can't
        inject markup. The trailing marker lets the action find and update this one comment."""
        out = [self.headline(), ""]
        if self.policy_changed:
            out += [f"Policy changed in this PR ({_cell(', '.join(self.policy_changed))}): judged by the "
                    f"{self.policy_source} branch's policy; the change applies once merged.", ""]
        if self.policy_source == "head":
            out += ["Note: the base branch has no Daedalus config, so this PR's own policy is used.", ""]
        if self.checks:
            rows = ["| Check | Result | Time | Detail |", "|---|---|---|---|"]
            for cid, (res, detail) in self.checks.items():
                took = f"{self.durations[cid]:.1f}s" if cid in self.durations else ""
                rows.append(f"| `{_cell(cid)}` | {res} | {took} | {_cell(detail)} |")
        else:
            rows = ["The committed policy defines no mandatory check."]
        out += _details("Checks", rows)
        out += _details("Risk tier", [f"{len(self.changed)} file(s) changed against `{_cell(self.base)}`.", "",
                                      _cell(self.risk)])
        if self.signals:
            rows = [f"- {_cell(k)}: `{_cell(p)}` {_cell(d)}" for k, p, d in self.signals[:_MAX_ROWS]]
            if len(self.signals) > _MAX_ROWS:
                rows.append(f"- … and {len(self.signals) - _MAX_ROWS} more")
            out += _details("Test-integrity signals",
                            ["Changes that may have weakened the tests; a reviewer should check them.", "", *rows])
        else:
            out += _details("Test-integrity signals", ["None found."])
        if self.reviews or self.approvals:
            need = [*(f"- {_cell(r)} review" for r in self.reviews), *(f"- {_cell(a)} approval" for a in self.approvals)]
            out += _details("Required reviews (listed, not verified)",
                            ["CI cannot see local review records. Confirm these before merging.", "", *need])
        if self.commits:
            n = len(self.commits) - len(self.unattested)
            rows = [f"- `{_cell(sha[:10])}` {_cell(subj)}"
                    + (f" (run `{_cell(rid)}`)" if rid else " — no Daedalus-Run trailer")
                    for sha, subj, rid in self.commits[:_MAX_ROWS]]
            if len(self.commits) > _MAX_ROWS:
                rows.append(f"- … and {len(self.commits) - _MAX_ROWS} more")
            out += _details("Provenance trailers",
                            [f"{n} of {len(self.commits)} commit(s) carry a Daedalus-Run trailer. Trailers are "
                             "claims; the checks are the proof.", "", *rows])
        out.append(COMMENT_MARKER)
        body = "\n".join(out)
        if len(body) > _MAX_COMMENT:  # keep the marker so the comment can still be updated
            body = _truncate(body, _MAX_COMMENT)
        return body + "\n"

    def annotations(self) -> list[str]:
        """GitHub workflow commands: warnings on test-integrity signals (inline on the diff) and
        errors on failing checks."""
        lines = []
        for kind, path, detail in self.signals:
            lines.append(f"::warning file={_prop(path)},line=1,title={_prop('Daedalus: ' + kind)}::{_data(detail)}")
        for cid, (res, detail) in self.checks.items():
            if res != "PASS":
                msg = f"check {cid} {res}" + (f": {detail}" if detail else "")
                lines.append(f"::error title={_prop('Daedalus check failed')}::{_data(msg)}")
        return lines

    def markdown(self) -> str:
        mark = "✅" if self.ok else "❌"
        lines = [f"## {mark} Daedalus gate", ""]
        lines.append(f"**Risk:** {self.risk} ({len(self.changed)} file(s) changed against `{self.base}`)")
        if self.policy_changed:
            lines.append(f"**Policy changed in this PR** ({', '.join(self.policy_changed)}): judged by the "
                         f"{self.policy_source} branch's policy; the change applies once merged.")
        if self.policy_source == "head":
            lines.append("**Note:** the base branch has no Daedalus config, so this PR's own policy is used.")
        if self.test_integrity:
            lines.append("**Tests weakened?** The scan found changes that may have weakened the tests; "
                         "a reviewer should check them:")
            lines += [f"- {_cell(sig)}" for sig in self.test_integrity]
        if self.reviews or self.approvals:
            need = ", ".join([*(f"{r} review" for r in self.reviews), *(f"`{a}` approval" for a in self.approvals)])
            lines.append(f"**This tier requires:** {need}. Confirm them before merging.")
        lines += ["", "| Check | Result | Detail |", "|---|---|---|"]
        for cid, (res, detail) in self.checks.items():
            lines.append(f"| `{cid}` | {res} | {_cell(detail)} |")
        if not self.checks:
            lines.append("| _none_ | ❌ | the committed policy defines no mandatory check |")
        if self.commits:
            n = len(self.commits) - len(self.unattested)
            lines += ["", f"**Provenance:** {n} of {len(self.commits)} commit(s) were accepted by the gate."]
            for sha, subject, rid in self.commits:
                lines.append(f"- `{sha[:10]}` {_cell(subject)}"
                             + (f" (run `{_cell(rid)}`)" if rid else " — no `Daedalus-Run` trailer"))
        return "\n".join(lines) + "\n"


COMMENT_MARKER = "<!-- daedalus-ci -->"
_FLOOR_SOURCES = ("path", "change", "behavioral")
_MAX_ROWS = 50
_MAX_COMMENT = 60000  # GitHub rejects comments over 65536 characters


def _truncate(body: str, limit: int) -> str:
    """Cut a comment body on a line boundary (never inside an entity), close any `<details>` the
    cut left open, and end with the marker so the comment can still be found and updated."""
    tail = "\n</details>\n\n(truncated)\n" + COMMENT_MARKER
    cut = body[: max(0, limit - len(tail))]
    cut = cut[: cut.rfind("\n")] if "\n" in cut else ""
    close = "\n</details>" * max(0, cut.count("<details>") - cut.count("</details>"))
    return cut + close + "\n\n(truncated)\n" + COMMENT_MARKER


def _details(title: str, body: list[str]) -> list[str]:
    return [f"<details><summary>{title}</summary>", "", *body, "", "</details>", ""]


def _data(text: str) -> str:
    """Escape a workflow command's message (GitHub's rules)."""
    return str(text).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _prop(text: str) -> str:
    """Escape a workflow command property: the message rules plus `:` and `,`."""
    return _data(text).replace(":", "%3A").replace(",", "%2C")


def _cell(text: str) -> str:
    """Text from commits and checks, made inert in the Markdown report: no markup, links, HTML or
    table breaks from a commit subject."""
    import html

    out = html.escape(" ".join(str(text).split()), quote=False)
    for ch in "\\`*_[]()#|!<>~":
        out = out.replace(ch, "\\" + ch)
    return out


def _git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="surrogateescape", check=False)
    if r.returncode != 0:
        raise DaedalusError(f"git {' '.join(a for a in args if a not in ('-c', 'core.quotepath=off'))[:40]} failed: {r.stderr.strip()}")
    return r.stdout


def _base_config(root: Path, merge_base: str, tmp: Path):
    """The Daedalus config as it is on the base, or None when the base has none."""
    from daedalus.core.config import CONFIG_FILE

    shown = subprocess.run(["git", "-C", str(root), "show", f"{merge_base}:{CONFIG_FILE}"],
                           capture_output=True, text=True, check=False)
    if shown.returncode != 0:
        return None
    (tmp / CONFIG_FILE).write_text(shown.stdout, encoding="utf-8")
    try:
        raw = yaml.safe_load(shown.stdout) or {}
    except yaml.YAMLError as exc:
        raise DaedalusError(f"{CONFIG_FILE} on the base is not valid YAML: {exc}") from exc
    pf = raw.get("policy_file") if isinstance(raw, dict) else None
    if pf:
        dest = (tmp / str(pf)).resolve()
        if Path(str(pf)).is_absolute() or tmp.resolve() not in dest.parents:
            raise DaedalusError(f"the base's policy_file {pf!r} must be a path inside the repository")
        body = _git(root, "show", f"{merge_base}:{pf}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")
    return load_config(tmp)


def run_ci(root: Path, base: str, *, require_provenance: bool = False,
           markdown_out: str | Path | None = None, head_sha: str = "") -> CIReport:
    from daedalus.core.config import CONFIG_FILE

    r = subprocess.run(["git", "-C", str(root), "merge-base", base, "HEAD"], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise DaedalusError(
            f"cannot find the base {base!r} in this checkout; fetch it (in GitHub Actions: "
            "`actions/checkout` with `fetch-depth: 0`) or pass `--base <ref>`"
        )
    merge_base = r.stdout.strip()
    changed = [
        p for p in _git(root, "-c", "core.quotepath=off", "diff", "--name-only", "-z", "--no-renames",
                        merge_base, "HEAD").split("\0") if p
    ]
    with tempfile.TemporaryDirectory(prefix="daedalus-ci-") as tmpdir:
        tmp = Path(tmpdir)
        (tmp / "base").mkdir()
        # The policy is the base branch's as it is now (its tip), not as it was when the PR branched:
        # a policy tightened on the base after branching still applies.
        base_tip = _git(root, "rev-parse", "--verify", f"{base}^{{commit}}").strip()
        cfg = _base_config(root, base_tip, tmp / "base")
        source = "base" if cfg is not None else "head"
        cfg = cfg or load_config(root)
        policy = cfg.policy
        try:
            head_raw = yaml.safe_load((root / CONFIG_FILE).read_text(encoding="utf-8")) if (root / CONFIG_FILE).exists() else {}
        except yaml.YAMLError as exc:
            raise DaedalusError(f"{CONFIG_FILE} on this branch is not valid YAML: {exc}") from exc
        policy_files = {CONFIG_FILE} | ({str(head_raw.get("policy_file"))} if isinstance(head_raw, dict) and head_raw.get("policy_file") else set())
        report = _report(root, base, merge_base, changed, policy, require_provenance, tmp / "logs")
        report.policy_source = source
        report.policy_changed = sorted(p for p in changed if p in policy_files)
    report.head_sha = head_sha or _git(root, "rev-parse", "HEAD").strip()
    if markdown_out:
        try:
            Path(markdown_out).write_text(report.pr_comment(), encoding="utf-8")
        except OSError as exc:
            raise DaedalusError(f"cannot write the comment body to {markdown_out}: {exc}") from exc
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(report.markdown())
        except OSError:
            pass  # the report is still printed; a summary is a convenience
    return report


def _tier_source(risk) -> str:
    """What set the effective tier; "escalated" when the effective tier is above every contribution."""
    found = next((c.source for c in risk.contributions
                  if c.tier == risk.effective_tier and c.source != "declared"), "")
    if found:
        return found
    return "" if any(c.tier == risk.effective_tier for c in risk.contributions) else "escalated"


def _report(root: Path, base: str, merge_base: str, changed: list[str], policy, require_provenance: bool,
            logs: Path) -> CIReport:
    signals: list[str] = []
    items: list[tuple[str, str, str]] = []
    paths: tuple[str, ...] = ()
    if policy.test_integrity.enabled:
        files = {}
        for p in changed:
            try:
                files[p] = file_sha256(root / p) if (root / p).is_file() else None
            except OSError:
                files[p] = None
        scan = analyze_tests(
            root, merge_base, files, policy.test_integrity.test_globs,
            check_runner_specs(policy.checks[cid] for cid in policy.mandatory_checks()),
        )
        paths = scan.paths
        signals = [f"{s.path}: {s.detail}" for s in scan.signals]
        items = [(s.kind, s.path, s.detail) for s in scan.signals]
    risk = assess(policy, changed, policy.tiers[0], test_integrity_paths=paths)
    req = policy.requirement(risk.effective_tier)
    report = CIReport(
        base=base, changed=changed, tier=risk.effective_tier, risk=risk.explain(),
        reviews=req.reviews, approvals=req.approvals, require_provenance=require_provenance,
        test_integrity=signals, signals=items,
        tier_source=_tier_source(risk),
    )
    for cid in policy.mandatory_checks():
        out = run_check(policy.checks[cid], root, logs, log_name=cid)
        detail = out.error or (f"exit {out.exit_code}" if out.result != "PASS" else "")
        report.checks[cid] = (out.result, detail)
        report.durations[cid] = max(0.0, out.ended_at - out.started_at)
    log = _git(root, "log", "--no-merges", "--format=%H%x1f%s%x1f%(trailers:key=Daedalus-Run,valueonly,separator=%x2c)%x1e",
               f"{merge_base}..HEAD")
    for rec in log.split("\x1e"):
        parts = rec.strip("\n").split("\x1f")
        if len(parts) == 3 and parts[0]:
            report.commits.append((parts[0], parts[1], parts[2].strip() or None))
    return report


DEFAULT_SOURCE = "git+https://github.com/KEV-INZHU/Daedalus"

WORKFLOW = """# Daedalus gate for pull requests (written by `daedalus init --ci`).
name: Daedalus
on:
  pull_request:
jobs:
  gate:
    runs-on: ubuntu-latest
    permissions:
      contents: read  # the PR's own check commands run with a read-only token
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install Daedalus
        run: {source}
      # Install whatever your checks need here (for example `pip install -e ".[dev]"`).
{install}      - name: Gate
        env:
          BASE_REF: ${{{{ github.base_ref }}}}  # passed through env, never interpolated into the script
        run: daedalus ci --base "origin/$BASE_REF"{flags}
"""


def render_workflow(source: str, install: str | None = None, require_provenance: bool = False) -> str:
    import shlex

    for name, value in (("--ci-install", install), ("--ci-source", source)):
        if value and ("\n" in value or "\r" in value):
            raise DaedalusError(f"{name} must be a single line")
    step = f"      - name: Install project\n        run: {json.dumps(install)}\n" if install else ""
    source = json.dumps(f"pip install {shlex.quote(source)}")  # one argument to pip, safe in YAML
    return WORKFLOW.format(source=source, install=step, flags=" --require-provenance" if require_provenance else "")


def write_result(path: str | Path, verdict: str, *, head_sha: str = "", detail: str = "") -> None:
    """The machine-readable verdict the action reads: `passed`, `blocked` or `error`. It is the only
    thing the action trusts for the outcome; an exit code alone cannot tell a block from a crash."""
    assert verdict in ("passed", "blocked", "error")
    data = {"verdict": verdict, "head_sha": head_sha, "detail": detail[:500]}
    Path(path).write_text(json.dumps(data) + "\n", encoding="utf-8")
