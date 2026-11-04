"""Release helpers for the workflow in .github/workflows/release.yml, stdlib only.

    python3 scripts/release.py version                     the version in pyproject.toml
    python3 scripts/release.py next patch|minor|major     the version a release of that size gets
    python3 scripts/release.py set-version 0.3.0          rewrite pyproject.toml to that version
    python3 scripts/release.py notes v0.3.0 [--since v0.2.0]   release notes from the commits since the last tag

Notes group commit subjects by their `area:` prefix (the convention of this repo's history), keep the first
paragraph of each body as the explanation, and end with the compare link."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
VERSION_RE = re.compile(r'^version = "(?P<v>\d+\.\d+\.\d+)"$', re.M)
SUBJECT_RE = re.compile(r"^(?P<area>[a-z][a-z0-9_/-]*): (?P<text>.+)$")
RELEASE_SUBJECT = "release: v"


class ReleaseError(Exception):
    pass


def current_version(text: str | None = None) -> str:
    m = VERSION_RE.search(PYPROJECT.read_text() if text is None else text)
    if not m:
        raise ReleaseError(f"no version line in {PYPROJECT}")
    return m.group("v")


def bump(version: str, part: str) -> str:
    major, minor, patch = (int(x) for x in version.split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ReleaseError(f"bump must be major, minor or patch, not {part!r}")


def set_version(version: str, text: str | None = None) -> str:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ReleaseError(f"not a release version: {version!r} (want MAJOR.MINOR.PATCH)")
    source = PYPROJECT.read_text() if text is None else text
    updated, n = VERSION_RE.subn(f'version = "{version}"', source, count=1)
    if n != 1:
        raise ReleaseError(f"no version line in {PYPROJECT}")
    if text is None:
        PYPROJECT.write_text(updated)
    return updated


def git(*args: str) -> str:
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    if r.returncode:
        raise ReleaseError(f"git {' '.join(args[:2])} failed: {r.stderr.strip()}")
    return r.stdout


def previous_tag(exclude: str, before: str = "HEAD") -> str | None:
    """The newest release tag reachable from `before`, other than the one being made."""
    tags = git("tag", "--list", "v*", "--sort=-v:refname", "--merged", before).split()
    return next((t for t in tags if t != exclude), None)


def commits_between(since: str | None, until: str) -> list[tuple[str, str]]:
    """(subject, body) of every non-merge commit in (since, until], newest first."""
    span = f"{since}..{until}" if since else until
    raw = git("log", "--no-merges", "--format=%s%x00%b%x1e", span)
    out = []
    for rec in raw.split("\x1e"):
        if not rec.strip():
            continue
        subject, _, body = rec.lstrip("\n").partition("\x00")
        out.append((subject.strip(), body.strip()))
    return out


def notes(tag: str, since: str | None, commits: list[tuple[str, str]], slug: str) -> str:
    groups: dict[str, list[str]] = defaultdict(list)
    for subject, body in commits:
        if subject.startswith(RELEASE_SUBJECT):
            continue
        m = SUBJECT_RE.match(subject)
        area, text = (m.group("area"), m.group("text")) if m else ("other", subject)
        first = body.split("\n\n", 1)[0].replace("\n", " ").strip()
        groups[area].append(f"- {text}" + (f"\n  {first}" if first else ""))
    lines = []
    for area in sorted(groups):
        lines += [f"### {area}", *groups[area], ""]
    if not lines:
        lines = ["No changes since the previous release.", ""]
    compare = (
        f"https://github.com/{slug}/compare/{since}...{tag}" if since else f"https://github.com/{slug}/commits/{tag}"
    )
    lines.append(f"**Full changelog**: {compare}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("version")
    n = sub.add_parser("next")
    n.add_argument("part", choices=["major", "minor", "patch"])
    s = sub.add_parser("set-version")
    s.add_argument("version")
    r = sub.add_parser("notes")
    r.add_argument("tag")
    r.add_argument("--since", help="previous tag (default: the newest v* tag reachable from HEAD)")
    r.add_argument("--repo", default="cookiestroke/opsquire", help="owner/name for the compare link")
    a = p.parse_args(argv)
    try:
        if a.cmd == "version":
            print(current_version())
        elif a.cmd == "next":
            print(bump(current_version(), a.part))
        elif a.cmd == "set-version":
            set_version(a.version)
            print(a.version)
        else:
            since = a.since or previous_tag(a.tag)
            print(notes(a.tag, since, commits_between(since, "HEAD"), a.repo), end="")
    except ReleaseError as e:
        print(f"release: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
