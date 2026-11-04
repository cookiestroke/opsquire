"""Closing a task: how it really ended (done, partial, abandoned) with a note, written to its meta.json only. Shared
by `opsquire close` and the UI, the one write the UI makes."""

from __future__ import annotations

import os
from typing import Any

from .homegit import commit_quietly
from .model import OUTCOME_STATES, OpsquireError, home_dir, iso, now, resolve_task, write_json
from .query import find_run


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
    elif not any(line.startswith("## Result") for line in design.read_text().splitlines()):
        notes.append("note: design.md has no '## Result' section; add one saying what ended up live")
    note_commit = commit_quietly(home_dir(), [task.folder], f"closed {outcome}: {task.id}")
    if note_commit:
        notes.append(note_commit)
    return {"id": task.id, "outcome": record, "notes": notes}
