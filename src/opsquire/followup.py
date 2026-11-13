"""An ask ends on RUN lines, before the operator has run anything, so its agent never saw what its runs found and the
operator had to ask again. The console follows them for it: once a run of a task an ask's conversation handed over
finishes after that conversation's last answer, it resumes the conversation with the run's outcome, and the agent
reads the record and carries on, as the contract asks of every agent that hands over a run."""

from __future__ import annotations

import datetime as dt
import shlex
from collections.abc import Callable
from typing import Any

from .handover import RUN_LINE_RE
from .model import iso

FOLLOW_DAYS = 3.0


def conversation_of(ask: dict[str, Any]) -> str:
    return str(ask.get("copies") or ask.get("ask_id") or "")


def handed_tasks(asks: list[dict[str, Any]]) -> list[str]:
    """The tasks these asks handed over in their RUN lines, in order."""
    out: list[str] = []
    for a in asks:
        for action in a.get("actions") or []:
            m = RUN_LINE_RE.match(str(action.get("text") or "").strip()) if action.get("kind") == "RUN" else None
            if m and m.group("task") not in out:
                out.append(m.group("task"))
    return out


def due(
    asks: list[dict[str, Any]],
    runs_of: Callable[[str], list[dict[str, Any]]],
    busy_conversations: set[str],
    busy_tasks: set[str],
    at: dt.datetime,
) -> list[tuple[str, list[dict[str, Any]]]]:
    """(ask to resume, its finished runs) for every conversation that owes its agent a look at a run: its last ask
    ended ok within FOLLOW_DAYS on RUN lines, a run of one of those tasks ended since, and nothing of it still goes
    (no ask job of the conversation, no run or job of its tasks). A run that ended before the last answer was the
    agent's to see then, so each run is reported once."""
    by: dict[str, list[dict[str, Any]]] = {}
    for a in sorted(asks, key=lambda a: str(a.get("started") or "")):
        by.setdefault(conversation_of(a), []).append(a)
    since = iso(at - dt.timedelta(days=FOLLOW_DAYS))
    out = []
    for conv, items in by.items():
        last = items[-1]
        ended = str(last.get("ended") or "")
        if conv in busy_conversations or last.get("status") != "ok" or not ended or ended < since:
            continue
        tasks = handed_tasks(items)
        if not tasks or busy_tasks.intersection(tasks):
            continue
        runs = [r for t in tasks for r in runs_of(t)]
        if any(r.get("status") == "running" for r in runs):
            continue
        new = sorted((r for r in runs if str(r.get("ended") or "") > ended), key=lambda r: str(r.get("ended")))
        if new:
            out.append((str(last.get("ask_id")), new))
    return out


def report(runs: list[dict[str, Any]]) -> str:
    """The message that resumes the conversation: what ran and how it ended, then what is expected."""
    lines = ["The operator ran what you handed over (the console sends this when the runs finish):"]
    for r in runs:
        flag = {"apply": " --apply", "verify": " --verify"}.get(str(r.get("mode")), "")
        args = [str(x) for x in r.get("argv") or []]
        cmd = f"opsquire run {r.get('task_id')}{flag}" + (" -- " + shlex.join(args) if args else "")
        error = f": {r.get('last_error')}" if r.get("last_error") and r.get("status") != "ok" else ""
        lines.append(f"- `{cmd}` ended {r.get('status')} (run {r.get('run_id')}){error}")
    lines.append(
        "Read the records (`opsquire log <run-id>`) and continue from them: report what they found, fix and hand "
        "over again, or close the task."
    )
    return "\n".join(lines)
