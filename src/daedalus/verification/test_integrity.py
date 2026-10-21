"""Test-tampering detector (deterministic, no LLM).

A passing check proves little if the agent changed the tests that define "passing". The classic
cheat: a failing test is replaced with `assert True`, or deleted, or skipped, and the suite goes
green. This module compares the base revision with the candidate for every changed *test* file
and reports the ways a test can get weaker:

- removed tests: a test function that existed on the base and exists nowhere in the candidate,
  and test files that were deleted or emptied
- removed assertions: a net drop in assertions, or a test that no longer asserts anything
- newly added skip or focus markers (`pytest.mark.skip`, `it.skip`, `t.Skip`, `#[ignore]`, ...)
- trivially true assertions (`assert True`, `expect(true).toBe(true)`, ...)
- a changed expected literal in `assert f(x) == <literal>` (Python)

Python is analysed with `ast`; JS/TS, Go and Rust with regex heuristics over the base and
candidate text. Tests that merely move between files, are renamed, or are newly added are not
flagged: matching is by test name across all changed test files.

It fails closed: a changed test file that cannot be analysed (syntax error, unreadable, too
large) is itself a signal. A signal never decides anything alone: it raises the risk tier (so an
independent review is required) and puts a specific reason on the decision; a human can
acknowledge a deliberate test change with a risk exception.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import re
import shlex
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Any

from daedalus.core.paths import MAKEFILE_NAMES, match, match_any, normalize

# Overridable in policy (`test_integrity.test_globs`). `*.test.js` and friends are listed one by
# one because the path matcher has no brace expansion.
DEFAULT_TEST_GLOBS: tuple[str, ...] = (
    "**/test_*.py",
    "**/*_test.py",
    "**/*_tests.py",
    "**/tests.py",
    "**/test.py",
    "**/tests/**",
    "**/*.test.js",
    "**/*.test.ts",
    "**/*.test.jsx",
    "**/*.test.tsx",
    "**/*.spec.*",
    "**/*_test.go",
    "**/__tests__/**",
    "**/test/**",
    "**/*.test.mjs",
    "**/*.test.cjs",
)

# File names a test runner collects on its own. A test that "moved" to any other file (a helper
# under tests/, say) is not running any more, so only these count as a destination.
COLLECTED_NAMES: tuple[str, ...] = ("test_*.py", "*_test.py", "*_tests.py", "tests.py", "test.py", "*.test.*", "*.spec.*", "*_test.go")

# Test-runner configuration: it can disable or skip tests without touching a single test file.
RUNNER_CONFIG_GLOBS: tuple[str, ...] = (
    "**/conftest.py", "**/pytest.ini", "**/setup.cfg", "**/tox.ini", "**/pyproject.toml",
    "**/jest.config.*", "**/vitest.config.*", "**/package.json", "**/noxfile.py",
    *(f"**/{name}" for name in MAKEFILE_NAMES),
)

MAX_FILE_BYTES = 1_000_000
MAX_HUNK_CHARS = 3000
MAX_HUNKS_CHARS = 16000

LANGS = {
    ".py": "python",
    ".js": "js", ".jsx": "js", ".ts": "js", ".tsx": "js", ".mjs": "js", ".cjs": "js",
    ".go": "go",
    ".rs": "rust",
}

# Signal kinds. `unanalyzable` means "could not tell", and counts as a signal.
REMOVED_FILE = "removed_test_file"
EMPTIED_FILE = "emptied_test_file"
REMOVED_TEST = "removed_test"
LOST_ASSERTIONS = "assertions_removed"
SKIP_ADDED = "skip_added"
FOCUS_ADDED = "focus_added"
TRIVIAL_ASSERT = "trivial_assert"
EXPECTED_CHANGED = "expected_changed"
UNANALYZABLE = "unanalyzable"
NEUTRALIZED = "assertions_neutralized"
RUNNER_CONFIG = "runner_config_changed"


@dataclass(frozen=True)
class Signal:
    kind: str
    path: str
    detail: str  # one clause, e.g. "'test_add' lost 1 assertion and now asserts True"

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "path": self.path, "detail": self.detail}


@dataclass(frozen=True)
class IntegrityReport:
    signals: tuple[Signal, ...] = ()
    analyzed: tuple[str, ...] = ()  # test files that were compared
    hunks: dict[str, str] = field(default_factory=dict, compare=False)  # path -> unified diff

    @property
    def flagged(self) -> bool:
        return bool(self.signals)

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(s.path for s in self.signals))

    def digests(self) -> tuple[str, ...]:
        """A stable digest per signal. A risk exception stores them to say which signals it covers."""
        return tuple(sorted({
            hashlib.sha256(f"{s.kind}\0{s.path}\0{s.detail}".encode()).hexdigest()[:16] for s in self.signals
        }))

    def summary(self) -> str:
        """`path: clause; clause | path: clause`, the body of the acceptance reason."""
        by_path: dict[str, list[str]] = {}
        for s in self.signals:
            by_path.setdefault(s.path, []).append(s.detail)
        shown = [f"{clean(p, 120)}: {'; '.join(d)}" for p, d in list(by_path.items())[:5]]
        more = len(by_path) - 5
        return " | ".join(shown) + (f" | (+{more} more file(s))" if more > 0 else "")

    def to_dict(self) -> dict[str, Any]:
        return {"flagged": self.flagged, "signals": [s.to_dict() for s in self.signals],
                "analyzed": list(self.analyzed)}

    def review_section(self) -> str:
        """Markdown for a review prompt: what was flagged, and the diffs of the flagged files."""
        if not self.flagged:
            return ""
        lines = [
            "## Test integrity signals: check these first",
            "",
            ("A deterministic scan found changes that may have weakened the tests. Decide for each whether it "
             "is a legitimate change that keeps the tests at least as strict, or whether it hides a defect "
             "(a failing test made to pass, a deleted or skipped test, an assertion removed or loosened). "
             "A weakened test that hides a real defect is a BLOCKER."),
            "",
        ]
        lines += [f"- `{clean(s.path, 120)}` ({s.kind}): {s.detail}" for s in self.signals]
        total = 0
        for path in self.paths:
            hunk = self.hunks.get(path)
            if not hunk or total >= MAX_HUNKS_CHARS:
                continue
            total += len(hunk)
            ticks = max((len(m) for m in re.findall(r"`+", hunk)), default=0)
            fence = "`" * max(3, ticks + 1)  # longer than any run inside the hunk: it cannot close the fence
            lines += ["", f"Flagged change to `{clean(path, 120)}`:", "", f"{fence}diff", hunk, fence]
        return "\n".join(lines)


# ---------------------------------------------------------------------------- classification
def is_test_path(path: str, globs: tuple[str, ...] | list[str] = DEFAULT_TEST_GLOBS) -> bool:
    return match_any(path, globs)


def language(path: str) -> str | None:
    return LANGS.get(PurePosixPath(path).suffix.lower())


def is_collected(path: str) -> bool:
    """Whether a test runner picks this file up by its name alone (Rust is judged by its content)."""
    if language(path) == "rust":
        return True
    return any(fnmatch(PurePosixPath(path).name, pat) for pat in COLLECTED_NAMES)


def clean(text: object, limit: int = 80) -> str:
    """Agent-controlled text (test names, expressions, paths) made safe to quote in a reason or a
    prompt: no backticks, no line breaks or control characters, capped in length."""
    out = "".join(" " if ord(c) < 32 else c for c in str(text)).replace("`", "'")
    out = " ".join(out.split())
    return out if len(out) <= limit else out[: limit - 3] + "..."


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


# -------------------------------------------------------------------------------- Python (ast)
_SKIP_NAMES = frozenset({"skip", "skipif", "xfail", "skipIf", "skipUnless", "expectedFailure", "skipTest", "importorskip", "SkipTest"})
_ASSERT_CALL = re.compile(r"^(assert[A-Z_]\w*|assert_\w+)$")  # unittest/numpy style, not `assert_`-less helpers


@dataclass
class PyTest:
    name: str  # `test_x` or `Class.test_x`
    asserts: int = 0  # real assertions
    trivial: list[str] = field(default_factory=list)  # source of each trivially true assert
    expected: dict[str, list[str]] = field(default_factory=dict)  # compared expression -> literals
    skips: int = 0
    neutralized: list[str] = field(default_factory=list)  # why some assertions here can never fail


@dataclass
class PyFile:
    tests: dict[str, PyTest]
    asserts: int
    skips: int  # all skip markers in the file, including module level and on classes
    trivial: int


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _truthy_const(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and bool(node.value)


def _falsy_const(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and not node.value


def _trivial_expr(test: ast.AST) -> bool:
    """`True`, `1`, `not False`, `1 == 1`, `x == x`, `x or True`, `(x, "msg")`: an assertion that
    cannot fail."""
    if _truthy_const(test):
        return True
    if isinstance(test, ast.BoolOp):
        if isinstance(test.op, ast.Or):
            return any(_trivial_expr(v) for v in test.values)
        return all(_trivial_expr(v) for v in test.values)
    if isinstance(test, (ast.Tuple, ast.List, ast.Set)):
        return bool(test.elts)  # a non-empty literal container is always truthy
    if isinstance(test, ast.Dict):
        return bool(test.keys)
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and _falsy_const(test.operand):
        return True
    if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], (ast.Eq, ast.Is, ast.GtE, ast.LtE)):
        return ast.dump(test.left) == ast.dump(test.comparators[0])
    return False


def _literal(node: ast.AST) -> str | None:
    """Source of a literal expected value (numbers, strings, None, containers of literals)."""
    try:
        ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None
    return ast.unparse(node)


def _is_assertion(node: ast.AST) -> bool:
    if isinstance(node, ast.Assert):
        return True
    return isinstance(node, ast.Call) and bool(_ASSERT_CALL.match(_dotted(node.func).rsplit(".", 1)[-1]))


def _has_assertion(stmts: list[ast.stmt]) -> bool:
    return any(_is_assertion(n) for stmt in stmts for n in ast.walk(stmt))


_CATCH_ALL = ("AssertionError", "BaseException", "Exception")


def _names_catch_all(node: ast.AST | None) -> bool:
    """Whether an exception expression (`Exception`, `builtins.Exception`, a tuple holding one)
    covers AssertionError. A bare `except:` has no expression."""
    if node is None:
        return True
    if isinstance(node, ast.Tuple):
        return any(_names_catch_all(e) for e in node.elts)
    return _dotted(node).rsplit(".", 1)[-1] in _CATCH_ALL


def _swallows(handler: ast.ExceptHandler) -> bool:
    """An `except` that catches AssertionError (or everything) and does not re-raise it."""
    return _names_catch_all(handler.type) and not any(
        isinstance(n, ast.Raise) for st in handler.body for n in ast.walk(st)
    )


def _suppresses(item: ast.withitem) -> bool:
    """`contextlib.suppress(AssertionError)` / `pytest.raises(Exception)`: a `with` that absorbs the
    failure of an assertion inside it."""
    call = item.context_expr
    if not isinstance(call, ast.Call):
        return False
    last = _dotted(call.func).rsplit(".", 1)[-1]
    return last in ("suppress", "raises") and any(_names_catch_all(a) for a in call.args)


def _exits(stmt: ast.stmt) -> bool:
    """A statement after which nothing in the same block runs: return, raise, or an
    `if <truthy constant>:` whose body ends in one."""
    if isinstance(stmt, (ast.Return, ast.Raise)):
        return True
    return isinstance(stmt, ast.If) and _truthy_const(stmt.test) and bool(stmt.body) and _exits(stmt.body[-1])


def _neutralized(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Assertions that are present but can never fail: after an unconditional return/raise (or an
    `if <truthy constant>: return`), under `if <falsy constant>`, inside a `try` whose handler
    swallows AssertionError, or inside a `with` that suppresses it."""
    out = []
    for node in ast.walk(fn):
        for block in (getattr(node, "body", None), getattr(node, "orelse", None), getattr(node, "finalbody", None)):
            if not isinstance(block, list):
                continue
            for i, stmt in enumerate(block):
                if isinstance(stmt, ast.stmt) and _exits(stmt) and _has_assertion(block[i + 1 :]):
                    out.append("assertions after an unconditional return/raise")
                    break
        if isinstance(node, ast.If) and _falsy_const(node.test) and _has_assertion(node.body):
            out.append("assertions under `if <falsy constant>`")
        elif isinstance(node, ast.Try) and _has_assertion(node.body) and any(_swallows(h) for h in node.handlers):
            out.append("assertions inside a try whose handler swallows AssertionError")
        elif isinstance(node, (ast.With, ast.AsyncWith)) and any(_suppresses(i) for i in node.items) \
                and _has_assertion(node.body):
            out.append("assertions inside a `with` that suppresses AssertionError")
    return out


def _scan_function(fn: ast.AST, t: PyTest) -> None:
    for node in ast.walk(fn):
        if isinstance(node, ast.Assert):
            if _trivial_expr(node.test):
                t.trivial.append("assert " + ast.unparse(node.test))
            else:
                t.asserts += 1
                _expected_from_compare(node.test, t)
        elif isinstance(node, ast.Call):
            name = _dotted(node.func)
            last = name.rsplit(".", 1)[-1]
            args = node.args
            if last in ("assertTrue", "assertFalse") and args:
                trivial = _trivial_expr(args[0]) if last == "assertTrue" else _falsy_const(args[0])
                if trivial:
                    t.trivial.append(f"{last}({ast.unparse(args[0])})")
                else:
                    t.asserts += 1
            elif last in ("assertEqual", "assertEquals") and len(args) >= 2:
                if ast.dump(args[0]) == ast.dump(args[1]):
                    t.trivial.append(f"{last}({ast.unparse(args[0])}, {ast.unparse(args[1])})")
                else:
                    t.asserts += 1
                    lit = _literal(args[1])
                    if lit is not None:
                        t.expected.setdefault(ast.unparse(args[0]), []).append(lit)
            elif _ASSERT_CALL.match(last) or last in ("fail", "raises", "warns") and "pytest" in name:
                t.asserts += 1


def _expected_from_compare(test: ast.AST, t: PyTest) -> None:
    if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq):
        left, right = test.left, test.comparators[0]
        for expr, lit in ((left, right), (right, left)):
            src = _literal(lit)
            if src is not None and _literal(expr) is None:
                t.expected.setdefault(ast.unparse(expr), []).append(src)
                return


def _count_skips(tree: ast.AST) -> int:
    n = 0
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and node.attr in _SKIP_NAMES) or (
            isinstance(node, ast.Name) and node.id in ("skip", "skipif", "xfail", "expectedFailure", "SkipTest")
        ):
            n += 1
    return n


def parse_python(text: str) -> PyFile:
    tree = ast.parse(text)
    tests: dict[str, PyTest] = {}

    def visit(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                visit(node.body, f"{prefix}{node.name}.")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                t = PyTest(prefix + node.name)
                _scan_function(node, t)
                t.neutralized = _neutralized(node)
                t.skips = _count_skips(ast.Module(body=[node], type_ignores=[]))
                tests[t.name] = t

    visit(tree.body, "")
    return PyFile(
        tests=tests,
        asserts=sum(t.asserts for t in tests.values()),
        skips=_count_skips(tree),
        trivial=sum(len(t.trivial) for t in tests.values()),
    )


# ------------------------------------------------------------------- JS / TS, Go, Rust (regex)
_JS_NAME = re.compile(r"""\b(?:it|test)(?:\.\w+)*\s*\(\s*(['"`])(.+?)\1""")
_GO_NAME = re.compile(r"^func\s+(Test\w+)\s*\(", re.MULTILINE)
_RUST_NAME = re.compile(r"#\[(?:\w+::)*test(?:\([^)]*\))?\]\s*(?:#\[[^\]]*\]\s*)*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)")

_ASSERT_RE = {
    "js": re.compile(r"\bexpect\s*\(|\bassert(?:\.\w+)?\s*\(|\.should\b|\bt\.(?:is|deepEqual|true|false|truthy|falsy|throws)\s*\("),
    "go": re.compile(r"\bt\.(?:Error|Errorf|Fatal|Fatalf|Fail|FailNow)\s*\(|\b(?:assert|require)\.\w+\s*\("),
    "rust": re.compile(r"\b(?:debug_)?assert(?:_eq|_ne)?!|#\[should_panic"),
}
_TRIVIAL_RE = {
    "js": re.compile(
        r"expect\s*\(\s*(?:true|1)\s*\)\s*\.\s*(?:toBe|toEqual|toStrictEqual)\s*\(\s*(?:true|1)\s*\)"
        r"|expect\s*\(\s*true\s*\)\s*\.\s*toBeTruthy\s*\(\s*\)"
        r"|expect\s*\(\s*false\s*\)\s*\.\s*toBeFalsy\s*\(\s*\)"
        r"|\bassert(?:\.ok)?\s*\(\s*(?:true|1)\s*[,)]"
        r"|\bassert\.(?:equal|strictEqual)\s*\(\s*(true|1)\s*,\s*\1\s*\)"
    ),
    "go": re.compile(r"\b(?:assert|require)\.True\s*\(\s*t\s*,\s*true\s*\)|\bif\s+false\s*\{"),
    "rust": re.compile(r"\bassert!\s*\(\s*true\s*[,)]|\bassert_eq!\s*\(\s*(\w+|\d+)\s*,\s*\1\s*\)"),
}
_SKIP_RE = {
    "js": re.compile(r"\b(?:it|test|describe)\.(?:skip|todo|skipIf|runIf)\b|\bx(?:it|describe|test)\s*\("),
    "go": re.compile(r"\bt\.Skip(?:f|Now)?\s*\("),
    "rust": re.compile(r"#\[ignore\b"),
}
_FOCUS_RE = {
    "js": re.compile(r"\b(?:it|test|describe)\.only\b|\bf(?:it|describe)\s*\("),
}
_QUOTES = {"js": "'\"`", "go": "\"`'", "rust": '"'}


_REGEX_PREV = frozenset("(,=:[!&|?{};+-*%<>~^") | {""}


def _regex_end(text: str, i: int) -> int:
    """Index just past the regex literal that starts at `text[i]` (a `/`), or -1 when the line holds
    no closing `/` (then it is a division, or something the scanner cannot tell)."""
    j, n, in_class = i + 1, len(text), False
    while j < n and text[j] != "\n":
        ch = text[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "[":
            in_class = True
        elif ch == "]":
            in_class = False
        elif ch == "/" and not in_class:
            return j + 1
        j += 1
    return -1


def _code_lines(text: str, lang: str = "js") -> str:
    """The code without comments, so a commented-out marker does not count and a block-commented
    test does not hide. `//` and `/* */` inside a string literal or a JS regex literal (`/\\//`)
    are kept: reading them as comments would hide the code after them."""
    quotes = _QUOTES.get(lang, "'\"`")
    out: list[str] = []
    i, n = 0, len(text)
    last = ""  # the last significant character before i
    while i < n:
        c = text[i]
        if lang == "js" and c == "/" and not text.startswith(("//", "/*"), i) and last in _REGEX_PREV:
            end = _regex_end(text, i)
            if end != -1:
                out.append(text[i:end])
                i, last = end, "/"
                continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j == -1 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            end = n if j == -1 else j + 2
            out.append("\n" * text.count("\n", i, end))
            i = end
        elif c in quotes:
            j = i + 1
            while j < n and text[j] != c and (text[j] != "\n" or c == "`"):
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
            last = c
        else:
            out.append(c)
            i += 1
            if not c.isspace():
                last = c
    return "".join(out)


@dataclass
class TextFile:
    lang: str
    names: set[str]
    asserts: int
    trivial: int
    skips: int
    focus: int
    per_test: dict[str, int]  # test name -> real assertions (Go and Rust only)
    nonblank: bool


def _names(lang: str, text: str) -> set[str]:
    if lang == "js":
        return {m.group(2) for m in _JS_NAME.finditer(text)}
    if lang == "go":
        return set(_GO_NAME.findall(text))
    return set(_RUST_NAME.findall(text))


def _per_test_asserts(lang: str, text: str) -> dict[str, int]:
    """Assertions per test body for languages whose functions start at a recognisable line."""
    if lang == "go":
        starts = [(m.start(), m.group(1)) for m in re.finditer(r"^func\s+(\w+)\s*\(", text, re.MULTILINE)]
        test_names = set(_GO_NAME.findall(text))
    elif lang == "rust":
        starts = [(m.start(), m.group(1)) for m in re.finditer(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)", text, re.MULTILINE)]
        test_names = set(_RUST_NAME.findall(text))
    else:
        return {}
    out = {}
    for i, (pos, name) in enumerate(starts):
        if name not in test_names:
            continue
        end = starts[i + 1][0] if i + 1 < len(starts) else len(text)
        body = text[pos:end]
        out[name] = len(_ASSERT_RE[lang].findall(body)) - len(_TRIVIAL_RE[lang].findall(body))
    if lang == "rust":  # `#[should_panic]` sits above the fn, outside the body slice above
        for m in re.finditer(r"#\[should_panic[^\]]*\]\s*(?:#\[[^\]]*\]\s*)*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)", text):
            if m.group(1) in out:
                out[m.group(1)] += 1
    return out


def parse_text(lang: str, text: str) -> TextFile:
    code = _code_lines(text, lang)
    asserts = len(_ASSERT_RE[lang].findall(code)) if lang in _ASSERT_RE else 0
    trivial = len(_TRIVIAL_RE[lang].findall(code)) if lang in _TRIVIAL_RE else 0
    return TextFile(
        lang=lang,
        names=_names(lang, code),
        asserts=max(0, asserts - trivial),
        trivial=trivial,
        skips=len(_SKIP_RE[lang].findall(code)) if lang in _SKIP_RE else 0,
        focus=len(_FOCUS_RE[lang].findall(code)) if lang in _FOCUS_RE else 0,
        per_test=_per_test_asserts(lang, code),
        nonblank=bool(text.strip()),
    )


# ------------------------------------------------------------------------------------ analysis
@dataclass
class _Side:
    """One side (base or candidate) of one test file, parsed."""

    path: str
    lang: str
    py: PyFile | None = None
    txt: TextFile | None = None

    @property
    def names(self) -> set[str]:
        return set(self.py.tests) if self.py else set(self.txt.names)  # type: ignore[union-attr]

    @property
    def asserts(self) -> int:
        return self.py.asserts if self.py else self.txt.asserts  # type: ignore[union-attr]

    @property
    def skips(self) -> int:
        return self.py.skips if self.py else self.txt.skips  # type: ignore[union-attr]

    @property
    def trivial(self) -> int:
        return self.py.trivial if self.py else self.txt.trivial  # type: ignore[union-attr]

    @property
    def blank(self) -> bool:
        return not self.names and self.asserts == 0 and self.trivial == 0


def _parse_side(path: str, lang: str, text: str) -> _Side:
    if lang == "python":
        return _Side(path, lang, py=parse_python(text))
    return _Side(path, lang, txt=parse_text(lang, text))


def _short(name: str) -> str:
    return clean(name.rsplit(".", 1)[-1] if "." in name else name)


def _compare(
    changes: list[tuple[str, _Side | None, _Side | None, str | None, str | None]],
) -> list[Signal]:
    """`changes`: (path, base side, candidate side, base text, candidate text) for every changed
    test file that could be parsed on both sides it exists on."""
    signals: list[Signal] = []
    # A name counts as a move destination only where it is new: in a candidate file whose base
    # version lacked it (or the file itself is new). An unrelated same-named test elsewhere does
    # not make a deleted test look moved.
    new_names: set[str] = set()
    cand_asserts: dict[tuple[str, str], int] = {}  # (file, name) -> assertions, for those names
    for path, b, c, _, _ in changes:
        if c is None or not is_collected(path):
            continue  # a test only counts as moved when the runner collects its new file
        fresh = c.names - (b.names if b else set())
        new_names |= fresh
        counts = {n: t.asserts for n, t in c.py.tests.items()} if c.py else (c.txt.per_test if c.txt else {})
        cand_asserts.update({(path, n): counts[n] for n in fresh if n in counts})
    cand_asserts_by_name: dict[str, int] = {}
    for (_, n), count in cand_asserts.items():
        cand_asserts_by_name[n] = max(count, cand_asserts_by_name.get(n, 0))

    base_total = sum(b.asserts for _, b, _, _, _ in changes if b)
    cand_total = sum(c.asserts for _, _, c, _, _ in changes if c)
    net_drop = base_total - cand_total

    for path, b, c, _, _ in changes:
        if b is None:
            # A new file: only the markers that make a test pass without testing anything.
            if c is not None:
                signals += _new_markers(path, c, None) + _new_test_trivials(path, c, set())
                signals += _neutral_signals(path, c, None)
            continue
        if c is None:
            gone = sorted(n for n in b.names if n not in new_names)
            if gone or (b.asserts > 0 and not b.names):
                what = f"{_plural(len(gone), 'test')} gone ({', '.join(_short(n) for n in gone[:4])}"
                what += ", ..." if len(gone) > 4 else ""
                what += ")" if gone else ""
                signals.append(Signal(REMOVED_FILE, path, f"test file deleted; {what}" if gone else "test file deleted"))
            continue

        gone = sorted(n for n in b.names if n not in c.names and n not in new_names)
        if gone and not c.names and c.blank:
            signals.append(Signal(EMPTIED_FILE, path, f"test file emptied: {_plural(len(gone), 'test')} removed"))
        elif gone:
            listed = ", ".join(f"'{_short(n)}'" for n in gone[:4]) + (", ..." if len(gone) > 4 else "")
            signals.append(Signal(REMOVED_TEST, path, f"{_plural(len(gone), 'test')} removed: {listed}"))

        signals += _assertion_signals(path, b, c, cand_asserts_by_name, net_drop)
        signals += _neutral_signals(path, c, b)
        signals += _new_markers(path, c, b) + _new_test_trivials(path, c, b.names)
        if b.py and c.py:
            signals += _expected_signals(path, b.py, c.py)
    return signals


def _assertion_signals(
    path: str, b: _Side, c: _Side, cand_asserts_by_name: dict[str, int], net_drop: int
) -> list[Signal]:
    out: list[Signal] = []
    if b.py and c.py:
        for name, bt in b.py.tests.items():
            ct = c.py.tests.get(name)
            if ct is None:
                other = cand_asserts_by_name.get(name)
                if other is None:
                    continue  # reported as a removed test
                ct = PyTest(name, asserts=other)  # moved to another file: compare there
            parts = []
            lost = bt.asserts - ct.asserts
            new_trivial = len(ct.trivial) - len(bt.trivial)
            if ct.asserts == 0 and bt.asserts > 0:
                parts.append(f"lost {_plural(bt.asserts, 'assertion')}")
                if new_trivial <= 0:
                    parts[-1] += " and no longer asserts anything"
            elif lost > 0:  # whatever the net drop: new assertions elsewhere do not make up for it
                parts.append(f"lost {_plural(lost, 'assertion')}")
            if new_trivial > 0:
                parts.append("now " + _trivial_phrase([s for s in ct.trivial if s not in bt.trivial] or ct.trivial))
            if parts:
                out.append(Signal(LOST_ASSERTIONS if lost > 0 else TRIVIAL_ASSERT, path,
                                  f"'{_short(name)}' " + " and ".join(parts)))
        return out

    # Regex languages: file level, unless the language lets us see inside a test.
    if b.txt and c.txt:
        for name, before in b.txt.per_test.items():
            after = c.txt.per_test.get(name, cand_asserts_by_name.get(name))
            if after is not None and before > 0 and after == 0:
                out.append(Signal(LOST_ASSERTIONS, path, f"'{_short(name)}' lost {_plural(before, 'assertion')} and no longer asserts anything"))
        drop = b.txt.asserts - c.txt.asserts
        if drop > 0 and net_drop > 0 and not out:
            out.append(Signal(LOST_ASSERTIONS, path, f"{_plural(drop, 'assertion')} removed ({b.txt.asserts} -> {c.txt.asserts})"))
    return out


def _neutral_signals(path: str, c: _Side, b: _Side | None) -> list[Signal]:
    """Tests that gained assertions which cannot fail (compared with the same test on the base)."""
    if not c.py:
        return []
    out = []
    for name, ct in c.py.tests.items():
        before = b.py.tests.get(name) if b and b.py else None
        if len(ct.neutralized) > len(before.neutralized if before else []):
            out.append(Signal(NEUTRALIZED, path, f"'{_short(name)}' has {'; '.join(dict.fromkeys(ct.neutralized))} "
                                                 "(they can never fail)"))
    return out


def _trivial_phrase(found: list[str]) -> str:
    """`asserts True`, `calls assertTrue(True)`: how a trivially true check reads in a reason."""
    shown = [
        f"asserts `{clean(t.removeprefix('assert '))}`" if t.startswith("assert ") else f"calls `{clean(t)}`"
        for t in found[:2]
    ]
    return " and ".join(shown)


def _new_test_trivials(path: str, c: _Side, known: set[str]) -> list[Signal]:
    """Trivially true assertions in tests the base did not have (a new test that can never fail)."""
    if not c.py:
        return []
    return [
        Signal(TRIVIAL_ASSERT, path, f"new test '{_short(n)}' {_trivial_phrase(t.trivial)}")
        for n, t in c.py.tests.items()
        if t.trivial and n not in known
    ]


def _new_markers(path: str, c: _Side, b: _Side | None) -> list[Signal]:
    out = []
    skips = c.skips - (b.skips if b else 0)
    if skips > 0:
        out.append(Signal(SKIP_ADDED, path, f"{_plural(skips, 'skip marker')} added"))
    elif b is not None and b.py and c.py:
        # The file-level count held still, but a marker may have moved onto another test.
        for name, ct in c.py.tests.items():
            bt = b.py.tests.get(name)
            if bt is not None and ct.skips > bt.skips:
                out.append(Signal(SKIP_ADDED, path, f"'{_short(name)}' gained {_plural(ct.skips - bt.skips, 'skip marker')}"))
    if c.txt:
        focus = c.txt.focus - (b.txt.focus if b and b.txt else 0)
        if focus > 0:
            out.append(Signal(FOCUS_ADDED, path, f"{_plural(focus, 'focus marker')} added (`.only` runs just these tests)"))
    before = b.txt.trivial if b and b.txt else 0
    if c.txt and c.txt.trivial > before:
        out.append(Signal(TRIVIAL_ASSERT, path, f"{_plural(c.txt.trivial - before, 'trivially true assertion')} added"))
    return out


def _expected_signals(path: str, b: PyFile, c: PyFile) -> list[Signal]:
    out = []
    for name, bt in b.tests.items():
        ct = c.tests.get(name)
        if ct is None:
            continue
        for expr, before in bt.expected.items():
            after = ct.expected.get(expr)
            if after is not None and sorted(after) != sorted(before):
                out.append(Signal(EXPECTED_CHANGED, path,
                                  f"'{_short(name)}' now expects {clean(', '.join(after[:2]))} instead of "
                                  f"{clean(', '.join(before[:2]))} for `{clean(expr)}`"))
    return out


# --------------------------------------------------------------------------- git and entry point
def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False)


def _base_text(root: Path, base: str, path: str) -> tuple[bool, bytes | None]:
    """(ok, content). Content is None when the file does not exist on the base."""
    if _git(root, "cat-file", "-e", f"{base}:{path}").returncode != 0:
        return True, None
    r = _git(root, "show", f"{base}:{path}")
    return (r.returncode == 0), (r.stdout if r.returncode == 0 else None)


def _decode(data: bytes) -> str | None:
    if len(data) > MAX_FILE_BYTES:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _hunk(path: str, before: str | None, after: str | None) -> str:
    diff = difflib.unified_diff(
        (before or "").splitlines(), (after or "").splitlines(), f"a/{path}", f"b/{path}", lineterm="", n=2
    )
    text = "\n".join(diff)
    return text if len(text) <= MAX_HUNK_CHARS else text[:MAX_HUNK_CHARS] + "\n... truncated ..."


HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _read_candidate(root: Path, path: str) -> bytes | None:
    p = root / path
    try:
        if p.is_symlink() or not p.is_file():
            return None
        return p.read_bytes()
    except OSError:
        return None


def _parse(lang: str, path: str, text: str | None) -> tuple[_Side | None, str | None]:
    if text is None:
        return None, None
    try:
        return _parse_side(path, lang, text), None
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        why = getattr(exc, "msg", None) or str(exc) or type(exc).__name__
        line = getattr(exc, "lineno", None)
        return None, f"could not analyze: {clean(why)}" + (f" (line {line})" if line else "")


def _sections(text: str, wanted: tuple[str, ...]) -> str:
    """The lines of the `[section]` headers whose name starts with one of `wanted` (ini/toml)."""
    keep, on = [], False
    for ln in text.splitlines():
        if ln.startswith("["):
            on = ln.strip().startswith(wanted)
        if on:
            keep.append(ln.rstrip())
    return "\n".join(keep)


_TOOL_RUNNER_KEY = re.compile(r"""^\s*["']?(?:pytest|coverage)\b""")


def _pyproject_part(text: str) -> str:
    """pytest/coverage tables, plus a bare `[tool]` table when it holds pytest/coverage keys
    (`[tool]\\npytest.ini_options.addopts = ...`)."""
    part = _sections(text, ("[tool.pytest", "[tool.coverage"))
    tool, on = [], False
    for ln in text.splitlines():
        if ln.startswith("["):
            on = ln.strip() == "[tool]"
        if on:
            tool.append(ln.rstrip())
    if any(_TOOL_RUNNER_KEY.match(ln) for ln in tool):
        part += "\n" + "\n".join(tool)
    return part


_MAKE_TARGET = re.compile(r"^(test|tests|check)\s*:(?![=:])")


def _make_test_targets(text: str) -> str:
    """The `test:`/`tests:`/`check:` rule lines of a Makefile, with their recipes."""
    keep, on = [], False
    for ln in text.splitlines():
        if ln[:1] not in ("\t", " ", "#", ""):
            on = bool(_MAKE_TARGET.match(ln))
        if on:
            keep.append(ln.rstrip())
    return "\n".join(keep)


def _runner_part(path: str, text: str | None, whole: bool = False) -> str:
    """The part of a config file that configures the test runner. An absent file, or one with no
    runner configuration, is the empty string, so adding or deleting an unrelated file is no change.
    `whole`: the file is one a mandatory check runs (a script), so every byte of it is runner."""
    if text is None:
        return ""
    if whole:
        return text
    name = PurePosixPath(path).name
    if name == "pyproject.toml":
        return _pyproject_part(text)
    if name == "setup.cfg":
        return _sections(text, ("[tool:pytest]", "[coverage:", "[pytest]"))
    if name == "tox.ini":
        return _sections(text, ("[pytest]", "[testenv", "[tool:pytest]"))
    if name in MAKEFILE_NAMES:
        return _make_test_targets(text)
    if name == "package.json":
        try:
            data = json.loads(text)
            scripts = data.get("scripts") or {}
            tests = {k: v for k, v in scripts.items()
                     if k in ("test", "pretest", "posttest") or k.startswith("test:")}
            part = {"jest": data.get("jest"), "scripts": tests}
        except (ValueError, AttributeError):
            return text  # unparseable: any change counts
        return "" if not part["jest"] and not tests else json.dumps(part, sort_keys=True)
    return text


def check_runner_specs(checks: Iterable[Any]) -> tuple[str, ...]:
    """Repository-relative files (or globs) that mandatory checks run or read: every word of each
    check's command, `--opt=value` values included, and its `cache_inputs`. Whether a word names a
    changed file is decided by `analyze`; words that match nothing are harmless."""
    out: dict[str, None] = {}
    for check in checks:
        words = list(check.command)
        for w in list(words):
            if "=" in w:
                words.append(w.split("=", 1)[1])
            if any(ch.isspace() for ch in w):
                try:
                    words += shlex.split(w)
                except ValueError:
                    words += w.split()
        for w in [*words, *check.cache_inputs]:
            w = normalize(str(w))
            if w and not w.startswith(("-", "/")) and ".." not in w.split("/"):
                out[w] = None
    return tuple(out)


def _config_signals(
    root: Path, base: str, configs: list[str], files: dict[str, str | None], whole: frozenset[str] = frozenset()
) -> tuple[list[Signal], dict[str, str]]:
    signals: list[Signal] = []
    hunks: dict[str, str] = {}
    for path in configs:
        ok, raw = _base_text(root, base, path)
        before = _decode(raw) if raw is not None else None
        after = None
        if files[path] is not None:
            data = _read_candidate(root, path)
            after = _decode(data) if data is not None else None
            if after is None:
                signals.append(Signal(UNANALYZABLE, path, "could not analyze: the config file is unreadable or too large"))
                continue
        if not ok or (raw is not None and before is None):
            signals.append(Signal(UNANALYZABLE, path, "could not analyze: the base version is unreadable or too large"))
            continue
        if _runner_part(path, before, path in whole) != _runner_part(path, after, path in whole):
            signals.append(Signal(RUNNER_CONFIG, path, f"test-runner configuration changed ({clean(PurePosixPath(path).name)}); "
                                                      "check that it does not disable, skip or narrow the tests"))
            hunks[path] = _hunk(path, before, after)
    return signals, hunks


def _rust_with_tests(root: Path, base: str, path: str, digest: str | None) -> bool:
    """A changed .rs file outside the test globs that holds `#[test]` functions (inline unit tests)."""
    _, raw = _base_text(root, base, path)
    if raw is not None and b"#[test]" in raw:
        return True
    if digest is None:
        return False
    data = _read_candidate(root, path)
    return data is not None and b"#[test]" in data


def analyze(
    root: str | Path,
    base_revision: str | None,
    files: dict[str, str | None],
    globs: tuple[str, ...] | list[str] = DEFAULT_TEST_GLOBS,
    runner_files: Iterable[str] = (),
) -> IntegrityReport:
    """Compare the candidate with its base for every changed test file.

    `files` is the candidate manifest's `files` map: changed path -> content hash, or None when
    the file was deleted. Without a base revision there is nothing to compare against.

    `runner_files`: paths or globs a mandatory check runs (see `check_runner_specs`). A changed file
    that matches one is runner configuration, compared whole."""
    root = Path(root)
    if base_revision is None:
        return IntegrityReport()
    paths = sorted(files)
    specs = tuple(runner_files)
    whole = frozenset(p for p in paths if any(match(p, sp) for sp in specs))
    configs = [p for p in paths if p in whole or match_any(p, RUNNER_CONFIG_GLOBS)]
    candidates = [p for p in paths if language(p) and is_test_path(p, globs)]
    if not candidates and not configs and not any(p.endswith(".rs") for p in paths):
        return IntegrityReport()
    if _git(root, "rev-parse", "--verify", "-q", f"{base_revision}^{{commit}}").returncode != 0:
        first = (candidates or configs or paths)[0]
        return IntegrityReport((Signal(UNANALYZABLE, first, "could not analyze: the base revision is not readable"),))
    candidates += [p for p in paths if p.endswith(".rs") and p not in candidates
                   and _rust_with_tests(root, base_revision, p, files[p])]
    candidates.sort()

    signals: list[Signal] = []
    changes: list[tuple[str, _Side | None, _Side | None, str | None, str | None]] = []
    for path in candidates:
        lang = language(path)
        assert lang is not None
        deleted = files[path] is None
        ok, raw = _base_text(root, base_revision, path)
        is_new = ok and raw is None
        # A new file the runner would not collect (a fixture, a helper) is not a test: unreadable is fine.
        lenient = is_new and not is_collected(path)
        base_text = _decode(raw) if raw is not None else None
        cand_text: str | None = None
        cand_problem: str | None = None
        if not deleted:
            data = _read_candidate(root, path)
            if data is None:
                cand_problem = "the file is unreadable"
            elif HEX64.match(files[path] or "") and hashlib.sha256(data).hexdigest() != files[path]:
                cand_problem = "the file changed during analysis"
            else:
                cand_text = _decode(data)
                if cand_text is None:
                    cand_problem = "the file is binary or too large"
        if not ok or (raw is not None and base_text is None):
            if deleted:
                signals.append(Signal(REMOVED_FILE, path, "test file deleted (its base version could not be analyzed)"))
            else:
                signals.append(Signal(UNANALYZABLE, path, "could not analyze: the base version is unreadable or too large"))
            continue
        if cand_problem:
            if not lenient:
                signals.append(Signal(UNANALYZABLE, path, f"could not analyze: {cand_problem}"))
            continue
        if base_text is None and cand_text is None:
            continue
        b, berr = _parse(lang, path, base_text)
        c, cerr = _parse(lang, path, cand_text)
        if berr:
            signals.append(Signal(REMOVED_FILE, path, "test file deleted (its base version could not be analyzed)")
                           if deleted else Signal(UNANALYZABLE, path, berr))
            continue
        if cerr:
            if not lenient:
                signals.append(Signal(UNANALYZABLE, path, cerr))
            continue
        changes.append((path, b, c, base_text, cand_text))

    signals += _compare(changes)
    texts = {p: (bt, ct) for p, _, _, bt, ct in changes}
    cfg_signals, cfg_hunks = _config_signals(root, base_revision, configs, files, whole)
    signals += cfg_signals
    flagged = {s.path for s in signals}
    hunks = {p: _hunk(p, *texts[p]) for p in flagged if p in texts} | cfg_hunks
    return IntegrityReport(tuple(signals), tuple(p for p, *_ in changes), hunks)
