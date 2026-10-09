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
