"""Connect opsquire to the agent tools on this machine: install its skill for Claude Code, so agents in any folder
(including Orca worktrees) follow the contract, and register the home as an Orca workspace. Idempotent."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .model import DATA_DIR, home_dir, write_text

SKILL_SRC = DATA_DIR / "skills" / "opsquire" / "SKILL.md"


def claude_skill_file() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(base) / "skills" / "opsquire" / "SKILL.md"


def install_skill(dry: bool) -> str:
    """Install or update the opsquire skill for Claude Code agents; returns what happened."""
    dest = claude_skill_file()
    want = SKILL_SRC.read_text()
    if dest.is_file() and dest.read_text() == want:
        return f"claude skill: unchanged ({dest})"
    verb = "updated" if dest.exists() else "installed"
    if dry:
        return f"claude skill: would be {verb} ({dest})"
    dest.parent.mkdir(parents=True, exist_ok=True)
    write_text(dest, want)
    return f"claude skill: {verb} ({dest})"


def orca_cli() -> str | None:
    """Orca's CLI is `orca-ide` on Linux; plain `orca` may be GNOME's screen reader, so it is never used."""
    return shutil.which("orca-ide")


def orca_json(cli: str, *args: str) -> dict[str, Any]:
    r = subprocess.run([cli, *args, "--json"], capture_output=True, text=True, timeout=60)
    try:
        data: dict[str, Any] = json.loads(r.stdout)
    except ValueError:
        return {"ok": False, "error": (r.stderr or r.stdout).strip()[:300] or f"exit {r.returncode}"}
    return data


def register_home(dry: bool) -> str:
    """Register the opsquire home as an Orca workspace, so agents can be launched there."""
    cli = orca_cli()
    if not cli:
        return "orca: not installed (no orca-ide on PATH); skipped"
    status = orca_json(cli, "status")
    if not ((status.get("result") or {}).get("runtime") or {}).get("reachable"):
        return "orca: not running; open Orca and run `opsquire integrate` again to register the home"
    home = str(home_dir())
    repos = orca_json(cli, "repo", "list")
    listed = json.dumps(repos.get("result", ""))
    if home in listed:
        return f"orca: home already registered ({home})"
    if dry:
        return f"orca: would register {home}"
    added = orca_json(cli, "repo", "add", "--path", home)
    if not added.get("ok", False):
        return f"orca: could not register {home}: {added.get('error') or added}"
    return f"orca: registered {home}"


def integrate(dry: bool) -> list[str]:
    return [install_skill(dry), register_home(dry)]
