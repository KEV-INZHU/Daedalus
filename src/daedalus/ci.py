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

It writes a Markdown report to `$GITHUB_STEP_SUMMARY` when that is set, and
exits non-zero when a check fails or required provenance is missing.
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

    @property
    def checks_ok(self) -> bool:
        return bool(self.checks) and all(r == "PASS" for r, _ in self.checks.values())

    @property
    def unattested(self) -> list[tuple[str, str, str | None]]:
        return [c for c in self.commits if not c[2]]

    @property
    def ok(self) -> bool:
        return self.checks_ok and not (self.require_provenance and self.unattested)

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


def run_ci(root: Path, base: str, *, require_provenance: bool = False) -> CIReport:
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
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(report.markdown())
        except OSError:
            pass  # the report is still printed; a summary is a convenience
    return report


def _report(root: Path, base: str, merge_base: str, changed: list[str], policy, require_provenance: bool,
            logs: Path) -> CIReport:
    signals: list[str] = []
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
    risk = assess(policy, changed, policy.tiers[0], test_integrity_paths=paths)
    req = policy.requirement(risk.effective_tier)
    report = CIReport(
        base=base, changed=changed, tier=risk.effective_tier, risk=risk.explain(),
        reviews=req.reviews, approvals=req.approvals, require_provenance=require_provenance,
        test_integrity=signals,
    )
    for cid in policy.mandatory_checks():
        out = run_check(policy.checks[cid], root, logs, log_name=cid)
        detail = out.error or (f"exit {out.exit_code}" if out.result != "PASS" else "")
        report.checks[cid] = (out.result, detail)
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
