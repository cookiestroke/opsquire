from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .lint import cached_lint_ok
from .model import (
    OUTCOME_STATES,
    Task,
    all_task_folders,
    is_last_stage,
    is_offboarded,
    load_task,
    offboarded_folders,
    runs_dir,
)
from .runner import load_run

TASK_STATUSES = ("draft", "ready", "done", "partial", "superseded", "abandoned", "archived")
SCRIPT_STATUSES = ("lint-ok", "lint-failing", "unchecked")


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
    return sorted(out, key=lambda r: str(r.get("started")))


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


def recorded_end(raw: dict[str, Any]) -> str | None:
    """How meta.json says the task ended. Read from the file as written: an error elsewhere in meta.json (a general
    script it used was renamed since) must not turn a retired or closed task back into a draft."""
    if raw.get("archived"):
        return "archived"
    if raw.get("abandoned_reason"):
        return "abandoned"
    if raw.get("superseded_by"):
        return "superseded"
    outcome = raw.get("outcome")
    if isinstance(outcome, dict) and outcome.get("state") in OUTCOME_STATES:
        return str(outcome["state"])
    return None


def script_status(task: Task) -> str:
    """A general script never runs on its own, so its status is its lint: of the files as they are now."""
    ok = cached_lint_ok(task)
    if ok is None:
        return "unchecked"
    return "lint-ok" if ok else "lint-failing"


def task_status(task: Task, runs: list[dict[str, Any]]) -> str:
    end = recorded_end(task.raw)
    if end:
        return end
    if task.kind == "general":
        return script_status(task)
    final_mode = "apply" if task.raw.get("changes_prod") is True else "read"
    last_stage = [
        r for r in runs if task.meta is None or is_last_stage(task.meta, [str(x) for x in r.get("argv") or []])
    ]
    if any(r.get("mode") == final_mode and r.get("status") == "ok" for r in last_stage):
        return "done"
    if task.meta and cached_lint_ok(task):
        return "ready"
    return "draft"


def used_by(script_id: str, tasks: list[Task]) -> list[str]:
    """The tasks whose meta.json names this general script, valid or not."""
    return [t.id for t in tasks if isinstance(t.raw.get("uses"), list) and script_id in t.raw["uses"]]


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
        "offboarded": is_offboarded(task.folder),
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
    for folder in [*all_task_folders(), *offboarded_folders()]:
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
    order = {"done": 0, "lint-ok": 0, "partial": 1, "ready": 1, "draft": 2, "unchecked": 2, "lint-failing": 2}
    order |= {"archived": 3, "superseded": 4, "abandoned": 5}
    hits.sort(key=lambda h: (-h["score"], h["offboarded"], order.get(h["status"], 9), h["id"]))
    return hits[:limit]


def wait_for_run(
    task_id: str,
    since: str | None,
    timeout: float,
    progress: Callable[[str], None],
    poll: float = 1.0,
    decided: Callable[[], str | None] = lambda: None,
) -> dict[str, Any] | None:
    """The next finished run of the task, None on timeout, or {"decided": why} when the task was abandoned or
    superseded while no run of it was going."""
    existing = runs_for(task_id)
    baseline = since or (str(existing[-1]["started"]) if existing else "")
    progress(f"waiting for a run of {task_id} started after {baseline or 'now'} (Ctrl-C to stop)")
    deadline = time.monotonic() + timeout
    announced = asked = ""
    while True:
        fresh = [r for r in runs_for(task_id) if str(r.get("started")) > baseline]
        if fresh:
            rec = fresh[-1]
            if rec.get("status") != "running":
                return rec
            if rec["run_id"] != announced:
                announced = str(rec["run_id"])
                progress(f"run {announced} started, waiting for it to finish")
            question = str((rec.get("waiting") or {}).get("question") or "")
            if question != asked:
                asked = question
                if question:
                    progress(f"run {announced} is waiting for the operator to answer: {question}")
        else:
            why = decided()
            if why:
                return {"decided": why}
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)
