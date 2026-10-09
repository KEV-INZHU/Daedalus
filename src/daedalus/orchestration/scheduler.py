"""Work packages, plan validation and dispatch eligibility (spec §12, §14).

Brunel proposes packages; Ariadne checks them mechanically before anything is
dispatched. Ownership overlap is detected conservatively: two glob sets are
treated as overlapping whenever one literal prefix contains the other, so a
false "conflict" is possible and a missed conflict is not.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from daedalus.core.errors import IntegrationError
from daedalus.core.paths import match, normalize
from daedalus.core.run import RunState
from daedalus.core.state_machine import WORKER_TERMINAL, ExecutionState, WorkerState

_GLOB_CHARS = "*?["


@dataclass(frozen=True)
class WorkPackage:
    task_id: str
    description: str
    owned_paths: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    provides: tuple[str, ...] = ()
    consumes: dict[str, str] = field(default_factory=dict)
    deadline_s: float = 3600.0
    max_attempts: int = 1
    in_place: bool = False  # single-worker baseline: edits the authoritative tree directly

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WorkPackage:
        if not d.get("task_id"):
            raise IntegrationError("work package needs a task_id")
        return cls(
            task_id=str(d["task_id"]),
            description=str(d.get("description", "")),
            owned_paths=tuple(normalize(p) for p in d.get("owned_paths", ())),
            depends_on=tuple(d.get("depends_on", ())),
            provides=tuple(d.get("provides", ())),
            consumes=dict(d.get("consumes", {})),
            deadline_s=float(d.get("deadline_s", 3600)),
            max_attempts=int(d.get("max_attempts", 1)),
            in_place=bool(d.get("in_place", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "description": self.description,
            "owned_paths": list(self.owned_paths),
            "depends_on": list(self.depends_on),
            "provides": list(self.provides),
            "consumes": dict(self.consumes),
            "deadline_s": self.deadline_s,
            "max_attempts": self.max_attempts,
            "in_place": self.in_place,
        }

    def owns(self, path: str) -> bool:
        return any(match(path, p) for p in self.owned_paths)


def _prefix(pattern: str) -> str:
    """Literal directory prefix before the first glob character."""
    cut = min((pattern.find(c) for c in _GLOB_CHARS if c in pattern), default=len(pattern))
    lit = pattern[:cut]
    return lit if cut == len(pattern) else lit[: lit.rfind("/") + 1]


def patterns_overlap(a: str, b: str) -> bool:
    if match(a, b) or match(b, a):
        return True
    pa, pb = _prefix(a), _prefix(b)
    if pa == a and pb == b:
        return a == b  # two literal paths
    if pa == a:
        return match(a, b)
    if pb == b:
        return match(b, a)
    return pa.startswith(pb) or pb.startswith(pa)


def validate_plan(
    packages: Sequence[WorkPackage], interfaces: dict[str, str], existing: Sequence[WorkPackage] = ()
) -> list[str]:
    """Return every mechanical conflict; an empty list means the plan may dispatch."""
    conflicts: list[str] = []
    all_pkgs = list(existing) + list(packages)
    ids = [p.task_id for p in all_pkgs]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        conflicts.append(f"duplicate task id {dup!r}")
    known = set(ids)
    for p in packages:
        if not p.owned_paths:
            conflicts.append(f"{p.task_id}: no owned_paths declared")
        for dep in p.depends_on:
            if dep not in known:
                conflicts.append(f"{p.task_id}: depends on unknown task {dep!r}")
        for iface, version in p.consumes.items():
            if iface not in interfaces:
                conflicts.append(f"{p.task_id}: consumes undeclared interface {iface!r}")
            elif interfaces[iface] != version:
                conflicts.append(
                    f"{p.task_id}: consumes {iface}@{version} but the plan declares {iface}@{interfaces[iface]}"
                )
    providers: dict[str, str] = {}
    for p in all_pkgs:
        for iface in p.provides:
            if iface in providers:
                conflicts.append(f"interface {iface!r} provided by both {providers[iface]} and {p.task_id}")
            providers[iface] = p.task_id
    n_existing = len(existing)
    for i, a in enumerate(all_pkgs):
        for j in range(max(i + 1, n_existing), len(all_pkgs)):
            b = all_pkgs[j]
            hits = [f"{x} ~ {y}" for x in a.owned_paths for y in b.owned_paths if patterns_overlap(x, y)]
            if hits:
                conflicts.append(f"ownership overlap between {a.task_id} and {b.task_id}: {hits[0]}")
    deps = {p.task_id: set(p.depends_on) for p in all_pkgs}
    if _has_cycle(deps):
        conflicts.append("dependency cycle in plan")
    return conflicts


def _has_cycle(deps: dict[str, set[str]]) -> bool:
    state: dict[str, int] = {}

    def visit(n: str) -> bool:
        if state.get(n) == 1:
            return True
        if state.get(n) == 2:
            return False
        state[n] = 1
        if any(visit(d) for d in deps.get(n, ()) if d in deps):
            return True
        state[n] = 2
        return False

    return any(visit(n) for n in deps)


def live_packages(state: RunState) -> list[WorkPackage]:
    """Packages that still own their paths (not terminal, not yet integrated)."""
    return [
        WorkPackage.from_dict(w.package)
        for w in state.workers.values()
        if w.state not in WORKER_TERMINAL and not w.integrated
    ]


def dispatch_blockers(state: RunState, task_id: str) -> list[str]:
    """Why `task_id` may not be dispatched now (empty list: eligible)."""
    out: list[str] = []
    w = state.workers.get(task_id)
    if w is None:
        return [f"unknown task {task_id!r}"]
    pkg = WorkPackage.from_dict(w.package)
    if state.execution is not ExecutionState.RUNNING or state.cancel_requested:
        out.append(f"run is {state.execution.value}{' (cancelling)' if state.cancel_requested else ''}")
    if w.state is not WorkerState.PENDING:
        out.append(f"task is {w.state.value}, not PENDING")
    if w.contract_version != state.contract.contract_version:
        out.append("task was planned under an older contract version")
    for dep in pkg.depends_on:
        d = state.workers.get(dep)
        if d is None or not d.integrated:
            out.append(f"prerequisite {dep} has not been integrated")
    if len(state.active_workers) >= state.contract.budget.max_workers:
        out.append(f"concurrency limit {state.contract.budget.max_workers} reached")
    if w.attempts >= pkg.max_attempts:
        out.append(f"task retry budget exhausted ({w.attempts}/{pkg.max_attempts})")
    if state.attempts_used >= state.contract.budget.max_attempts:
        out.append("run attempt budget exhausted")
    return out
