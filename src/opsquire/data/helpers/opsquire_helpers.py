"""Structured events for scripts run by `opsquire run`. Remote payloads can't import this; they print the same
`@@OPSQUIRE {json}` lines themselves (see AGENTS.md)."""

from __future__ import annotations

import json
import os
import select
import signal
import sys
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


def _wait_for_echo() -> None:
    """Make sure everything printed so far is on screen before a prompt (the runner echoes stdout via a pipe)."""
    fd = os.environ.get("OPSQUIRE_SYNC_FD")
    if not fd:
        return
    emit("sync")
    try:
        ready, _, _ = select.select([int(fd)], [], [], 10)
        if ready:
            os.read(int(fd), 1)
    except (OSError, ValueError):
        pass


def confirm(question: str) -> None:
    _wait_for_echo()
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
    """Run main with graceful termination: Ctrl-C, SIGTERM or SIGHUP run cleanup, report, and exit 130."""
    signal.signal(signal.SIGTERM, _to_interrupt)
    signal.signal(signal.SIGHUP, _to_interrupt)
    code = 0
    try:
        main()
    except KeyboardInterrupt:
        code = 130
    finally:
        if cleanup:
            cleanup()
    if code:
        warn("interrupted by the operator")
    sys.exit(code)
