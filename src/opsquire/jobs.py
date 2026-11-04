"""Runs started from the UI. A job is `opsquire run <task> [--apply]` under a pseudo-terminal, so the runner and the
helpers find a /dev/tty for their confirmations; the page reads the terminal output, answers prompts and can
interrupt. The runner writes the real records as always; jobs live only in this server process."""

from __future__ import annotations

import fcntl
import os
import pty
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .model import OpsquireError, iso, now, resolve_task
from .query import runs_for

MODES = ("read", "dry", "apply", "verify")
FLAGS = {"apply": ["--apply"], "verify": ["--verify"]}
PROMPT_RE = re.compile(r"(\[y/N\] ?|Type BREAK to take the lock anyway: ?)$")
PROMPT_QUIET_S = 0.3
MAX_JOBS = 50
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


@dataclass
class Job:
    job_id: str
    task_id: str
    mode: str
    started: str
    ended: str | None = None
    status: str = "running"
    exit_code: int | None = None
    run_id: str | None = None
    prompt: str | None = None
    output: bytearray = field(default_factory=bytearray)
    fd: int = -1
    proc: subprocess.Popen[bytes] | None = None
    last_read: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def view(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "task_id": self.task_id,
            "mode": self.mode,
            "started": self.started,
            "ended": self.ended,
            "status": self.status,
            "exit_code": self.exit_code,
            "run_id": self.run_id,
            "prompt": self.prompt,
        }


class Jobs:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.changed = threading.Event()

    def start(self, task_query: str, mode: str) -> Job:
        """A run from the page (mode read, dry or apply), or `judge`: the agent that decides how the task ended."""
        task = resolve_task(task_query)
        if task.meta is None:
            raise OpsquireError(f"{task.id} has invalid meta.json: " + "; ".join(task.errors))
        if mode == "judge":
            if task.kind == "general":
                raise OpsquireError("general scripts are not closed; judge the tasks that use them")
        elif mode not in MODES or mode not in task.meta.modes:
            raise OpsquireError(f"{task.id} runs in {', '.join(task.meta.modes)} mode, not {mode}")
        with self.lock:
            if any(j.task_id == task.id and j.status != "done" for j in self.jobs.values()):
                raise OpsquireError(f"{task.id} already has a job running")
            if len([j for j in self.jobs.values() if j.status != "done"]) >= MAX_JOBS:
                raise OpsquireError("too many jobs running")
            job = Job(f"{now():%Y%m%dT%H%M%S%f}"[:-3] + "Z", task.id, mode, iso(now()))
            self.jobs[job.job_id] = job
        if mode == "judge":
            argv = [sys.executable, "-m", "opsquire", "judge", task.id]
        else:
            argv = [sys.executable, "-m", "opsquire", "run", task.id, *FLAGS.get(mode, [])]
        master, slave = pty.openpty()
        env = {**os.environ, "OPSQUIRE_AUTHOR": os.environ.get("OPSQUIRE_AUTHOR", "opsquire ui"), "TERM": "dumb"}
        try:
            job.proc = subprocess.Popen(
                argv,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
                start_new_session=True,
                preexec_fn=controlling(slave),
            )
        finally:
            os.close(slave)
        job.fd = master
        flags = fcntl.fcntl(master, fcntl.F_GETFL)
        fcntl.fcntl(master, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        threading.Thread(target=self.pump, args=(job,), daemon=True).start()
        self.changed.set()
        return job

    def pump(self, job: Job) -> None:
        proc = job.proc
        assert proc is not None
        while True:
            try:
                chunk = os.read(job.fd, 65536)
            except BlockingIOError:
                chunk = b""
            except OSError:
                chunk = None
            if chunk:
                with job.lock:
                    job.output += chunk
                    job.last_read = time.monotonic()
                    if job.prompt:
                        job.prompt, job.status = None, "running"
                        self.changed.set()
            elif chunk is None or proc.poll() is not None:
                if proc.poll() is None:
                    time.sleep(0.05)
                    continue
                break
            else:
                self.detect_prompt(job)
                time.sleep(0.05)
        self.finish(job)

    def detect_prompt(self, job: Job) -> None:
        with job.lock:
            if job.prompt or not job.output or time.monotonic() - job.last_read < PROMPT_QUIET_S:
                return
            tail = ANSI_RE.sub("", job.output[-400:].decode("utf-8", "replace"))
            if tail.endswith("\n"):
                return
            last = tail.rsplit("\n", 1)[-1]
            if PROMPT_RE.search(last):
                job.prompt, job.status = last.strip(), "waiting"
                self.changed.set()

    def finish(self, job: Job) -> None:
        proc = job.proc
        assert proc is not None
        code = proc.wait()
        with job.lock:
            job.exit_code = code
            job.status, job.prompt = "done", None
            job.ended = iso(now())
            try:
                os.close(job.fd)
            except OSError:
                pass
        runs = [r for r in runs_for(job.task_id) if str(r.get("started", "")) >= job.started]
        job.run_id = str(runs[-1]["run_id"]) if runs and job.mode != "judge" else None
        self.changed.set()

    def get(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise OpsquireError(f"no job {job_id}")
        return job

    def read(self, job_id: str, offset: int) -> dict[str, Any]:
        job = self.get(job_id)
        with job.lock:
            data = bytes(job.output[max(0, offset) :])
            view = job.view()
        return {**view, "text": data.decode("utf-8", "replace"), "offset": max(0, offset) + len(data)}

    def answer(self, job_id: str, text: str) -> None:
        job = self.get(job_id)
        if job.status == "done":
            raise OpsquireError(f"job {job_id} has finished")
        os.write(job.fd, (text.strip() + "\n").encode())
        with job.lock:
            job.prompt, job.status = None, "running"
        self.changed.set()

    def interrupt(self, job_id: str) -> None:
        job = self.get(job_id)
        if job.status == "done" or job.proc is None:
            raise OpsquireError(f"job {job_id} has finished")
        os.killpg(job.proc.pid, signal.SIGINT)

    def stop_all(self) -> None:
        for job in list(self.jobs.values()):
            if job.status != "done" and job.proc is not None:
                try:
                    os.killpg(job.proc.pid, signal.SIGINT)
                except OSError:
                    pass
        deadline = time.monotonic() + 10
        for job in list(self.jobs.values()):
            if job.proc is not None and job.status != "done":
                try:
                    job.proc.wait(timeout=max(0.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    job.proc.kill()

    def views(self) -> list[dict[str, Any]]:
        return [j.view() for j in sorted(self.jobs.values(), key=lambda j: j.started, reverse=True)]


def controlling(slave: int) -> Any:
    """Make the pty the child's controlling terminal, so /dev/tty opens it."""
    import termios

    def hook() -> None:
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

    return hook
