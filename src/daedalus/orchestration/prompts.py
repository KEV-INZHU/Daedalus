"""Role prompts: a skill's instructions plus the authoritative run context.

Skills are instruction packages, not enforcement (spec §2.3). The same files
are installed into harnesses for inline use and rendered here for agents that
Daedalus launches itself. Nothing an agent returns is trusted beyond the
structured fields Ariadne validates.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from daedalus.core.contracts import TaskContract

SKILLS_DIR = Path(__file__).resolve().parent.parent / "data" / "skills"

ROLE_SKILLS = {
    "brunel": "brunel-builder",
    "socrates": "socrates-validator",
    "mozi": "mozi-simplicity-review",
    "aristotle": "aristotle-quality-review",
    "james": "james-functionality-review",
    "plato": "plato-arbitration",
    "vitruvius": "vitruvius-planner",
    "gate": "daedalus-gate",
}

REVIEW_OUTPUT = """## Output

Review the **actual diff and evidence** below, not anyone's summary of them. Critique the artifact,
not the author. Every finding needs concrete evidence (file, line, input, observed behaviour) and must
connect to the contract, a real risk, project conventions, or measurable cost. Preferences are ADVISORY.

Severity: BLOCKER (must not be accepted as is), MAJOR (a real defect or risk; should be fixed),
MINOR (worth fixing), ADVISORY (optional). A clean review is not proof of correctness.

If earlier findings from your perspective are listed and the candidate now fixes them, list their ids
in `resolves`. Do not edit any files.

End your reply with exactly one fenced JSON block:

```json
{
  "summary": "one paragraph",
  "findings": [
    {"title": "...", "evidence": "...", "severity": "BLOCKER|MAJOR|MINOR|ADVISORY",
     "required_change": "...", "verification": "how to confirm the fix"}
  ],
  "resolves": []
}
```
"""


def skill_body(role: str) -> str:
    text = (SKILLS_DIR / ROLE_SKILLS[role] / "SKILL.md").read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4 :]
    return text.strip()


def contract_block(c: TaskContract) -> str:
    lines = [
        f"## Contract (run {c.run_id}, version {c.contract_version})",
        "",
        f"**Objective:** {c.objective}",
        "",
        "**Acceptance criteria:**",
    ]
    for crit in c.acceptance_criteria:
        via = f" (verified by checks: {', '.join(crit.checks)})" if crit.checks else " (human attestation)"
        lines.append(f"- {crit.criterion_id}: {crit.description}{via}")
    lines += ["", f"**Scope (you may only change these paths):** {', '.join(c.scope)}"]
    if c.invariants:
        lines += ["", "**Invariants:**", *[f"- {i}" for i in c.invariants]]
    if c.hard_constraints:
        lines += ["", "**Hard constraints:**", *[f"- {h}" for h in c.hard_constraints]]
    if c.design_decisions:
        lines += ["", "**Design decisions:**", *[f"- {d}" for d in c.design_decisions]]
    lines += ["", f"**Required checks:** {', '.join(c.required_checks) or 'none'}"]
    if c.verification_plan:
        lines += ["", f"**Verification plan:** {c.verification_plan}"]
    return "\n".join(lines)


def builder_prompt(c: TaskContract, feedback: list[str], round_no: int) -> str:
    parts = [skill_body("brunel"), "", contract_block(c)]
    if feedback:
        parts += [
            "",
            f"## Feedback from round {round_no - 1}",
            "",
            "The previous candidate was not accepted. Address every item:",
            "",
            *[f"- {f}" for f in feedback],
        ]
    return "\n".join(parts)


PLAN_OUTPUT = """## Your task: plan, do not implement

Up to {cap} workers can build in parallel, each in an isolated copy of the repository. Split the work
into packages **only where they are genuinely independent**: disjoint files, stable interfaces between
them. If the work is small or tightly coupled, return a single package. Do not edit any files.

Rules Daedalus enforces mechanically (an invalid plan is rejected and one Builder does everything):
- `owned_paths` are path globs; no two packages may own overlapping paths, and every change a worker
  makes must fall inside its own package's paths.
- `depends_on` lists packages whose results a package needs; they are integrated first.

End your reply with exactly one fenced JSON block:

```json
{{"packages": [
  {{"id": "api", "description": "what this package does", "owned_paths": ["src/api/**"], "depends_on": []}}
]}}
```
"""


def plan_prompt(c: TaskContract, cap: int) -> str:
    return "\n".join([skill_body("brunel"), "", PLAN_OUTPUT.format(cap=cap), "", contract_block(c)])


def worker_prompt(c: TaskContract, package: dict[str, Any]) -> str:
    owned = ", ".join(package.get("owned_paths", ()))
    return "\n".join(
        [
            skill_body("brunel"),
            "",
            contract_block(c),
            "",
            f"## Your package: {package.get('task_id')}",
            "",
            str(package.get("description") or ""),
            "",
            f"You may change **only** these paths: {owned}. Other packages, built in parallel, own the rest;",
            "a change outside your paths gets your whole package rejected.",
        ]
    )


ARBITRATION_OUTPUT = """## Your task

A finding has been disputed. Decide whether it stands. **Uphold** it if the candidate really must change
to satisfy the contract, an invariant, a real risk or the project's conventions. **Overrule** it if the
finding is mistaken, out of scope, or a preference that does not justify its severity. Judge the actual
diff and evidence below, not who argued more confidently. You cannot waive checks, approvals or risk
floors, and you do not decide acceptance. Do not edit any files.

End your reply with exactly one fenced JSON block:

```json
{"decision": "uphold", "rationale": "why, citing evidence", "reversal_condition": "what would change your mind"}
```
"""


def arbitration_prompt(
    c: TaskContract,
    finding: dict[str, Any],
    disputes: list[dict[str, Any]],
    all_findings: list[dict[str, Any]],
    checks: dict[str, Any],
    diff: str,
) -> str:
    parts = [skill_body("plato"), "", ARBITRATION_OUTPUT, "", contract_block(c), "", "## Disputed finding", ""]
    parts += [
        f"- {finding['id']} [{finding['severity']}] from {finding['perspective']}: {finding['title']}",
        f"  Evidence: {finding['evidence']}",
        f"  Required change: {finding['required_change']}",
        "",
        "## The dispute",
        "",
        *[f"- {d['by']}: {d['reason']}" for d in disputes],
        "",
        "## Every other open finding, from every perspective",
        "",
        *(
            [
                f"- {f['id']} [{f['severity']}] {f['perspective']}: {f['title']}. Evidence: {f['evidence'][:400]}"
                for f in all_findings
            ]
            or ["- none"]
        ),
        "",
        "## Verification evidence",
        "",
    ]
    parts += [f"- {cid}: {st} — {detail}" for cid, (st, detail) in checks.items()] or ["- none"]
    parts += ["", "## Candidate diff", "", "```diff", diff or "(no changes)", "```"]
    return "\n".join(parts)


FIRST_PASS_INTRO = """## This is the only full review this candidate gets

Later rounds review only what changes after this one, so anything you leave out now is unlikely to be
looked at again. Report **every** issue you find in this pass, MINOR edge cases included, rather than
only the most important few."""

REREVIEW_INTRO = """## Full re-review

An earlier review of this run could not be extended to just the change, so this one covers the whole
candidate again. Report every issue you find, MINOR edge cases included."""

SWEEP = """Work through the sweep below for each changed function or file before you write the result; skip an
item only when it cannot apply.

1. **Inputs at the edges**: empty, None or missing, zero and negative, very large, duplicates, unicode,
   paths with `..`, absolute paths or symlinks, malformed files.
2. **Failure paths**: every error, exception, timeout and non-zero exit. What is left behind (partial
   writes, open handles, half-applied state), and is the error message accurate and actionable?
3. **State and ordering**: re-entry, retries, crashes between two steps, concurrent callers, stale or
   cached data, operations that are not idempotent.
4. **Contract and interfaces**: every acceptance criterion and invariant, callers of changed functions,
   changed defaults, backward compatibility of files and formats already on disk.
5. **Trust and security**: injection (shell, YAML, Markdown), path traversal, secrets, privilege, and
   anything a user, a commit or an agent controls that the code treats as trusted.
6. **Tests**: does each criterion and each failure path above have a test that would fail without the
   change? A test that can pass trivially is a finding.

Before the JSON block, list the areas you swept in one line each, so coverage is visible.
"""


def _review_frame(perspective: str, task: str, c: TaskContract, checks: dict[str, Any],
                  prior_findings: list[dict[str, str]]) -> list[str]:
    """The parts every review prompt shares: role, output format, task, contract, evidence, prior findings."""
    parts = [skill_body(perspective), "", REVIEW_OUTPUT, "", task, "", contract_block(c), "",
             "## Verification evidence", ""]
    parts += [f"- {cid}: {st} — {detail}" for cid, (st, detail) in checks.items()] or ["- none"]
    if prior_findings:
        parts += ["", "## Your earlier open findings", ""]
        parts += [f"- {f['id']} [{f['severity']}] {f['title']}: {f['required_change']}" for f in prior_findings]
    return parts


DELTA_OUTPUT = """## Your task: review what changed since your last review

You already reviewed this run's candidate {old}. The diff below is **only** what changed since then.
Review it with the same care and the same checklist as a full review, including how it interacts with
code it touches: you may read any file in the repository. Report findings caused by this change, and
confirm which of your earlier open findings it resolves (list their ids in `resolves`). Do not re-review
unchanged code for new issues; the earlier review covered it. Do not edit any files.
"""




def review_prompt(
    perspective: str,
    c: TaskContract,
    diff: str,
    checks: dict[str, Any],
    prior_findings: list[dict[str, str]],
    first_pass: bool = True,
) -> str:
    """A full review. `first_pass` is False for a full re-review (one that could not be a delta)."""
    task = (FIRST_PASS_INTRO if first_pass else REREVIEW_INTRO) + "\n\n" + SWEEP
    parts = _review_frame(perspective, task, c, checks, prior_findings)
    parts += ["", "## Candidate diff", "", "```diff", diff or "(no changes)", "```"]
    return "\n".join(parts)


def delta_review_prompt(
    perspective: str,
    c: TaskContract,
    inter_diff: str,
    touched: list[str],
    old_candidate: str,
    checks: dict[str, Any],
    prior_findings: list[dict[str, str]],
) -> str:
    task = DELTA_OUTPUT.format(old=old_candidate[:12]) + "\n" + SWEEP
    parts = _review_frame(perspective, task, c, checks, prior_findings)
    parts += ["", f"## Changed since your last review ({len(touched)} file(s))", "", "```diff",
              inter_diff or "(no textual changes)", "```"]
    return "\n".join(parts)


FACTORY_PLAN_OUTPUT = """## Your task: plan work orders, do not implement

Plan at most {max_orders} orders. Order ids are lowercase slugs (letters, digits, `-`); they name git
branches. `depends_on` may name orders in this plan or the existing orders listed below. Higher
`priority` runs first among orders that are ready. Do not edit any files.

End your reply with exactly one fenced JSON block:

```json
{{"summary": "how the plan breaks the goal down, and anything left out",
  "orders": [
    {{"id": "api-keys", "objective": "what to build, in one or two sentences",
      "acceptance_criteria": ["a behaviour a test can decide"],
      "scope": ["src/api/**", "tests/api/**"],
      "depends_on": [], "priority": 0}}
  ]}}
```
"""


def factory_plan_prompt(goal: str, existing: list[dict[str, Any]], checks: list[str], max_orders: int) -> str:
    parts = [skill_body("vitruvius"), "", FACTORY_PLAN_OUTPUT.format(max_orders=max_orders), "", "## The goal", ""]
    parts += [goal.strip(), "", "## Checks every order must pass", ""]
    parts += [f"- {c}" for c in checks] or ["- none configured"]
    parts += ["", "## Existing orders", ""]
    parts += [f"- {o['id']} ({o['status']}): {o['objective']}" for o in existing] or ["- none"]
    return "\n".join(parts)
