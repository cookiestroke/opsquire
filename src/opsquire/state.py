"""The home as one picture: every task, run, ask and close folded into threads (the stories work belongs to), with
per-task suggestions (stale, awaiting close, possibly superseded) and the targets and general scripts they touch.
`opsquire ui` serves it; `opsquire threads` prints it. Threads come from the `thread` field in meta.json or, failing
that, are inferred from the session that created the tasks, the asks that produced them, plans, supersession, shared
PR or Slack links, and task ids mentioned in purposes and close notes."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

from . import locks
from .docs import final_mode, run_stats
from .model import Task, all_task_folders, home_dir, iso, load_task, now, parse_iso, read_json, var_dir
from .query import all_runs, task_status

TASK_ID_RE = re.compile(r"\b\d{8}-\d{4}_[a-z0-9-]+_[a-z0-9-]+\b")
GENERIC_AUTHOR_WORDS = {"claude", "code", "codex", "chat", "opsquire", "ask", "migrated", "by", "unknown", "test"}
CLOSED = {"done", "partial", "abandoned", "superseded", "archived"}
STALE_NEVER_RUN_DAYS = 7
STALE_AFTER_FAILURE_DAYS = 3
RERUN_HEAVY = 3.0


def author_key(author: str) -> str:
    """What identifies the session in an author string, or "" when it names only the tool ("claude-code (chat)")."""
    text = re.sub(r"\d{4}-\d{2}-\d{2}", " ", author.lower())
    words = [w for w in re.findall(r"[a-z0-9]+", text) if w not in GENERIC_AUTHOR_WORDS]
    return " ".join(words)


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
    design = folder / "design.md"
    return design.is_file() and any(line.startswith("## Result") for line in design.read_text().splitlines())


def suggestions(
    task: Task, status: str, runs: list[dict[str, Any]], tasks: list[Task], at: dt.datetime
) -> list[dict[str, str]]:
    """What a task's record suggests about it; the UI shows these with their evidence, nothing acts on them."""
    out: list[dict[str, str]] = []
    m = task.meta
    if m is None or m.archived or task.kind == "general":
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
    return out


def is_settled(view: dict[str, Any]) -> bool:
    """A task is settled when its end is recorded: closed, retired, or a read-only task that ran ok. A changing task
    whose apply ran ok still waits for `opsquire close` to say what really ended up live."""
    status = str(view["status"])
    if status not in CLOSED:
        return False
    return bool(view["outcome"]) or not view["changes_prod"] or status in ("superseded", "archived")


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
        "warnings": r.get("warnings") or [],
        "plan_run": r.get("plan_run"),
        "steps": r.get("steps"),
        "user": r.get("user"),
        "dir": (r.get("paths") or {}).get("dir"),
    }


def ask_view(a: dict[str, Any]) -> dict[str, Any]:
    return {
        "ask_id": a.get("ask_id"),
        "status": a.get("status"),
        "started": a.get("started"),
        "ended": a.get("ended"),
        "prompt": a.get("prompt"),
        "task_ids": a.get("task_ids") or [],
        "cost_usd": a.get("cost_usd"),
        "pieces": [{"name": p.get("name"), "status": p.get("status")} for p in a.get("pieces") or []],
        "rounds": a.get("rounds") or [],
        "waiting_on": a.get("waiting_on"),
        "actions": a.get("actions") or [],
    }


def state(at: dt.datetime | None = None) -> dict[str, Any]:
    """Everything the UI shows, computed fresh from the home (small enough to rebuild on every request)."""
    at = at or now()
    home = home_dir()
    tasks = [load_task(f) for f in all_task_folders()]
    runs = all_runs()
    by_task: dict[str, list[dict[str, Any]]] = {}
    for r in runs:
        by_task.setdefault(str(r.get("task_id")), []).append(r)
    asks = load_asks()
    threads_of = infer_threads(tasks, asks)
    final = {t.id: final_mode(t) for t in tasks}
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
            "targets": m.targets if m else list(raw.get("targets") or []),
            "uses": m.uses if m else [],
            "links": m.links if m else [],
            "steps": [s.get("task") for s in (m.steps or [])] if m else [],
            "outcome": m.outcome if m else None,
            "superseded_by": m.superseded_by if m else None,
            "abandoned_reason": m.abandoned_reason if m else None,
            "archived": bool(m.archived) if m else False,
            "thread": threads_of.get(t.id),
            "thread_explicit": bool(m and m.thread),
            "runs": [str(r.get("run_id")) for r in trs],
            "stats": run_stats(trs, final) if t.kind != "general" else None,
            "meta_errors": t.errors,
            "has_design": (t.folder / "design.md").is_file(),
            "has_result": has_result(t.folder),
            "suggestions": suggestions(t, status, trs, tasks, at),
            "path": str(t.folder),
        }
    threads: dict[str, dict[str, Any]] = {}
    for t in tasks:
        tid = threads_of.get(t.id)
        if tid:
            threads.setdefault(tid, {"id": tid, "tasks": []})["tasks"].append(t.id)
    for tid, th in threads.items():
        members = [t for t in tasks if t.id in th["tasks"]]
        settled = [is_settled(task_views[m]) for m in th["tasks"]]
        stamps = [task_views[m]["created"] or "" for m in th["tasks"]]
        stamps += [str(r.get("ended") or r.get("started") or "") for m in th["tasks"] for r in by_task.get(m, [])]
        stamps += [str(task_views[m]["outcome"]["closed"]) for m in th["tasks"] if task_views[m]["outcome"]]
        result = home / "threads" / f"{tid}.md"
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
                "result": result.read_text() if result.is_file() else None,
                "suggestions": sum(len(task_views[m]["suggestions"]) for m in th["tasks"]),
            }
        )
    scripts = {}
    for t in tasks:
        if t.kind != "general":
            continue
        users = [x.id for x in tasks if x.meta and t.id in x.meta.uses]
        srun = [r for tid in [t.id, *users] for r in by_task.get(tid, [])]
        scripts[t.id] = {
            "id": t.id,
            "purpose": task_views[t.id]["purpose"],
            "used_by": users,
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
    return {
        "generated": iso(at),
        "home": str(home),
        "threads": sorted(threads.values(), key=lambda th: str(th["updated"] or ""), reverse=True),
        "tasks": task_views,
        "runs": {str(r.get("run_id")): run_view(r) for r in runs},
        "asks": [ask_view(a) for a in asks],
        "scripts": scripts,
        "locks": held,
        "running": [str(r.get("run_id")) for r in runs if r.get("status") == "running"],
    }
