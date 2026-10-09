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


def review_prompt(
    perspective: str,
    c: TaskContract,
    diff: str,
    checks: dict[str, Any],
    prior_findings: list[dict[str, str]],
) -> str:
    parts = [skill_body(perspective), "", REVIEW_OUTPUT, "", contract_block(c), "", "## Verification evidence", ""]
    parts += [f"- {cid}: {st} — {detail}" for cid, (st, detail) in checks.items()] or ["- none"]
    if prior_findings:
        parts += ["", "## Your earlier open findings", ""]
        parts += [f"- {f['id']} [{f['severity']}] {f['title']}: {f['required_change']}" for f in prior_findings]
    parts += ["", "## Candidate diff", "", "```diff", diff or "(no changes)", "```"]
    return "\n".join(parts)
