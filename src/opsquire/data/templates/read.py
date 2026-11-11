"""What this task reads and the question it answers, in one paragraph.

Gather everything the decision might need in one run: state, config, logs, versions, and what you would ask for next.
"""

from __future__ import annotations

from typing import Any

import opsquire_helpers as sq


def read_state(target: str) -> dict[str, Any]:
    return {"target": target}


def finding(state: dict[str, Any]) -> str:
    """What the state says, in one line."""
    return f"read {len(state)} fields"


def main() -> None:
    for i, name in enumerate(sq.TARGETS):
        sq.progress("reading targets", i, len(sq.TARGETS))
        state = read_state(name)
        sq.data(f"{name}: state", **state)
        sq.target(name, "ok", finding(state))
    sq.progress("reading targets", len(sq.TARGETS), len(sq.TARGETS))
    sq.result(f"read {len(sq.TARGETS)} targets")


if __name__ == "__main__":
    sq.run(main)
