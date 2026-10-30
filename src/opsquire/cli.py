from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import shutil
import signal
import sys
from pathlib import Path
from types import FrameType
from typing import Any

from . import locks
from .ask import ask
from .lint import lint_task, save_lint
from .model import (
    DATA_DIR,
    GENERAL_ID_RE,
    TASK_ID_RE,
    TEMPLATES_DIR,
    Meta,
    OpsquireError,
    config_path,
    home_dir,
    iso,
    now,
    resolve_task,
    write_json,
    write_text,
)
from .plan import run_plan
from .query import all_runs, find_run, find_tasks, list_tasks, runs_for, summarize, wait_for_run
from .runner import EXIT_CODES, load_run, run_task, tty_prompt


def emit(data: Any, as_json: bool, table: list[list[str]] | None = None) -> None:
    if as_json or table is None:
        print(json.dumps(data, indent=2))
        return
    if not table:
        print("(none)")
        return
    widths = [max(len(row[i]) for row in table) for i in range(len(table[0]))]
    for row in table:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip())


def cmd_new(a: argparse.Namespace) -> int:
    t = now()
    name = f"{a.area}_{a.name}" if a.general else f"{t:%Y%m%d-%H%M}_{a.area}_{a.name}"
    if not (GENERAL_ID_RE if a.general else TASK_ID_RE).match(name):
        raise OpsquireError(f"{name!r}: area and name must be lowercase kebab-case")
    folder = home_dir() / "general" / name if a.general else home_dir() / "tasks" / f"{t:%Y-%m}" / name
    if folder.exists():
        raise OpsquireError(f"{folder} already exists")
    if a.changes_prod and not a.rollback and not a.plan:
        raise OpsquireError("--rollback is required with --changes-prod")
    created_parents = [p for p in reversed(folder.parents) if not p.exists()]
    staging = folder.with_name(f".{name}.{os.getpid()}.tmp")
    try:
        folder.parent.mkdir(parents=True, exist_ok=True)
        staging.mkdir()
        build_task(a, name, t, staging)
        staging.rename(folder)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        for parent in reversed(created_parents):
            with contextlib.suppress(OSError):
                parent.rmdir()
        raise
    print(folder)
    return 0


def build_task(a: argparse.Namespace, name: str, t: dt.datetime, folder: Path) -> None:
    if a.plan:
        build_plan(a, name, t, folder)
        return
    entry = f"main.{a.lang}"
    shutil.copy(TEMPLATES_DIR / entry, folder / entry)
    meta = Meta(
        id=name,
        kind="general" if a.general else "task",
        created=iso(t),
        author=a.author or os.environ.get("OPSQUIRE_AUTHOR", "unknown"),
        purpose=a.purpose,
        area=a.area,
        entry=entry,
        changes_prod=a.changes_prod,
        targets=a.target or [a.area],
        lock=a.lock or ("exclusive" if a.changes_prod else "shared"),
        lock_reason=a.lock_reason or "",
        credentials=dict(c.split("=", 1) for c in a.cred or []),
        uses=a.uses or [],
        rollback=a.rollback or "read-only",
        links=a.link or [],
        interpreter=a.interpreter,
    )
    write_json(folder / "meta.json", meta.to_json())


def build_plan(a: argparse.Namespace, name: str, t: dt.datetime, folder: Path) -> None:
    if not a.step:
        raise OpsquireError("a plan needs at least one --step <task-id>, in order")
    steps = [resolve_task(s) for s in a.step]
    for step in steps:
        if step.meta is None or step.kind != "task":
            raise OpsquireError(f"step {step.id} is not a valid task")
    metas = [s.meta for s in steps if s.meta]
    changes = any(m.changes_prod for m in metas)
    meta = Meta(
        id=name,
        kind="plan",
        created=iso(t),
        author=a.author or os.environ.get("OPSQUIRE_AUTHOR", "unknown"),
        purpose=a.purpose,
        area=a.area,
        entry="",
        changes_prod=changes,
        targets=sorted({t for m in metas for t in m.targets}),
        lock="exclusive" if changes else "shared",
        lock_reason="",
        credentials={},
        uses=[],
        rollback=a.rollback or ("each step's own rollback, see steps" if changes else "read-only"),
        links=a.link or [],
        steps=[{"task": s.id} for s in steps],
    )
    write_json(folder / "meta.json", meta.to_json())


def cmd_lint(a: argparse.Namespace) -> int:
    task = resolve_task(a.task)
    result = lint_task(task)
    save_lint(task, result)
    if a.json:
        emit({"id": task.id, "ok": result.ok, "content_hash": result.content_hash, "output": result.output}, True)
    else:
        print(result.output)
        print(f"\n{task.id}: {'LINT OK' if result.ok else 'LINT FAILED'}")
    return 0 if result.ok else 1


def cmd_run(a: argparse.Namespace) -> int:
    task = resolve_task(a.task)
    if task.kind == "plan":
        if a.script_args:
            raise OpsquireError("plans take no arguments after --; put them on the steps in meta.json")
        return run_plan(task, a.apply, a.break_lock)
    return run_task(task, a.apply, a.script_args, a.break_lock)


def cmd_ls(a: argparse.Namespace) -> int:
    rows = [
        t
        for t in list_tasks()
        if (not a.area or t["area"] == a.area)
        and (not a.status or t["status"] == a.status)
        and (not a.kind or t["kind"] == a.kind)
    ]
    table = [["STATUS", "ID", "LAST RUN", "PURPOSE"]] + [
        [
            t["status"],
            t["id"],
            f"{t['last_run']['status']} {t['last_run']['mode']} {t['last_run']['started']}" if t["last_run"] else "-",
            str(t["purpose"] or "")[:70],
        ]
        for t in rows
    ]
    emit(rows, a.json, table if rows else [])
    return 0


def cmd_show(a: argparse.Namespace) -> int:
    task = resolve_task(a.task)
    data = summarize(task) | {"meta": task.raw, "run_history": runs_for(task.id)}
    if a.json:
        emit(data, True)
        return 0
    print(json.dumps(task.raw, indent=2))
    if task.errors:
        print("\nmeta errors:\n  " + "\n  ".join(task.errors))
    print(f"\nstatus: {data['status']}   folder: {task.folder}")
    emit(None, False, run_table(data["run_history"]))
    return 0


def run_table(recs: list[dict[str, Any]]) -> list[list[str]]:
    if not recs:
        return []
    return [["STATUS", "MODE", "STARTED", "TASK", "TARGETS", "LAST ERROR"]] + [
        [
            str(r.get("status")),
            str(r.get("mode")),
            str(r.get("started")),
            str(r.get("task_id")),
            " ".join(f"{k}={v['status']}" for k, v in (r.get("targets") or {}).items()),
            str(r.get("last_error") or "")[:60],
        ]
        for r in recs
    ]


def cmd_runs(a: argparse.Namespace) -> int:
    recs = runs_for(resolve_task(a.task).id) if a.task else all_runs()
    recs = [
        r
        for r in recs
        if (not a.status or r.get("status") == a.status)
        and (not a.area or r.get("area") == a.area)
        and (not a.since or str(r.get("started")) >= a.since)
    ][-a.limit :]
    emit(recs, a.json, run_table(recs))
    return 0


def cmd_last(a: argparse.Namespace) -> int:
    recs = all_runs()
    if not recs:
        raise OpsquireError("no runs yet")
    rec = recs[-1]
    if a.json:
        emit(rec, True)
        return 0
    emit(None, False, run_table([rec]))
    print(f"\nrun dir: {rec['paths']['dir']}")
    return 0


def cmd_log(a: argparse.Namespace) -> int:
    path = find_run(a.run)
    if path is None:
        task_runs = runs_for(resolve_task(a.run).id)
        if not task_runs:
            raise OpsquireError(f"no runs for {a.run!r}")
        path = home_dir() / task_runs[-1]["paths"]["dir"]
    target = path / ("events.jsonl" if a.events else "output.log")
    if a.events and a.json:
        print(json.dumps([json.loads(line) for line in target.read_text().splitlines()], indent=2))
    else:
        sys.stdout.write(target.read_text())
    if not a.events:
        rec = load_run(path / "run.json")
        print(f"\n--- status={rec['status']} exit={rec['exit_code']} run={path}")
    return 0


def cmd_find(a: argparse.Namespace) -> int:
    hits = find_tasks(" ".join(a.words), a.limit)
    table = [["SCORE", "STATUS", "ID", "PURPOSE"]] + [
        [f"{h['score']:.1f}", h["status"], h["id"], str(h["purpose"] or "")[:80]] for h in hits
    ]
    emit(hits, a.json, table if hits else [])
    return 0


def cmd_ask(a: argparse.Namespace) -> int:
    task_id = resolve_task(a.task).id if a.task else None
    if not a.request and not a.resume:
        raise OpsquireError('say what you want, e.g. opsquire ask "restart the bots on host x"')
    res = ask(" ".join(a.request) or "Continue where we stopped.", a.agent, task_id, a.new, a.resume, not a.no_grill)
    if res.interrupted:
        print(f"opsquire: interrupted; continue with: opsquire ask --resume {res.ask_id}", file=sys.stderr)
        return EXIT_CODES["interrupted"]
    if res.waiting:
        print(f"opsquire: waiting on: {res.waiting}", file=sys.stderr)
        print(f'answer with: opsquire ask --resume {res.ask_id} "<your answer>"', file=sys.stderr)
        return 0
    if res.exit_code and not res.text.strip():
        print(
            f"opsquire: agent failed (exit {res.exit_code}) without an answer; see its messages above", file=sys.stderr
        )
        return 1
    print(res.text.strip())
    if res.actions:
        print()
        for kind, text in res.actions:
            print(f"{kind}: {text}")
    if res.session:
        print(f'\n(session {res.session}; follow up with: opsquire ask --task <id> "...")', file=sys.stderr)
    return 0 if res.exit_code == 0 else 1


def cmd_wait(a: argparse.Namespace) -> int:
    task = resolve_task(a.task)

    def progress(msg: str) -> None:
        print(f"opsquire: {msg}", file=sys.stderr, flush=True)

    rec = wait_for_run(task.id, a.since, a.timeout, progress)
    if rec is None:
        print(f"opsquire: no finished run of {task.id} within {a.timeout:g}s", file=sys.stderr)
        return 124
    if a.json:
        emit(rec, True)
    else:
        emit(None, False, run_table([rec]))
        print(f"\nrun dir: {rec['paths']['dir']}")
    return EXIT_CODES.get(str(rec.get("status")), 1)


def cmd_home(a: argparse.Namespace) -> int:
    if a.path:
        target = Path(a.path).expanduser().resolve()
        config_path().parent.mkdir(parents=True, exist_ok=True)
        write_json(config_path(), {"home": str(target)})
        print(f"home set to {target} (in {config_path()})")
        return 0
    source = "OPSQUIRE_HOME" if os.environ.get("OPSQUIRE_HOME") else str(config_path())
    print(f"{home_dir()}  (from {source if source == 'OPSQUIRE_HOME' or config_path().is_file() else 'default'})")
    return 0


def contract_text() -> str:
    text = (DATA_DIR / "AGENTS.md").read_text()
    local = home_dir() / "LOCAL.md"
    if local.is_file():
        text += "\n## Local rules for this home\n\n" + local.read_text()
    return text


def cmd_contract(a: argparse.Namespace) -> int:
    if not a.write:
        sys.stdout.write(contract_text())
        return 0
    home = home_dir()
    write_text(home / "AGENTS.md", contract_text())
    write_text(home / "CLAUDE.md", "@AGENTS.md\n")
    print(f"wrote {home / 'AGENTS.md'} and {home / 'CLAUDE.md'}")
    return 0


def cmd_locks(a: argparse.Namespace) -> int:
    data = {t: [h.__dict__ | {"alive": h.alive()} for h in hs] for t, hs in locks.holders().items() if hs}
    table = [["TARGET", "MODE", "RUN", "TASK", "SINCE", "ALIVE"]] + [
        [t, h["mode"], h["run_id"], h["task_id"], h["since"], str(h["alive"])] for t, hs in data.items() for h in hs
    ]
    emit(data, a.json, table if data else [])
    return 0


def cmd_unlock(a: argparse.Namespace) -> int:
    answer = tty_prompt(f"Remove every lock on {a.target!r}? Type the target name to confirm: ")
    if answer != a.target:
        print("not removed")
        return 1
    removed = locks.remove(a.target)
    print(f"removed {len(removed)} lock(s): " + ", ".join(h.run_id for h in removed))
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="opsquire",
        description="Agents write the scripts, you run them, everything is recorded for the next agent.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("new", help="scaffold a task (or --general script)")
    n.add_argument("area")
    n.add_argument("name")
    n.add_argument("--purpose", required=True)
    n.add_argument("--lang", choices=["py", "sh"], default="py")
    n.add_argument("--general", action="store_true")
    n.add_argument("--plan", action="store_true", help="a plan: runs --step tasks in order with one command")
    n.add_argument("--step", action="append", metavar="TASK", help="plan step, in order (repeat)")
    n.add_argument("--changes-prod", action="store_true")
    n.add_argument("--target", action="append")
    n.add_argument("--lock", choices=["exclusive", "shared", "none"])
    n.add_argument("--lock-reason")
    n.add_argument("--cred", action="append", metavar="ENV=dotted.key")
    n.add_argument("--uses", action="append", metavar="GENERAL_ID")
    n.add_argument("--rollback")
    n.add_argument("--link", action="append")
    n.add_argument("--author")
    n.add_argument("--interpreter", help="python for this task (e.g. a project venv); default: opsquire's own")
    n.set_defaults(fn=cmd_new)

    for name, fn, helptext in (
        ("lint", cmd_lint, "ruff + mypy --strict + shellcheck + meta checks"),
        ("show", cmd_show, "task details and run history"),
    ):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("task")
        s.add_argument("--json", action="store_true")
        s.set_defaults(fn=fn)

    r = sub.add_parser("run", help="run a task (dry or read by default); script args go after --")
    r.add_argument("task")
    r.add_argument("--apply", action="store_true", help="really change things (prod-changing tasks only)")
    r.add_argument("--break-lock", action="store_true", help="offer to override a held lock (asks you to type BREAK)")
    r.set_defaults(fn=cmd_run)

    ls = sub.add_parser("ls", help="list tasks with derived status")
    ls.add_argument("--area")
    ls.add_argument("--status", choices=["draft", "ready", "done", "superseded", "abandoned", "archived"])
    ls.add_argument("--kind", choices=["task", "general", "plan"])
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(fn=cmd_ls)

    rs = sub.add_parser("runs", help="list runs, oldest first")
    rs.add_argument("task", nargs="?")
    rs.add_argument("--status", choices=["running", "ok", "partial", "failed", "aborted", "interrupted", "blocked"])
    rs.add_argument("--area")
    rs.add_argument("--since", help="UTC ISO prefix, e.g. 2026-10-30")
    rs.add_argument("--limit", type=int, default=50)
    rs.add_argument("--json", action="store_true")
    rs.set_defaults(fn=cmd_runs)

    last = sub.add_parser("last", help="most recent run")
    last.add_argument("--json", action="store_true")
    last.set_defaults(fn=cmd_last)

    lg = sub.add_parser("log", help="print a run's output.log (or --events)")
    lg.add_argument("run", help="run id, task id, or a unique part of either")
    lg.add_argument("--events", action="store_true")
    lg.add_argument("--json", action="store_true")
    lg.set_defaults(fn=cmd_log)

    fd = sub.add_parser("find", help="search tasks and general scripts by keywords (reuse before creating)")
    fd.add_argument("words", nargs="+")
    fd.add_argument("--limit", type=int, default=10)
    fd.add_argument("--json", action="store_true")
    fd.set_defaults(fn=cmd_find)

    ak = sub.add_parser("ask", help="ask an agent to find or write the task for a request; prints what to run")
    ak.add_argument("request", nargs="*")
    ak.add_argument("--task", help="follow up on a task (resumes its agent conversation when possible)")
    ak.add_argument("--resume", metavar="ASK_ID", help="continue an earlier (e.g. interrupted) ask")
    ak.add_argument("--agent", help="agent from retinue.json (default from retinue.json)")
    ak.add_argument("--new", action="store_true", help="start a fresh conversation even with --task")
    ak.add_argument("--no-grill", action="store_true", help="no interview: the agent takes defaults and records them")
    ak.set_defaults(fn=cmd_ask)

    wt = sub.add_parser("wait", help="wait for the next finished run of a task, then print it (for agents)")
    wt.add_argument("task")
    wt.add_argument("--since", help="UTC ISO time; default: newer than the latest run that exists now")
    wt.add_argument("--timeout", type=float, default=540, help="seconds (default 540); exit 124 on timeout")
    wt.add_argument("--json", action="store_true")
    wt.set_defaults(fn=cmd_wait)

    ct = sub.add_parser("contract", help="print the agent contract (packaged AGENTS.md + the home's LOCAL.md)")
    ct.add_argument("--write", action="store_true", help="write it to the home as AGENTS.md and CLAUDE.md")
    ct.set_defaults(fn=cmd_contract)

    hm = sub.add_parser("home", help="show the data home, or set it")
    hm.add_argument("path", nargs="?")
    hm.set_defaults(fn=cmd_home)

    lk = sub.add_parser("locks", help="show held locks")
    lk.add_argument("--json", action="store_true")
    lk.set_defaults(fn=cmd_locks)

    ul = sub.add_parser("unlock", help="remove all locks on a target (asks you to type it)")
    ul.add_argument("target")
    ul.set_defaults(fn=cmd_unlock)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    script_args = argv[argv.index("--") + 1 :] if "--" in argv else []
    a = parser().parse_args(argv[: argv.index("--")] if "--" in argv else argv)
    a.script_args = script_args

    def to_interrupt(_signum: int, _frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, to_interrupt)
    try:
        code: int = a.fn(a)
        return code
    except OpsquireError as e:
        print(f"opsquire: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nopsquire: interrupted", file=sys.stderr)
        return EXIT_CODES["interrupted"]
    except BrokenPipeError:
        return 0
