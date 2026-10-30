from __future__ import annotations

import fcntl
import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from .model import locks_dir, read_json, write_json


@dataclass
class Holder:
    run_id: str
    task_id: str
    pid: int
    host: str
    mode: str
    since: str

    def alive(self) -> bool:
        if self.host != socket.gethostname():
            return True
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


@contextmanager
def _state() -> Iterator[dict[str, list[dict[str, Any]]]]:
    d = locks_dir()
    d.mkdir(parents=True, exist_ok=True)
    with (d / ".mutex").open("a") as mutex:
        fcntl.flock(mutex, fcntl.LOCK_EX)
        path = d / "locks.json"
        state: dict[str, list[dict[str, Any]]] = read_json(path) if path.is_file() else {}
        yield state
        write_json(path, {k: v for k, v in sorted(state.items()) if v})


def holders() -> dict[str, list[Holder]]:
    path = locks_dir() / "locks.json"
    state: dict[str, list[dict[str, Any]]] = read_json(path) if path.is_file() else {}
    return {t: [Holder(**h) for h in hs] for t, hs in state.items()}


def acquire(targets: list[str], me: Holder, break_lock: bool) -> tuple[list[Holder], list[Holder], list[Holder]]:
    """Returns (conflicts, stale_reclaimed, broken). Acquires only when there are no conflicts or break_lock is set."""
    with _state() as state:
        stale: list[Holder] = []
        conflicts: list[Holder] = []
        for target in targets:
            current = [Holder(**h) for h in state.get(target, [])]
            live = [h for h in current if h.alive()]
            stale += [h for h in current if not h.alive()]
            state[target] = [h.__dict__ for h in live]
            for h in live:
                if me.mode == "exclusive" or h.mode == "exclusive":
                    conflicts.append(h)
        if conflicts and not break_lock:
            return conflicts, stale, []
        broken_ids = {h.run_id for h in conflicts}
        for target in targets:
            kept = [h for h in state.get(target, []) if h["run_id"] not in broken_ids]
            state[target] = kept + [me.__dict__]
        return [], stale, conflicts


def release(targets: list[str], run_id: str) -> None:
    with _state() as state:
        for target in targets:
            state[target] = [h for h in state.get(target, []) if h["run_id"] != run_id]


def remove(target: str) -> list[Holder]:
    with _state() as state:
        removed = [Holder(**h) for h in state.get(target, [])]
        state[target] = []
        return removed
