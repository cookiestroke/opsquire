"""What this task changes and why, in one paragraph.

Dry: read every target and report what an apply would change. Apply: one question listing every change, then change
each target and report it. A target already as intended is reported unchanged and left alone."""

from __future__ import annotations

from typing import Any

import opsquire_helpers as sq


def read_state(target: str) -> dict[str, Any]:
    """Read-only: everything the plan and the operator's decision need, in one pass."""
    return {"target": target}


def plan_for(state: dict[str, Any]) -> list[str]:
    """The changes a target needs, decided from its state alone (test_main.py covers it); empty when it is right."""
    return []


def change(target: str, changes: list[str]) -> None:
    sq.step(f"{target}: {'; '.join(changes)}")


def main() -> None:
    plans: dict[str, list[str]] = {}
    for i, name in enumerate(sq.TARGETS):
        sq.progress("reading targets", i, len(sq.TARGETS))
        state = read_state(name)
        sq.data(f"{name}: state", **state)
        plans[name] = plan_for(state)
    sq.progress("reading targets", len(sq.TARGETS), len(sq.TARGETS))
    todo = {name: changes for name, changes in plans.items() if changes}
    for name in sq.TARGETS:
        if name not in todo:
            sq.target(name, "unchanged", "already as intended")
        elif not sq.APPLY:
            sq.target(name, "ok", "would " + "; ".join(todo[name]))
    if not sq.APPLY or not todo:
        sq.result(f"{len(todo)} of {len(sq.TARGETS)} targets need a change")
        return
    sq.confirm("Apply: " + "; ".join(f"{name}: {', '.join(changes)}" for name, changes in todo.items()) + "?")
    for name, changes in todo.items():
        change(name, changes)
        sq.target(name, "ok", "; ".join(changes))
    sq.result(f"changed {len(todo)} of {len(sq.TARGETS)} targets")


if __name__ == "__main__":
    sq.run(main)
