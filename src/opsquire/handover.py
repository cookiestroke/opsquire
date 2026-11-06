"""A handover is an agent's recommendation to run a task now. The inbox shows a task only while its record says a
run is owed (a ready task never run, the apply after an ok dry run), so a reused task (a read task that already ran,
a closed task to run again) recommended in a chat or by `opsquire ask` never reached the operator.
`opsquire handover <task> [--apply|--verify] [--why ...] [-- args]` writes the recommendation into the task's
meta.json as an annotation (outside the content hash, like `outcome`), and the inbox shows that run, with the reason,
until a run of the task starts after it. Asks record one for every RUN line their agents end with."""

from __future__ import annotations

import os
import re
import shlex
from typing import Any

from .homegit import commit_quietly
from .model import OpsquireError, Task, home_dir, iso, now, resolve_task, write_json

RUN_LINE_RE = re.compile(r"^opsquire run (?P<task>\S+)(?P<flags>(?:\s+--\w[\w-]*)*)(?:\s+--\s+(?P<args>.*))?\s*$")


def mode_for(task: Task, apply: bool, verify: bool) -> str:
    m = task.meta
    if m is None:
        raise OpsquireError("cannot hand over a task with invalid meta.json: " + "; ".join(task.errors))
    if task.kind == "general":
        raise OpsquireError(f"{task.id} is a general script; hand over a task that uses it")
    if apply and verify:
        raise OpsquireError("--apply or --verify, not both")
    if verify:
        if not m.verify:
            raise OpsquireError(f"{task.id} has no verify check")
        return "verify"
    if apply:
        if not m.changes_prod:
            raise OpsquireError(f"{task.id} is read-only; it has no apply run")
        return "apply"
    return "dry" if m.changes_prod else "read"


def record(task: Task, mode: str, args: list[str], why: str, by: str) -> dict[str, Any]:
    entry = {"at": iso(now()), "by": by, "mode": mode, "args": list(args), "why": why.strip()}
    raw = dict(task.raw)
    raw["handover"] = entry
    write_json(task.folder / "meta.json", raw)
    commit_quietly(home_dir(), [task.folder], f"handover: {task.id}")
    return entry


def command(task_id: str, mode: str, args: list[str]) -> str:
    flag = {"apply": " --apply", "verify": " --verify"}.get(mode, "")
    return f"opsquire run {task_id}{flag}" + (" -- " + shlex.join(args) if args else "")


def pending(task: Task, runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The task's handover while no run of it has started since; runs are the task's run records."""
    h = task.raw.get("handover")
    if not isinstance(h, dict) or not isinstance(h.get("at"), str):
        return None
    if any(str(r.get("started") or "") >= str(h["at"]) for r in runs):
        return None
    return h


def args_for(task: Task, runs: list[dict[str, Any]], mode: str) -> list[str]:
    """What a run in this mode takes from the console: the arguments of the handed-over run the inbox shows. Any other
    run of the task (another mode, no handover) has none."""
    h = pending(task, runs)
    return [str(x) for x in h.get("args") or []] if h and h.get("mode") == mode else []


def record_run_lines(actions: list[tuple[str, str]], why: str, by: str) -> list[str]:
    """Record a handover for every `RUN: opsquire run ...` line an agent ended with; lines that are not such a
    command, or name no task, are left alone (they are the agent's text, not a contract)."""
    handed: list[str] = []
    for kind, text in actions:
        m = RUN_LINE_RE.match(text.strip()) if kind == "RUN" else None
        if not m:
            continue
        try:
            task = resolve_task(m.group("task"))
            flags = set((m.group("flags") or "").split())
            mode = mode_for(task, "--apply" in flags, "--verify" in flags)
            args = shlex.split(m.group("args") or "")
        except (OpsquireError, ValueError):
            continue
        if task.id in handed:
            continue
        record(task, mode, args, why, by)
        handed.append(task.id)
    return handed


def author() -> str:
    return os.environ.get("OPSQUIRE_AUTHOR", "unknown")
