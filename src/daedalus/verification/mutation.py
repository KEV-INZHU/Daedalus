"""Test strength: do the tests notice when the changed code is broken on purpose?

The gate is only as good as the tests it runs. `daedalus mutate -- <test command>` makes one small,
deliberate break at a time (a mutant) on the Python lines the change touched, runs the tests against
each mutant in a temporary copy of the tree, and reports the mutants the tests let through.

    ==  <-> !=     <  <-> >=     >  <-> <=     is <-> is not     in <-> not in
    and <-> or     +  <-> -      True <-> False     not x -> x     return x -> return None

Changed lines are those differing from the base: `--base`, else $DAEDALUS_BASE_REVISION (which the
gate sets for every check: the run's base revision), else HEAD. Test files themselves are never
mutated. The working tree is never touched: everything happens in a throwaway copy.

As a check (results depend on the run's base, so do not reuse them across runs):

    checks:
      mutation: {command: [daedalus, mutate, --, python, -m, pytest, -q, -x], mandatory: false, cache: false}
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

ENV_BASE = "DAEDALUS_BASE_REVISION"
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_TEST_FILE = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$|conftest\.py$)")
_SWAP_CMP = {
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.GtE: ast.Lt, ast.Gt: ast.LtE, ast.LtE: ast.Gt,
    ast.Is: ast.IsNot, ast.IsNot: ast.Is, ast.In: ast.NotIn, ast.NotIn: ast.In,
}  # fmt: skip


@dataclass(frozen=True)
class Mutant:
    path: str  # relative to the repository
    line: int
    what: str


@dataclass(frozen=True)
class Result:
    mutants: tuple[Mutant, ...]
    survived: tuple[Mutant, ...]
    baseline_error: str = ""

    @property
    def score(self) -> float | None:
        return None if not self.mutants else 1 - len(self.survived) / len(self.mutants)


def _git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    return r.stdout if r.returncode == 0 else ""


class MutationError(Exception):
    pass


def changed_lines(root: Path, base: str) -> dict[str, set[int]]:
    """Python source files (not tests) -> the line numbers that differ from `base`. An unknown base is
    an error, never an empty change: "nothing to mutate" must mean nothing changed."""
    probe = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        raise MutationError(f"base revision {base!r} is not a commit in {root}")
    out: dict[str, set[int]] = {}
    current: str | None = None
    for line in _git(root, "diff", "-U0", "--no-color", "--no-ext-diff", base, "--", "*.py").splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else None
        elif current and (m := _HUNK.match(line)):
            start, count = int(m[1]), int(m[2] or 1)
            out.setdefault(current, set()).update(range(start, start + count))
    for rel in _git(root, "ls-files", "--others", "--exclude-standard", "--", "*.py").splitlines():
        try:
            n = len((root / rel).read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeDecodeError):
            continue
        out[rel] = set(range(1, n + 1))
    return {p: ls for p, ls in out.items() if ls and not _TEST_FILE.search(p)}


def _sites(tree: ast.AST, lines: set[int]) -> Iterator[tuple[ast.AST, str, Callable[[], ast.AST | None]]]:
    """Every mutation available on `lines`, in a fixed order: (node, description, apply). `apply`
    changes the node in place and returns a replacement node when the node itself is replaced."""
    for node in ast.walk(tree):
        if getattr(node, "lineno", None) not in lines:
            continue
        if isinstance(node, ast.Compare):
            for i, op in enumerate(node.ops):
                if type(op) in _SWAP_CMP:
                    new = _SWAP_CMP[type(op)]

                    def swap(node=node, i=i, new=new):
                        node.ops[i] = new()

                    yield node, f"{type(op).__name__} -> {new.__name__}", swap
        elif isinstance(node, ast.BoolOp):
            flip = ast.Or if isinstance(node.op, ast.And) else ast.And

            def boolop(node=node, flip=flip):
                node.op = flip()

            yield node, f"{type(node.op).__name__.lower()} -> {flip.__name__.lower()}", boolop
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
            flip = ast.Sub if isinstance(node.op, ast.Add) else ast.Add

            def binop(node=node, flip=flip):
                node.op = flip()

            yield node, f"{type(node.op).__name__} -> {flip.__name__}", binop
        elif isinstance(node, ast.Constant) and isinstance(node.value, bool):

            def const(node=node):
                node.value = not node.value

            yield node, f"{node.value} -> {not node.value}", const
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            yield node, "not x -> x", (lambda node=node: node.operand)
        elif (
            isinstance(node, ast.Return)
            and node.value is not None
            and not (isinstance(node.value, ast.Constant) and node.value.value is None)
        ):

            def ret(node=node):
                node.value = ast.Constant(None)

            yield node, "return x -> return None", ret


class _Replace(ast.NodeTransformer):
    def __init__(self, target: ast.AST, replacement: ast.AST):
        self.target, self.replacement = target, replacement

    def generic_visit(self, node: ast.AST) -> ast.AST:
        return self.replacement if node is self.target else super().generic_visit(node)


def sites_of(source: str, lines: set[int]) -> list[tuple[int, str]]:
    """(line, description) of each mutation available on `lines`, in a fixed order. Cheap: one parse."""
    try:
        return [(node.lineno, what) for node, what, _ in _sites(ast.parse(source), lines)]
    except SyntaxError:
        return []


def build_mutant(source: str, lines: set[int], k: int) -> str:
    """`source` with only its k-th mutation (in `sites_of` order) applied."""
    tree = ast.parse(source)  # a fresh tree per mutant: mutations never stack
    for i, (node, _what, apply) in enumerate(_sites(tree, lines)):
        if i == k:
            replacement = apply()
            if replacement is not None:
                tree = _Replace(node, replacement).visit(tree)
            return ast.unparse(ast.fix_missing_locations(tree)) + "\n"
    raise IndexError(k)


def mutants_of(source: str, path: str, lines: set[int]) -> list[tuple[Mutant, str]]:
    """Each mutant of `source` on `lines`, with the mutated source (every one: for small inputs)."""
    return [
        (Mutant(path, line, what), build_mutant(source, lines, k))
        for k, (line, what) in enumerate(sites_of(source, lines))
    ]


def _copy_tree(root: Path, dest: Path) -> None:
    for rel in _git(root, "ls-files", "--cached", "--others", "--exclude-standard").splitlines():
        src = root / rel
        if src.is_file():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / rel)


def _passes(cmd: list[str], cwd: Path, timeout_s: float) -> bool:
    try:
        return (
            subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout_s, check=False).returncode == 0
        )
    except subprocess.TimeoutExpired:
        return False  # a mutant that hangs the tests is caught


def run(
    root: Path,
    test_cmd: list[str],
    *,
    base: str | None = None,
    max_mutants: int = 30,
    timeout_s: float = 300.0,
) -> Result:
    base = base or os.environ.get(ENV_BASE) or "HEAD"
    if max_mutants < 1:
        raise MutationError("max_mutants must be at least 1")
    # Enumerate cheaply, sample, then build only the chosen mutants.
    sources: dict[str, tuple[str, set[int]]] = {}
    sites: list[tuple[str, int, Mutant]] = []  # (path, index within its file, mutant)
    for rel, lines in sorted(changed_lines(root, base).items()):
        try:
            source = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        sources[rel] = (source, lines)
        sites += [(rel, k, Mutant(rel, line, what)) for k, (line, what) in enumerate(sites_of(source, lines))]
    if len(sites) > max_mutants:  # an even spread over the change, the same every time
        step = len(sites) / max_mutants
        sites = [sites[int(i * step)] for i in range(max_mutants)]
    if not sites:
        return Result((), ())
    mutants = tuple(m for _, _, m in sites)
    with tempfile.TemporaryDirectory(prefix="daedalus-mutate-") as tmp:
        copy = Path(tmp)
        _copy_tree(root, copy)
        if not _passes(test_cmd, copy, timeout_s):
            return Result(mutants, (), "the tests fail without any mutation")
        survived = []
        for rel, k, mutant in sites:
            source, lines = sources[rel]
            target = copy / rel
            original = target.read_bytes()
            try:
                target.write_text(build_mutant(source, lines, k), encoding="utf-8")
                if _passes(test_cmd, copy, timeout_s):
                    survived.append(mutant)
            finally:
                target.write_bytes(original)
    return Result(mutants, tuple(survived))
