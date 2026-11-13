"""The home as one picture: every task, run, ask and close folded into threads (the stories work belongs to), with
per-task suggestions (stale, awaiting close, possibly superseded) and the targets and general scripts they touch.
`opsquire ui` serves it; `opsquire threads` prints it. Threads come from the `thread` field in meta.json or, failing
that, are inferred from the session that created the tasks, the asks that produced them, plans, supersession, shared
PR or Slack links, and task ids mentioned in purposes and close notes."""

from __future__ import annotations

import datetime as dt
import re
import shlex
from pathlib import Path
from typing import Any

from . import handover, locks, repos
from .docs import final_mode, run_stats
from .lint import asks_first, last_lint
from .model import (
    Task,
    all_task_folders,
    author_key,
    code_version,
    home_dir,
    is_last_stage,
    iso,
    load_task,
    next_stage,
    now,
    offboarded_folders,
    parse_iso,
    read_json,
    validate_outcome,
    var_dir,
)
from .query import all_runs, runs_for, task_status, used_by

TASK_ID_RE = re.compile(r"\b\d{8}-\d{4}_[a-z0-9-]+_[a-z0-9-]+\b")
CLOSED = {"done", "partial", "abandoned", "superseded", "archived"}
HANDOVER_CLOSED = {"draft", "abandoned", "superseded", "archived"}
STALE_NEVER_RUN_DAYS = 7
INBOX_STALE_DAYS = 3.0
STALE_AFTER_FAILURE_DAYS = 3
RERUN_HEAVY = 3.0
READ_VERSIONS_HEAVY = 3


class Union:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def join(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def load_asks() -> list[dict[str, Any]]:
    folder = var_dir() / "asks"
    out: list[dict[str, Any]] = []
    for p in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            rec = read_json(p)
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def infer_threads(tasks: list[Task], asks: list[dict[str, Any]]) -> dict[str, str]:
    """Task id -> thread id. Explicit threads are never merged with a different explicit thread; inferred groups are
    named after their earliest task."""
    by_id = {t.id: t for t in tasks}
    explicit = {t.id: t.meta.thread for t in tasks if t.meta and t.meta.thread}
    u = Union()
    edges: list[tuple[str, str]] = []
    by_author: dict[str, list[str]] = {}
    for t in tasks:
        if t.kind == "general" or not t.meta:
            continue
        key = author_key(t.meta.author)
        if key:
            by_author.setdefault(key, []).append(t.id)
        for step in t.meta.steps or []:
            for other in (step.get("task"), step.get("on_failure")):
                if other in by_id:
                    edges.append((t.id, str(other)))
        if t.meta.superseded_by in by_id:
            edges.append((t.id, str(t.meta.superseded_by)))
        mentioned = set(TASK_ID_RE.findall(t.meta.purpose))
        if t.meta.outcome:
            mentioned |= set(TASK_ID_RE.findall(str(t.meta.outcome.get("note", ""))))
        if t.meta.abandoned_reason:
            mentioned |= set(TASK_ID_RE.findall(t.meta.abandoned_reason))
        edges += [(t.id, m) for m in mentioned if m in by_id and m != t.id]
    for group in by_author.values():
        edges += [(group[0], other) for other in group[1:]]
    by_link: dict[str, list[str]] = {}
    for t in tasks:
        for link in t.meta.links if t.meta else []:
            by_link.setdefault(link.rstrip("/"), []).append(t.id)
    for group in by_link.values():
        edges += [(group[0], other) for other in group[1:]]
    by_session: dict[str, list[str]] = {}
    for ask in asks:
        ids = [x for x in ask.get("task_ids") or [] if x in by_id and created_during(by_id[x], ask)]
        key = str(ask.get("session") or ask.get("ask_id"))
        by_session.setdefault(key, []).extend(ids)
        if ask.get("copies"):
            by_session.setdefault(str(ask["copies"]), []).extend(ids)
    for group in by_session.values():
        edges += [(group[0], other) for other in group[1:]]
    for a, b in edges:
        ea, eb = explicit.get(a), explicit.get(b)
        if ea and eb and ea != eb:
            continue
        u.join(a, b)
    groups: dict[str, list[str]] = {}
    for t in tasks:
        if t.kind != "general":
            groups.setdefault(u.find(t.id), []).append(t.id)
    out: dict[str, str] = {}
    for members in groups.values():
        names = sorted({explicit[m] for m in members if m in explicit})
        thread = names[0] if names else min(members)
        for m in members:
            out[m] = thread
    return out


def created_during(task: Task, ask: dict[str, Any]) -> bool:
    """An ask's record lists every task id its agent mentioned; only the ones it created belong to its thread."""
    created = task.meta.created if task.meta else ""
    return bool(created) and str(ask.get("started", "")) <= created <= str(ask.get("ended") or "9")


def thread_title(thread_id: str, members: list[Task]) -> str:
    if not TASK_ID_RE.match(thread_id):
        return thread_id.replace("-", " ")
    first = min(members, key=lambda t: t.id)
    name = first.id.split("_", 2)[-1].replace("-", " ")
    areas = sorted({t.meta.area for t in members if t.meta})
    return f"{name} ({', '.join(areas)})" if areas else name


def days_since(stamp: str | None, at: dt.datetime) -> float:
    if not stamp:
        return 0.0
    try:
        return (at - parse_iso(stamp)).total_seconds() / 86400
    except ValueError:
        return 0.0


def has_result(folder: Path) -> bool:
    try:
        text = (folder / "design.md").read_text()
    except OSError:
        return False
    return any(line.startswith("## Result") for line in text.splitlines())


def read_or_none(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def changed_since_lint(task: Task, status: str) -> bool:
    """A handed-over task (its lint passed once) whose files changed since, often a general script it uses edited
    for another task. It stays in the inbox: the runner lints the exact files again before it runs anything."""
    return status == "draft" and task.meta is not None and bool((last_lint(task) or {}).get("ok"))


def suggestions(
    task: Task, status: str, runs: list[dict[str, Any]], tasks: list[Task], at: dt.datetime
) -> list[dict[str, str]]:
    """What a task's record suggests about it; the UI shows these with their evidence, nothing acts on them."""
    out: list[dict[str, str]] = []
    m = task.meta
    if task.kind == "general" or task.raw.get("archived"):
        return out
    if m is None:
        if status == "draft" and (last_lint(task) or {}).get("ok"):
            out.append({"kind": "broken", "text": f"was ready, now its meta.json is invalid: {'; '.join(task.errors)}"})
        return out
    last = runs[-1] if runs else None
    final = final_mode(task)
    ok_final = [r for r in runs if r.get("status") == "ok" and r.get("mode") == final]
    if m.changes_prod and status == "done" and not m.outcome:
        out.append(
            {
                "kind": "awaiting-close",
                "text": "an apply run finished ok; record how it really ended with `opsquire close`",
            }
        )
    if m.outcome and m.changes_prod and not has_result(task.folder):
        out.append({"kind": "no-result", "text": "design.md has no `## Result` section saying what ended up live"})
    if status in ("ready", "draft") and not runs:
        age = days_since(m.created, at)
        if age >= STALE_NEVER_RUN_DAYS:
            out.append(
                {"kind": "stale", "text": f"never run, created {age:.0f} days ago; close it abandoned or run it"}
            )
    if status not in CLOSED and m.changes_prod and last and last.get("status") in ("failed", "partial", "interrupted"):
        age = days_since(str(last.get("ended") or last.get("started")), at)
        if age >= STALE_AFTER_FAILURE_DAYS:
            out.append(
                {
                    "kind": "stale",
                    "text": f"last run {last.get('status')} {age:.0f} days ago and nothing since; close or fix it",
                }
            )
    if status not in CLOSED and not ok_final and m.uses:
        newer = [
            t.id
            for t in tasks
            if t.meta
            and t.id > task.id
            and t.kind == task.kind
            and t.meta.area == m.area
            and set(t.meta.uses) == set(m.uses)
        ]
        if newer:
            out.append(
                {
                    "kind": "superseded",
                    "text": f"a newer task in the same area uses the same scripts: {newer[-1]}; superseded?",
                }
            )
    stats = run_stats(runs, {task.id: final})
    if stats["ok"] and len(runs) / stats["ok"] > RERUN_HEAVY:
        out.append(
            {
                "kind": "rerun-heavy",
                "text": f"{len(runs)} runs for {stats['ok']} ok {final}: debugged by rerunning; fix the task",
            }
        )
    elif not stats["ok"] and len(runs) >= RERUN_HEAVY:
        out.append({"kind": "rerun-heavy", "text": f"{len(runs)} runs, none ok in {final} mode"})
    elif not m.changes_prod and stats["versions"] >= READ_VERSIONS_HEAVY:
        out.append(
            {
                "kind": "rerun-heavy",
                "text": f"{len(runs)} runs over {stats['versions']} script versions: a read task should gather "
                "everything in one run",
            }
        )
    return out


def agent_work(tasks: list[Task], asks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per task: how many asks produced it, how many parallel pieces, and what the agents cost (an ask's cost is
    shared evenly between the tasks it created)."""
    out: dict[str, dict[str, Any]] = {}
    by_id = {t.id: t for t in tasks}
    for a in asks:
        made = [x for x in a.get("task_ids") or [] if x in by_id and created_during(by_id[x], a)]
        if not made:
            continue
        share = float(a.get("cost_usd") or 0) / len(made)
        pieces = len(a.get("pieces") or [])
        for tid in made:
            rec = out.setdefault(tid, {"asks": 0, "pieces": 0, "cost_usd": 0.0, "operator_s": 0.0})
            rec["asks"] += 1
            rec["pieces"] += pieces
            rec["cost_usd"] = round(rec["cost_usd"] + share, 2)
    for t in tasks:
        if t.meta and " piece " in t.meta.author:
            rec = out.setdefault(t.id, {"asks": 0, "pieces": 0, "cost_usd": 0.0, "operator_s": 0.0})
            rec["pieces"] = max(rec["pieces"], 1)
    return out


def is_settled(view: dict[str, Any]) -> bool:
    """A task is settled when its end is recorded: closed, retired, or a read-only task that ran ok. A changing task
    whose apply ran ok still waits for `opsquire close` to say what really ended up live."""
    status = str(view["status"])
    if status not in CLOSED:
        return False
    return bool(view["outcome"]) or not view["changes_prod"] or status in ("superseded", "archived")


def argv_of(run: dict[str, Any]) -> list[str]:
    return [str(x) for x in run.get("argv") or []]


def next_mode(task: Task, runs: list[dict[str, Any]], status: str) -> str | None:
    """The run the operator owes a task: its first read or dry run when it is ready and never ran, a read again when
    the last one did not end ok and its scripts changed since, the apply after a dry run of its current scripts that
    ended ok, the verify check after an apply that did not close the task (or when the judge asked for it), else a
    fresh dry run. None when nothing waits: a read that ended ok or was not fixed since, closed tasks, and a task
    whose verify check failed (that one needs a decision)."""
    m = task.meta
    if m is None or task.kind == "general" or status in CLOSED or status == "draft":
        return None
    runs = [r for r in runs if r.get("status") != "blocked"]
    if not runs:
        return "dry" if m.changes_prod else "read"
    last = runs[-1]
    same_scripts = code_version(last.get("files") or {}) == code_version(task.file_hashes())
    if not m.changes_prod:
        return "read" if last.get("status") not in ("ok", "running") and not same_scripts else None
    judged = task.raw.get("judged") or {}
    if m.verify and judged.get("decision") == "verify":
        return "verify"
    if last.get("mode") == "dry" and last.get("status") == "ok":
        return "apply" if same_scripts else "dry"
    if last.get("mode") == "verify":
        return None
    if last.get("mode") == "apply" and last.get("status") == "ok" and not is_last_stage(m, argv_of(last)):
        return "dry"
    if last.get("mode") == "apply" and last.get("status") != "aborted" and m.verify:
        return "verify"
    if last.get("status") in ("failed", "partial") and same_scripts:
        return None
    return "dry"


def same_target(a: str, b: str) -> bool:
    """Areas are kebab-case and hosts often are not: db-2 and db_2 are one machine."""
    return a.replace("_", "-") == b.replace("_", "-")


def waiting_run(task: Task, runs: list[dict[str, Any]], status: str) -> tuple[str | None, dict[str, Any] | None]:
    """The run this task has waiting in the inbox, and the handover that put it there if one did."""
    handed = handover.pending(task, runs) if status not in HANDOVER_CLOSED else None
    return (str(handed["mode"]), handed) if handed else (next_mode(task, runs, status), None)


def waiting_since(task: Task, runs: list[dict[str, Any]], handed: dict[str, Any] | None) -> str:
    """When the inbox item began to wait: its handover, else the last run that started something, else the day the
    task was created."""
    if handed:
        return str(handed.get("at") or "")
    started = [r for r in runs if r.get("status") != "blocked"]
    if started:
        return str(started[-1].get("ended") or started[-1].get("started") or "")
    return str(task.raw.get("created") or "")


def stale_text(task_id: str, days: float) -> str | None:
    """What an inbox item that waited too long asks of the operator; None while it is fresh."""
    if days < INBOX_STALE_DAYS:
        return None
    return (
        f"waiting {days:.0f} days. Decide: run it, `opsquire supersede {task_id} --by <newer task>`, or "
        f'`opsquire close {task_id} --outcome abandoned --note "why"`'
    )


def waiting_on(targets: list[str], exclude: str = "") -> list[dict[str, Any]]:
    """What else waits for these targets in the inbox: related runs on one machine belong in one plan or task."""
    out = []
    for folder in all_task_folders():
        task = load_task(folder)
        if task.id == exclude or task.meta is None or task.kind == "general":
            continue
        shared = [t for t in task.meta.targets if any(same_target(t, want) for want in targets)]
        if not shared:
            continue
        runs = runs_for(task.id)
        status = task_status(task, runs)
        relint = changed_since_lint(task, status)
        nxt, handed = waiting_run(task, runs, "ready" if relint else status)
        if nxt:
            why = str(handed.get("why") or "") if handed else ""
            out.append({"task_id": task.id, "next": nxt, "targets": shared, "why": why})
    return sorted(out, key=lambda it: str(it["task_id"]))


def note_shared_inbox(item: dict[str, Any], items: list[dict[str, Any]]) -> list[str]:
    """The other inbox items that touch one of this item's targets (a plan's own steps do not count)."""
    own = {str(item["task_id"]), *(str(s["task_id"]) for s in item.get("steps") or [])}
    return [
        str(other["task_id"])
        for other in items
        if str(other["task_id"]) not in own
        and any(same_target(a, b) for a in item["targets"] for b in other["targets"])
    ]


def nest_steps(inbox: list[dict[str, Any]], tasks: dict[str, Task]) -> list[dict[str, Any]]:
    """A plan in the inbox carries its waiting steps under it (in plan order, as `steps`): the operator runs the
    plan once, so its steps are not separate items. A step whose plan is not waiting stays an item of its own."""
    by_id = {str(it["task_id"]): it for it in inbox}
    nested: set[str] = set()
    for it in inbox:
        plan = tasks.get(str(it["task_id"]))
        if plan is None or plan.meta is None or plan.kind != "plan":
            continue
        steps = [by_id[sid] for step in plan.meta.steps or [] if (sid := str(step.get("task"))) in by_id]
        it["steps"] = [dict(st, steps=[]) for st in steps if st["task_id"] not in nested]
        nested.update(st["task_id"] for st in it["steps"])
    return [dict(it, steps=it.get("steps") or []) for it in inbox if it["task_id"] not in nested]


def applied_already(task: Task, runs: list[dict[str, Any]]) -> str | None:
    if task.meta is None or not task.meta.changes_prod or task.kind == "general":
        return None
    same = handover.same_apply(task, runs, [])
    return handover.applied_text(same) if same else None


def with_shared_targets(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(it, same_target=note_shared_inbox(it, items)) for it in items]


def run_view(r: dict[str, Any]) -> dict[str, Any]:
    targets = r.get("targets") or {}
    return {
        "run_id": r.get("run_id"),
        "task_id": r.get("task_id"),
        "status": r.get("status"),
        "mode": r.get("mode"),
        "started": r.get("started"),
        "ended": r.get("ended"),
        "duration_s": r.get("duration_s"),
        "targets": {t: (v.get("status") if isinstance(v, dict) else v) for t, v in targets.items()},
        "last_error": r.get("last_error"),
        "results": r.get("results") or [],
        "usage": r.get("usage"),
        "waiting": r.get("waiting") if r.get("status") == "running" else None,
        "warnings": r.get("warnings") or [],
        "plan_run": r.get("plan_run"),
        "steps": r.get("steps"),
        "user": r.get("user"),
        "origin": r.get("origin"),
        "dir": (r.get("paths") or {}).get("dir"),
    }


def repo_slugs() -> dict[str, str]:
    """{name: owner/repo} for the home's repos, so the console can link `name#123`; {} when repos.json is unreadable."""
    try:
        return {name: r.github for name, r in repos.registry().items()}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def ask_view(a: dict[str, Any]) -> dict[str, Any]:
    return {
        "ask_id": a.get("ask_id"),
        "conversation": a.get("copies") or a.get("ask_id"),
        "status": a.get("status"),
        "started": a.get("started"),
        "ended": a.get("ended"),
        "prompt": a.get("prompt"),
        "task_ids": a.get("task_ids") or [],
        "cost_usd": a.get("cost_usd"),
        "billing": a.get("billing"),
        "model": a.get("model"),
        "agent": a.get("agent"),
        "pieces": [{"name": p.get("name"), "status": p.get("status")} for p in a.get("pieces") or []],
        "rounds": a.get("rounds") or [],
        "waiting_on": a.get("waiting_on"),
        "actions": a.get("actions") or [],
    }


def state(at: dt.datetime | None = None, with_offboarded: bool = False) -> dict[str, Any]:
    """Everything the UI shows, computed fresh from the home (small enough to rebuild on every request). Offboarded
    tasks still shape the threads (a thread says how many of its tasks left) and count as users of a general script;
    they get a view of their own only with_offboarded, for readers of history such as milestones."""
    at = at or now()
    home = home_dir()
    active = [load_task(f) for f in all_task_folders()]
    gone = [load_task(f) for f in offboarded_folders()]
    gone_ids = {t.id for t in gone}
    everything = active + gone
    tasks = everything if with_offboarded else active
    runs = all_runs()
    by_task: dict[str, list[dict[str, Any]]] = {}
    for r in runs:
        by_task.setdefault(str(r.get("task_id")), []).append(r)
    asks = load_asks()
    threads_of = infer_threads(everything, asks)
    final = {t.id: final_mode(t) for t in everything}
    agents_of = agent_work(tasks, asks)
    task_views: dict[str, dict[str, Any]] = {}
    for t in tasks:
        m = t.meta
        trs = by_task.get(t.id, [])
        status = task_status(t, trs)
        raw = t.raw
        task_views[t.id] = {
            "id": t.id,
            "kind": t.kind,
            "status": status,
            "area": m.area if m else raw.get("area"),
            "purpose": m.purpose if m else raw.get("purpose", ""),
            "created": m.created if m else raw.get("created"),
            "author": m.author if m else raw.get("author"),
            "changes_prod": m.changes_prod if m else bool(raw.get("changes_prod")),
            "entry": m.entry if m else raw.get("entry"),
            "verify": m.verify if m else raw.get("verify"),
            "targets": m.targets if m else list(raw.get("targets") or []),
            "uses": m.uses if m else [u for u in raw.get("uses") or [] if isinstance(u, str)],
            "links": m.links if m else [],
            "steps": [s.get("task") for s in (m.steps or [])] if m else [],
            "outcome": raw.get("outcome") if not validate_outcome(raw.get("outcome")) else None,
            "judged": raw.get("judged"),
            "superseded_by": raw.get("superseded_by"),
            "abandoned_reason": raw.get("abandoned_reason"),
            "archived": bool(raw.get("archived")),
            "offboarded": t.id in gone_ids,
            "thread": threads_of.get(t.id),
            "thread_explicit": bool(m and m.thread),
            "runs": [str(r.get("run_id")) for r in trs],
            "stats": run_stats(trs, final) if t.kind != "general" else None,
            "meta_errors": t.errors,
            "has_design": (t.folder / "design.md").is_file(),
            "has_result": has_result(t.folder),
            "suggestions": suggestions(t, status, trs, tasks, at),
            "applied_already": applied_already(t, trs),
            "asks_first": bool(m and m.changes_prod and asks_first(t)),
            "settled": False,
            "agents": agents_of.get(t.id, {"asks": 0, "pieces": 0, "cost_usd": 0.0, "operator_s": 0.0}),
            "path": str(t.folder),
        }
        task_views[t.id]["settled"] = is_settled(task_views[t.id])
        task_views[t.id]["agents"]["operator_s"] = round(sum(float(r.get("duration_s") or 0) for r in trs), 1)
    threads: dict[str, dict[str, Any]] = {}
    for t in tasks:
        tid = threads_of.get(t.id)
        if tid:
            threads.setdefault(tid, {"id": tid, "tasks": []})["tasks"].append(t.id)
    left: dict[str, int] = {}
    for t in gone:
        if t.id in threads_of and not with_offboarded:
            left[threads_of[t.id]] = left.get(threads_of[t.id], 0) + 1
    for tid, th in threads.items():
        members = [t for t in tasks if t.id in th["tasks"]]
        settled = [is_settled(task_views[m]) for m in th["tasks"]]
        stamps = [task_views[m]["created"] or "" for m in th["tasks"]]
        stamps += [str(r.get("ended") or r.get("started") or "") for m in th["tasks"] for r in by_task.get(m, [])]
        stamps += [str(task_views[m]["outcome"]["closed"]) for m in th["tasks"] if task_views[m]["outcome"]]
        th.update(
            {
                "title": thread_title(tid, members),
                "explicit": not TASK_ID_RE.match(tid),
                "state": "done" if all(settled) else "open",
                "started": min(stamps) if stamps else None,
                "updated": max(stamps) if stamps else None,
                "areas": sorted({task_views[m]["area"] for m in th["tasks"] if task_views[m]["area"]}),
                "targets": sorted({x for m in th["tasks"] for x in task_views[m]["targets"]}),
                "links": sorted({x for m in th["tasks"] for x in task_views[m]["links"]}),
                "result": read_or_none(home / "threads" / f"{tid}.md"),
                "suggestions": sum(len(task_views[m]["suggestions"]) for m in th["tasks"]),
                "offboarded": left.get(tid, 0),
            }
        )
    scripts = {}
    for t in tasks:
        if t.kind != "general":
            continue
        users = used_by(t.id, tasks)
        past = [u for u in used_by(t.id, gone) if u not in users]
        srun = [r for tid in [t.id, *users, *past] for r in by_task.get(tid, [])]
        scripts[t.id] = {
            "id": t.id,
            "purpose": task_views[t.id]["purpose"],
            "used_by": users,
            "used_by_offboarded": len(past),
            "lint": task_views[t.id]["status"],
            "stats": run_stats(srun, final),
        }
    held = []
    for target, hs in locks.holders().items():
        for h in hs:
            held.append(
                {
                    "target": target,
                    "run_id": h.run_id,
                    "task_id": h.task_id,
                    "mode": h.mode,
                    "since": h.since,
                    "alive": h.alive(),
                }
            )
    holds = {t.id: kept for t in tasks if (kept := handover.held(t, at))}
    for t in tasks:
        if t.id in holds:
            for step in t.meta.steps or [] if t.meta else []:
                holds.setdefault(str(step.get("task")), {**holds[t.id], "plan": t.id})
    for tid, kept in holds.items():
        if tid in task_views:
            task_views[tid]["held"] = kept
    inbox = []
    for t in sorted(tasks, key=lambda t: t.id, reverse=True):
        if t.id in holds:
            continue
        v = task_views[t.id]
        relint = changed_since_lint(t, str(v["status"]))
        status = "ready" if relint else str(v["status"])
        task_runs = by_task.get(t.id, [])
        nxt, handed = waiting_run(t, task_runs, status)
        stages = t.meta.stages if t.meta else None
        if nxt in ("dry", "apply") and stages and not (handed and handed.get("args")):
            nxt = "apply"
        if nxt:
            whole = bool(stages) and nxt == "apply" and not (handed and handed.get("args"))
            args: list[str] = [] if whole else handover.args_for(t, task_runs, nxt)
            same = handover.same_apply(t, task_runs, args) if nxt == "apply" else None
            since = waiting_since(t, task_runs, handed)
            waited = days_since(since, at)
            inbox.append(
                {
                    "task_id": t.id,
                    "next": nxt,
                    "command": handover.command(t.id, nxt, args),
                    "args": shlex.join(args),
                    "created": v["created"],
                    "stages": stages or [],
                    "stage_at": next_stage(t.meta, task_runs) if t.meta and stages else None,
                    "waiting_since": since,
                    "waiting_days": round(waited, 1),
                    "stale": stale_text(t.id, waited),
                    "author": v["author"],
                    "purpose": v["purpose"],
                    "changes_prod": v["changes_prod"],
                    "thread": v["thread"],
                    "changed_since_lint": relint,
                    "targets": v["targets"],
                    "applied_already": handover.applied_text(same) if same else None,
                    "handed": {"at": handed.get("at"), "by": handed.get("by"), "why": handed.get("why")}
                    if handed
                    else None,
                }
            )
    return {
        "generated": iso(at),
        "home": str(home),
        "inbox": with_shared_targets(nest_steps(inbox, {t.id: t for t in tasks})),
        "threads": sorted(threads.values(), key=lambda th: str(th["updated"] or ""), reverse=True),
        "tasks": task_views,
        "offboarded": len([t for t in gone if t.kind != "general"]),
        "runs": {str(r.get("run_id")): run_view(r) for r in runs},
        "asks": [ask_view(a) for a in asks],
        "scripts": scripts,
        "locks": held,
        "running": [str(r.get("run_id")) for r in runs if r.get("status") == "running"],
        "repos": repo_slugs(),
    }
