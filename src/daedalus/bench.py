"""`daedalus bench`: what a reviewer catches, what it invents, and how well it knows which is which.

Each case is a small change with a contract (data/bench/cases.yaml). Most carry one planted bug;
clean controls carry none. Every case is reviewed with the same prompt a run would use (full or
lite), in a scratch git repository holding just that change, and scored:

- **recall**: planted bugs a finding names (the bug's file, plus its line or the words a finding
  about it would use);
- **false alarms**: MAJOR-or-worse findings that name no planted bug, and clean controls that drew one;
- **calibration**: the Brier score of each finding's `confidence` against whether it named the bug,
  and of the review's `residual_risk` against whether the bug was missed;
- **cost** and time per review.

Any reviewer can be benchmarked: the configured adapter, or `--reviewer-cmd`, a shell command that
reads the prompt on stdin and prints the review JSON, so another tool can be wrapped and compared on
the same cases. Results are written under .daedalus/bench/.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from daedalus.adapters.base import Adapter, AgentRequest, AgentResult, Capabilities, extract_json
from daedalus.core.calibration import score
from daedalus.core.errors import DaedalusError

CASES = Path(__file__).resolve().parent / "data" / "bench" / "cases.yaml"
SERIOUS = ("BLOCKER", "MAJOR")
LINE_SLACK = 3  # a finding within this many lines of the planted bug names it
REVIEW_TIMEOUT_S = 600.0


@dataclass(frozen=True)
class Case:
    name: str
    objective: str
    criteria: tuple[str, ...]
    before: dict[str, str]
    after: dict[str, str]
    bug: dict[str, Any] | None

    def bug_line(self) -> int | None:
        if not self.bug:
            return None
        text = self.after.get(self.bug["file"], "")
        for i, line in enumerate(text.splitlines(), 1):
            if self.bug["line"] in line:
                return i
        return None


def load_cases(path: Path | None = None) -> list[Case]:
    p = Path(path) if path else CASES
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise DaedalusError(f"cannot read benchmark cases {p}: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("cases"), list):
        raise DaedalusError(f"{p}: expected a mapping with a `cases:` list")
    out = []
    for i, c in enumerate(doc["cases"]):
        if not isinstance(c, dict) or not {"name", "objective", "before", "after"} <= set(c):
            raise DaedalusError(f"{p}: case {i + 1} needs name, objective, before and after")
        bug = c.get("bug")
        if bug is not None and (not isinstance(bug, dict) or not {"file", "line"} <= set(bug)):
            raise DaedalusError(f"{p}: case {c['name']}: `bug` needs file and line (or is null)")
        case = Case(str(c["name"]), str(c["objective"]), tuple(str(x) for x in c.get("criteria") or ()),
                    {str(k): str(v) for k, v in (c["before"] or {}).items()},
                    {str(k): str(v) for k, v in (c["after"] or {}).items()}, bug)
        if bug is not None and case.bug_line() is None:
            raise DaedalusError(f"{p}: case {case.name}: bug line {bug['line']!r} is not in {bug['file']}")
        out.append(case)
    return out


_LOC = re.compile(r"([\w./-]+\.\w+)(?::|\s+line\s+|#L)(\d+)")


def names_bug(case: Case, f: dict[str, Any]) -> bool:
    """Whether a finding is about the planted bug: it names the bug's file, and either a line near
    it or one of the words a finding about it would use."""
    if not case.bug:
        return False
    text = " ".join(str(f.get(k, "")) for k in ("title", "evidence", "required_change", "verification"))
    low = text.lower()
    path = case.bug["file"]
    if path.lower() not in low and Path(path).name.lower() not in low:
        return False
    line = case.bug_line() or 0
    for m in _LOC.finditer(text):
        if Path(m.group(1)).name == Path(path).name and abs(int(m.group(2)) - line) <= LINE_SLACK:
            return True
    if case.bug["line"].lower() in low:
        return True
    return any(str(w).lower() in low for w in case.bug.get("words") or ())


@dataclass
class CaseResult:
    case: str
    planted: bool
    status: str
    caught: bool = False
    findings: int = 0
    false_serious: int = 0  # MAJOR-or-worse findings that name no planted bug
    other_minor: int = 0  # MINOR or ADVISORY findings that name no planted bug (not scored either way)
    forecasts: list[tuple[float, int]] = field(default_factory=list)
    residual: tuple[float, int] | None = None
    cost: float = 0.0
    cost_basis: str = "api_equivalent"
    seconds: float = 0.0
    error: str = ""
    details: list[dict[str, Any]] = field(default_factory=list)  # each finding, for a person to check the scoring


def _git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise DaedalusError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")


def stage(case: Case, root: Path) -> str:
    """A scratch repository holding the case's base commit and its change; returns the diff."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "bench@daedalus.invalid")
    _git(root, "config", "user.name", "bench")
    _write(root, case.before)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    for rel in set(case.before) - set(case.after):
        (root / rel).unlink()
    _write(root, case.after)
    _git(root, "add", "-A", "--intent-to-add")
    return _git(root, "diff", "HEAD")


def case_prompt(case: Case, diff: str, lite: bool, perspective: str = "aristotle") -> str:
    from daedalus.core.contracts import build_contract
    from daedalus.core.policy import default_policy_text, policy_from_dict
    from daedalus.orchestration import prompts

    policy = policy_from_dict(yaml.safe_load(default_policy_text()))
    spec = {"objective": case.objective,
            "acceptance_criteria": [{"id": f"C{i}", "description": c, "checks": []}
                                    for i, c in enumerate(case.criteria, 1)]}
    contract = build_contract(f"bench-{case.name}", spec, policy, None)
    checks = {"tests": ("NOT_RUN", "the benchmark runs no checks: judge the diff")}
    return prompts.review_prompt(perspective, contract, diff, checks, [], first_pass=True, lite=lite)


def score_case(case: Case, res: AgentResult, seconds: float) -> CaseResult:
    out = CaseResult(case.name, case.bug is not None, res.status, cost=res.cost, cost_basis=res.cost_basis,
                     seconds=round(seconds, 2))
    body = res.structured if isinstance(res.structured, dict) else None
    if res.status != "COMPLETED" or body is None or not isinstance(body.get("findings", []), list):
        out.error = res.error or "no valid JSON result block"
        out.status = "FAILED" if res.status == "COMPLETED" else res.status
        return out
    from daedalus.orchestration.ariadne import probability

    for f in body.get("findings", []):
        if not isinstance(f, dict):
            continue
        out.findings += 1
        hit = names_bug(case, f)
        sev = str(f.get("severity", "")).upper()
        out.caught |= hit
        out.details.append({"title": str(f.get("title", ""))[:200], "severity": sev, "confidence": f.get("confidence"),
                            "names_bug": hit, "evidence": str(f.get("evidence", ""))[:300]})
        if not hit:
            if sev in SERIOUS:
                out.false_serious += 1
            else:
                out.other_minor += 1
        p = probability(f.get("confidence"))
        if p is not None and (hit or sev in SERIOUS):  # an unmatched MINOR may still be real: not scored
            out.forecasts.append((p, int(hit)))
    rr = probability(body.get("residual_risk"))
    if rr is not None:
        out.residual = (rr, int(case.bug is not None and not out.caught))
    return out


class ShellReviewer(Adapter):
    """A reviewer that is any shell command: the prompt on stdin, the review JSON on stdout."""

    name = "reviewer-cmd"
    capabilities = Capabilities(launch_agent=True, cancellation="none", isolation="none", identity="launched",
                                structured_results=True, cost_reporting=False, status_query=False)

    def __init__(self, command: str):
        self.command = command

    def run_agent(self, request: AgentRequest) -> AgentResult:
        try:
            r = subprocess.run(self.command, shell=True, input=request.prompt, capture_output=True, text=True,
                               cwd=str(request.cwd), timeout=request.timeout_s, check=False)
        except subprocess.TimeoutExpired:
            return AgentResult("TIMED_OUT", error=f"timed out after {request.timeout_s:g}s")
        if r.returncode != 0:
            return AgentResult("FAILED", output=r.stdout, error=f"exit {r.returncode}: {r.stderr.strip()[:300]}")
        return AgentResult("COMPLETED", output=r.stdout, structured=extract_json(r.stdout) or _whole_json(r.stdout))


def _whole_json(text: str) -> dict[str, Any] | None:
    try:
        v = json.loads(text)
    except ValueError:
        return None
    return v if isinstance(v, dict) else None


def run_bench(cases: list[Case], adapter: Adapter, *, lite: bool = False, repeat: int = 1,
              notify=lambda _m: None) -> dict[str, Any]:
    results: list[CaseResult] = []
    scratch = Path(tempfile.mkdtemp(prefix="daedalus-bench-"))
    try:
        for n in range(repeat):
            for case in cases:
                root = scratch / f"{n}-{case.name}"
                diff = stage(case, root)
                started = time.monotonic()
                res = adapter.run_agent(AgentRequest(f"bench-{n}", f"bench-{case.name}", "aristotle",
                                                     case_prompt(case, diff, lite), root, REVIEW_TIMEOUT_S,
                                                     read_only=True, context_paths=tuple(case.after)))
                r = score_case(case, res, time.monotonic() - started)
                results.append(r)
                verdict = ("caught" if r.caught else "missed") if r.planted else (
                    "clean" if not r.false_serious else "false alarm")
                notify(f"{case.name}: {verdict if r.status == 'COMPLETED' else r.status.lower()} "
                       f"({r.findings} finding(s), ${r.cost:.4f})")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return summarize(results, adapter, lite, repeat)


def summarize(results: list[CaseResult], adapter: Any, lite: bool, repeat: int) -> dict[str, Any]:
    from daedalus.verification.evidence_cache import adapter_identity

    done = [r for r in results if r.status == "COMPLETED"]
    planted = [r for r in done if r.planted]
    clean = [r for r in done if not r.planted]
    cost = sum(r.cost for r in results)
    return {
        "reviewer": adapter_identity(adapter) or getattr(adapter, "name", type(adapter).__name__),
        "lite": lite,
        "repeat": repeat,
        "reviews": len(results),
        "failed": len(results) - len(done),
        "recall": round(sum(r.caught for r in planted) / len(planted), 4) if planted else None,
        "caught": sum(r.caught for r in planted),
        "planted": len(planted),
        "clean_cases_with_false_alarm": sum(1 for r in clean if r.false_serious),
        "clean": len(clean),
        "false_serious_per_review": round(sum(r.false_serious for r in done) / len(done), 4) if done else None,
        "findings_brier": score(p for r in done for p in r.forecasts).to_dict(),
        "residual_brier": score(r.residual for r in done if r.residual is not None).to_dict(),
        "cost": round(cost, 6),
        "cost_per_review": round(cost / len(results), 6) if results else None,
        "cost_basis": sorted({r.cost_basis for r in results}),
        "seconds_per_review": round(sum(r.seconds for r in results) / len(results), 2) if results else None,
        "cases": [asdict(r) for r in results],
    }


def render(s: dict[str, Any]) -> list[str]:
    def pct(x):
        return "n/a" if x is None else f"{x:.0%}"

    fb, rb = s["findings_brier"], s["residual_brier"]
    lines = [
        f"reviewer {s['reviewer']}{' (lite)' if s['lite'] else ''} · {s['reviews']} review(s)"
        + (f", {s['failed']} failed" if s["failed"] else ""),
        f"  recall: caught {s['caught']} of {s['planted']} planted bugs ({pct(s['recall'])})",
        f"  false alarms: {s['clean_cases_with_false_alarm']} of {s['clean']} clean changes drew a MAJOR+ finding; "
        f"{s['false_serious_per_review']} unfounded MAJOR+ per review",
        f"  calibration: findings Brier {fb['brier'] if fb['brier'] is not None else 'n/a'} over {fb['n']}"
        f" · residual risk Brier {rb['brier'] if rb['brier'] is not None else 'n/a'} over {rb['n']} (0 is perfect)",
        f"  cost ${s['cost']:.4f} (${(s['cost_per_review'] or 0):.4f} per review, {'/'.join(s['cost_basis'])})"
        f" · {s['seconds_per_review']}s per review",
    ]
    missed = [c["case"] for c in s["cases"] if c["planted"] and c["status"] == "COMPLETED" and not c["caught"]]
    if missed:
        lines.append("  missed: " + ", ".join(dict.fromkeys(missed)))
    noisy = [c["case"] for c in s["cases"] if c["false_serious"]]
    if noisy:
        lines.append("  unfounded MAJOR+ on: " + ", ".join(dict.fromkeys(noisy)))
    return lines
