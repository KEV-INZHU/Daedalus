"""Commit provenance: accepted work says so in its own commit message.

A `prepare-commit-msg` git hook (installed by `daedalus init`, and by the
Claude Code SessionStart hook when missing) adds trailers to a commit whose
content is exactly an ACCEPTED candidate:

    Daedalus-Run: 75787c4300b4
    Daedalus-Candidate: 3455f25c94fb...

"Exactly" means: a new commit (not an amend) made directly on the run's base,
whose content is the accepted candidate and nothing else (the index equals the
working tree, nothing outside the candidate is staged). A partial commit, an
empty one, a commit of the remainder after part of the work was committed, or
one that adds anything after acceptance, gets no trailers. The hook never blocks a commit and never edits one it cannot vouch for.

Trailers are claims, not proof: anyone can type them. They make the gate's
result visible where people look (the log, the PR) and give CI something to
check; `daedalus ci` re-runs the trusted checks rather than trusting them.

A `pre-push` hook, installed alongside it under the same rules, lists outgoing
commits without a `Daedalus-Run` trailer. Merge commits and commits the remote
already has are not checked. It only warns, unless `.daedalus.yml` sets
`harness: {require_provenance_on_push: true}`, in which case it refuses the push.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from daedalus.core.config import CONFIG_FILE, STATE_DIR, find_root, gate_enabled, load_config
from daedalus.core.errors import DaedalusError
from daedalus.core.state_machine import Disposition

MARK = "# daedalus: prepare-commit-msg"
HOOK_SCRIPT = f"""#!/bin/sh
{MARK} (adds Daedalus-Run trailers to commits of accepted work; never blocks a commit)
command -v daedalus >/dev/null 2>&1 || exit 0
daedalus hook commit-msg "$1" "$2" >/dev/null 2>&1 || true
"""
PUSH_MARK = "# daedalus: pre-push"
PUSH_HOOK_SCRIPT = f"""#!/bin/sh
{PUSH_MARK} (lists outgoing commits without a Daedalus-Run trailer; blocks only if require_provenance_on_push)
command -v daedalus >/dev/null 2>&1 || exit 0
daedalus hook pre-push "$1" "$2"; s=$?
[ "$s" -eq 3 ] && exit 1  # 3 is a deliberate refusal; any other failure only skips the check
exit 0
"""
REFUSE = 3  # pre_push_hook's status for "refuse the push", distinct from a crash
MAX_LISTED = 20
# (hook name, marker, script, how to add ours to someone else's hook)
GIT_HOOKS = (
    ("prepare-commit-msg", MARK, HOOK_SCRIPT, 'add `daedalus hook commit-msg "$1" "$2"` to it'),
    ("pre-push", PUSH_MARK, PUSH_HOOK_SCRIPT,
     'pipe that hook\'s stdin to `daedalus hook pre-push "$1" "$2"` and exit non-zero when it exits '
     f'{REFUSE} (a refusal); treat any other status as "check skipped"'),
)
RECENT_RUNS = 5  # a commit hook must stay fast: only the latest runs can be what is committed


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)


def hook_path(root: Path, name: str = "prepare-commit-msg") -> Path | None:
    r = _git(root, "rev-parse", "--git-path", f"hooks/{name}")
    if r.returncode != 0 or not r.stdout.strip():
        return None
    p = Path(r.stdout.strip())
    return p if p.is_absolute() else root / p


def install_git_hook(root: Path, *, only_if_absent: bool = False) -> str:
    """Install the prepare-commit-msg and pre-push hooks. Never replaces someone else's hook;
    one that is in the way does not stop the other from being installed."""
    done: list[str] = []
    refused: list[str] = []
    for name, mark, script, hint in GIT_HOOKS:
        try:
            done.append(_install_one(root, name, mark, script, hint, only_if_absent=only_if_absent))
        except DaedalusError as exc:
            refused.append(str(exc))
    if refused:
        raise DaedalusError("\n".join([*refused, *done]))
    return "\n".join(dict.fromkeys(done))  # the "outside .git" note is the same for both


def _install_one(root: Path, name: str, mark: str, script: str, hint: str, *, only_if_absent: bool) -> str:
    p = hook_path(root, name)
    if p is None:
        raise DaedalusError(f"not a git repository; the {name} hook was not installed")
    common = _git(root, "rev-parse", "--git-common-dir").stdout.strip()  # hooks live here, even in a worktree
    git_dir = (Path(common) if Path(common).is_absolute() else root / common) if common else None
    outside = git_dir is not None and git_dir.resolve() not in p.resolve().parents
    if only_if_absent and (outside or _git(root, "config", "core.hooksPath").stdout.strip()):
        # A custom hooks directory is either tracked (husky and friends) or shared by other
        # repositories (~/.githooks). Installing unasked would change something beyond this
        # repository's own .git, so only an explicit `daedalus install claude-code` does it.
        return (f"git hooks live in {p.parent}, outside this repository's .git (core.hooksPath or a symlink); "
                "run `daedalus install claude-code` to add ours there")
    if p.exists() or p.is_symlink():
        if p.exists() and mark in p.read_text(encoding="utf-8", errors="replace"):
            if only_if_absent:
                return f"git hook {name} already installed"
        else:
            raise DaedalusError(f"{p} already exists and is not Daedalus's; {hint}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(script, encoding="utf-8")
    p.chmod(0o755)
    return f"git hook {name} -> {p}"


def accepted_run_for_commit(root: Path, source: str = "") -> tuple[str, str] | None:
    """(run id, candidate id) when the commit being made is exactly an accepted candidate:
    a new commit (not an amend), directly on the run's base, whose content is the candidate."""
    from daedalus.orchestration.ariadne import Ariadne
    from daedalus.repository.candidate import capture, git_head

    if source == "commit":
        return None  # an amend or -c/-C reuse: not a fresh commit of the accepted work
    if _git(root, "diff", "--quiet").returncode != 0:
        return None  # unstaged changes: the commit is not the working tree
    if _git(root, "diff", "--cached", "--quiet", "HEAD").returncode == 0:
        return None  # nothing changes: an empty commit vouches for nothing
    head = git_head(root)
    ari = Ariadne(root)
    try:
        exclude = load_config(root).policy.candidate_exclude
        for rid in reversed(ari.store.run_ids()[-RECENT_RUNS:]):
            try:
                state = ari.state(rid)
            except DaedalusError:
                continue
            if state.disposition is not Disposition.ACCEPTED or not state.accepted_candidate_id:
                continue
            base = state.contract.base_revision
            if not base or base != head:
                continue  # the commit must hold the whole candidate, so it must sit directly on the base
            cand = capture(root, base, exclude)
            if cand.candidate_id != state.accepted_candidate_id:
                continue
            staged = _git(root, "diff", "--cached", "--name-only", "--no-renames", base).stdout.splitlines()
            if any(p not in cand.manifest["files"] for p in staged):
                continue  # something outside the candidate is staged
            untracked = _git(root, "ls-files", "-o", "--exclude-standard").stdout.splitlines()
            if any(p in cand.manifest["files"] for p in untracked):
                continue  # part of the candidate is not in the commit
            return rid, state.accepted_candidate_id
        return None
    finally:
        ari.close()


def commit_msg_hook(args: list[str]) -> None:
    """`prepare-commit-msg <file> [source]`: add trailers for an accepted candidate. Never raises."""
    try:
        if not args:
            return
        msg_file = Path(args[0])
        root = find_root(".")
        text = msg_file.read_text(encoding="utf-8")
        if "Daedalus-Run:" in text:
            return  # the factory, or a human, already said where this came from
        found = accepted_run_for_commit(root, args[1] if len(args) > 1 else "")
        if found is None:
            return
        rid, cid = found
        trailers = [f"Daedalus-Run: {rid}", f"Daedalus-Candidate: {cid}"]
        cmd = ["git", "-C", str(root), "interpret-trailers", "--in-place"]
        for t in trailers:
            cmd += ["--trailer", t]
        subprocess.run([*cmd, str(msg_file)], capture_output=True, check=False)
    except Exception:  # noqa: BLE001 — a commit hook must never break a commit
        return


def unprovenanced_commits(root: Path, remote: str, updates: str) -> list[tuple[str, str]]:
    """(sha, subject) of outgoing non-merge commits without a Daedalus-Run trailer. `updates` is
    what git gives a pre-push hook on stdin: `<local ref> <local sha> <remote ref> <remote sha>` lines."""
    tips: list[str] = []
    known: list[str] = []
    for line in updates.splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        local_sha, remote_sha = parts[1], parts[3]
        if set(local_sha) == {"0"}:
            continue  # deleting a remote branch pushes no commits
        tips.append(local_sha)
        if set(remote_sha) != {"0"} and _git(root, "cat-file", "-e", f"{remote_sha}^{{commit}}").returncode == 0:
            known.append(remote_sha)
    if not tips:
        return []
    # What the remote has at the pushed refs, or in its tracking refs, is already there: not outgoing.
    # When the remote has no tracking refs here (a URL that is no configured remote, or one never
    # fetched), every remote-tracking ref is excluded instead: a commit some remote has goes
    # unreported rather than the remote's whole history being reported.
    name = _remote_name(root, remote)
    tracked = bool(name) and bool(_git(root, "for-each-ref", "--count=1", f"refs/remotes/{name}/").stdout.strip())
    exclude = ["--not", *known, f"--remotes={name}" if tracked else "--remotes"]
    r = _git(root, "log", "--no-merges", "--format=%H%x1f%s%x1f%(trailers:key=Daedalus-Run,valueonly)%x1e",
             *tips, *exclude, "--")
    if r.returncode != 0:
        raise DaedalusError(f"git log failed: {r.stderr.strip()}")
    missing = []
    for rec in r.stdout.split("\x1e"):
        fields = rec.strip("\n").split("\x1f")
        if len(fields) >= 3 and not fields[2].strip():
            missing.append((fields[0], fields[1]))
    return missing


def _remote_name(root: Path, remote: str) -> str:
    """The configured remote that `remote` names or is the URL of ("" if none)."""
    names = _git(root, "remote").stdout.split()
    if not remote or remote in names:
        return remote
    for n in names:
        urls = _git(root, "remote", "get-url", "--all", n).stdout.split()
        urls += _git(root, "remote", "get-url", "--push", "--all", n).stdout.split()
        if remote in urls:
            return n
    return ""


def _require_on_push(root: Path) -> bool:
    try:
        return load_config(root).harness.get("require_provenance_on_push") is True
    except Exception as exc:  # noqa: BLE001 — an unreadable config cannot say "refuse"; say so
        print(f"daedalus: could not read the configuration ({exc}); "
              "whether to refuse is unknown, so this is only a warning", file=sys.stderr)
        return False


def _uses_daedalus(root: Path) -> bool:
    return (root / CONFIG_FILE).exists() or (root / STATE_DIR).is_dir()


def pre_push_hook(args: list[str], stdin_text: str) -> int:
    """`pre-push <remote> <url>`: report outgoing commits without a Daedalus-Run trailer, and refuse
    the push if `harness.require_provenance_on_push` is true. Returns REFUSE to refuse, else 0.
    Silent in a repository that does not use Daedalus, or with the gate switched off."""
    try:
        root = find_root(".")
        if not _uses_daedalus(root) or not gate_enabled(root):
            return 0
        missing = unprovenanced_commits(root, args[0] if args else "", stdin_text)
    except Exception as exc:  # noqa: BLE001 — our own failure never blocks a push
        print(f"daedalus: provenance check skipped: {exc}", file=sys.stderr)
        return 0
    if not missing:
        return 0
    require = _require_on_push(root)
    lines = [f"daedalus: {len(missing)} outgoing commit(s) without a Daedalus-Run trailer (not accepted by the gate):"]
    lines += [f"  {sha[:12]} {subject}" for sha, subject in missing[:MAX_LISTED]]
    if len(missing) > MAX_LISTED:
        lines.append(f"  … and {len(missing) - MAX_LISTED} more")
    lines.append("daedalus: push refused (harness.require_provenance_on_push is true)" if require else
                 "daedalus: pushing anyway; set `harness: {require_provenance_on_push: true}` to refuse such pushes")
    print("\n".join(lines), file=sys.stderr)
    return REFUSE if require else 0
