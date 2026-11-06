from __future__ import annotations

import contextlib
import datetime as dt
import getpass
import json
import os
import shutil
import socket
import sys
from typing import IO, Any

from . import locks, runner
from .creds import Redactor, Secrets
from .lint import lint_task
from .model import (
    SCHEMA,
    Heartbeat,
    Meta,
    OpsquireError,
    Task,
    compact,
    iso,
    load_task,
    now,
    pid_namespace,
    resolve_task,
    run_origin,
    runs_dir,
    task_folder,
    write_json,
)
from .runner import EXIT_CODES, Run, style, tty_prompt


def plan_meta(
    name: str,
    t: dt.datetime,
    area: str,
    purpose: str,
    author: str,
    steps: list[dict[str, Any]],
    rollback: str | None = None,
    links: list[str] | None = None,
    thread: str | None = None,
) -> Meta:
    """Meta for a plan over existing tasks; targets and changes_prod follow from the steps."""
    metas = []
    for step in steps:
        task = resolve_task(str(step["task"]))
        if task.meta is None or task.kind != "task":
            raise OpsquireError(f"step {task.id} is not a valid task")
        metas.append(task.meta)
    changes = any(m.changes_prod for m in metas)
    return Meta(
        id=name,
        kind="plan",
        created=iso(t),
        author=author,
        purpose=purpose,
        area=area,
        entry="",
        changes_prod=changes,
        targets=sorted({x for m in metas for x in m.targets}),
        lock="exclusive" if changes else "shared",
        lock_reason="",
        credentials={},
        uses=[],
        rollback=rollback or ("each step's own rollback, see steps" if changes else "read-only"),
        links=links or [],
        steps=steps,
        thread=thread,
    )


STOPPING = {
    "failed": "failed",
    "partial": "failed",
    "aborted": "aborted",
    "interrupted": "interrupted",
    "blocked": "blocked",
}


def lock_mode(steps: list[Task]) -> str:
    modes = {t.meta.lock for t in steps if t.meta}
    if "exclusive" in modes:
        return "exclusive"
    return "shared" if "shared" in modes else "none"


class PlanRun:
    def __init__(self, plan: Task, meta: Meta, apply: bool, break_lock: bool) -> None:
        self.plan = plan
        self.meta = meta
        self.apply = apply
        self.break_lock = break_lock
        self.mode = "apply" if apply else meta.modes[0]
        self.started = now()
        self.run_id = f"{compact(self.started)}_{self.mode}"
        self.dir = runs_dir() / plan.id / self.run_id
        self.redact = Redactor([])
        self.out: IO[str] | None = None
        self.events: IO[str] | None = None
        self.seq = 0
        self.steps: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.last_error = ""
        self.locked: list[str] = []
        self.lock_info: dict[str, Any] = {"mode": "none", "targets": meta.targets, "stale_reclaimed": [], "broken": []}

    def event(self, event: str, msg: str = "", level: str = "info", **extra: Any) -> None:
        self.seq += 1
        ts = iso(now())
        rec = {"ts": ts, "seq": self.seq, "run_id": self.run_id, "src": "plan", "event": event, "level": level}
        rec |= {"msg": self.redact(msg), **extra}
        if self.events:
            self.events.write(json.dumps(rec) + "\n")
            self.events.flush()
        line = f"{ts} plan | [{event}] {self.redact(msg)}"
        if self.out:
            self.out.write(line + "\n")
            self.out.flush()
        if runner.VERBOSE or event in ("plan_start", "step_start", "step_end", "plan_end") or level != "info":
            sys.stderr.write(
                style(line if runner.VERBOSE else f"opsquire plan: {self.redact(msg)}", "1", sys.stderr) + "\n"
            )
            sys.stderr.flush()

    def write_record(self, status: str) -> None:
        ended = now() if status != "running" else None
        targets: dict[str, dict[str, str]] = {}
        for target in self.meta.targets:
            seen = [s["targets"].get(target, {}).get("status") for s in self.steps if s.get("targets")]
            seen = [x for x in seen if x]
            state = (
                "failed"
                if "failed" in seen
                else "ok"
                if seen and all(x in ("ok", "unchanged") for x in seen)
                else "skipped"
            )
            targets[target] = {"status": state, "msg": ""}
        write_json(
            self.dir / "run.json",
            {
                "schema": SCHEMA,
                "run_id": self.run_id,
                "task_id": self.plan.id,
                "task_kind": "plan",
                "purpose": self.meta.purpose,
                "area": self.meta.area,
                "mode": self.mode,
                "status": status,
                "started": iso(self.started),
                "ended": iso(ended) if ended else None,
                "duration_s": round((ended - self.started).total_seconds(), 3) if ended else None,
                "user": getpass.getuser(),
                "host": socket.gethostname(),
                "pid": os.getpid(),
                "pid_ns": pid_namespace(),
                "origin": run_origin(),
                "steps": self.steps,
                "targets": targets,
                "lock": self.lock_info,
                "warnings": self.warnings,
                "results": [r for s in self.steps for r in s.get("results", [])],
                "last_error": self.last_error,
                "paths": {
                    "dir": str(self.dir),
                    "output": str(self.dir / "output.log"),
                    "events": str(self.dir / "events.jsonl"),
                },
            },
        )

    def finish(self, status: str, reason: str = "") -> int:
        if reason:
            self.last_error = reason
        done = {s["task"] for s in self.steps}
        for step in self.meta.steps or []:
            if step["task"] not in done:
                self.steps.append({"task": step["task"], "status": "skipped"})
        self.write_record(status)
        self.event(
            "plan_end", f"status={status}" + (f": {reason}" if reason else ""), "info" if status == "ok" else "error"
        )
        return EXIT_CODES[status]

    def execute(self) -> int:
        try:
            self.dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            raise OpsquireError(f"run directory already exists: {self.dir}") from None
        except OSError:
            raise OpsquireError(
                f"cannot write {self.dir}: run records are written only by opsquire run in your own terminal"
            ) from None
        try:
            with (self.dir / "events.jsonl").open("a") as ev, (self.dir / "output.log").open("a") as out:
                self.events, self.out = ev, out
                try:
                    with Heartbeat(self.dir):
                        return self._execute()
                except KeyboardInterrupt:
                    return self.finish("interrupted", "interrupted by the operator between steps")
                finally:
                    if self.locked:
                        locks.release(self.locked, self.run_id)
                        self.locked = []
                    self.events = self.out = None
        except BaseException:
            if not (self.dir / "run.json").exists():
                shutil.rmtree(self.dir, ignore_errors=True)
                with contextlib.suppress(OSError):
                    self.dir.parent.rmdir()
            raise

    def _execute(self) -> int:
        self.write_record("running")
        specs = self.meta.steps or []
        self.event("plan_start", f"{self.plan.id} mode={self.mode} steps={len(specs)}", purpose=self.meta.purpose)
        tasks: list[Task] = []
        for spec in specs:
            folder = task_folder(spec["task"])
            task = load_task(folder) if folder else None
            if task is None or task.meta is None:
                return self.finish("blocked", f"step {spec['task']} is missing or invalid")
            tasks.append(task)

        try:
            secrets = Secrets()
        except OpsquireError as e:
            return self.finish("blocked", str(e))
        self.redact = secrets.redactor()
        for task in tasks:
            assert task.meta
            _, missing = secrets.resolve(task.meta.credentials)
            if missing:
                return self.finish("blocked", f"step {task.id}: missing credential keys: {', '.join(missing)}")

        self.event("lint", f"checking all {len(tasks)} steps before running any")
        for task in tasks:
            if not lint_task(task).ok:
                return self.finish("blocked", f"step {task.id} fails lint; run opsquire lint {task.id}")

        mode = lock_mode(tasks)
        self.lock_info["mode"] = mode
        if mode != "none":
            targets = self.meta.targets
            me = locks.Holder(
                self.run_id, self.plan.id, os.getpid(), socket.gethostname(), mode, iso(self.started), pid_namespace()
            )
            try:
                conflicts, stale, _ = locks.acquire(targets, me, break_lock=False)
            except OSError as e:
                return self.finish("blocked", f"cannot write locks ({e.strerror}); run opsquire in your own terminal")
            self.lock_info["stale_reclaimed"] = [h.__dict__ for h in stale]
            if conflicts:
                desc = "; ".join(f"{h.task_id} run {h.run_id} ({h.mode}, since {h.since})" for h in conflicts)
                if not self.break_lock:
                    return self.finish("blocked", f"targets locked by {desc}. Re-run with --break-lock to override")
                if tty_prompt(f"Locked by {desc}.\nType BREAK to take the lock anyway: ") != "BREAK":
                    return self.finish("blocked", f"lock override declined; held by {desc}")
                _, _, broken = locks.acquire(targets, me, break_lock=True)
                self.lock_info["broken"] = [h.__dict__ for h in broken]
            self.locked = targets
            self.event("lock", f"{mode} lock on {', '.join(targets)} for the whole plan")

        if not self.apply:
            for spec in specs:
                if spec.get("verify"):
                    self.warnings.append(
                        f"{spec['task']} is a verify step: in a dry run it checks the state before any change"
                    )

        for i, (spec, task) in enumerate(zip(specs, tasks, strict=True), 1):
            status = self.run_step(i, spec, task)
            if status not in ("ok",):
                reason = f"step {i} ({task.id}) ended {status}"
                if self.apply and status in ("failed", "partial") and spec.get("on_failure"):
                    self.rollback(i, spec["on_failure"])
                return self.finish(STOPPING.get(status, "failed"), reason)
        return self.finish("ok")

    def run_step(self, i: int, spec: dict[str, Any], task: Task) -> str:
        assert task.meta
        mode = "apply" if self.apply and task.meta.changes_prod else task.meta.modes[0]
        self.event("step_start", f"step {i}/{len(self.meta.steps or [])}: {task.id} mode={mode}")
        run = Run(task, task.meta, mode, list(spec.get("args", [])), False, plan_run=self.run_id)
        try:
            run.execute()
        except OpsquireError as e:
            self.steps.append({"task": task.id, "status": "failed", "error": str(e)})
            return "failed"
        rec = run.record
        self.steps.append(
            {
                "task": task.id,
                "run_id": rec.get("run_id"),
                "mode": mode,
                "status": rec.get("status"),
                "targets": rec.get("targets", {}),
                "results": rec.get("results", []),
                "last_error": rec.get("last_error", ""),
                "dir": rec.get("paths", {}).get("dir"),
            }
        )
        status = str(rec.get("status"))
        self.event("step_end", f"step {i}: {task.id} {status}", "info" if status == "ok" else "error")
        return status

    def rollback(self, i: int, task_id: str) -> None:
        folder = task_folder(task_id)
        task = load_task(folder) if folder else None
        if task is None or task.meta is None:
            self.warnings.append(f"rollback task {task_id} is missing or invalid; nothing was rolled back")
            return
        self.event("rollback", f"step {i} failed: running its rollback task {task_id}", "warn")
        mode = "apply" if task.meta.changes_prod else task.meta.modes[0]
        run = Run(task, task.meta, mode, [], False, plan_run=self.run_id)
        try:
            run.execute()
        except OpsquireError as e:
            self.warnings.append(f"rollback {task_id} could not start: {e}")
            return
        self.steps[-1]["rollback"] = {
            "task": task_id,
            "run_id": run.record.get("run_id"),
            "status": run.record.get("status"),
        }


def run_plan(plan: Task, apply: bool, break_lock: bool) -> int:
    if plan.meta is None:
        raise OpsquireError(f"{plan.id} has invalid meta.json:\n  " + "\n  ".join(plan.errors))
    if plan.meta.archived or plan.meta.superseded_by or plan.meta.abandoned_reason:
        raise OpsquireError(f"{plan.id} is retired; see its meta.json")
    if apply and not plan.meta.changes_prod:
        raise OpsquireError(f"{plan.id} only reads (no step changes prod); run it without --apply")
    return PlanRun(plan, plan.meta, apply, break_lock).execute()
