"""`opsquire judge <task>`: an agent reads the task's record (purpose, design, runs, the last output) and decides
how it ended: done, partial or abandoned with a note, or keep it open when the record does not say. The decision is
written with `close`, marked as the judge's, so the operator ticks nothing off by hand and can still amend it."""

from __future__ import annotations

import os
import re
from typing import Any

from .ask import Budget, Conversation, agent_env, pick_agent
from .close import close_task
from .model import OpsquireError, home_dir, iso, now, resolve_task, write_json
from .query import runs_for

CLOSE_RE = re.compile(r"^CLOSE:\s*(done|partial|abandoned)\s*\|\s*(.+?)\s*$", re.M)
KEEP_RE = re.compile(r"^KEEP:\s*(.+?)\s*$", re.M)
JUDGE_BUDGET_USD = 2.0
LOG_TAIL_LINES = 80

SYSTEM = """You judge how an operations task ended, from its record only. You change nothing and run nothing.
Decide from the evidence: the purpose says what was wanted; the runs, their target statuses, results, errors and
the last output say what happened; the design says what was decided. Rules:
- done: the purpose was achieved on every target (an ok apply, or later evidence that the hosts ended up right).
- partial: some of it was achieved and the rest will not be done by this task.
- abandoned: nothing of it will happen through this task (superseded, not needed any more, given up).
- keep: the record does not say; name what would settle it.
End your answer with exactly one line, nothing after it:
CLOSE: done|partial|abandoned | <one or two sentences: what actually ended up live, with the run id as evidence>
or
KEEP: <what is missing to decide>"""


def brief(task_id: str) -> str:
    task = resolve_task(task_id)
    m = task.meta
    if m is None:
        raise OpsquireError(f"{task.id} has invalid meta.json: " + "; ".join(task.errors))
    lines = [f"Task {task.id} (area {m.area}, {'changes production' if m.changes_prod else 'read-only'})"]
    lines.append(f"Purpose: {m.purpose}")
    lines.append(f"Targets: {', '.join(m.targets)}; created {m.created} by {m.author}; today is {iso(now())}")
    if m.superseded_by:
        lines.append(f"Marked superseded by {m.superseded_by}")
    if m.outcome:
        lines.append(
            f"Already closed {m.outcome.get('state')} by {m.outcome.get('closed_by')}: {m.outcome.get('note')}"
        )
    design = task.folder / "design.md"
    if design.is_file():
        lines += ["", "design.md:", design.read_text()[:6000]]
    runs = runs_for(task.id)
    lines += ["", f"Runs ({len(runs)}), oldest first:"]
    for r in runs[-12:]:
        targets = ", ".join(
            f"{t}={v.get('status') if isinstance(v, dict) else v}" for t, v in (r.get("targets") or {}).items()
        )
        lines.append(
            f"- {r.get('run_id')} {r.get('mode')} {r.get('status')} ({r.get('duration_s')} s) targets: {targets}"
            + (f" error: {r.get('last_error')}" if r.get("last_error") else "")
            + (f" results: {' | '.join(str(x) for x in r.get('results') or [])[:600]}" if r.get("results") else "")
        )
    if runs:
        last = runs[-1]
        try:
            with open(str(last["paths"]["output"]), errors="replace") as f:
                tail = f.read().splitlines()[-LOG_TAIL_LINES:]
            lines += ["", f"Last {len(tail)} lines of the last run's output ({last.get('run_id')}):", *tail]
        except (OSError, KeyError):
            pass
    return "\n".join(lines)


def judge(task_id: str, agent_name: str | None = None) -> dict[str, Any]:
    """Ask the agent; close the task when it decides, or report why it kept it open."""
    name, agent = pick_agent(agent_name)
    task = resolve_task(task_id)
    text = brief(task.id)
    env = agent_env(name, f"judge-{task.id}", {"OPSQUIRE_AUTHOR": f"opsquire judge ({name})"})
    conv = Conversation(name, agent, str(home_dir()), SYSTEM, env, label=f"[judge {task.id}]")
    res = conv.turn(text, Budget(JUDGE_BUDGET_USD, True))
    if res.exit_code:
        raise OpsquireError(f"the judge agent failed (exit {res.exit_code}): {res.text[-300:]}")
    close = CLOSE_RE.search(res.text)
    keep = KEEP_RE.search(res.text)
    if close:
        os.environ["OPSQUIRE_AUTHOR"] = f"opsquire judge ({name})"
        closed = close_task(task.id, close.group(1), close.group(2))
        return {"id": task.id, "decision": close.group(1), "note": close.group(2), "cost_usd": conv.cost, **closed}
    why = keep.group(1) if keep else "the judge gave no decision line"
    raw = dict(task.raw)
    raw["judged"] = {"decision": "keep", "note": why, "by": f"opsquire judge ({name})", "when": iso(now())}
    write_json(task.folder / "meta.json", raw)
    return {"id": task.id, "decision": "keep", "note": why, "cost_usd": conv.cost, "text": res.text}
