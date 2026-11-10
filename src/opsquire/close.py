"""Closing a task: how it really ended (done, partial, abandoned) with a note, written to its meta.json only. Shared
by `opsquire close` and the UI, the one write the UI makes. Also the two other decisions about a task that will not
run as it is: superseded by a newer task, and reopened after a decision that was wrong."""

from __future__ import annotations

import os
from typing import Any

from .homegit import commit_quietly
from .model import OUTCOME_STATES, OpsquireError, Task, home_dir, is_last_stage, iso, now, resolve_task, write_json
from .query import find_run, runs_for


def close_task(query: str, outcome: str, note: str, run: str | None = None, by: str = "") -> dict[str, Any]:
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
        "closed_by": by or os.environ.get("OPSQUIRE_AUTHOR", "unknown"),
        "closed": iso(now()),
    }
    if run_id:
        record["run"] = run_id
    raw = dict(task.raw)
    raw["outcome"] = record
    if outcome == "abandoned":
        raw["abandoned_reason"] = note
    else:
        raw.pop("abandoned_reason", None)
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


def open_task_for_decision(query: str, verb: str) -> Task:
    task = resolve_task(query)
    if task.meta is None:
        raise OpsquireError(f"cannot {verb} a task with invalid meta.json: " + "; ".join(task.errors))
    if task.kind == "general":
        raise OpsquireError(f"general scripts are not {verb}d; {verb} the tasks that use them")
    if task.meta.archived:
        raise OpsquireError("archived tasks are historical records and stay as they are")
    return task


def supersede_task(query: str, by_query: str, note: str = "") -> dict[str, Any]:
    """Record that a newer task replaced this one: it leaves the inbox and refuses to run."""
    task = open_task_for_decision(query, "supersede")
    newer = resolve_task(by_query)
    if newer.id == task.id:
        raise OpsquireError("a task cannot supersede itself")
    if newer.kind == "general":
        raise OpsquireError(f"{newer.id} is a general script; name the task that replaced {task.id}")
    if task.meta and (task.meta.superseded_by or task.meta.outcome):
        raise OpsquireError(f"{task.id} is already closed or superseded; `opsquire reopen {task.id}` first")
    record = {"by": os.environ.get("OPSQUIRE_AUTHOR", "unknown"), "at": iso(now()), "note": note.strip()}
    raw = {k: v for k, v in task.raw.items() if k != "handover"}
    raw["superseded_by"] = newer.id
    raw["superseded"] = record
    write_json(task.folder / "meta.json", raw)
    commit_quietly(home_dir(), [task.folder], f"superseded by {newer.id}: {task.id}")
    return {"id": task.id, "superseded_by": newer.id, "superseded": record}


REOPENED_KEYS = ("outcome", "abandoned_reason", "superseded_by", "superseded")


def reopen_task(query: str, note: str) -> dict[str, Any]:
    """Undo a close or a supersession (an expiry that was wrong, say): the task is open again and what it was is
    kept in `reopened`."""
    task = open_task_for_decision(query, "reopen")
    was = {k: task.raw[k] for k in REOPENED_KEYS if k in task.raw}
    if not was:
        raise OpsquireError(f"{task.id} is not closed or superseded; there is nothing to reopen")
    note = note.strip()
    if not note:
        raise OpsquireError("--note is empty: say why it is open again")
    record = {"by": os.environ.get("OPSQUIRE_AUTHOR", "unknown"), "at": iso(now()), "note": note, "was": was}
    raw = {k: v for k, v in task.raw.items() if k not in REOPENED_KEYS}
    raw["reopened"] = record
    write_json(task.folder / "meta.json", raw)
    commit_quietly(home_dir(), [task.folder], f"reopened: {task.id}")
    return {"id": task.id, "reopened": record}


def has_result(task: Task) -> bool:
    design = task.folder / "design.md"
    return design.is_file() and any(line.startswith("## Result") for line in design.read_text().splitlines())


def unwritten(run: dict[str, Any]) -> bool:
    """A verify run that finished ok with every target `skipped`: the scaffolded check nobody wrote, which says
    nothing. A blocked or failed verify run is not this, even though its targets read skipped."""
    targets = run.get("targets") or {}
    return (
        run.get("status") == "ok"
        and bool(targets)
        and all((v.get("status") if isinstance(v, dict) else v) == "skipped" for v in targets.values())
    )


def auto_close(task_id: str, since: str) -> str | None:
    """After an apply run finished ok: close the task as done with a note built from the run record and write the
    Result section of its design.md from it, unless an agent or the operator already closed it. The common case
    ends itself; `opsquire close` stays for the runs that lie (a failed run whose host ended up fine). A task with a
    verify check closes only on that check: `opsquire run --apply` runs it after an ok apply, and an apply whose
    check reports a target not ok (a build stage before the deploy) leaves the task open. A check that is still the
    unwritten scaffold (every target skipped) says nothing, so the apply closes the task as before."""
    task = resolve_task(task_id)
    if task.meta is None or task.kind == "general" or task.meta.archived or task.meta.outcome:
        return None
    runs = [r for r in runs_for(task.id) if str(r.get("started", "")) >= since]
    last = runs[-1] if runs else None
    if last and len(runs) > 1 and last.get("mode") == "verify" and unwritten(last):
        last = runs[-2]
    if not last or last.get("mode") not in ("apply", "verify") or last.get("status") != "ok":
        return None
    if last.get("mode") == "apply" and task.meta.verify and last is runs[-1]:
        return None
    if last.get("mode") == "apply" and not is_last_stage(task.meta, [str(x) for x in last.get("argv") or []]):
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
