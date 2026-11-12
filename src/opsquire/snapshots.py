"""The console's state, built once per change of the home and shared by every request, with what changed between
versions: a page that has a version gets only the tasks, runs and scripts that moved, not the whole home again."""

from __future__ import annotations

import json
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

KEYED = ("tasks", "runs", "scripts")
KEPT = 8


def encoded(entries: dict[str, Any]) -> dict[str, str]:
    return {i: json.dumps(v, sort_keys=True) for i, v in entries.items()}


class Snapshots:
    def __init__(self, build: Callable[[], dict[str, Any]]) -> None:
        self.build = build
        self.boot = uuid.uuid4().hex[:8]
        self.lock = threading.Lock()
        self.history: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.built_for = -1
        self.current = ""

    def version(self, generation: int) -> str:
        return f"{self.boot}.{generation}"

    def at(self, generation: int) -> str:
        """The version for this generation of the home, built at most once however many pages ask."""
        with self.lock:
            if generation != self.built_for:
                state = self.build()
                version = self.version(generation)
                self.history[version] = {
                    "state": state,
                    "parts": {
                        k: {i: json.dumps(v, sort_keys=True) for i, v in (state.get(k) or {}).items()} for k in KEYED
                    },
                    "rest": {k: json.dumps(v, sort_keys=True) for k, v in state.items() if k not in KEYED},
                }
                while len(self.history) > KEPT:
                    self.history.popitem(last=False)
                self.built_for, self.current = generation, version
            return self.current

    def answer(self, generation: int, since: str) -> dict[str, Any]:
        """`same` when the page is current, a `patch` from a version still kept, else the `full` state, in the shape
        readers had before versions (the state's own keys at the top)."""
        version = self.at(generation)
        with self.lock:
            now = self.history[version]
            if since == version:
                return {"kind": "same", "version": version}
            base = self.history.get(since)
            if base is None:
                return {**now["state"], "kind": "full", "version": version}
            changed: dict[str, dict[str, Any]] = {}
            for key in KEYED:
                old, new = base["parts"][key], now["parts"][key]
                moved = {i: now["state"][key][i] for i, text in new.items() if old.get(i) != text}
                moved |= {i: None for i in old if i not in new}
                if moved:
                    changed[key] = moved
            replace = {k: now["state"][k] for k, text in now["rest"].items() if base["rest"].get(k) != text}
            return {"kind": "patch", "version": version, "since": since, "changed": changed, "replace": replace}
