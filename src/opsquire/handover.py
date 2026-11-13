"""A handover is an agent's recommendation to run a task now. The inbox shows a task only while its record says a
run is owed (a ready task never run, the apply after an ok dry run), so a reused task (a read task that already ran,
a closed task to run again) recommended in a chat or by `opsquire ask` never reached the operator.
`opsquire handover <task> [--apply|--verify] [--why ...] [-- args]` writes the recommendation into the task's
meta.json as an annotation (outside the content hash, like `outcome`), and the inbox shows that run, with the reason,
until a run of the task starts after it. Asks record one for every RUN line their agents end with."""

from __future__ import annotations

import datetime as dt
import os
import re
import shlex
import sys
from typing import Any

from .homegit import commit_quietly
from .model import OpsquireError, Task, code_version, home_dir, iso, next_stage, now, resolve_task, write_json

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


def record(task: Task, mode: str, args: list[str], why: str, by: str, again: bool = False) -> dict[str, Any]:
    entry: dict[str, Any] = {"at": iso(now()), "by": by, "mode": mode, "args": list(args), "why": why.strip()}
    if again:
        entry["again"] = True
    raw = dict(task.raw)
    raw["handover"] = entry
    write_json(task.folder / "meta.json", raw)
    commit_quietly(home_dir(), [task.folder], f"handover: {task.id}")
    return entry


def hold(task: Task, why: str, by: str, until: dt.datetime | None = None) -> dict[str, Any]:
    """Keep the task, and a plan's steps, out of the inbox until it is handed over or `until` passes."""
    entry: dict[str, Any] = {"at": iso(now()), "by": by, "why": why.strip()}
    if until:
        entry["until"] = iso(until)
    raw = {k: v for k, v in task.raw.items() if k != "handover"}
    raw["hold"] = entry
    write_json(task.folder / "meta.json", raw)
    return entry


def release(task: Task) -> bool:
    if "hold" not in task.raw:
        return False
    write_json(task.folder / "meta.json", {k: v for k, v in task.raw.items() if k != "hold"})
    return True


def held(task: Task, at: dt.datetime) -> dict[str, Any] | None:
    """The task's hold while it lasts: not handed over since, and before its `until`."""
    h = task.raw.get("hold")
    if not isinstance(h, dict) or not isinstance(h.get("at"), str):
        return None
    handed = task.raw.get("handover")
    if isinstance(handed, dict) and str(handed.get("at") or "") > h["at"]:
        return None
    until = h.get("until")
    if isinstance(until, str) and until <= iso(at):
        return None
    return h


def withdraw(task: Task) -> bool:
    """Take the task's handover out of the inbox; False when there was none."""
    if "handover" not in task.raw:
        return False
    raw = {k: v for k, v in task.raw.items() if k != "handover"}
    write_json(task.folder / "meta.json", raw)
    commit_quietly(home_dir(), [task.folder], f"handover withdrawn: {task.id}")
    return True


def same_apply(task: Task, runs: list[dict[str, Any]], args: list[str]) -> dict[str, Any] | None:
    """The latest apply, finished ok or still running, with these arguments and the task's current scripts: running
    it again changes production a second time for no new reason. Interrupted, failed and aborted applies do not
    count."""
    applies = [
        r
        for r in runs
        if r.get("mode") == "apply" and r.get("status") in ("ok", "running") and list(r.get("argv") or []) == list(args)
    ]
    if not applies:
        return None
    current = code_version(task.file_hashes())
    return next((r for r in reversed(applies) if code_version(r.get("files") or {}) == current), None)


def covering_dry(task: Task, runs: list[dict[str, Any]], args: list[str]) -> tuple[dict[str, Any] | None, str]:
    """The dry run that covers an apply now: finished ok with these arguments and the task's current scripts. Without
    one, why: an apply of scripts no dry run has seen is a first try on production."""
    current = code_version(task.file_hashes())
    dries = [r for r in runs if r.get("mode") == "dry"]
    same_args = [r for r in dries if list(r.get("argv") or []) == list(args)]
    same = [r for r in same_args if code_version(r.get("files") or {}) == current]
    ok = next((r for r in reversed(same) if r.get("status") == "ok"), None)
    if ok:
        return ok, ""
    if same:
        return None, f"its dry run {same[-1].get('run_id')} of these scripts ended {same[-1].get('status')}"
    if same_args:
        return None, f"the scripts changed since its last dry run, {same_args[-1].get('run_id')}"
    if dries:
        return None, f"its last dry run, {dries[-1].get('run_id')}, had other arguments"
    return None, "it never had a dry run"


def needs_dry(task: Task, runs: list[dict[str, Any]], args: list[str]) -> str | None:
    """Why an apply of this task cannot be handed over yet, None when a dry run covers it. A plan is checked step by
    step when it runs, and a staged task's apply with no arguments makes each stage's dry run itself."""
    if task.kind == "plan" or (task.meta and task.meta.stages and not args):
        return None
    covered, why = covering_dry(task, runs, args)
    if covered:
        return None
    return f"no dry run covers this apply ({why}); the dry run comes first: `{command(task.id, 'dry', args)}`"


def applied_text(run: dict[str, Any]) -> str:
    verb = "is running" if run.get("status") == "running" else "finished ok"
    return f"apply {run.get('run_id')}, started {run.get('started')}, {verb} with the same scripts and arguments"


def command(task_id: str, mode: str, args: list[str]) -> str:
    flag = {"apply": " --apply", "verify": " --verify"}.get(mode, "")
    return f"opsquire run {task_id}{flag}" + (" -- " + shlex.join(args) if args else "")


def pending(task: Task, runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The task's handover while no run of it has started since; runs are the task's run records. A blocked run
    started nothing, so the handover outlives it."""
    h = task.raw.get("handover")
    if not isinstance(h, dict) or not isinstance(h.get("at"), str):
        return None
    if any(str(r.get("started") or "") >= str(h["at"]) for r in runs if r.get("status") != "blocked"):
        return None
    return h


def args_for(task: Task, runs: list[dict[str, Any]], mode: str) -> list[str]:
    """What a run in this mode takes from the inbox, and the console runs: the arguments of the handed-over run it
    shows; for a staged task's apply nobody handed arguments to, none, so it walks every stage left with each stage's
    own dry run; for any other apply nobody handed over, those of the dry run it follows (an apply is covered only by
    a dry run with its arguments); a staged task's dry run is of its next stage. Any other run of the task has none."""
    h = pending(task, runs)
    if h:
        return [str(x) for x in h.get("args") or []] if h.get("mode") == mode else []
    if mode == "apply" and task.meta and task.meta.stages:
        return []
    ran = [r for r in runs if r.get("status") != "blocked"]
    if mode == "apply" and ran and ran[-1].get("mode") == "dry":
        return [str(x) for x in ran[-1].get("argv") or []]
    stage = next_stage(task.meta, ran) if task.meta and mode == "dry" else None
    return [stage] if stage else []


def record_run_lines(actions: list[tuple[str, str]], why: str, by: str) -> list[str]:
    """Record a handover for every `RUN: opsquire run ...` line an agent ended with; lines that are not such a
    command, or name no task, are left alone (they are the agent's text, not a contract). An apply that already ran
    the same way stays out of the inbox and is reported."""
    from .query import runs_for

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
        same = same_apply(task, runs_for(task.id), args) if mode == "apply" else None
        if same:
            print(f"opsquire: {task.id} not put in the inbox: {applied_text(same)}", file=sys.stderr)
            continue
        uncovered = needs_dry(task, runs_for(task.id), args) if mode == "apply" else None
        if uncovered:
            print(f"opsquire: {task.id} not put in the inbox: {uncovered}", file=sys.stderr)
            continue
        record(task, mode, args, why, by)
        handed.append(task.id)
    return handed


def author() -> str:
    return os.environ.get("OPSQUIRE_AUTHOR", "unknown")
