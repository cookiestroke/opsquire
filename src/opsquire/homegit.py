"""The home as a local-only git repository: opsquire commits ready versions (lint passes), snapshots loose changes
before a parallel ask, and gives each parallel piece its own worktree and branch that merge back when done.

Home commits and merges are never signed, whatever the operator's git config says: the repository cannot have a
remote, so a signature would prove nothing to anyone, and signing would ask for a passphrase before every run. They
use the operator's git identity, or a placeholder when git has none, so a fresh machine is never stopped by it."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
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
REPO_COPIES = ".repos"
EXCLUDE_LOCK = threading.Lock()
PLACEHOLDER_IDENTITY = ("-c", "user.name=opsquire", "-c", "user.email=opsquire@localhost")


class GitError(OpsquireError):
    pass


def git(home: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    exe = shutil.which("git")
    if not exe:
        raise GitError("git is not installed")
    base = [exe, "-C", str(home), "-c", "commit.gpgsign=false"]
    if args[0] in ("commit", "merge") and not has_identity(base):
        base += PLACEHOLDER_IDENTITY
    r = subprocess.run([*base, *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()[:400]}")
    return r


def has_identity(base: list[str]) -> bool:
    """Whether git would accept a commit's author and committer here, by whatever means it finds them."""
    idents = ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT")
    return all(subprocess.run([*base, "var", i], capture_output=True).returncode == 0 for i in idents)


def is_repo(home: Path) -> bool:
    if not shutil.which("git") or not home.is_dir():
        return False
    return git(home, "rev-parse", "--is-inside-work-tree", check=False).stdout.strip() == "true"


def remotes(home: Path) -> list[str]:
    return git(home, "remote").stdout.split()


def ensure_repo(home: Path, dry: bool) -> str:
    """Make the home a local-only git repository (init, .gitignore, first commit); refuse one with a remote."""
    if not shutil.which("git"):
        return "home repo: skipped, git is not installed; `opsquire integrate` makes it one once it is"
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
    """Keep opsquire's scratch folders (parallel piece worktrees, repo copies) out of the home's commits, without
    touching the operator's .gitignore."""
    with EXCLUDE_LOCK:
        common = Path(git(home, "rev-parse", "--git-common-dir").stdout.strip())
        exclude = (common if common.is_absolute() else home / common) / "info" / "exclude"
        lines = exclude.read_text().splitlines() if exclude.is_file() else []
        missing = [e for e in (f"/{WORKTREES}/", f"/{REPO_COPIES}/") if e not in lines]
        if missing:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text("\n".join([*lines, *missing]) + "\n")


def commit(home: Path, paths: list[Path] | None, message: str) -> bool:
    """Stage `paths` (everything when None) and commit if anything changed. True when a commit was made."""
    exclude_worktrees(home)
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
    """Best-effort commit (after a lint pass, before a run, after an ask); a note when it could not be made, shown
    only on a terminal: an agent sandbox may not be able to write the repository, and the next commit includes it."""
    if not is_repo(home):
        return ""
    try:
        commit(home, paths, message)
    except GitError as e:
        if not os.isatty(2):
            return ""
        return f"(home not committed: {str(e).splitlines()[0]}; the next successful commit includes it)"
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
