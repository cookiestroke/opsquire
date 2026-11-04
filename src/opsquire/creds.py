from __future__ import annotations

import json
from typing import Any

from .model import OpsquireError, creds_path

MIN_SECRET_LEN = 6


def _load(required: bool) -> tuple[Any, str]:
    """The credentials file and a note. A task that declares no credentials runs even where the file cannot be read
    (an agent sandbox denies it): nothing is passed to it, only the redaction of known values is lost, and the note
    goes into the run record."""
    path = creds_path()
    try:
        with path.open() as f:
            return json.load(f), ""
    except FileNotFoundError:
        return None, ""
    except (OSError, json.JSONDecodeError) as e:
        if required or isinstance(e, json.JSONDecodeError):
            raise OpsquireError(f"cannot read {path}: {type(e).__name__}") from None
        return None, f"{path} not readable ({type(e).__name__}); no credentials needed, output not redacted"


def _leaves(node: Any, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(node, dict):
        for k, v in node.items():
            out.update(_leaves(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out.update(_leaves(v, f"{prefix}.{i}"))
    elif isinstance(node, (str, int, float)) and not isinstance(node, bool):
        out[prefix] = str(node)
    return out


class Secrets:
    def __init__(self, required: bool = True) -> None:
        data, self.note = _load(required)
        self.leaves = _leaves(data)

    def resolve(self, wanted: dict[str, str]) -> tuple[dict[str, str], list[str]]:
        env: dict[str, str] = {}
        missing: list[str] = []
        for name, key in wanted.items():
            if key in self.leaves:
                env[name] = self.leaves[key]
            else:
                missing.append(key)
        return env, missing

    def redactor(self) -> Redactor:
        pairs = sorted(
            ((v, k) for k, v in self.leaves.items() if len(v) >= MIN_SECRET_LEN),
            key=lambda p: -len(p[0]),
        )
        return Redactor(pairs)


class Redactor:
    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self.pairs = pairs
        self.count = 0

    def __call__(self, text: str) -> str:
        for value, key in self.pairs:
            if value in text:
                self.count += text.count(value)
                text = text.replace(value, f"[REDACTED:{key}]")
        return text
