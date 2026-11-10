"""Offboarding: a settled task (closed, superseded, or a read-only task that ran ok) that sat idle for the retention
period leaves tasks/ for offboarded/YYYY-MM/<id>/, so `ls`, the catalog, the console and the inbox show the work that
is still alive. Nothing is deleted: the folder is moved inside the home and versioned as before, its run records stay
where they are, `opsquire find` still finds it and `show`, `log` and `runs` still read it. `opsquire restore` moves
it back. A sweep rides `new`, `lint` and `run` at most once a day; `opsquire offboard --due` runs it on demand.

A plan and its steps leave together, plan first, so an interrupted sweep never leaves an active plan without its
steps. General scripts never leave on their own: `opsquire offboard <script>` takes one out by hand, and only when
no active task uses it. Restoring a task brings back the general scripts and steps it needs."""

from __future__ import annotations

import contextlib
import datetime as dt
import os
from pathlib import Path
from typing import Any

from .homegit import commit_quietly
from .model import (
    OFFBOARDED,
    OpsquireError,
    Task,
    all_task_folders,
    config_path,
    home_dir,
    is_offboarded,
    iso,
    load_task,
    now,
    offboarded_folders,
    parse_iso,
    read_json,
    resolve_task,
    validate_outcome,
    var_dir,
    write_json,
)
from .query import runs_for, task_status, used_by
from .state import is_settled, waiting_run

DEFAULT_KEEP_DAYS = 7.0
SWEEP_EVERY = dt.timedelta(days=1)
CONFIG_KEY = "offboard_after_days"


def keep_days() -> float:
    """Days a settled task stays after its last activity (`opsquire init --offboard-days N`); 0 turns the automatic
    sweep off."""
    try:
        value = read_json(config_path()).get(CONFIG_KEY)
    except (OSError, ValueError, AttributeError):
        value = None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return DEFAULT_KEEP_DAYS


def days_text(days: float) -> str:
    return f"{days:g}"


def last_activity(task: Task, runs: list[dict[str, Any]]) -> str:
    """When the task was last touched: created, a run started or ended, or its meta.json written (a close, a
    handover, a restore)."""
    stamps = [str(task.raw.get("created") or "")]
    stamps += [str(r.get("ended") or r.get("started") or "") for r in runs]
    with contextlib.suppress(OSError):
        written = (task.folder / "meta.json").stat().st_mtime
        stamps.append(iso(dt.datetime.fromtimestamp(written, dt.timezone.utc)))
    return max(stamps)


def idle_days(task: Task, runs: list[dict[str, Any]], at: dt.datetime) -> float:
    try:
        return (at - parse_iso(last_activity(task, runs))).total_seconds() / 86400
    except ValueError:
        return 0.0


def stays_because(task: Task, runs: list[dict[str, Any]]) -> str | None:
    """Why a task cannot be offboarded yet; None when it can."""
    running = [str(r.get("run_id")) for r in runs if r.get("status") == "running"]
    if running:
        return f"run {running[-1]} is still running"
    status = task_status(task, runs)
    outcome = task.raw.get("outcome")
    view = {
        "status": status,
        "outcome": outcome if not validate_outcome(outcome) else None,
        "changes_prod": task.raw.get("changes_prod") is True,
    }
    if not is_settled(view):
        if status == "done":
            return "its apply ran ok but nobody recorded how it ended: `opsquire close` it first"
        return f"it is not settled (status {status}): run it, or close it abandoned with `opsquire close`"
    waiting, _ = waiting_run(task, runs, status)
    if waiting:
        return f"a {waiting} run of it is waiting in the inbox"
    return None


def step_ids(plan: Task) -> list[str]:
    steps = plan.raw.get("steps")
    out: list[str] = []
    for step in steps if isinstance(steps, list) else []:
        if isinstance(step, dict):
            out += [str(step[key]) for key in ("task", "on_failure") if isinstance(step.get(key), str)]
    return out


def held_by_plans(chosen: set[str], active: dict[str, Task]) -> dict[str, str]:
    """The chosen tasks that must stay because a plan and its steps leave together, with the reason."""
    held: dict[str, str] = {}
    plans = [t for t in active.values() if t.kind == "plan"]
    changed = True
    while changed:
        changed = False
        for plan in plans:
            steps = [s for s in step_ids(plan) if s in active]
            leaving = plan.id in chosen and plan.id not in held
            if leaving:
                staying = [s for s in steps if s not in chosen or s in held]
                if staying:
                    held[plan.id] = f"its step {staying[0]} stays"
                    changed = True
                continue
            for step in steps:
                if step in chosen and step not in held:
                    held[step] = f"it is a step of plan {plan.id}, which stays"
                    changed = True
    return held


def destination(folder: Path) -> Path:
    """Where a folder goes: tasks/2026-11/<id> <-> offboarded/2026-11/<id>, general/<id> <-> offboarded/general/<id>."""
    home = home_dir()
    group = folder.parent.name
    if is_offboarded(folder):
        return home / "general" / folder.name if group == "general" else home / "tasks" / group / folder.name
    return home / OFFBOARDED / group / folder.name


def move(folder: Path) -> Path:
    """One rename, so a Ctrl-C leaves the folder whole on one side or the other."""
    dest = destination(folder)
    if dest.exists():
        raise OpsquireError(f"{dest} already exists; nothing moved")
    dest.parent.mkdir(parents=True, exist_ok=True)
    folder.rename(dest)
    if folder.parent.name != "general" or is_offboarded(folder):
        with contextlib.suppress(OSError):
            folder.parent.rmdir()
    return dest


def leaving_order(tasks: list[Task]) -> list[Task]:
    rank = {"plan": 0, "task": 1, "general": 2}
    return sorted(tasks, key=lambda t: (rank[t.kind], t.id))


def commit_moves(message: str) -> str:
    home = home_dir()
    return commit_quietly(home, [home / "tasks", home / "general", home / OFFBOARDED], message)


def due(at: dt.datetime | None = None, days: float | None = None) -> list[Task]:
    """The tasks a sweep would offboard now: settled, nothing waiting, idle for the retention period. Archived
    records (scripts from before opsquire) and general scripts only ever leave by hand."""
    at = at or now()
    days = keep_days() if days is None else days
    active = {t.id: t for t in (load_task(f) for f in all_task_folders())}
    chosen: dict[str, Task] = {}
    for task in active.values():
        if task.kind == "general" or task.raw.get("archived"):
            continue
        runs = runs_for(task.id)
        if stays_because(task, runs) is None and idle_days(task, runs, at) >= days:
            chosen[task.id] = task
    held = held_by_plans(set(chosen), active)
    return leaving_order([t for t in chosen.values() if t.id not in held])


def offboard_due(at: dt.datetime | None = None, days: float | None = None, dry: bool = False) -> list[str]:
    days = keep_days() if days is None else days
    tasks = due(at, days)
    if dry or not tasks:
        return [t.id for t in tasks]
    moved: list[str] = []
    try:
        for task in tasks:
            with contextlib.suppress(FileNotFoundError):
                move(task.folder)
                moved.append(task.id)
    finally:
        if moved:
            commit_moves(f"offboarded {len(moved)} tasks idle {days_text(days)}+ days")
    return moved


def offboard_named(queries: list[str], dry: bool = False) -> list[str]:
    """Offboard these now, whatever their age. All of them or none: a task that is not settled, has a run waiting,
    or is tied to a plan that stays, and a general script an active task still uses, refuse the whole command."""
    active = {t.id: t for t in (load_task(f) for f in all_task_folders())}
    chosen: dict[str, Task] = {}
    for query in queries:
        task = resolve_task(query, offboarded=True)
        if is_offboarded(task.folder):
            raise OpsquireError(f"{task.id} is already offboarded")
        chosen[task.id] = task
        if task.kind == "plan":
            chosen.update({s: active[s] for s in step_ids(task) if s in active})
    refused: list[str] = []
    for task in chosen.values():
        if task.kind == "general":
            users = [u for u in used_by(task.id, list(active.values())) if u not in chosen]
            if users:
                more = f" and {len(users) - 1} more" if len(users) > 1 else ""
                refused.append(f"{task.id}: active task {users[0]}{more} uses it")
            continue
        why = stays_because(task, runs_for(task.id))
        if why:
            refused.append(f"{task.id}: {why}")
    refused += [f"{tid}: {why}" for tid, why in held_by_plans(set(chosen), active).items()]
    if refused:
        raise OpsquireError("nothing offboarded:\n  " + "\n  ".join(refused))
    tasks = leaving_order(list(chosen.values()))
    if dry:
        return [t.id for t in tasks]
    moved: list[str] = []
    try:
        for task in tasks:
            move(task.folder)
            moved.append(task.id)
    finally:
        if moved:
            commit_moves("offboarded by hand: " + ", ".join(moved))
    return moved


def restore(queries: list[str]) -> list[str]:
    """Move offboarded tasks back, with the offboarded general scripts and plan steps they need, and start their
    idle clock again."""
    gone = {f.name: f for f in offboarded_folders()}
    wanted: dict[str, Task] = {}

    def want(task: Task) -> None:
        if task.id in wanted:
            return
        wanted[task.id] = task
        uses = task.raw.get("uses")
        needs = [*step_ids(task), *(u for u in (uses if isinstance(uses, list) else []) if isinstance(u, str))]
        for need in needs:
            if need in gone:
                want(load_task(gone[need]))

    for query in queries:
        task = resolve_task(query, offboarded=True)
        if not is_offboarded(task.folder):
            raise OpsquireError(f"{task.id} is not offboarded")
        want(task)
    restored: list[str] = []
    try:
        for task in reversed(leaving_order(list(wanted.values()))):
            dest = move(task.folder)
            with contextlib.suppress(OSError):
                os.utime(dest / "meta.json")
            restored.append(task.id)
    finally:
        if restored:
            commit_moves("restored: " + ", ".join(restored))
    return restored


def sweep_stamp() -> Path:
    return var_dir() / "offboard.json"


def sweep_is_due(at: dt.datetime) -> bool:
    try:
        last = parse_iso(str(read_json(sweep_stamp()).get("swept")))
    except (OSError, ValueError, AttributeError):
        return True
    return at - last >= SWEEP_EVERY


def daily_sweep(at: dt.datetime | None = None) -> list[str]:
    """The sweep that rides `new`, `lint` and `run`: at most once a day, never inside a parallel piece's worktree
    (its tasks/ is a copy that merges back), and off when the retention is 0."""
    at = at or now()
    days = keep_days()
    if os.environ.get("OPSQUIRE_VAR") or days <= 0 or not sweep_is_due(at):
        return []
    moved = offboard_due(at, days)
    with contextlib.suppress(OSError):
        sweep_stamp().parent.mkdir(parents=True, exist_ok=True)
        write_json(sweep_stamp(), {"swept": iso(at)})
    return moved
