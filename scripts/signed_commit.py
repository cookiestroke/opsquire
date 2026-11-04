"""Commit the staged changes of this checkout to GitHub as a commit GitHub signs itself (createCommitOnBranch),
then move the local branch onto it. No `git push` happens, so the result is a verified commit whatever the local
signing setup is. Used by hand for every change to main and by the release workflow for the version bump.

    python3 scripts/signed_commit.py --branch main -m "area: what changed" [-m "body paragraph"]

Needs `gh` logged in (or GH_TOKEN) and the remote `origin` on github.com. Refuses when nothing is staged, and
GitHub refuses when the branch moved since the last fetch, so a stale checkout never overwrites anyone's commit."""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


class SignedCommitError(Exception):
    pass


def run(*cmd: str, stdin: str | None = None, cwd: Path | None = None) -> str:
    r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, cwd=cwd)
    if r.returncode:
        raise SignedCommitError(f"{' '.join(cmd[:3])} failed: {(r.stderr or r.stdout).strip()}")
    return r.stdout


def repo_slug(checkout: Path) -> str:
    """owner/name of `origin`, whichever GitHub URL form it uses."""
    url = run("git", "config", "--get", "remote.origin.url", cwd=checkout).strip()
    m = re.search(r"github\.com[:/](?P<slug>[^/]+/[^/]+?)(?:\.git)?/?$", url)
    if not m:
        raise SignedCommitError(f"origin is not a GitHub remote: {url}")
    return m.group("slug")


def staged_changes(checkout: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """GraphQL fileChanges for what is staged: whole-file additions (also for modifications) and deletions."""
    status = run("git", "diff", "--cached", "--name-status", "-z", "--no-renames", cwd=checkout)
    fields = status.split("\0")
    additions, deletions = [], []
    for code, path in zip(fields[::2], fields[1::2], strict=False):
        if not path:
            continue
        if code.startswith("D"):
            deletions.append({"path": path})
        else:
            additions.append({"path": path, "contents": base64.b64encode(staged_bytes(checkout, path)).decode()})
    return additions, deletions


def staged_bytes(checkout: Path, path: str) -> bytes:
    r = subprocess.run(["git", "show", f":{path}"], capture_output=True, cwd=checkout)
    if r.returncode:
        raise SignedCommitError(f"git show :{path} failed: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout


def create_commit(
    slug: str,
    branch: str,
    expected_head: str,
    headline: str,
    body: str,
    additions: list[dict[str, str]],
    deletions: list[dict[str, str]],
) -> str:
    """Run the createCommitOnBranch mutation through `gh`; returns the new commit oid."""
    mutation = "mutation($input: CreateCommitOnBranchInput!) { createCommitOnBranch(input: $input) { commit { oid } } }"
    payload = {
        "query": mutation,
        "variables": {
            "input": {
                "branch": {"repositoryNameWithOwner": slug, "branchName": branch},
                "expectedHeadOid": expected_head,
                "message": {"headline": headline, "body": body},
                "fileChanges": {"additions": additions, "deletions": deletions},
            }
        },
    }
    out: dict[str, Any] = json.loads(run("gh", "api", "graphql", "--input", "-", stdin=json.dumps(payload)))
    oid = ((out.get("data") or {}).get("createCommitOnBranch") or {}).get("commit", {}).get("oid")
    if not isinstance(oid, str) or not oid:
        raise SignedCommitError(f"GitHub refused the commit: {json.dumps(out.get('errors') or out)[:400]}")
    return oid


def commit_staged(checkout: Path, branch: str, messages: list[str]) -> str:
    """Fetch, commit what is staged onto origin/<branch> as a signed commit, and move the local branch onto it.
    The working tree is left as it is: it already holds the committed content."""
    headline, body = messages[0].strip(), "\n\n".join(m.strip() for m in messages[1:] if m.strip())
    if not headline:
        raise SignedCommitError("the commit needs a headline (-m)")
    slug = repo_slug(checkout)
    run("git", "fetch", "-q", "origin", branch, cwd=checkout)
    expected = run("git", "rev-parse", f"origin/{branch}", cwd=checkout).strip()
    local = run("git", "rev-parse", "HEAD", cwd=checkout).strip()
    if local != expected:
        raise SignedCommitError(
            f"HEAD {local[:10]} is not origin/{branch} {expected[:10]}; bring the checkout up to date first "
            f"(git pull --ff-only), so the whole files sent here cannot revert anyone's commit"
        )
    additions, deletions = staged_changes(checkout)
    if not additions and not deletions:
        raise SignedCommitError("nothing is staged (git add the files first)")
    oid = create_commit(slug, branch, expected, headline, body, additions, deletions)
    run("git", "fetch", "-q", "origin", branch, cwd=checkout)
    run("git", "reset", "-q", oid, cwd=checkout)
    run("git", "update-ref", f"refs/remotes/origin/{branch}", oid, cwd=checkout)
    return oid


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=(__doc__ or "").split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--branch", default="main", help="branch to commit onto (default: main)")
    p.add_argument("-m", dest="messages", action="append", required=True, help="headline, then body paragraphs")
    p.add_argument("--checkout", type=Path, default=Path.cwd(), help="the git checkout (default: cwd)")
    a = p.parse_args(argv)
    try:
        oid = commit_staged(a.checkout.resolve(), a.branch, a.messages)
    except SignedCommitError as e:
        print(f"signed_commit: {e}", file=sys.stderr)
        return 2
    print(oid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
