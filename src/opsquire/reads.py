"""Which run records each agent has read. An agent reads a record through `wait`, `log`, `runs <task>`, `last` or
`show`, or by running it itself; `handover` and `new` refuse an agent that has not read the task's latest run or
the results of the runs it handed over, and the stop hook keeps a session from ending its turn on them. The ledger
lives in the home, outside `var/`, which agents cannot write."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import homegit
from .model import author_key, main_home, read_json, var_dir, write_json
from .query import all_runs, runs_for

FINISHED = ("ok", "failed", "partial", "aborted", "interrupted", "blocked")


def ledger(key: str) -> Path:
    return main_home() / homegit.READ_LEDGERS / f"{key.replace(' ', '_')}.json"


def legacy_ledger(key: str) -> Path:
    return var_dir() / "reads" / f"{key.replace(' ', '_')}.json"


def load(path: Path) -> set[str]:
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return set()
    return {str(r) for r in data} if isinstance(data, list) else set()


def read_ids(author: str) -> set[str]:
    key = author_key(author)
    if not key:
        return set()
    return load(ledger(key)) | load(legacy_ledger(key))


def mark(author: str, run_ids: Iterable[str]) -> None:
    key = author_key(author)
    ids = {str(r) for r in run_ids if r}
    if not key or not ids:
        return
    have = read_ids(author)
    if ids <= have:
        return
    path = ledger(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, sorted(have | ids))
    except OSError as e:
        print(
            f"opsquire: could not record that {author} read {', '.join(sorted(ids - have))} "
            f"({path}: {e.strerror or e}); `handover` and `new` will refuse as if it were unread",
            file=sys.stderr,
        )


def mark_records(author: str, recs: Iterable[dict[str, Any]]) -> None:
    mark(author, (str(r.get("run_id")) for r in recs if r.get("status") in FINISHED))


def unread_results(author: str) -> list[dict[str, Any]]:
    """Finished runs this author handed over and has not read."""
    key = author_key(author)
    if not key:
        return []
    seen = read_ids(author)
    return [
        r
        for r in all_runs()
        if r.get("status") in FINISHED
        and author_key(str(r.get("handed_by") or "")) == key
        and str(r.get("run_id")) not in seen
    ]


def unread_latest(task_id: str, author: str) -> dict[str, Any] | None:
    """The task's latest finished run when this author has not read it."""
    if not author_key(author):
        return None
    done = [r for r in runs_for(task_id) if r.get("status") in FINISHED]
    if done and str(done[-1].get("run_id")) not in read_ids(author):
        return done[-1]
    return None


def describe(recs: list[dict[str, Any]]) -> str:
    return "; ".join(f"{r.get('run_id')} of {r.get('task_id')} ({r.get('mode')} {r.get('status')})" for r in recs)


def session_unread(session_id: str) -> list[dict[str, Any]]:
    """Unread results of the agents whose author names this Claude session (its id or the start of it)."""
    sid = session_id.lower()
    if not sid:
        return []
    out = []
    seen_by: dict[str, set[str]] = {}
    for r in all_runs():
        by = str(r.get("handed_by") or "")
        key = author_key(by)
        if r.get("status") not in FINISHED or not key:
            continue
        if not any(len(tok) >= 6 and sid.startswith(tok) for tok in key.split()):
            continue
        if key not in seen_by:
            seen_by[key] = read_ids(by)
        if str(r.get("run_id")) not in seen_by[key]:
            out.append(r)
    return out


def stop_hook(stdin: str) -> dict[str, Any] | None:
    """The Claude Code Stop hook's answer: block once per stop attempt while the session has unread results."""
    try:
        event = json.loads(stdin or "{}")
    except ValueError:
        return None
    if not isinstance(event, dict) or event.get("stop_hook_active"):
        return None
    unread = session_unread(str(event.get("session_id") or ""))
    if not unread:
        return None
    first = unread[0]
    return {
        "decision": "block",
        "reason": (
            f"opsquire: runs you handed over finished and you have not read them: {describe(unread)}. Read each "
            f"(`opsquire wait {first.get('task_id')} --json` or `opsquire log {first.get('run_id')}`) and act on it "
            "before ending your turn."
        ),
    }
