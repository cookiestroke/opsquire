"""The home's AGENTS.md: the packaged contract plus the home's LOCAL.md, written by opsquire and kept current. The
file carries a hash of its own text, so an edit made in it by hand is noticed and never overwritten."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .integrate import COMMAND_PLACEHOLDER, own_command
from .model import DATA_DIR, OpsquireError, write_text

MARK = "<!-- written by opsquire from the packaged contract and LOCAL.md; edit LOCAL.md, not this file. sha256:"


def contract_text(home: Path) -> str:
    text = (DATA_DIR / "AGENTS.md").read_text().replace(COMMAND_PLACEHOLDER, own_command())
    local = home / "LOCAL.md"
    if local.is_file():
        text += "\n## Local rules for this home\n\n" + local.read_text()
    return text


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def stamped(text: str) -> str:
    return f"{text.rstrip()}\n\n{MARK}{digest(text.rstrip())} -->\n"


def state(home: Path) -> str:
    """missing, current, outdated (written by opsquire, since changed upstream) or edited (by hand)."""
    path = home / "AGENTS.md"
    if not path.is_file():
        return "missing"
    have = path.read_text()
    if have == stamped(contract_text(home)):
        return "current"
    body, _, last = have.rstrip("\n").rpartition("\n")
    if last.startswith(MARK) and last.endswith(" -->") and last[len(MARK) : -4] == digest(body.rstrip()):
        return "outdated"
    return "edited"


EDITED = (
    "AGENTS.md was edited by hand since opsquire wrote it: move the edit into LOCAL.md, then "
    "`opsquire contract --write --force`"
)


def write_contract(home: Path, force: bool = False) -> None:
    if not force and state(home) == "edited":
        raise OpsquireError(f"not written: {EDITED}")
    write_text(home / "AGENTS.md", stamped(contract_text(home)))
    write_text(home / "CLAUDE.md", "@AGENTS.md\n")


def sync(home: Path) -> str | None:
    """Rewrite AGENTS.md when the packaged contract or LOCAL.md changed; a note when it cannot."""
    now = state(home)
    if now == "outdated":
        write_contract(home)
        return "AGENTS.md rewritten: the contract or LOCAL.md changed"
    if now == "edited":
        return EDITED
    return None
