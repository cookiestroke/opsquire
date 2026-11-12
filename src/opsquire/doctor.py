"""`opsquire doctor`: is this machine's opsquire working as it should, read from files alone so it answers from any
shell, an agent's sandbox included: the console, the agents' stop hook, the contract, the author, unread results."""

from __future__ import annotations

import json
from typing import Any

from . import contract, reads
from .integrate import HOOK_MARK, claude_skill_files
from .model import author_key, home_dir, now, parse_iso, read_json, var_dir
from .ui import HEALTH_STALE_S


def consoles() -> list[dict[str, Any]]:
    out = []
    for path in sorted((var_dir() / "ui").glob("*.json")):
        try:
            rec = read_json(path)
            age = (now() - parse_iso(str(rec.get("beat")))).total_seconds()
        except (OSError, ValueError, TypeError, AttributeError):
            continue
        out.append({**rec, "beat_age_s": round(age, 1), "alive": age < HEALTH_STALE_S})
    return out


def console_check() -> tuple[bool, str]:
    live = [c for c in consoles() if c["alive"]]
    if not live:
        return False, "console: not running (start it with `opsquire ui --open`)"
    lines = []
    ok = True
    for c in live:
        line = f"console: up at {c['url']} since {c['started']} (pid {c['pid']}, {c['jobs']} jobs running)"
        if c.get("stale_code"):
            ok = False
            line += f"; RUNS OLD CODE, restart it: {', '.join(c['stale_code'])} changed"
        if c.get("state_error"):
            ok = False
            line += f"; its last state build FAILED: {c['state_error']}"
        elif c.get("state_s") is not None:
            line += f"; last state build {c['state_s']}s at {c['state_at']}"
        else:
            line += "; no page has loaded its state yet"
        lines.append(line)
    return ok, "\n".join(lines)


def hook_check() -> tuple[bool, str]:
    found: list[str] = []
    missing: list[str] = []
    for skill in claude_skill_files():
        path = skill.parent.parent.parent / "settings.json"
        try:
            settings = json.loads(path.read_text())
            hooks = [h for g in settings.get("hooks", {}).get("Stop", []) for h in g.get("hooks", [])]
        except (OSError, ValueError, AttributeError):
            hooks = []
        (found if any(HOOK_MARK in str(h.get("command", "")) for h in hooks) else missing).append(str(path))
    if found:
        return True, f"stop hook: installed in {', '.join(found)}"
    return False, f"stop hook: missing in {', '.join(missing)} (`opsquire integrate` installs it)"


def report(author: str) -> tuple[bool, list[str]]:
    checks = [console_check(), hook_check()]
    state = contract.state(home_dir())
    checks.append((state == "current", f"contract: AGENTS.md {state}"))
    if author_key(author):
        unread = reads.unread_results(author)
        checks.append((True, f"author: {author}"))
        checks.append((not unread, f"unread results: {reads.describe(unread) if unread else 'none'}"))
    else:
        checks.append((False, f"author: {author or 'not set'}; set OPSQUIRE_AUTHOR so runs and reads are yours"))
    return all(ok for ok, _ in checks), [("ok   " if ok else "FIX  ") + text for ok, text in checks]
