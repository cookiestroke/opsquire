from __future__ import annotations

import contextlib
import getpass
import json
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path
from types import FrameType
from typing import IO, Any

from . import locks
from .creds import Redactor, Secrets
from .lint import lint_task, save_lint
from .model import (
    HELPERS_DIR,
    SCHEMA,
    Heartbeat,
    Meta,
    OpsquireError,
    Task,
    compact,
    home_dir,
    iso,
    now,
    pid_namespace,
    run_alive,
    run_origin,
    runs_dir,
    write_json,
)
from .progress import ProgressBar

OPSQUIRE_PREFIX = "@@OPSQUIRE "
VERBOSE = os.environ.get("OPSQUIRE_VERBOSE") == "1"
ALWAYS_SHOWN = {"blocked", "interrupted", "lock_broken", "lock_stale_reclaimed"}
SCRIPT_EVENTS = {"step", "target", "result", "warn", "error", "confirm", "data", "progress"}
TARGET_STATUSES = {"ok", "failed", "skipped", "unchanged"}
EXIT_CODES = {"ok": 0, "failed": 1, "partial": 1, "blocked": 2, "aborted": 3, "interrupted": 130}


def style(text: str, code: str, stream: IO[str]) -> str:
    return f"\033[{code}m{text}\033[0m" if stream.isatty() else text


def child_usage() -> dict[str, float]:
    """What the script and everything it spawned cost this machine: peak memory and CPU time (children so far)."""
    import resource

    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {"max_rss_mb": round(ru.ru_maxrss / 1024, 1), "cpu_s": round(ru.ru_utime + ru.ru_stime, 2)}


def tty_prompt(question: str) -> str:
    try:
        with open("/dev/tty", "w") as out, open("/dev/tty") as inp:
            out.write(question)
            out.flush()
            return inp.readline().strip()
    except OSError:
        return ""


class Run:
    def __init__(
        self, task: Task, meta: Meta, mode: str, args: list[str], break_lock: bool, plan_run: str | None = None
    ) -> None:
        self.plan_run = plan_run
        self.task = task
        self.meta = meta
        self.mode = mode
        self.args = args
        self.break_lock = break_lock
        self.started = now()
        self.run_id = f"{compact(self.started)}_{mode}"
        self.dir = runs_dir() / task.id / self.run_id
        self.seq = 0
        self.redact: Redactor = Redactor([])
        self.events_f: IO[str] | None = None
        self.output_f: IO[str] | None = None
        self.targets: dict[str, dict[str, str]] = {}
        self.target_events = False
        self.results: list[str] = []
        self.counts = {"lines": 0, "events": 0, "warn": 0, "error": 0}
        self.last_error = ""
        self.declined = False
        self.interrupted = False
        self.lock_info: dict[str, Any] = {
            "mode": "shared" if mode == "verify" else meta.lock,
            "targets": meta.targets,
            "stale_reclaimed": [],
            "broken": [],
        }
        self.record: dict[str, Any] = {}
        self.locked: list[str] = []
        self.sync_w = -1
        self.tests = 0
        self.usage: dict[str, float] | None = None
        self.bar = ProgressBar(sys.stdout)

    def event(self, event: str, msg: str = "", level: str = "info", src: str = "runner", **extra: Any) -> None:
        self.seq += 1
        self.counts["events"] += 1
        if level in ("warn", "error"):
            self.counts[level] += 1
        rec = {"ts": iso(now()), "seq": self.seq, "run_id": self.run_id, "src": src, "event": event, "level": level}
        rec["msg"] = self.redact(msg)
        for k, v in extra.items():
            rec[k] = json.loads(self.redact(json.dumps(v)))
        if self.events_f:
            self.events_f.write(json.dumps(rec) + "\n")
            self.events_f.flush()
        if src == "runner":
            shown = VERBOSE or event in ALWAYS_SHOWN or level in ("warn", "error")
            self.line("run", f"[{event}] {msg}", echo=sys.stderr if shown else None)
        if event == "run_start" and not VERBOSE:
            sys.stderr.write(style(f"opsquire: {self.task.id} ({self.mode})", "1", sys.stderr) + "\n")
            sys.stderr.flush()

    def line(self, stream: str, text: str, echo: IO[str] | None) -> None:
        text = self.redact(text)
        ts = iso(now())
        if self.output_f:
            self.output_f.write(f"{ts} {stream:<3} | {text}\n")
            self.output_f.flush()
        if echo:
            self.bar.suspend()
            echo.write(f"{text}\n" if stream != "run" else style(f"{ts} run | {text}", "2", echo) + "\n")
            echo.flush()
            self.bar.resume()
        if stream in ("out", "err"):
            self.counts["lines"] += 1

    def handle_script_event(self, payload: str, stream: str) -> None:
        try:
            data = json.loads(payload)
            assert isinstance(data, dict)
        except (ValueError, AssertionError):
            self.event("warn", f"unparseable @@OPSQUIRE line: {payload[:200]}", level="warn")
            return
        kind = str(data.pop("event", ""))
        if kind == "sync":
            self.release_prompt()
            return
        msg = str(data.pop("msg", ""))
        level = str(data.pop("level", "error" if kind == "error" else "warn" if kind == "warn" else "info"))
        if kind not in SCRIPT_EVENTS:
            self.event("warn", f"unknown script event {kind!r}", level="warn")
            return
        if kind == "progress":
            self.show_progress(msg, data, stream)
            return
        if kind == "target":
            name, status = str(data.get("target", "")), str(data.get("status", ""))
            if name not in self.meta.targets or status not in TARGET_STATUSES:
                self.event("warn", f"bad target event {name!r}/{status!r}", level="warn")
            else:
                self.target_events = True
                self.targets[name] = {"status": status, "msg": self.redact(msg)}
                if status == "failed":
                    level = "error"
        if kind == "confirm" and str(data.get("answer", "")) != "y":
            self.declined = True
        if kind == "result":
            self.results.append(self.redact(msg))
        if level == "error":
            self.last_error = self.redact(msg)
        self.event(kind, msg, level=level, src="script", **data)
        shown = msg
        if kind == "target":
            shown = f"{data.get('target')} {data.get('status')}" + (f": {msg}" if msg else "")
        elif kind == "confirm":
            shown = f"{msg} -> {data.get('answer')}"
        self.line(stream, f"{OPSQUIRE_PREFIX}{kind}: {shown}", echo=None)
        if kind != "data" or VERBOSE:
            self.bar.suspend()
            sys.stdout.write(f"{style(f'[{kind}]', '1', sys.stdout)} {self.redact(shown)}\n")
            sys.stdout.flush()
            self.bar.resume()

    def show_progress(self, label: str, data: dict[str, Any], stream: str) -> None:
        try:
            done, total = float(data.get("done", 0)), float(data.get("total", 0))
        except (TypeError, ValueError):
            self.event("warn", f"bad progress event for {label!r}", level="warn")
            return
        text = self.bar.update(self.redact(label), done, total, str(data.get("unit", "")))
        if text:
            self.event("progress", label, src="script", done=done, total=total, unit=data.get("unit", ""))
            self.line(stream, f"{OPSQUIRE_PREFIX}progress: {text}", echo=None)

    def release_prompt(self) -> None:
        """The script printed everything before its prompt and that output is now echoed: let it ask."""
        self.bar.close()
        if self.sync_w >= 0:
            with contextlib.suppress(OSError):
                os.write(self.sync_w, b"x")

    def write_record(self, status: str, exit_code: int | None) -> None:
        ended = now() if status != "running" else None
        self.record = {
            "schema": SCHEMA,
            "run_id": self.run_id,
            "task_id": self.task.id,
            "task_kind": self.task.kind,
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
            "cwd": os.getcwd(),
            "origin": run_origin(),
            "argv": self.args,
            "entry": self.meta.verify if self.mode == "verify" and self.meta.verify else self.meta.entry,
            "interpreter": self.meta.command(self.task.folder / self.meta.entry)[0],
            "exit_code": exit_code,
            "task_hash": self.task.content_hash(),
            "files": self.task.file_hashes(),
            "targets": self.targets,
            "lock": self.lock_info,
            "tests": self.tests,
            "plan_run": self.plan_run,
            "credentials_used": sorted(self.meta.credentials),
            "redactions": self.redact.count,
            "counts": self.counts,
            "results": self.results,
            "usage": self.usage,
            "last_error": self.last_error,
            "paths": {
                "dir": str(self.dir),
                "output": str(self.dir / "output.log"),
                "events": str(self.dir / "events.jsonl"),
                "snapshot": str(self.dir / "snapshot"),
            },
        }
        write_json(self.dir / "run.json", self.record)

    def finish_targets(self, status: str) -> None:
        inherit = {"ok": "ok", "failed": "failed"}.get(status, "skipped")
        for t in self.meta.targets:
            if t not in self.targets:
                self.targets[t] = {"status": inherit if not self.target_events else "skipped", "msg": ""}

    def verdict(self, status: str) -> None:
        took = round((now() - self.started).total_seconds(), 1)
        tests = f", {self.tests} tests" if self.tests else ""
        warns = f", {self.counts['warn']} warnings" if self.counts["warn"] else ""
        line = (
            f"opsquire: {self.task.id} {status.upper()} in {took}s{tests}{warns}; details: opsquire log {self.task.id}"
        )
        if self.plan_run:
            return
        sys.stderr.write(style(line, "1" if status == "ok" else "31;1", sys.stderr) + "\n")
        sys.stderr.flush()

    def blocked(self, reason: str) -> int:
        self.event("blocked", reason, level="error")
        self.last_error = reason
        self.finish_targets("blocked")
        self.write_record("blocked", None)
        self.event("run_end", "status=blocked")
        self.verdict("blocked")
        return EXIT_CODES["blocked"]

    def snapshot(self) -> None:
        home = home_dir()
        for p in self.task.files():
            dest = self.dir / "snapshot" / p.relative_to(home)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)

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
                self.events_f, self.output_f = ev, out
                try:
                    with Heartbeat(self.dir):
                        return self._execute()
                except KeyboardInterrupt:
                    return self.stopped_before_script()
                finally:
                    self.release_locks()
                    self.events_f = self.output_f = None
        except BaseException:
            if not (self.dir / "run.json").exists():
                shutil.rmtree(self.dir, ignore_errors=True)
                with contextlib.suppress(OSError):
                    self.dir.parent.rmdir()
            raise

    def release_locks(self) -> None:
        if self.locked:
            locks.release(self.locked, self.run_id)
            self.locked = []

    def stopped_before_script(self) -> int:
        self.event("interrupted", "stopped by the operator before the script started", level="warn")
        self.last_error = "interrupted before the script started"
        self.finish_targets("interrupted")
        self.write_record("interrupted", None)
        self.event("run_end", "status=interrupted")
        self.verdict("interrupted")
        return EXIT_CODES["interrupted"]

    def _execute(self) -> int:
        self.write_record("running", None)
        self.event(
            "run_start",
            f"{self.task.id} mode={self.mode}",
            purpose=self.meta.purpose,
            targets=self.meta.targets,
            user=getpass.getuser(),
            host=socket.gethostname(),
        )
        self.snapshot()
        try:
            secrets = Secrets(required=bool(self.meta.credentials))
        except OpsquireError as e:
            return self.blocked(str(e))
        self.redact = secrets.redactor()
        if secrets.note:
            self.event("warn", secrets.note, level="warn")
        env_creds, missing = secrets.resolve(self.meta.credentials)
        if missing:
            return self.blocked("missing credential keys in ~/.creds: " + ", ".join(missing))
        self.event("lint", "checking script versions with ruff/mypy/shellcheck")
        result = lint_task(self.task)
        save_lint(self.task, result)
        self.tests = result.tests
        if not result.ok:
            self.line("run", result.output, echo=sys.stderr)
            return self.blocked("lint or tests failed on the exact files being run")
        if self.task.content_hash() != result.content_hash:
            return self.blocked("files changed while linting")

        holders: list[str] = self.meta.targets if self.meta.lock != "none" else []
        lock_kind = "shared" if self.mode == "verify" else self.meta.lock
        if self.plan_run:
            self.lock_info["held_by_plan_run"] = self.plan_run
            self.event("lock", f"targets locked by plan run {self.plan_run}")
        elif self.meta.lock == "none":
            self.event("lock", f"no lock: {self.meta.lock_reason}")
        else:
            me = locks.Holder(
                self.run_id,
                self.task.id,
                os.getpid(),
                socket.gethostname(),
                lock_kind,
                iso(self.started),
                pid_namespace(),
            )
            try:
                conflicts, stale, _ = locks.acquire(holders, me, break_lock=False)
            except OSError as e:
                return self.blocked(f"cannot write locks ({e.strerror}); run opsquire in your own terminal")
            if not conflicts:
                self.locked = holders
            for h in stale:
                self.event("lock_stale_reclaimed", f"{h.run_id} pid {h.pid} is gone", level="warn")
                self.lock_info["stale_reclaimed"].append(h.__dict__)
            if conflicts:
                desc = "; ".join(f"{h.task_id} run {h.run_id} ({h.mode}, since {h.since})" for h in conflicts)
                if not self.break_lock:
                    return self.blocked(f"targets locked by {desc}. Re-run with --break-lock to override")
                answer = tty_prompt(f"Locked by {desc}.\nType BREAK to take the lock anyway: ")
                if answer != "BREAK":
                    return self.blocked(f"lock override declined; held by {desc}")
                _, _, broken = locks.acquire(holders, me, break_lock=True)
                self.locked = holders
                for h in broken:
                    self.event("lock_broken", f"overrode {h.run_id} ({h.task_id})", level="warn")
                    self.lock_info["broken"].append(h.__dict__)
            self.event("lock", f"{lock_kind} lock on {', '.join(holders)}")

        return self._spawn(env_creds)

    def _spawn(self, env_creds: dict[str, str]) -> int:
        entry = self.task.folder / (self.meta.verify if self.mode == "verify" and self.meta.verify else self.meta.entry)
        cmd = [*self.meta.command(entry), str(entry), *self.args]
        env = {
            **os.environ,
            **env_creds,
            "OPSQUIRE_MODE": self.mode,
            "OPSQUIRE_RUN_ID": self.run_id,
            "OPSQUIRE_TASK_ID": self.task.id,
            "OPSQUIRE_TARGETS": " ".join(self.meta.targets),
            "OPSQUIRE_HOME": str(home_dir()),
            "OPSQUIRE_GENERAL": str(home_dir() / "general"),
            "OPSQUIRE_HELPERS": str(HELPERS_DIR),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join(filter(None, [str(HELPERS_DIR), os.environ.get("PYTHONPATH", "")])),
        }
        self.event("spawn", " ".join(cmd))
        sync_r, self.sync_w = os.pipe()
        env["OPSQUIRE_SYNC_FD"] = str(sync_r)
        try:
            proc = subprocess.Popen(
                cmd, cwd=self.task.folder, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(sync_r,)
            )
        finally:
            os.close(sync_r)
        lines: queue.Queue[tuple[str, str | None]] = queue.Queue()

        def pump(pipe: IO[bytes], name: str) -> None:
            for raw in iter(pipe.readline, b""):
                lines.put((name, raw.decode("utf-8", "replace").rstrip("\r\n")))
            lines.put((name, None))

        assert proc.stdout and proc.stderr
        threads = [
            threading.Thread(target=pump, args=(proc.stdout, "out"), daemon=True),
            threading.Thread(target=pump, args=(proc.stderr, "err"), daemon=True),
        ]
        for t in threads:
            t.start()

        def on_signal(signum: int, _frame: FrameType | None) -> None:
            if self.interrupted and proc.poll() is None:
                proc.kill()
            self.interrupted = True
            if signum != signal.SIGINT and proc.poll() is None:
                proc.terminate()

        previous = {s: signal.signal(s, on_signal) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        try:
            open_streams = 2
            while open_streams:
                name, text = lines.get()
                if text is None:
                    open_streams -= 1
                elif text.startswith(OPSQUIRE_PREFIX):
                    self.handle_script_event(text[len(OPSQUIRE_PREFIX) :], name)
                else:
                    self.line(name, text, echo=sys.stdout if name == "out" else sys.stderr)
            code = proc.wait()
            self.usage = child_usage()
        finally:
            self.bar.close()
            for s, h in previous.items():
                signal.signal(s, h)
            os.close(self.sync_w)
            self.sync_w = -1

        status = self.classify(code)
        self.finish_targets(status)
        self.write_record(status, code)
        self.event("run_end", f"status={status} exit={code}", level="info" if status == "ok" else "error")
        self.line("run", f"run {self.run_id}: {status.upper()}  ->  {self.dir / 'run.json'}", echo=None)
        self.verdict(status)
        return EXIT_CODES[status]

    def classify(self, code: int) -> str:
        if self.interrupted or code < 0 or code == EXIT_CODES["interrupted"]:
            return "interrupted"
        if self.declined:
            return "aborted"
        statuses = [t["status"] for t in self.targets.values()]
        good = any(s in ("ok", "unchanged") for s in statuses)
        bad = any(s == "failed" for s in statuses)
        if code != 0:
            if not self.last_error:
                self.last_error = f"exit code {code}"
            return "partial" if good else "failed"
        if bad:
            return "partial" if good else "failed"
        return "ok"


def run_task(task: Task, apply: bool, args: list[str], break_lock: bool, verify: bool = False) -> int:
    if task.meta is None:
        raise OpsquireError(f"{task.id} has invalid meta.json:\n  " + "\n  ".join(task.errors))
    if task.kind == "general":
        raise OpsquireError("general scripts are not run directly; create a task that calls it")
    if task.kind == "plan":
        raise OpsquireError("plans run through opsquire run, not run_task")
    if task.meta.archived:
        raise OpsquireError(
            f"{task.id} is archived (historical record, not runnable); see archived.note in its meta.json"
        )
    if task.meta.superseded_by or task.meta.abandoned_reason:
        raise OpsquireError(f"{task.id} is superseded or abandoned; see its meta.json")
    meta = task.meta
    if apply and not meta.changes_prod:
        raise OpsquireError(f"{task.id} is read-only (changes_prod is false); run it without --apply")
    if verify and not meta.verify:
        raise OpsquireError(f"{task.id} has no verify check (meta.json `verify`); add verify.py or verify.sh")
    mode = "verify" if verify else "apply" if apply else meta.modes[0]
    return Run(task, meta, mode, args, break_lock).execute()


def load_run(path: Path) -> dict[str, Any]:
    with path.open() as f:
        rec: dict[str, Any] = json.load(f)
    alive = run_alive(int(rec.get("pid", 0)), str(rec.get("host")), path.parent, str(rec.get("pid_ns", "")))
    if rec.get("status") == "running" and not alive:
        rec["status"] = "interrupted"
        rec["last_error"] = "runner process vanished without finishing (killed or terminal closed)"
    return rec
