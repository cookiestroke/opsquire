"""Closing a task: how it really ended (done, partial, abandoned) with a note, written to its meta.json only. Shared
by `opsquire close` and the UI, the one write the UI makes."""

from __future__ import annotations

import os
from typing import Any

from .homegit import commit_quietly
from .model import OUTCOME_STATES, OpsquireError, Task, home_dir, iso, now, resolve_task, write_json
from .query import find_run, runs_for


def close_task(query: str, outcome: str, note: str, run: str | None = None) -> dict[str, Any]:
    """Annotate the task's meta.json with the outcome; returns the task id and notes about what is still missing
    (no design.md, no Result section)."""
    if outcome not in OUTCOME_STATES:
        raise OpsquireError(f"outcome must be one of {', '.join(OUTCOME_STATES)}")
    task = resolve_task(query)
    if task.meta is None:
        raise OpsquireError("cannot close a task with invalid meta.json: " + "; ".join(task.errors))
    if task.kind == "general":
        raise OpsquireError("general scripts are not closed; close the tasks that use them")
    if task.meta.archived:
        raise OpsquireError("archived tasks are historical records and stay as they are")
    note = note.strip()
    if not note:
        raise OpsquireError("--note is empty: say what actually happened")
    run_id = None
    if run:
        found = find_run(run)
        if found is None or found.parent.name != task.id:
            raise OpsquireError(f"run {run!r} is not a run of {task.id}")
        run_id = found.name
    record: dict[str, Any] = {
        "state": outcome,
        "note": note,
        "closed_by": os.environ.get("OPSQUIRE_AUTHOR", "unknown"),
        "closed": iso(now()),
    }
    if run_id:
        record["run"] = run_id
    raw = dict(task.raw)
    raw["outcome"] = record
    if outcome == "abandoned":
        raw["abandoned_reason"] = note
    write_json(task.folder / "meta.json", raw)
    notes: list[str] = []
    design = task.folder / "design.md"
    if not design.is_file():
        notes.append("note: no design.md; the decisions behind this task are not recorded")
    elif not has_result(task):
        notes.append("note: design.md has no '## Result' section; add one saying what ended up live")
    note_commit = commit_quietly(home_dir(), [task.folder], f"closed {outcome}: {task.id}")
    if note_commit:
        notes.append(note_commit)
    return {"id": task.id, "outcome": record, "notes": notes}


def has_result(task: Task) -> bool:
    design = task.folder / "design.md"
    return design.is_file() and any(line.startswith("## Result") for line in design.read_text().splitlines())


def auto_close(task_id: str, since: str) -> str | None:
    """After an apply run finished ok: close the task as done with a note built from the run record and write the
    Result section of its design.md from it, unless an agent or the operator already closed it. The common case
    ends itself; `opsquire close` stays for the runs that lie (a failed run whose host ended up fine)."""
    task = resolve_task(task_id)
    if task.meta is None or task.kind == "general" or task.meta.archived or task.meta.outcome:
        return None
    runs = [r for r in runs_for(task.id) if str(r.get("started", "")) >= since]
    last = runs[-1] if runs else None
    if not last or last.get("mode") not in ("apply", "verify") or last.get("status") != "ok":
        return None
    targets = last.get("targets") or {}
    if last.get("mode") == "verify":
        states = {str(v.get("status") if isinstance(v, dict) else v) for v in targets.values()}
        if not targets or states - {"ok", "unchanged"}:
            return None
    by_status: dict[str, list[str]] = {}
    for name, info in targets.items():
        by_status.setdefault(str(info.get("status") if isinstance(info, dict) else info), []).append(name)
    parts = [f"{status} on {', '.join(sorted(names))}" for status, names in sorted(by_status.items())]
    results = [str(x) for x in last.get("results") or []]
    note = f"{last.get('mode')} run {last.get('run_id')} finished ok" + (f": {'; '.join(parts)}" if parts else "")
    if results:
        note += ". Results: " + " | ".join(results)[:1500]
    record: dict[str, Any] = {
        "state": "done",
        "note": note,
        "closed_by": "opsquire run (auto)",
        "closed": iso(now()),
        "run": last.get("run_id"),
    }
    raw = dict(task.raw)
    raw["outcome"] = record
    write_json(task.folder / "meta.json", raw)
    if not has_result(task):
        design = task.folder / "design.md"
        head = design.read_text().rstrip() + "\n\n" if design.is_file() else f"# {task.id}\n\n"
        lines = [f"- {r}" for r in results] or ["- (the run reported no results)"]
        design.write_text(
            head
            + f"## Result\n\nClosed automatically: {note.split('. Results:')[0]} ({record['closed']}).\n\n"
            + "\n".join(lines)
            + "\n"
        )
    commit_quietly(home_dir(), [task.folder], f"closed done (auto): {task.id}")
    return note
