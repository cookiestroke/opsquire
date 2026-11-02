"""Code and config changes as GitHub PRs, without touching the operator's own checkouts. `checkout` exports a repo's
base branch (read-only git, `git archive`) into a copy under the home's `.repos/`; the agent edits and tests the
copy; `pr` compares it with the base tree and turns the differences into one GitHub-signed commit
(createCommitOnBranch) on a branch, then opens the PR or updates it. Nothing is ever merged or deployed."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import homegit
from .model import OpsquireError, home_dir, main_home, read_json, write_json

REPOS_FILE = "repos.json"
COPIES = homegit.REPO_COPIES
STATE = ".opsquire-pr.json"
AUTO_DEPLOY_BRANCHES = {"hedger_v2_main"}


@dataclass
class Repo:
    name: str
    path: Path
    github: str
    base: str
    branch_prefix: str


def registry() -> dict[str, Repo]:
    """Repos agents may change, from the home's repos.json: {"umm": {"path": "~/umm", "github": "owner/umm",
    "base": "main"}, ...} plus an optional top-level "branch_prefix" (default "opsquire/")."""
    path = home_dir() / REPOS_FILE
    if not path.is_file():
        return {}
    raw: dict[str, Any] = read_json(path)
    prefix = str(raw.get("branch_prefix", "opsquire/"))
    repos = {}
    for name, cfg in (raw.get("repos") or {}).items():
        repos[name] = Repo(name, Path(cfg["path"]).expanduser(), cfg["github"], cfg.get("base", "main"), prefix)
    return repos


def get_repo(name: str) -> Repo:
    repos = registry()
    if name not in repos:
        known = ", ".join(repos) or f"none (add them to {home_dir() / REPOS_FILE})"
        raise OpsquireError(f"unknown repo {name!r}; registered: {known}")
    return repos[name]


def git(repo: Path, *args: str, binary: bool = False) -> Any:
    exe = shutil.which("git")
    if not exe:
        raise OpsquireError("git is not installed")
    r = subprocess.run([exe, "-C", str(repo), *args], capture_output=True, text=not binary)
    if r.returncode != 0:
        err = r.stderr if isinstance(r.stderr, str) else r.stderr.decode(errors="replace")
        raise OpsquireError(f"git {' '.join(args[:2])} in {repo}: {err.strip()[:300]}")
    return r.stdout


def gh(*args: str, stdin: str | None = None) -> str:
    exe = shutil.which("gh")
    if not exe:
        raise OpsquireError("the GitHub CLI (gh) is not installed or not on PATH")
    r = subprocess.run([exe, *args], capture_output=True, text=True, input=stdin)
    if r.returncode != 0:
        raise OpsquireError(f"gh {' '.join(args[:2])}: {(r.stderr or r.stdout).strip()[:400]}")
    return r.stdout


def fetch(repo: Repo) -> bool:
    """Best effort: refresh origin/<base>. Fails quietly where .git is not writable (agent sandboxes); asks fetch
    every registered repo from the operator's terminal before the agent starts."""
    exe = shutil.which("git")
    if not exe or not repo.path.is_dir():
        return False
    r = subprocess.run([exe, "-C", str(repo.path), "fetch", "-q", "origin", repo.base], capture_output=True)
    return r.returncode == 0


def copies_dir(owner: str) -> Path:
    """Copies live in the main home (not a parallel piece's worktree), so they outlive a parked piece."""
    return main_home() / COPIES / owner


def owner_id() -> str:
    """The ask this copy belongs to (or "chat" for agents outside an ask)."""
    return re.sub(r"[^A-Za-z0-9_.-]", "-", os.environ.get("OPSQUIRE_ASK_ID", "chat"))


def checkout(name: str) -> Path:
    """Export the repo's base branch into a copy; returns its path. An ask reuses its one copy per repo; outside an
    ask every checkout is a new copy, so chat sessions never share (and push) each other's edits."""
    repo = get_repo(name)
    owner = owner_id()
    dest = copies_dir(owner) / (name if owner != "chat" else f"{name}-{secrets.token_hex(4)}")
    if (dest / STATE).is_file():
        return dest
    fetch(repo)
    sha = git(repo.path, "rev-parse", f"origin/{repo.base}").strip()
    staging = dest.with_name(f".{name}.{os.getpid()}.tmp")
    try:
        staging.mkdir(parents=True)
        archive = staging / ".archive.tar"
        archive.write_bytes(git(repo.path, "archive", "--format=tar", sha, binary=True))
        with tarfile.open(archive) as tar:
            tar.extractall(staging, filter="tar")
        archive.unlink()
        write_json(staging / STATE, {"repo": name, "base": repo.base, "base_sha": sha})
        staging.rename(dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return dest


def copy_state(copy: Path) -> dict[str, Any]:
    state = copy / STATE
    if not state.is_file():
        raise OpsquireError(f"{copy} is not a repo copy made by `opsquire repo checkout`")
    data: dict[str, Any] = read_json(state)
    return data


def changes(copy: Path) -> tuple[list[str], list[str]]:
    """(changed or added paths, deleted paths) of a copy against its base commit, by git blob hash."""
    state = copy_state(copy)
    repo = get_repo(state["repo"])
    base: dict[str, str] = {}
    for line in git(repo.path, "ls-tree", "-r", "-z", state["base_sha"]).split("\0"):
        if line:
            meta, path = line.split("\t", 1)
            if meta.split()[1] == "blob":
                base[path] = meta.split()[2]
    files = [f for f in walk(copy) if not ignored(f)]
    hashes = {f: blob_sha(copy / f) for f in files}
    changed = [f for f in files if base.get(f) != hashes[f]]
    deleted = sorted(f for f in base if not os.path.lexists(copy / f))
    return changed, deleted


def walk(copy: Path) -> list[str]:
    """Every file and symlink in a copy (symlinked folders are entries, never followed), relative and sorted."""
    out = []
    for root, dirs, names in os.walk(copy):
        rel = Path(root).relative_to(copy)
        for d in dirs[:]:
            if (Path(root) / d).is_symlink():
                dirs.remove(d)
                names.append(d)
        out += [str(rel / n) for n in names if not (rel == Path(".") and n == STATE)]
    return sorted(str(Path(f)) for f in out)


def blob_sha(path: Path) -> str:
    """Git's blob id: a symlink is stored as its target text, never followed."""
    data = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def ignored(path: str) -> bool:
    """Build and test leftovers that never belong in a PR."""
    parts = Path(path).parts
    caches = {"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
    return any(p in caches for p in parts) or path.endswith((".pyc", ".pyo"))


def slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    out = ""
    for w in words:
        if len(out) + len(w) + 1 > 40:
            break
        out = f"{out}-{w}" if out else w
    return out or "change"


def open_pr(copy: Path, title: str, body: str, branch: str | None = None) -> dict[str, Any]:
    """Commit the copy's changes (GitHub-signed) to a branch and open its PR, or add a commit to an existing one."""
    state = copy_state(copy)
    repo = get_repo(state["repo"])
    changed, deleted = changes(copy)
    if not changed and not deleted:
        raise OpsquireError(f"{copy} has no changes against {repo.base}")
    explicit = branch is not None
    earlier = state.get("branch")
    if earlier and not explicit and state.get("title") != title:
        raise OpsquireError(
            f"{copy} already pushed to branch {earlier} ({state.get('pr')}) for "
            f"{state.get('title') or 'an earlier PR'!r}; for a new PR run `opsquire repo checkout {repo.name}` and "
            f"edit the new copy, or pass --branch {earlier} to add these changes to that PR"
        )
    branch = branch or earlier or f"{repo.branch_prefix}{slug(title)}"
    if earlier and branch != earlier:
        state["pushed"] = {}
    head = branch_head(repo, branch)
    if head is None:
        gh(
            "api",
            "-X",
            "POST",
            f"repos/{repo.github}/git/refs",
            "-f",
            f"ref=refs/heads/{branch}",
            "-f",
            f"sha={state['base_sha']}",
        )
        head = state["base_sha"]
    elif not state.get("branch") and not explicit:
        raise OpsquireError(f"branch {branch} already exists on {repo.github}; pass --branch with a new name")
    links = [f for f in changed if (copy / f).is_symlink()]
    if links:
        raise OpsquireError(f"symlinks cannot be changed through a PR from here: {', '.join(links)}")
    pushed: dict[str, str] = state.get("pushed") or {}
    current = {f: blob_digest(copy / f) for f in changed} | {f: "deleted" for f in deleted}
    current |= {f: blob_digest(copy / f) for f in pushed if f not in current and (copy / f).is_file()}
    send = {f: d for f, d in current.items() if pushed.get(f) != d}
    if not send and state.get("pr"):
        return summary(repo, state, changed, deleted)
    additions = [
        {"path": f, "contents": base64.b64encode((copy / f).read_bytes()).decode()}
        for f, d in send.items()
        if d != "deleted"
    ]
    removals = [{"path": f} for f, d in send.items() if d == "deleted"]
    mutation = "mutation($input: CreateCommitOnBranchInput!) { createCommitOnBranch(input: $input) { commit { oid } } }"
    headline, _, rest = title.partition("\n")
    payload = {
        "query": mutation,
        "variables": {
            "input": {
                "branch": {"repositoryNameWithOwner": repo.github, "branchName": branch},
                "expectedHeadOid": head,
                "message": {"headline": headline, "body": rest.strip()},
                "fileChanges": {"additions": additions, "deletions": removals},
            }
        },
    }
    out = json.loads(gh("api", "graphql", "--input", "-", stdin=json.dumps(payload)))
    oid = ((out.get("data") or {}).get("createCommitOnBranch") or {}).get("commit", {}).get("oid")
    if not oid:
        raise OpsquireError(f"GitHub refused the commit: {json.dumps(out.get('errors') or out)[:400]}")
    existing = gh("pr", "list", "--repo", repo.github, "--head", branch, "--state", "open", "--json", "url")
    urls = [p["url"] for p in json.loads(existing or "[]")]
    url = (
        urls[0]
        if urls
        else gh(
            "pr",
            "create",
            "--repo",
            repo.github,
            "--base",
            repo.base,
            "--head",
            branch,
            "--title",
            headline,
            "--body",
            body,
        )
        .strip()
        .splitlines()[-1]
    )
    state |= {"branch": branch, "title": title, "pr": url, "commit": oid, "pushed": pushed | send}
    write_json(copy / STATE, state)
    return summary(repo, state, changed, deleted)


def summary(repo: Repo, state: dict[str, Any], changed: list[str], deleted: list[str]) -> dict[str, Any]:
    return {
        "repo": repo.name,
        "branch": state["branch"],
        "base": repo.base,
        "commit": state["commit"],
        "url": state["pr"],
        "changed": changed,
        "deleted": deleted,
        "auto_deploy_base": repo.base in AUTO_DEPLOY_BRANCHES,
    }


def blob_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def branch_head(repo: Repo, branch: str) -> str | None:
    exe = shutil.which("gh")
    if not exe:
        raise OpsquireError("the GitHub CLI (gh) is not installed or not on PATH")
    r = subprocess.run(
        [exe, "api", f"repos/{repo.github}/git/ref/heads/{branch}", "--jq", ".object.sha"],
        capture_output=True,
        text=True,
    )
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def drop(owner: str) -> None:
    """Remove every repo copy of an ask (when the ask is finished)."""
    shutil.rmtree(copies_dir(owner), ignore_errors=True)
    parent = main_home() / COPIES
    if parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
