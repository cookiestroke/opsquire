"""Inbox items that wait: a stale one asks for a decision (run, supersede, abandon), a task that never ran expires
to abandoned, and the agent that wrote a task hears what became of it."""

from __future__ import annotations

import datetime as dt
from typing import Any

from .close import close_task
from .model import Task, all_task_folders, author_key, decision, decision_text, load_task, now, parse_iso
from .query import all_runs
from .state import INBOX_STALE_DAYS, days_since, state

EXPIRE_DAYS = 14.0
TOLD_DAYS = 7.0
EXPIRY_AUTHOR = "opsquire (expiry)"


def started_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in runs if r.get("status") != "blocked"]


def never_ran_since(task: Task) -> str:
    handed = task.raw.get("handover")
    if isinstance(handed, dict) and handed.get("at"):
        return str(handed["at"])
    return str(task.raw.get("created") or "")


def expiring(at: dt.datetime) -> list[tuple[Task, float]]:
    """Open tasks and plans that never started a run in EXPIRE_DAYS. Anything that ran once, a failed or partial
    apply above all, waits for a person's decision instead."""
    runs: dict[str, list[dict[str, Any]]] = {}
    for r in all_runs():
        runs.setdefault(str(r.get("task_id")), []).append(r)
    out = []
    for folder in all_task_folders():
        task = load_task(folder)
        m = task.meta
        if m is None or task.kind == "general" or m.archived or m.superseded_by or task.raw.get("outcome"):
            continue
        if started_runs(runs.get(task.id, [])):
            continue
        days = days_since(never_ran_since(task), at)
        if days >= EXPIRE_DAYS:
            out.append((task, days))
    return out


def expire_unrun(at: dt.datetime | None = None) -> list[str]:
    at = at or now()
    expired = []
    for task, days in expiring(at):
        close_task(task.id, "abandoned", f"expired: never ran in {days:.0f} days", by=EXPIRY_AUTHOR)
        expired.append(task.id)
    return expired


def inbox_items(view: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for it in view["inbox"]:
        out.append(it)
        out.extend(it.get("steps") or [])
    return out


def stale_items(view: dict[str, Any]) -> list[dict[str, Any]]:
    return [it for it in inbox_items(view) if it.get("stale")]


def reminder(view: dict[str, Any]) -> str | None:
    stale = stale_items(view)
    if not stale:
        return None
    oldest = max(stale, key=lambda it: float(it.get("waiting_days") or 0))
    return (
        f"{len(stale)} inbox item{'s' if len(stale) != 1 else ''} waiting {INBOX_STALE_DAYS:g}+ days need a decision "
        f"(oldest {oldest['task_id']}, {oldest['waiting_days']:.0f} days): run, supersede or abandon; "
        "`opsquire ui` lists them"
    )


def is_recent(stamp: str, at: dt.datetime) -> bool:
    try:
        return (at - parse_iso(stamp)).total_seconds() < TOLD_DAYS * 86400
    except ValueError:
        return False


def told(author: str, at: dt.datetime | None = None) -> list[str]:
    """What the author of tasks should hear: decisions others made on them this week, and their items that wait."""
    key = author_key(author)
    if not key:
        return []
    at = at or now()
    lines = []
    mine: set[str] = set()
    for folder in all_task_folders():
        task = load_task(folder)
        if task.meta is None or author_key(task.meta.author) != key:
            continue
        mine.add(task.id)
        d = decision(task.raw)
        if d and author_key(d["by"]) != key and is_recent(d["at"], at):
            lines.append(decision_text(task.id, d))
    if mine:
        for it in stale_items(state(at)):
            if it["task_id"] in mine:
                lines.append(f"{it['task_id']} is {it['stale']}")
    return lines
