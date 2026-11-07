"""Connect opsquire to the agent tools on this machine: install its skill for Claude Code, so agents in any folder
follow the contract, and make the home a local-only git repository (parallel asks work in its worktrees).
Idempotent."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .homegit import ensure_repo
from .model import DATA_DIR, home_dir, write_text

SKILL_SRC = DATA_DIR / "skills" / "opsquire" / "SKILL.md"
COMMAND_PLACEHOLDER = "{{opsquire_command}}"


def own_command() -> str:
    """How to call this opsquire from a shell that does not have it on PATH, as agents' non-login shells often do."""
    found = shutil.which("opsquire")
    if found:
        return str(Path(found).resolve())
    script = Path(sys.argv[0])
    if script.name == "opsquire" and script.is_file():
        return str(script.resolve())
    return f"{sys.executable} -m opsquire"


def claude_skill_files() -> list[Path]:
    """Every Claude Code config dir an agent may use: $CLAUDE_CONFIG_DIR when set (terminal sessions) and the
    default ~/.claude (apps started without that variable)."""
    dirs = [os.environ.get("CLAUDE_CONFIG_DIR", ""), str(Path.home() / ".claude")]
    unique = list(dict.fromkeys(Path(d).expanduser() for d in dirs if d))
    return [d / "skills" / "opsquire" / "SKILL.md" for d in unique]


def install_skill(dry: bool) -> list[str]:
    """Install or update the opsquire skill in each Claude config dir; one line per dir."""
    want = SKILL_SRC.read_text().replace(COMMAND_PLACEHOLDER, own_command())
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


def integrate(dry: bool) -> list[str]:
    return [*install_skill(dry), ensure_repo(home_dir(), dry)]
