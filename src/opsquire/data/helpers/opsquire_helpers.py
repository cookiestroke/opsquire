"""Structured events for scripts run by `opsquire run`. Remote payloads can't import this; they print the same
`@@OPSQUIRE {json}` lines themselves (see AGENTS.md)."""

from __future__ import annotations

import json
import os
import select
import signal
import sys
import time
import traceback
from collections.abc import Callable
from types import FrameType
from typing import Any

MODE = os.environ.get("OPSQUIRE_MODE", "dry")
APPLY = MODE == "apply"
TARGETS = os.environ.get("OPSQUIRE_TARGETS", "").split()


def emit(event: str, msg: str = "", **data: Any) -> None:
    sys.stdout.write("@@OPSQUIRE " + json.dumps({"event": event, "msg": msg, **data}) + "\n")
    sys.stdout.flush()


def step(msg: str, **data: Any) -> None:
    emit("step", msg, **data)


def target(name: str, status: str, msg: str = "", **data: Any) -> None:
    emit("target", msg, target=name, status=status, **data)


def result(msg: str, **data: Any) -> None:
    emit("result", msg, **data)


def warn(msg: str, **data: Any) -> None:
    emit("warn", msg, **data)


def error(msg: str, **data: Any) -> None:
    emit("error", msg, **data)


def data(msg: str, **values: Any) -> None:
    emit("data", msg, **values)


def _announce_prompt(question: str) -> None:
    """Record that the run now waits for the operator, and hold the prompt until everything printed so far is on
    screen (the runner echoes stdout via a pipe). A path, not a file descriptor: subprocess closes inherited
    descriptors by default, and a general script run by a task must get this without the task knowing."""
    emit("waiting", question)
    path = os.environ.get("OPSQUIRE_SYNC_PATH")
    if not path:
        return
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return
    try:
        ready, _, _ = select.select([fd], [], [], 10)
        if ready:
            os.read(fd, 1)
    except OSError:
        pass
    finally:
        os.close(fd)


_last_progress = [0.0]


def progress(label: str, done: float, total: float, unit: str = "") -> None:
    """Report progress; opsquire draws one updating bar and logs every 10%. unit "B" means bytes (shown as
    KB/MB/GB with speed). Calls closer than 0.2 s apart are dropped, except the final one."""
    now = time.monotonic()
    if done < total and now - _last_progress[0] < 0.2:
        return
    _last_progress[0] = now
    emit("progress", label, done=done, total=total, unit=unit)


def confirm(question: str) -> None:
    _announce_prompt(question)
    try:
        with open("/dev/tty", "w") as out, open("/dev/tty") as inp:
            out.write(f"\n>>> {question} [y/N] ")
            out.flush()
            answer = inp.readline().strip().lower()
    except OSError:
        answer = ""
    emit("confirm", question, answer="y" if answer == "y" else "n")
    if answer != "y":
        sys.exit(3)


def _to_interrupt(_signum: int, _frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def run(main: Callable[[], None], cleanup: Callable[[], None] | None = None) -> None:
    """Run main with graceful termination: Ctrl-C, SIGTERM or SIGHUP run cleanup, report, and exit 130. An exception
    runs cleanup too and is reported as an error event with its traceback (the record's last_error names it)."""
    signal.signal(signal.SIGTERM, _to_interrupt)
    signal.signal(signal.SIGHUP, _to_interrupt)
    code = 0
    try:
        main()
    except KeyboardInterrupt:
        code = 130
    except Exception:
        error(traceback.format_exc().rstrip())
        code = 1
    finally:
        if cleanup:
            cleanup()
    if code == 130:
        warn("interrupted by the operator")
    sys.exit(code)
