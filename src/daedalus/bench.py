"""`daedalus bench`: what a reviewer catches, what it invents, and how well it knows which is which.

Each case is a small change with a contract (data/bench/cases.yaml). Most carry one planted bug;
clean controls carry none. Every case is reviewed with the same prompt a run would use (full or
lite), in a scratch git repository holding just that change, and scored:

- **recall**: planted bugs a finding names (the bug's file, plus its line or the words a finding
  about it would use);
- **false alarms**: MAJOR-or-worse findings on clean controls (findings that only say tests are missing
  are counted apart: the cases have no tests by design). Serious findings on a planted case that are about
  something else are listed, not scored: they may be real;
- **calibration**: the Brier score of each finding's `confidence` where the truth is known (a finding that
  names the bug, or a serious one on a clean control), and of the review's `residual_risk` against whether
  the bug was missed;
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
LINE_SLACK = 2  # a finding within this many lines of the planted bug names it
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
    names: set[str] = set()
    for i, c in enumerate(doc["cases"]):
        if not isinstance(c, dict) or not {"name", "objective", "before", "after"} <= set(c):
            raise DaedalusError(f"{p}: case {i + 1} needs name, objective, before and after")
        name = c["name"]
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name in names:
            raise DaedalusError(f"{p}: case {i + 1}: name must be a unique slug (letters, digits, - and _)")
        names.add(name)
        before, after = _files(p, name, "before", c["before"]), _files(p, name, "after", c["after"])
        bug = c.get("bug")
        if bug is not None and (not isinstance(bug, dict) or not isinstance(bug.get("file"), str)
                                or not isinstance(bug.get("line"), str) or not bug["line"].strip()
                                or not isinstance(bug.get("words", []), list)):
            raise DaedalusError(f"{p}: case {name}: `bug` needs a file and a line (strings), or is null")
        case = Case(name, str(c["objective"]), tuple(str(x) for x in c.get("criteria") or ()), before, after, bug)
        if bug is not None and case.bug_line() is None:
            raise DaedalusError(f"{p}: case {case.name}: bug line {bug['line']!r} is not in {bug['file']}")
        out.append(case)
    return out


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def _files(src: Path, case: str, key: str, v: Any) -> dict[str, str]:
    """A case's files: relative paths inside the scratch repository, mapped to their text."""
    if v is None:
        return {}
    if not isinstance(v, dict) or not all(isinstance(k, str) and isinstance(b, str) for k, b in v.items()):
        raise DaedalusError(f"{src}: case {case}: `{key}` must map file paths to their text")
    for rel in v:
        parts = Path(rel).parts
        if not rel.strip() or Path(rel).is_absolute() or ".." in parts or (parts and parts[0] == ".git"):
            raise DaedalusError(f"{src}: case {case}: {rel!r} must be a relative path inside the case")
    return dict(v)


_TEST_GAP = re.compile(r"\b(no|missing|without|lacks?|add(ed)?)\b[^.]{0,40}\btests?\b|\buntested\b|"
                       r"\btests?\b[^.]{0,30}\b(missing|cover|pin|added)\b", re.IGNORECASE)


def test_gap(f: dict[str, Any]) -> bool:
    """A finding that only says tests are missing. The cases ship without tests on purpose, so such a
    finding is fair but says nothing about the planted bug: it is counted apart."""
    return bool(_TEST_GAP.search(str(f.get("title", ""))))


_LOC = re.compile(r"([\w./-]+\.\w+)(?::|\s+line\s+|#L)(\d+)")


def names_bug(case: Case, f: dict[str, Any]) -> bool:
    """Whether a finding is about the planted bug: it names the bug's file, and either a line near
    it or one of the words a finding about it would use."""
    if not case.bug:
        return False
    text = " ".join(str(f.get(k, "")) for k in ("title", "evidence", "required_change", "verification"))
    low = text.lower()
    path = case.bug["file"]
    # A change to one file needs no file name to say which one is meant.
    if len(case.after) > 1 and path.lower() not in low and Path(path).name.lower() not in low:
        return False
    line = case.bug_line() or 0
    locs = [(Path(m.group(1)).name, int(m.group(2))) for m in _LOC.finditer(text)]
    if locs and all(name != Path(path).name for name, _ in locs):
        return False  # it points somewhere else
    if any(name == Path(path).name and abs(n - line) <= LINE_SLACK for name, n in locs):
        return True
    if case.bug["line"].strip().lower() in low:
        return True
    return any(_word(str(w).lower(), low) for w in case.bug.get("words") or ())


def _word(w: str, text: str) -> bool:
    """`w` in `text` as whole words, where it starts and ends with a word character."""
    if not w.strip():
        return False
    pat = re.escape(w)
    if w[0].isalnum():
        pat = r"\b" + pat
    if w[-1].isalnum():
        pat += r"\b"
    return re.search(pat, text) is not None


@dataclass
class CaseResult:
    case: str
    planted: bool
    status: str
    caught: bool = False
    findings: int = 0
    false_serious: int = 0  # on a clean control: MAJOR-or-worse findings (not test gaps); a false-alarm proxy
    other_serious: int = 0  # on a planted case: MAJOR-or-worse findings about something else (may be real)
    other_minor: int = 0  # MINOR or ADVISORY findings that name no planted bug (not scored either way)
    test_gaps: int = 0  # findings that only say tests are missing (the cases have none by design)
    forecasts: list[tuple[float, int]] = field(default_factory=list)
    residual: tuple[float, int] | None = None
    cost: float = 0.0
    cost_basis: str = "api_equivalent"
    seconds: float = 0.0
    error: str = ""
    details: list[dict[str, Any]] = field(default_factory=list)  # each finding, for a person to check the scoring


def _git(root: Path, *args: str) -> str:
    # The user's global git setup (signing, hooks) has no business in a scratch repository.
    r = subprocess.run(["git", "-C", str(root), "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                        *args], capture_output=True, text=True, check=False)
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
        gap = not hit and test_gap(f)
        out.caught |= hit
        out.details.append({"title": str(f.get("title", ""))[:200], "severity": sev, "confidence": f.get("confidence"),
                            "names_bug": hit, "test_gap": gap, "evidence": str(f.get("evidence", ""))[:300]})
        if hit:
            pass
        elif gap:
            out.test_gaps += 1
        elif sev not in SERIOUS:
            out.other_minor += 1
        elif case.bug is None:
            out.false_serious += 1
        else:
            out.other_serious += 1
        p = probability(f.get("confidence"))
        # Scored only where the truth is known: a hit (1), or a serious finding on a clean control (0).
        if p is not None and (hit or (case.bug is None and sev in SERIOUS and not gap)):
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
    if repeat < 1:
        raise DaedalusError("--repeat must be at least 1")
    if not cases:
        raise DaedalusError("no cases to run")
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
        "false_serious_per_clean_review": round(sum(r.false_serious for r in clean) / len(clean), 4) if clean else None,
        "other_serious": sum(r.other_serious for r in planted),
        "test_gaps": sum(r.test_gaps for r in done),
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
    s = {k: ("n/a" if v is None else v) for k, v in s.items()}
    lines = [
        f"reviewer {s['reviewer']}{' (lite)' if s['lite'] else ''} · {s['reviews']} review(s)"
        + (f", {s['failed']} failed" if s["failed"] else ""),
        f"  recall: caught {s['caught']} of {s['planted']} planted bugs ({pct(s['recall'])})",
        f"  false alarms: {s['clean_cases_with_false_alarm']} of {s['clean']} clean changes drew a MAJOR+ finding "
        f"({s['false_serious_per_clean_review']} per clean change)",
        f"  also: {s['other_serious']} MAJOR+ finding(s) on planted cases about something else (may be real: see "
        f"`details`), {s['test_gaps']} saying tests are missing (the cases have none, by design)",
        f"  calibration: findings Brier {fb['brier'] if fb['brier'] is not None else 'n/a'} over {fb['n']}"
        f" · residual risk Brier {rb['brier'] if rb['brier'] is not None else 'n/a'} over {rb['n']} (0 is perfect)",
        f"  cost ${s['cost']:.4f} ({s['cost_per_review']} per review, {'/'.join(s['cost_basis'])})"
        f" · {s['seconds_per_review']}s per review",
    ]
    missed = [c["case"] for c in s["cases"] if c["planted"] and c["status"] == "COMPLETED" and not c["caught"]]
    if missed:
        lines.append("  missed: " + ", ".join(dict.fromkeys(missed)))
    noisy = [c["case"] for c in s["cases"] if c["false_serious"]]
    if noisy:
        lines.append("  false alarms on: " + ", ".join(dict.fromkeys(noisy)))
    return lines
