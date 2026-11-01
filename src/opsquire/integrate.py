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


def claude_skill_files() -> list[Path]:
    """Every Claude Code config dir an agent may use: $CLAUDE_CONFIG_DIR when set (terminal sessions) and the
    default ~/.claude (apps started without that variable, such as Orca from the desktop)."""
    dirs = [os.environ.get("CLAUDE_CONFIG_DIR", ""), str(Path.home() / ".claude")]
    unique = list(dict.fromkeys(Path(d).expanduser() for d in dirs if d))
    return [d / "skills" / "opsquire" / "SKILL.md" for d in unique]


def install_skill(dry: bool) -> list[str]:
    """Install or update the opsquire skill in each Claude config dir; one line per dir."""
    want = SKILL_SRC.read_text()
    lines = []
    for dest in claude_skill_files():
        if dest.is_file() and dest.read_text() == want:
            lines.append(f"claude skill: unchanged ({dest})")
            continue
        verb = "updated" if dest.exists() else "installed"
        if dry:
            lines.append(f"claude skill: would be {verb} ({dest})")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        write_text(dest, want)
        lines.append(f"claude skill: {verb} ({dest})")
    return lines


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
    return [*install_skill(dry), register_home(dry)]
