"""Repository-path glob matching with `**` support.

`fnmatch` lets `*` cross directory separators, which makes `src/*.py` match
`src/a/b.py` and quietly widens risk floors and ownership boundaries. These
semantics are the gitignore-like ones: `*` and `?` stay inside one segment,
`**/` matches zero or more whole directories, and a trailing `**` matches
everything below.
"""

from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=1024)
def _compile(pattern: str) -> re.Pattern[str]:
    pattern = pattern.replace("\\", "/").lstrip("/")
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def normalize(path: str) -> str:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def match(path: str, pattern: str) -> bool:
    return bool(_compile(pattern).match(normalize(path)))


def match_any(path: str, patterns) -> bool:
    return any(match(path, p) for p in patterns)
