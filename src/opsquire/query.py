from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .lint import cached_lint_ok
from .model import Task, all_task_folders, load_task, runs_dir
from .runner import load_run


def runs_for(task_id: str) -> list[dict[str, Any]]:
    folder = runs_dir() / task_id
    if not folder.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(folder.glob("*/run.json")):
        try:
            out.append(load_run(p))
        except (OSError, ValueError):
            continue
    return out


def all_runs() -> list[dict[str, Any]]:
    root = runs_dir()
    if not root.is_dir():
        return []
    recs: list[dict[str, Any]] = []
    for p in root.glob("*/*/run.json"):
        try:
            recs.append(load_run(p))
        except (OSError, ValueError):
            continue
    return sorted(recs, key=lambda r: str(r.get("started")))


def task_status(task: Task, runs: list[dict[str, Any]]) -> str:
    if task.meta and task.meta.archived:
        return "archived"
    if task.meta and task.meta.abandoned_reason:
        return "abandoned"
    if task.meta and task.meta.superseded_by:
        return "superseded"
    if task.meta and task.meta.outcome:
        return str(task.meta.outcome["state"])
    if task.meta:
        final_mode = "apply" if task.meta.changes_prod else "read"
        if any(r.get("mode") == final_mode and r.get("status") == "ok" for r in runs):
            return "done"
    if task.meta and cached_lint_ok(task):
        return "ready"
    return "draft"


def summarize(task: Task) -> dict[str, Any]:
    runs = runs_for(task.id)
    last = runs[-1] if runs else None
    m = task.meta
    return {
        "id": task.id,
        "kind": task.kind,
        "status": task_status(task, runs),
        "area": m.area if m else task.raw.get("area"),
        "purpose": m.purpose if m else task.raw.get("purpose"),
        "changes_prod": m.changes_prod if m else task.raw.get("changes_prod"),
        "targets": m.targets if m else task.raw.get("targets"),
        "path": str(task.folder),
        "meta_errors": task.errors,
        "runs": len(runs),
        "last_run": (
            {k: last.get(k) for k in ("run_id", "mode", "status", "started", "last_error")}
            | {"dir": last["paths"]["dir"]}
            if last
            else None
        ),
    }


def list_tasks() -> list[dict[str, Any]]:
    return [summarize(load_task(f)) for f in all_task_folders()]


def find_run(query: str) -> Path | None:
    root = runs_dir()
    if not root.is_dir():
        return None
    candidates = [p for p in root.glob("*/*") if p.is_dir() and (query == p.name or query in str(p))]
    return sorted(candidates)[-1] if candidates else None


FIELD_WEIGHTS = {"id": 3.0, "area": 3.0, "targets": 2.5, "purpose": 2.0, "uses": 1.5, "history": 1.0}


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 1]


def _fields(task: Task) -> dict[str, str]:
    raw = task.raw
    archived_raw = raw.get("archived")
    archived: dict[str, Any] = archived_raw if isinstance(archived_raw, dict) else {}
    history = " ".join(json.dumps(h) for h in archived.get("history", []) if isinstance(h, dict))
    return {
        "id": task.id,
        "area": str(raw.get("area", "")),
        "targets": " ".join(str(t) for t in raw.get("targets", []) or []),
        "purpose": f"{raw.get('purpose', '')} {archived.get('note', '')}",
        "uses": " ".join(str(u) for u in raw.get("uses", []) or []),
        "history": history,
    }


def find_tasks(query: str, limit: int = 10) -> list[dict[str, Any]]:
    words = _tokens(query)
    if not words:
        return []
    hits: list[dict[str, Any]] = []
    for folder in all_task_folders():
        task = load_task(folder)
        fields = {name: set(_tokens(text)) for name, text in _fields(task).items()}
        score = 0.0
        matched: set[str] = set()
        for word in words:
            for name, toks in fields.items():
                if word in toks:
                    score += FIELD_WEIGHTS[name]
                    matched.add(word)
                elif any(t.startswith(word) or (len(t) > 3 and word.startswith(t)) for t in toks):
                    score += FIELD_WEIGHTS[name] / 2
                    matched.add(word)
        if not matched:
            continue
        score *= len(matched) / len(words)
        hits.append(summarize(task) | {"score": round(score, 2), "matched": sorted(matched)})
    order = {"done": 0, "partial": 1, "ready": 1, "draft": 2, "archived": 3, "superseded": 4, "abandoned": 5}
    hits.sort(key=lambda h: (-h["score"], order.get(h["status"], 9), h["id"]))
    return hits[:limit]


def wait_for_run(
    task_id: str, since: str | None, timeout: float, progress: Callable[[str], None], poll: float = 1.0
) -> dict[str, Any] | None:
    existing = runs_for(task_id)
    baseline = since or (str(existing[-1]["started"]) if existing else "")
    progress(f"waiting for a run of {task_id} started after {baseline or 'now'} (Ctrl-C to stop)")
    deadline = time.monotonic() + timeout
    announced = ""
    while True:
        fresh = [r for r in runs_for(task_id) if str(r.get("started")) > baseline]
        if fresh:
            rec = fresh[-1]
            if rec.get("status") != "running":
                return rec
            if rec["run_id"] != announced:
                announced = str(rec["run_id"])
                progress(f"run {announced} started, waiting for it to finish")
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)
