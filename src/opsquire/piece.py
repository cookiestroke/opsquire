"""Fan-out from a chat: the same parallel pieces `opsquire ask` gives its agents, for an agent that has its own
subagent tool. `piece open` gives a piece its own git worktree of the home (branch `piece/<group>/<name>`) and prints
the environment its subagent must use; `piece land` commits the piece, merges it into the home, lints what it changed
and removes the worktree; `piece drop` parks or discards an unfinished one. Git is the only state: an open piece is a
worktree, a parked piece is a branch."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import homegit, repos
from .lint import lint_task, save_lint
from .model import OpsquireError, home_dir, load_task, main_home, now

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,29}$")
PREFIX = "piece/"


@dataclass
class PieceInfo:
    group: str
    name: str
    branch: str
    worktree: str
    state: str
    request: str = ""

    @property
    def env(self) -> dict[str, str]:
        """What a piece's subagent exports before any `opsquire` command: the worktree is its home, run records stay
        in the main home, and its author and repo copies are labelled with the piece."""
        author = os.environ.get("OPSQUIRE_AUTHOR", "chat")
        return {
            "OPSQUIRE_HOME": self.worktree,
            "OPSQUIRE_VAR": str(main_home() / "var"),
            "OPSQUIRE_AUTHOR": f"{author} piece {self.name}",
            "OPSQUIRE_ASK_ID": f"piece-{self.group}-{self.name}",
        }

    def to_json(self) -> dict[str, Any]:
        return {**asdict(self), "env": self.env}


def check_name(name: str, what: str) -> str:
    if not NAME_RE.match(name):
        raise OpsquireError(f"{what} {name!r}: lowercase kebab-case, at most 30 characters")
    return name


def default_group() -> str:
    """Pieces of one chat session share a group: the session id in OPSQUIRE_AUTHOR, else the time."""
    author = os.environ.get("OPSQUIRE_AUTHOR", "")
    words = re.findall(r"[a-z0-9]+", author.lower())
    if words and words != ["chat"]:
        return "-".join(words)[:30].strip("-")
    return now().strftime("chat-%Y%m%dT%H%M%S")


def home_repo() -> Path:
    home = home_dir()
    if not homegit.is_repo(home):
        raise OpsquireError(f"{home} is not a git repository; run `opsquire integrate` first")
    git_dir = (home / homegit.git(home, "rev-parse", "--git-dir").stdout.strip()).resolve()
    common = (home / homegit.git(home, "rev-parse", "--git-common-dir").stdout.strip()).resolve()
    if git_dir != common:
        raise OpsquireError("pieces do not nest: this home is already a piece's worktree; land it from the main home")
    return home


def reopen(p: PieceInfo) -> str:
    return f"`opsquire piece open {p.name} --group {p.group}`"


def worktree_path(home: Path, group: str, name: str) -> Path:
    return home / homegit.WORKTREES / group / name


def request_of(home: Path, branch: str) -> str:
    """The request recorded when the piece was opened (the body of its first commit)."""
    base = homegit.branch(home)
    r = homegit.git(home, "log", "--reverse", "--format=%b", f"{base}..{branch}", check=False)
    for para in r.stdout.split("\n\n"):
        if para.strip():
            return para.strip()
    return ""


def open_piece(name: str, group: str | None, request: str) -> PieceInfo:
    """Give a piece its worktree and branch (reusing a parked branch of the same name), with the contract and docs
    written there, and record the request as the branch's first commit."""
    home = home_repo()
    name = check_name(name, "piece name")
    group = check_name(group or default_group(), "group")
    branch = f"{PREFIX}{group}/{name}"
    wt = worktree_path(home, group, name)
    if wt.exists():
        raise OpsquireError(f"piece {group}/{name} is already open at {wt}")
    homegit.commit(home, None, f"snapshot before piece {group}/{name}")
    homegit.exclude_worktrees(home)
    parked = homegit.git(home, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0
    homegit.add_worktree(home, wt, branch)
    info = PieceInfo(group, name, branch, str(wt), "open", request)
    if not parked:
        message = f"piece {group}/{name}: opened" + (f"\n\n{request.strip()}" if request.strip() else "")
        homegit.git(wt, "commit", "-q", "--allow-empty", "-m", message)
    else:
        info.request = request_of(home, branch)
    env = {**os.environ, **info.env}
    for cmd in (["contract", "--write", "--force"], ["docs"]):
        subprocess.run([sys.executable, "-m", "opsquire", *cmd], env=env, capture_output=True, check=False)
    return info


def open_pieces(home: Path) -> list[PieceInfo]:
    """Every piece with a worktree (open) or only a branch (parked)."""
    found: list[PieceInfo] = []
    porcelain = homegit.git(home, "worktree", "list", "--porcelain").stdout
    with_tree: dict[str, str] = {}
    path = ""
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :]
        elif line.startswith("branch refs/heads/"):
            with_tree[line[len("branch refs/heads/") :]] = path
    branches = homegit.git(home, "branch", "--list", "--format=%(refname:short)", f"{PREFIX}*").stdout.split()
    for branch in sorted(branches):
        group, _, name = branch[len(PREFIX) :].partition("/")
        wt = with_tree.get(branch, "")
        state = "open" if wt else "parked"
        path = wt or str(worktree_path(home, group, name))
        found.append(PieceInfo(group, name, branch, path, state, request_of(home, branch)))
    return found


def find_piece(home: Path, target: str) -> PieceInfo:
    """A piece by worktree path, `group/name`, or a name unique across groups."""
    pieces = open_pieces(home)
    as_path = Path(target).expanduser()
    if as_path.exists():
        resolved = as_path.resolve()
        for p in pieces:
            if Path(p.worktree).resolve() == resolved:
                return p
    hits = [p for p in pieces if target in (f"{p.group}/{p.name}", p.name, p.branch)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise OpsquireError(f"no piece {target!r}; `opsquire piece ls` lists them")
    raise OpsquireError(f"{target!r} names several pieces: " + ", ".join(f"{p.group}/{p.name}" for p in hits))


def changed_ids(home: Path, branch: str) -> list[str]:
    """Task and general-script ids the piece's branch adds or changes relative to the home's branch."""
    base = homegit.branch(home)
    files = homegit.git(home, "diff", "--name-only", f"{base}...{branch}").stdout.split()
    ids: list[str] = []
    for f in files:
        parts = Path(f).parts
        tid = ""
        if len(parts) > 3 and parts[0] == "tasks":
            tid = parts[2]
        elif len(parts) > 2 and parts[0] == "general":
            tid = parts[1]
        if tid and tid not in ids:
            ids.append(tid)
    return ids


def unresolved(wt: Path) -> list[str]:
    return homegit.git(wt, "diff", "--name-only", "--diff-filter=U").stdout.split()


@dataclass
class Landing:
    piece: str
    merged: bool
    conflicts: list[str]
    tasks: dict[str, str]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def land(target: str) -> Landing:
    """Commit the piece and merge it into the home. On a clash the home's branch is merged into the piece instead and
    the conflicting files are reported; after the agent resolves them, `land` again finishes. Landed tasks are linted
    again in the home (lint results are not versioned), then the worktree, branch and repo copies go."""
    home = home_repo()
    p = find_piece(home, target)
    wt = Path(p.worktree)
    if p.state != "open":
        raise OpsquireError(f"piece {p.group}/{p.name} is parked; open it again first: {reopen(p)}")
    touched = unresolved(wt) + homegit.git(wt, "diff", "--name-only", "HEAD").stdout.split()
    left = homegit.has_markers(wt, touched)
    if left:
        return Landing(f"{p.group}/{p.name}", False, left, {})
    homegit.commit(wt, None, f"piece {p.group}/{p.name}: built")
    homegit.commit(home, None, f"snapshot before landing piece {p.group}/{p.name}")
    ids = changed_ids(home, p.branch)
    message = f"land piece {p.group}/{p.name}"
    if not homegit.merge_into(home, p.branch, message):
        conflicts = homegit.merge_from(wt, homegit.branch(home))
        if conflicts:
            return Landing(f"{p.group}/{p.name}", False, conflicts, {})
        if not homegit.merge_into(home, p.branch, message):
            raise OpsquireError("could not merge into the home: it changed again while landing; run land once more")
    results: dict[str, str] = {}
    for tid in ids:
        folder = next(iter(home.glob(f"tasks/*/{tid}")), None) or home / "general" / tid
        if not folder.is_dir():
            results[tid] = "deleted"
            continue
        task = load_task(folder)
        result = lint_task(task)
        save_lint(task, result)
        results[tid] = "ready" if result.ok else "lint fails: " + result.output.strip().splitlines()[-1][:200]
    repos.drop(f"piece-{p.group}-{p.name}")
    homegit.remove_worktree(home, wt)
    homegit.delete_branch(home, p.branch)
    return Landing(f"{p.group}/{p.name}", True, [], results)


def drop(target: str, delete: bool) -> str:
    """Park an unfinished piece (its work stays on its branch; `piece open` with the same name and group resumes it)
    or, with `delete`, discard it entirely."""
    home = home_repo()
    p = find_piece(home, target)
    wt = Path(p.worktree)
    if p.state == "open":
        if not delete:
            homegit.git(wt, "merge", "--abort", check=False)
            homegit.commit(wt, None, f"piece {p.group}/{p.name}: parked")
        homegit.remove_worktree(home, wt)
    repos.drop(f"piece-{p.group}-{p.name}")
    if delete:
        homegit.delete_branch(home, p.branch)
        return f"piece {p.group}/{p.name}: discarded"
    return f"piece {p.group}/{p.name}: parked on {p.branch}; {reopen(p)} resumes it"


def brief(info: PieceInfo) -> str:
    """The text a chat agent hands its subagent: where to work and how, in the piece's own words."""
    exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in info.env.items())
    lines = [
        f"You build one piece of a larger request, in your own copy of the opsquire home at {info.worktree}.",
        f"Before every `opsquire` command, and for every file you touch, use that copy: export {exports}",
        f"Read {info.worktree}/AGENTS.md (the contract) and {info.worktree}/docs/CATALOG.md first; the contract wins.",
        "Build only your piece. Do not interview the operator: if a decision is truly open, stop and end with",
        "ASK: <question> and RECOMMEND: <answer> lines; otherwise end with RUN:/PR:/NEED: lines as the contract says.",
    ]
    if info.request:
        lines.append(f"Your piece: {info.request}")
    return "\n".join(lines)


def describe(infos: list[PieceInfo]) -> list[list[str]]:
    rows = [["GROUP", "NAME", "STATE", "WORKTREE", "REQUEST"]]
    for p in infos:
        req = p.request[:60].replace("\n", " ")
        rows.append([p.group, p.name, p.state, p.worktree if p.state == "open" else "", req])
    return rows
