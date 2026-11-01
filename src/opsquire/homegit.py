"""The home as a local-only git repository: opsquire commits ready versions (lint passes), snapshots loose changes
before a parallel ask, and gives each parallel piece its own worktree and branch that merge back when done."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .model import OpsquireError

GITIGNORE = """# Local-only repository: it holds operator-specific tasks and run history. Never add a remote.

# run records, locks and asks: written by the runner, kept on disk, not versioned
var/

# generated: `opsquire docs` and `opsquire contract --write` rebuild these
docs/
AGENTS.md
CLAUDE.md
.lint.json

# caches and local agent config
__pycache__/
.mypy_cache/
.ruff_cache/
.claude/
.mcp.json
"""

WORKTREES = ".worktrees"


class GitError(OpsquireError):
    pass


def git(home: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    exe = shutil.which("git")
    if not exe:
        raise GitError("git is not installed")
    r = subprocess.run([exe, "-C", str(home), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()[:400]}")
    return r


def is_repo(home: Path) -> bool:
    if not shutil.which("git") or not home.is_dir():
        return False
    return git(home, "rev-parse", "--is-inside-work-tree", check=False).stdout.strip() == "true"


def remotes(home: Path) -> list[str]:
    return git(home, "remote").stdout.split()


def ensure_repo(home: Path, dry: bool) -> str:
    """Make the home a local-only git repository (init, .gitignore, first commit); refuse one with a remote."""
    if is_repo(home):
        found = remotes(home)
        if found:
            return f"home repo: refusing: it has remotes ({', '.join(found)}); it must stay local-only"
        return f"home repo: local-only git repository ({home})"
    if dry:
        return f"home repo: would initialize a local git repository in {home}"
    git(home, "init", "-q")
    ignore = home / ".gitignore"
    if not ignore.exists():
        ignore.write_text(GITIGNORE)
    commit(home, None, "Opsquire home: tasks, general scripts, local rules")
    return f"home repo: initialized a local git repository in {home}"


def exclude_worktrees(home: Path) -> None:
    common = Path(git(home, "rev-parse", "--git-common-dir").stdout.strip())
    exclude = (common if common.is_absolute() else home / common) / "info" / "exclude"
    lines = exclude.read_text().splitlines() if exclude.is_file() else []
    if f"/{WORKTREES}/" not in lines:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("\n".join([*lines, f"/{WORKTREES}/"]) + "\n")


def commit(home: Path, paths: list[Path] | None, message: str) -> bool:
    """Stage `paths` (everything when None) and commit if anything changed. True when a commit was made."""
    if paths is not None:
        paths = [p for p in paths if p.exists()]
        if not paths:
            return False
    git(home, "add", "-A", "--", *([str(p) for p in paths] if paths else ["."]))
    if git(home, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    git(home, "commit", "-q", "-m", message)
    return True


def commit_quietly(home: Path, paths: list[Path], message: str) -> str:
    """Best-effort commit after a lint pass; returns a note when it could not be made."""
    if not is_repo(home):
        return ""
    try:
        commit(home, paths, message)
    except GitError as e:
        return f"(not committed: {e})"
    return ""


def branch(home: Path) -> str:
    return git(home, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def add_worktree(home: Path, path: Path, name: str) -> None:
    """Check out branch `name` at `path`, creating the branch from HEAD when it does not exist yet."""
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = git(home, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False).returncode == 0
    git(home, "worktree", "add", "-q", *([str(path), name] if exists else ["-b", name, str(path)]))


def remove_worktree(home: Path, path: Path) -> None:
    if path.exists():
        git(home, "worktree", "remove", "--force", str(path), check=False)
        shutil.rmtree(path, ignore_errors=True)
    git(home, "worktree", "prune", check=False)
    for parent in (path.parent, path.parent.parent):
        if parent.name and parent != home and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()


def merge_into(home: Path, name: str, message: str) -> bool:
    """Merge branch `name` into the home's checked-out branch. False (and nothing changed) on a conflict."""
    r = git(home, "merge", "--no-ff", "-q", "-m", message, name, check=False)
    if r.returncode == 0:
        return True
    git(home, "merge", "--abort", check=False)
    return False


def merge_from(worktree: Path, name: str) -> list[str]:
    """Merge branch `name` into a worktree; the files left with conflict markers (empty when clean)."""
    r = git(worktree, "merge", "-q", "-m", f"merge {name}", name, check=False)
    if r.returncode == 0:
        return []
    files = git(worktree, "diff", "--name-only", "--diff-filter=U").stdout.split()
    if not files:
        raise GitError(f"merging {name} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return files


def has_markers(worktree: Path, files: list[str]) -> list[str]:
    left = []
    for f in files:
        p = worktree / f
        if p.is_file() and any(line.startswith(("<<<<<<< ", ">>>>>>> ")) for line in p.read_text().splitlines()):
            left.append(f)
    return left


def delete_branch(home: Path, name: str) -> None:
    git(home, "branch", "-D", name, check=False)
