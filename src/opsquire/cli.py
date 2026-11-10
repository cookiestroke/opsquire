from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import re
import shutil
import signal
import sys
from pathlib import Path
from types import FrameType
from typing import Any

from . import __version__, handover, locks, piece, repos, runner
from .ageing import EXPIRE_DAYS, expire_unrun, reminder, told
from .ask import Budget, ask, configured_budget
from .bootstrap import init
from .close import auto_close, close_task, reopen_task, supersede_task
from .docs import write_docs
from .homegit import commit_quietly
from .integrate import COMMAND_PLACEHOLDER, integrate, own_command
from .judge import judge
from .lint import lint_changed_scripts, lint_task, save_lint
from .milestones import KINDS, milestones, render, since_arg
from .model import (
    DATA_DIR,
    GENERAL_ID_RE,
    OFFBOARDED,
    OUTCOME_STATES,
    TASK_ID_RE,
    TEMPLATES_DIR,
    THREAD_RE,
    Meta,
    OpsquireError,
    code_version,
    config_path,
    decision,
    decision_text,
    home_dir,
    iso,
    load_task,
    now,
    resolve_task,
    update_config,
    write_json,
    write_text,
)
from .offboard import daily_sweep, days_text, keep_days, offboard_due, offboard_named, restore, sweep_is_due_here
from .plan import plan_meta, run_plan
from .query import (
    SCRIPT_STATUSES,
    TASK_STATUSES,
    all_runs,
    find_run,
    find_tasks,
    list_tasks,
    runs_for,
    summarize,
    wait_for_run,
)
from .runner import EXIT_CODES, load_run, run_task, tty_prompt
from .state import state, waiting_on
from .ui import DEFAULT_PORT, serve, url

WAIT_DECIDED = 4


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


def kebab(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def report_shared_targets(targets: list[str], exclude: str = "") -> list[dict[str, Any]]:
    """Tell the agent what else waits for these targets; it is the agent's call whether the work is related."""
    waiting = waiting_on(targets, exclude)
    for it in waiting:
        why = f" ({it['why']})" if it["why"] else ""
        on = ", ".join(it["targets"])
        print(f"opsquire: also waiting on {on}: {it['task_id']}, {it['next']} run{why}", file=sys.stderr)
    if waiting:
        print(
            "opsquire: related runs on one machine go in one plan or task; withdraw what it replaces with "
            "`opsquire handover <task> --withdraw`. Unrelated work stays separate: say so at handover.",
            file=sys.stderr,
        )
    return waiting


def cmd_new(a: argparse.Namespace) -> int:
    t = now()
    name = f"{a.area}_{a.name}" if a.general else f"{t:%Y%m%d-%H%M}_{a.area}_{a.name}"
    if not (GENERAL_ID_RE if a.general else TASK_ID_RE).match(name):
        parts = (("area", a.area), ("name", a.name))
        wrong = [f"{label} {value!r}, use {kebab(value)!r}" for label, value in parts if kebab(value) != value]
        hint = " A host name with underscores keeps its spelling in --target." if "_" in a.area else ""
        raise OpsquireError(
            f"nothing created: area and name must be lowercase kebab-case: {'; '.join(wrong) or name}.{hint}"
        )
    folder = home_dir() / "general" / name if a.general else home_dir() / "tasks" / f"{t:%Y-%m}" / name
    if folder.exists():
        raise OpsquireError(f"{folder} already exists")
    if a.general and (home_dir() / OFFBOARDED / "general" / name).exists():
        raise OpsquireError(f"{name} exists and is offboarded: `opsquire restore {name}` brings it back")
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
    if not a.json:
        print(folder)
    waiting = report_shared_targets(a.target or [a.area], name)
    tell_author()
    if a.json:
        emit({"id": name, "path": str(folder), "waiting_on_same_target": waiting}, True)
    return 0


def build_task(a: argparse.Namespace, name: str, t: dt.datetime, folder: Path) -> None:
    if a.thread and not THREAD_RE.match(a.thread):
        raise OpsquireError("--thread must be a lowercase kebab-case name of at most 40 characters")
    if a.plan:
        build_plan(a, name, t, folder)
        return
    entry = f"main.{a.lang}"
    shutil.copy(TEMPLATES_DIR / entry, folder / entry)
    verify = None
    if a.changes_prod and not a.general and not a.no_verify:
        verify = f"verify.{a.lang}"
        shutil.copy(TEMPLATES_DIR / verify, folder / verify)
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
        thread=a.thread,
        verify=verify,
    )
    write_json(folder / "meta.json", meta.to_json())


def build_plan(a: argparse.Namespace, name: str, t: dt.datetime, folder: Path) -> None:
    if not a.step:
        raise OpsquireError("a plan needs at least one --step <task-id>, in order")
    steps = [{"task": resolve_task(s).id} for s in a.step]
    author = a.author or os.environ.get("OPSQUIRE_AUTHOR", "unknown")
    meta = plan_meta(name, t, a.area, a.purpose, author, steps, a.rollback, a.link or [], a.thread)
    write_json(folder / "meta.json", meta.to_json())


def cmd_lint(a: argparse.Namespace) -> int:
    task = resolve_task(a.task)
    result = lint_task(task)
    save_lint(task, result)
    scripts = lint_changed_scripts(task)
    failing = sorted(s for s, r in scripts.items() if not r.ok)
    if a.json:
        data = {"id": task.id, "ok": result.ok, "content_hash": result.content_hash, "output": result.output}
        emit(data | {"scripts": {s: r.ok for s, r in scripts.items()}}, True)
    else:
        print(result.output)
        for script in failing:
            print(f"\n== general script {script}, on its own\n{scripts[script].output}")
        tests = f" ({result.tests} tests)" if result.tests else ""
        print(f"\n{task.id}: {'LINT OK' if result.ok else 'LINT FAILED'}{tests}")
        for script, checked in scripts.items():
            print(f"{script}: {'LINT OK' if checked.ok else 'LINT FAILED'} (general script, changed since its lint)")
    if failing:
        return 1
    if result.ok:
        uses = [home_dir() / "general" / u for u in (task.meta.uses if task.meta else [])]
        note = commit_quietly(home_dir(), [task.folder, *uses], f"ready: {task.id}")
        if note:
            print(note, file=sys.stderr)
    return 0 if result.ok else 1


def cmd_supersede(a: argparse.Namespace) -> int:
    done = supersede_task(a.task, a.by, a.note or "")
    if a.json:
        emit(done, True)
    else:
        print(f"{done['id']}: superseded by {done['superseded_by']}; out of the inbox and refused by run")
    return 0


def cmd_reopen(a: argparse.Namespace) -> int:
    done = reopen_task(a.task, a.note)
    if a.json:
        emit(done, True)
    else:
        print(f"{done['id']}: open again")
    return 0


def tell_author() -> None:
    """The agent that runs new or handover hears what became of its earlier tasks: decided by someone else, or
    waiting too long. Never the reason the command fails."""
    try:
        lines = told(handover.author())
    except (OpsquireError, OSError, ValueError):
        return
    for line in lines:
        print(f"opsquire: {line}", file=sys.stderr)


def cmd_close(a: argparse.Namespace) -> int:
    """Record how a task really ended: done, partial or abandoned, with a note (what was finished by hand, which
    run landed). Only annotates meta.json; touches no host. The catalog and `ls` show the state from then on."""
    closed = close_task(a.task, a.outcome, a.note, a.run)
    if a.json:
        emit({"id": closed["id"], "outcome": a.outcome, "notes": closed["notes"]}, True)
        return 0
    print(f"{closed['id']}: closed {a.outcome}")
    for note in closed["notes"]:
        print(note, file=sys.stderr)
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    if a.verbose:
        runner.VERBOSE = True
    task = resolve_task(a.task)
    skip_dry = (a.skip_dry or "").strip()
    if a.skip_dry is not None and not (a.apply and skip_dry):
        raise OpsquireError('--skip-dry goes with --apply and needs the reason: --skip-dry "why no dry run"')
    versioned = [home_dir() / sub for sub in ("tasks", "general", OFFBOARDED)]
    note = commit_quietly(home_dir(), versioned, f"before running {task.id}")
    if note:
        print(f"opsquire: {note}", file=sys.stderr)
    since = iso(now())
    if task.kind == "plan":
        if a.script_args:
            raise OpsquireError("plans take no arguments after --; put them on the steps in meta.json")
        if a.verify:
            raise OpsquireError("plans have no verify check; verify their steps")
        code = run_plan(task, a.apply, a.break_lock, skip_dry)
    else:
        code = run_task(task, a.apply, a.script_args, a.break_lock, a.verify, skip_dry)
    if a.apply and code == 0 and task.kind != "plan" and task.meta and task.meta.verify:
        # A staged task (build, then deploy) ends ok after every stage; only its verify check knows the end state.
        print(f"opsquire: apply ok; running the verify check of {task.id} before closing it")
        if run_task(task, False, [], False, True) != 0:
            print("opsquire: the verify check did not finish ok; the task stays open")
    if (a.apply or a.verify) and code == 0:
        steps = [str(s["task"]) for s in (task.meta.steps if task.meta else None) or []]
        closed = [t for t in [task.id, *steps] if auto_close(t, since)]
        if closed:
            print(f"closed done: {', '.join(closed)} (auto, from the run record; `opsquire close` can amend it)")
    return code


def cmd_handover(a: argparse.Namespace) -> int:
    task = resolve_task(a.task)
    if a.withdraw:
        withdrawn = handover.withdraw(task)
        if a.json:
            emit({"id": task.id, "withdrawn": withdrawn}, True)
        else:
            print(f"{task.id}: handover withdrawn" if withdrawn else f"{task.id}: no handover to withdraw")
        return 0
    mode = handover.mode_for(task, a.apply, a.verify)
    same = handover.same_apply(task, runs_for(task.id), a.script_args) if mode == "apply" else None
    if same and not (a.again and (a.why or "").strip()):
        raise OpsquireError(
            f"not handed over: {handover.applied_text(same)}. Read that record first; if production really needs "
            f'it again (after a rollback, say), pass --again --why "the reason"'
        )
    uncovered = handover.needs_dry(task, runs_for(task.id), a.script_args) if mode == "apply" else None
    if uncovered:
        raise OpsquireError(f"not handed over: {uncovered}")
    entry = handover.record(task, mode, a.script_args, a.why or "", handover.author(), again=bool(same))
    cmd = handover.command(task.id, mode, a.script_args)
    targets = task.meta.targets if task.meta else []
    if a.json:
        waiting = report_shared_targets(targets, task.id)
        emit({"id": task.id, "command": cmd, "handover": entry, "waiting_on_same_target": waiting}, True)
    else:
        print(f"handed over: {cmd}\n  in the inbox until it runs" + (f"; why: {entry['why']}" if entry["why"] else ""))
        report_shared_targets(targets, task.id)
    tell_author()
    return 0


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
    task = resolve_task(a.task, offboarded=True)
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
    return [["STATUS", "MODE", "ORIGIN", "STARTED", "SCRIPT", "TASK", "TARGETS", "LAST ERROR"]] + [
        [
            "waiting" if question(r) else str(r.get("status")),
            str(r.get("mode")),
            str(r.get("origin") or "-"),
            str(r.get("started")),
            code_version(r.get("files") or {})[:7] or "-",
            str(r.get("task_id")),
            " ".join(f"{k}={v['status']}" for k, v in (r.get("targets") or {}).items()),
            f"asks the operator: {question(r)}"[:80] if question(r) else str(r.get("last_error") or "")[:60],
        ]
        for r in recs
    ]


def question(rec: dict[str, Any]) -> str:
    """What a running run is waiting for the operator to answer, if anything."""
    return str((rec.get("waiting") or {}).get("question") or "") if rec.get("status") == "running" else ""


def cmd_runs(a: argparse.Namespace) -> int:
    recs = runs_for(resolve_task(a.task, offboarded=True).id) if a.task else all_runs()
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
        task_runs = runs_for(resolve_task(a.run, offboarded=True).id)
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
        print(f"\n--- status={rec['status']} exit={rec.get('exit_code')} origin={rec.get('origin') or '-'} run={path}")
        if question(rec):
            print(f"--- waiting for the operator to answer: {question(rec)}")
    return 0


def cmd_find(a: argparse.Namespace) -> int:
    hits = find_tasks(" ".join(a.words), a.limit)
    table = [["SCORE", "STATUS", "ID", "PURPOSE"]] + [
        [
            f"{h['score']:.1f}",
            h["status"] + (", offboarded" if h["offboarded"] else ""),
            h["id"],
            str(h["purpose"] or "")[:80],
        ]
        for h in hits
    ]
    emit(hits, a.json, table if hits else [])
    return 0


def cmd_offboard(a: argparse.Namespace) -> int:
    """Move settled tasks out of tasks/ into offboarded/: the named ones now, or with --due every one that sat idle
    for the retention period (what the daily sweep does). Nothing is deleted; `opsquire restore` brings one back."""
    if bool(a.task) == a.due:
        raise OpsquireError("name the tasks to offboard now, or pass --due for every settled task past the retention")
    if a.days is not None and not a.due:
        raise OpsquireError("--days goes with --due; a named task is offboarded whatever its age")
    days = keep_days() if a.days is None else a.days
    if a.due and a.days is None and days <= 0:
        raise OpsquireError("automatic offboarding is off for this home (retention 0); pass --days N")
    if days < 0:
        raise OpsquireError("--days cannot be negative")
    moved = offboard_due(days=days, dry=a.dry) if a.due else offboard_named(a.task, a.dry)
    if a.json:
        emit({"offboarded": [] if a.dry else moved, "would_offboard": moved if a.dry else [], "days": days}, True)
        return 0
    verb = "would offboard" if a.dry else "offboarded"
    age = f" idle {days_text(days)}+ days" if a.due else ""
    print(f"{verb} {len(moved)}{age}" + (":" if moved else ""))
    for tid in moved:
        print(f"  {tid}")
    return 0


def cmd_restore(a: argparse.Namespace) -> int:
    restored = restore(a.task)
    if a.json:
        emit({"restored": restored}, True)
        return 0
    print("restored: " + ", ".join(restored))
    return 0


def sweep_after_command() -> None:
    """Once a day `new`, `lint` and `run` also expire tasks that never ran, offboard settled tasks past the retention
    and count the inbox items that wait for a decision. Never the reason a command fails: a home it cannot write keeps
    its tasks until the next command that can."""
    at = now()
    due = sweep_is_due_here(at)
    if due:
        try:
            expired = expire_unrun(at)
        except (OpsquireError, OSError) as e:
            expired = []
            print(f"opsquire: unrun tasks not expired this time: {str(e).splitlines()[0]}", file=sys.stderr)
        if expired:
            print(
                f"opsquire: abandoned {len(expired)} tasks that never ran in {EXPIRE_DAYS:g} days: "
                f'{", ".join(expired)} (`opsquire reopen <task> --note "why"` undoes one)',
                file=sys.stderr,
            )
    try:
        moved = daily_sweep(at)
    except (OpsquireError, OSError) as e:
        print(f"opsquire: settled tasks not offboarded this time: {str(e).splitlines()[0]}", file=sys.stderr)
        return
    if due:
        with contextlib.suppress(OpsquireError, OSError, ValueError):
            note = reminder(state(at))
            if note:
                print(f"opsquire: {note}", file=sys.stderr)
    if moved:
        print(
            f"opsquire: offboarded {len(moved)} settled tasks idle {days_text(keep_days())}+ days "
            "(`opsquire find` still finds them, `opsquire restore <task>` brings one back)",
            file=sys.stderr,
        )


def cmd_ask(a: argparse.Namespace) -> int:
    task_id = resolve_task(a.task).id if a.task else None
    if not a.request and not a.resume:
        raise OpsquireError('say what you want, e.g. opsquire ask "restart the bots on host x"')
    if a.max_agents is not None and a.max_agents < 1:
        raise OpsquireError("--max-agents must be at least 1")
    res = ask(
        " ".join(a.request) or "Continue where we stopped.",
        a.agent,
        task_id,
        a.new,
        a.resume,
        not a.no_grill,
        split=not a.no_split,
        max_agents=a.max_agents,
        budget=Budget(a.budget if a.budget is not None else configured_budget(), a.strict_budget),
        reply_given=bool(a.request),
    )
    if res.pieces:
        print(res.text.strip())
        if res.interrupted or res.waiting:
            print(f"\nopsquire: continue with: opsquire ask --resume {res.ask_id}", file=sys.stderr)
        if res.cost_usd:
            print(f"opsquire: ${res.cost_usd:.2f} spent", file=sys.stderr)
        return EXIT_CODES["interrupted"] if res.interrupted else res.exit_code
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
    if res.session:
        hint = f'\nfollow up: opsquire ask --resume {res.ask_id} "..."'
        for tid in res.task_ids:
            hint += f'\n       or: opsquire ask --task {tid} "..."'
        print(hint, file=sys.stderr)
    return 0 if res.exit_code == 0 else 1


def cmd_wait(a: argparse.Namespace) -> int:
    task = resolve_task(a.task)

    def progress(msg: str) -> None:
        print(f"opsquire: {msg}", file=sys.stderr, flush=True)

    def decided() -> str | None:
        d = decision(load_task(task.folder).raw) if task.folder.is_dir() else None
        return decision_text(task.id, d) if d else None

    rec = wait_for_run(task.id, a.since, a.timeout, progress, decided=decided)
    if rec is None:
        print(f"opsquire: no finished run of {task.id} within {a.timeout:g}s", file=sys.stderr)
        return 124
    if "decided" in rec:
        if a.json:
            emit({"id": task.id, "decided": rec["decided"]}, True)
        print(f"opsquire: no run is coming: {rec['decided']}", file=sys.stderr)
        return WAIT_DECIDED
    if a.json:
        emit(rec, True)
    else:
        emit(None, False, run_table([rec]))
        print(f"\nrun dir: {rec['paths']['dir']}")
    if rec.get("mode") == "apply" and rec.get("status") != "ok":
        print(
            f"opsquire: {task.id} is not closed: decide what happens next (fix and hand over again, or "
            f"`opsquire close {task.id} --outcome partial|abandoned --note ...`)",
            file=sys.stderr,
        )
    return EXIT_CODES.get(str(rec.get("status")), 1)


def cmd_init(a: argparse.Namespace) -> int:
    if a.offboard_days is not None and a.offboard_days < 0:
        raise OpsquireError("--offboard-days must be 0 (never automatically) or more")
    for line in init(a.path, a.budget, a.dry_run, a.offboard_days):
        print(line)
    return 0


def cmd_home(a: argparse.Namespace) -> int:
    if a.path:
        target = Path(a.path).expanduser().resolve()
        update_config(home=str(target))
        print(f"home set to {target} (in {config_path()})")
        return 0
    source = "OPSQUIRE_HOME" if os.environ.get("OPSQUIRE_HOME") else str(config_path())
    print(f"{home_dir()}  (from {source if source == 'OPSQUIRE_HOME' or config_path().is_file() else 'default'})")
    return 0


def contract_text() -> str:
    text = (DATA_DIR / "AGENTS.md").read_text().replace(COMMAND_PLACEHOLDER, own_command())
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


def cmd_integrate(a: argparse.Namespace) -> int:
    for line in integrate(a.dry_run):
        print(line)
    return 0


def cmd_repo(a: argparse.Namespace) -> int:
    if a.action == "list":
        found = repos.registry()
        local = {r.name: repos.freshness(r) for r in found.values()}
        emit(
            [
                {"name": r.name, "path": str(r.path), "github": r.github, "base": r.base, "local": local[r.name]}
                for r in found.values()
            ],
            a.json,
            [["NAME", "GITHUB", "BASE", "PATH", "LOCAL CHECKOUT"]]
            + [[r.name, r.github, r.base, str(r.path), local[r.name]] for r in found.values()],
        )
        return 0
    if a.action == "checkout":
        if not a.target:
            raise OpsquireError("say which repo: opsquire repo checkout <name>")
        copy = repos.checkout(a.target, a.base)
        print(copy)
        stale = repos.stale_copy_note(copy)
        if stale:
            print(f"opsquire: WARNING: {stale}", file=sys.stderr)
        return 0
    if a.action == "overlap":
        if not a.target:
            raise OpsquireError("say which copy: opsquire repo overlap <copy-path>")
        overlaps = repos.overlapping_prs(Path(a.target).expanduser().resolve())
        emit(overlaps, a.json, overlap_table(overlaps))
        return 0
    if not a.target or not a.title:
        raise OpsquireError('opsquire repo pr <copy-path> --title "..." --body-file <file>')
    body = Path(a.body_file).read_text() if a.body_file else ""
    result = repos.open_pr(Path(a.target).expanduser().resolve(), a.title, body, a.branch)
    if result["auto_deploy_base"]:
        print(f"opsquire: WARNING: {result['base']} auto-deploys; merging this PR deploys it", file=sys.stderr)
    for other in repos.overlapping_prs(Path(a.target).expanduser().resolve()):
        files = ", ".join(other["files"][:5])
        print(f"opsquire: WARNING: {other['url']} ({other['author']}) also changes {files}", file=sys.stderr)
    if a.json:
        emit(result, True)
    else:
        print(f"PR: {result['url']}" if result["url"] else f"Pushed to {result['branch']} (no open PR)")
        print(
            f"   {result['repo']} {result['branch']} -> {result['base']}, commit {result['commit'][:10]}, "
            f"{len(result['changed'])} changed, {len(result['deleted'])} deleted"
        )
    return 0


def overlap_table(overlaps: list[dict[str, Any]]) -> list[list[str]]:
    if not overlaps:
        return []
    return [["PR", "AUTHOR", "BRANCH", "SHARED FILES"]] + [
        [str(o["url"]), str(o["author"]), str(o["branch"]), ", ".join(o["files"])] for o in overlaps
    ]


def cmd_piece(a: argparse.Namespace) -> int:
    """Fan-out for chat agents: open a piece (worktree + env for its subagent), land it, drop it, list them."""
    if a.action == "open":
        if not a.target:
            raise OpsquireError('opsquire piece open <name> [--group <g>] [--request "..."]')
        info = piece.open_piece(a.target, a.group, a.request)
        if a.json:
            emit({**info.to_json(), "brief": piece.brief(info)}, True)
        else:
            print(info.worktree)
            print(f"branch: {info.branch}")
            print("\nBrief for the subagent:\n" + piece.brief(info))
        return 0
    if a.action == "land":
        if not a.target:
            raise OpsquireError("opsquire piece land <name|group/name|path>")
        landing = piece.land(a.target)
        if a.json:
            emit(landing.to_json(), True)
        elif landing.merged:
            extra = "" if landing.tasks else "; no task or general script changed"
            print(f"piece {landing.piece}: landed in the home{extra}")
            for tid, state in landing.tasks.items():
                print(f"  {tid}: {state}")
        else:
            print(f"piece {landing.piece}: NOT landed, conflicts in: " + ", ".join(landing.conflicts))
            print("  resolve the markers in the worktree keeping both intents, lint, then `opsquire piece land` again")
        failed = any(not s.startswith(("ready", "deleted")) for s in landing.tasks.values())
        return 0 if landing.merged and not failed else 1
    if a.action == "drop":
        if not a.target:
            raise OpsquireError("opsquire piece drop <name|group/name|path> [--delete]")
        print(piece.drop(a.target, a.delete))
        return 0
    home = piece.home_repo()
    infos = piece.open_pieces(home)
    emit([p.to_json() for p in infos], a.json, piece.describe(infos))
    return 0


def cmd_milestones(a: argparse.Namespace) -> int:
    at = now()
    kinds = tuple(k.strip() for k in a.kind.split(",")) if a.kind else KINDS
    unknown = [k for k in kinds if k not in KINDS]
    if unknown:
        raise OpsquireError(f"--kind: unknown {', '.join(unknown)}; use {', '.join(KINDS)}")
    items = milestones(since_arg(a.since, at), a.until, a.thread, kinds, at)
    if a.json:
        emit(items, True)
    else:
        print(render(items, at))
    return 0


def cmd_threads(a: argparse.Namespace) -> int:
    """The home as threads (the stories tasks belong to), with each task's status and suggestions."""
    s = state()
    if a.json:
        emit(s, True)
        return 0
    for th in s["threads"]:
        flag = "" if th["explicit"] else " (inferred)"
        left = f"  (+{th['offboarded']} offboarded)" if th["offboarded"] else ""
        print(f"{th['state']:<5} {th['title']}{flag}  [{th['updated'] or '-'}]{left}")
        for tid in th["tasks"]:
            t = s["tasks"][tid]
            print(f"      {t['status']:<10} {tid}")
            for sg in t["suggestions"]:
                print(f"                 ! {sg['kind']}: {sg['text']}")
    return 0


def cmd_judge(a: argparse.Namespace) -> int:
    """An agent reads the task's record and closes it (done, partial, abandoned) or says what is missing."""
    out = judge(resolve_task(a.task).id, a.agent)
    if a.json:
        emit(out, True)
        return 0
    if out["decision"] == "keep":
        print(f"{out['id']}: kept open: {out['note']}")
        return 1
    if out["decision"] == "verify":
        print(f"{out['id']}: needs the verify check: {out['note']}")
        return 1
    print(f"{out['id']}: closed {out['decision']}: {out['note']}")
    for note in out.get("notes") or []:
        print(note, file=sys.stderr)
    return 0


def cmd_ui(a: argparse.Namespace) -> int:
    """Serve the home's picture (threads, tasks, runs, scripts, locks; closing is its one write) on localhost."""

    def ready() -> None:
        print(f"opsquire ui: {url(a.port)}  (home {home_dir()}; Ctrl-C stops)", flush=True)
        if a.open:
            import webbrowser

            webbrowser.open(url(a.port))

    try:
        serve(a.port, on_ready=ready)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_docs(a: argparse.Namespace) -> int:
    out = write_docs()
    print(f"wrote {out / 'CATALOG.md'} and one page per general script in {out}")
    return 0


def refresh_docs() -> None:
    with contextlib.suppress(OSError):
        write_docs()


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
    p.add_argument("--version", action="version", version=f"opsquire {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("new", help="scaffold a task (or --general script)")
    n.add_argument("area")
    n.add_argument("name")
    n.add_argument("--purpose", required=True)
    n.add_argument("--lang", choices=["py", "sh"], default="py")
    n.add_argument("--general", action="store_true")
    n.add_argument("--plan", action="store_true", help="a plan: runs --step tasks in order with one command")
    n.add_argument("--json", action="store_true")
    n.add_argument("--step", action="append", metavar="TASK", help="plan step, in order (repeat)")
    n.add_argument("--changes-prod", action="store_true")
    n.add_argument(
        "--no-verify", action="store_true", help="changing task without a verify check (say why in design.md)"
    )
    n.add_argument("--target", action="append")
    n.add_argument("--lock", choices=["exclusive", "shared", "none"])
    n.add_argument("--lock-reason")
    n.add_argument("--cred", action="append", metavar="ENV=dotted.key")
    n.add_argument("--uses", action="append", metavar="GENERAL_ID")
    n.add_argument("--rollback")
    n.add_argument("--link", action="append")
    n.add_argument("--thread", help="the thread (story) this task belongs to; else inferred from session and links")
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
    r.add_argument("--verify", action="store_true", help="run the task's verify check: is its intended state live?")
    r.add_argument("--break-lock", action="store_true", help="offer to override a held lock (asks you to type BREAK)")
    r.add_argument(
        "--skip-dry",
        metavar="WHY",
        help="with --apply: go ahead although no ok dry run covers these scripts and arguments; the reason is recorded",
    )
    r.add_argument("-v", "--verbose", action="store_true", help="also show the runner's bookkeeping and [data] events")
    r.set_defaults(fn=cmd_run)

    ho = sub.add_parser(
        "handover",
        help="recommend a run of an existing task: in the inbox with your reason until it runs (args after --)",
    )
    ho.add_argument("task")
    ho.add_argument("--apply", action="store_true", help="hand over the apply run (changing tasks)")
    ho.add_argument("--verify", action="store_true", help="hand over the verify check")
    ho.add_argument("--why", help="one line on why this run is needed now; shown in the inbox")
    ho.add_argument("--again", action="store_true", help="an apply that already ran the same way must run again")
    ho.add_argument("--withdraw", action="store_true", help="take the task's handover out of the inbox")
    ho.add_argument("--json", action="store_true")
    ho.set_defaults(fn=cmd_handover)

    sp = sub.add_parser("supersede", help="a newer task replaced this one: it leaves the inbox and will not run")
    sp.add_argument("task")
    sp.add_argument("--by", required=True, help="the task that replaced it")
    sp.add_argument("--note", help="what changed")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_supersede)

    ro = sub.add_parser("reopen", help="undo a close or a supersession (an expiry that was wrong, say)")
    ro.add_argument("task")
    ro.add_argument("--note", required=True, help="why it is open again")
    ro.add_argument("--json", action="store_true")
    ro.set_defaults(fn=cmd_reopen)

    cl = sub.add_parser("close", help="record how a task really ended (done, partial, abandoned) with a note")
    cl.add_argument("task")
    cl.add_argument("--outcome", required=True, choices=OUTCOME_STATES)
    cl.add_argument("--note", required=True, help="what actually happened, e.g. finished by hand, which run landed")
    cl.add_argument("--run", help="the run that landed it (id or unique part)")
    cl.add_argument("--json", action="store_true")
    cl.set_defaults(fn=cmd_close)

    ls = sub.add_parser("ls", help="list tasks with derived status")
    ls.add_argument("--area")
    ls.add_argument("--status", choices=[*TASK_STATUSES, *SCRIPT_STATUSES])
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

    ob = sub.add_parser(
        "offboard", help="move settled tasks out of tasks/ into offboarded/ (nothing is deleted; see restore)"
    )
    ob.add_argument("task", nargs="*", help="tasks (a plan takes its steps along) or unused general scripts, now")
    ob.add_argument("--due", action="store_true", help="every settled task idle for the retention period")
    ob.add_argument("--days", type=float, metavar="N", help="with --due: this retention instead of the home's")
    ob.add_argument("--dry", action="store_true", help="say what would move, move nothing")
    ob.add_argument("--json", action="store_true")
    ob.set_defaults(fn=cmd_offboard)

    rst = sub.add_parser("restore", help="bring offboarded tasks back, with the general scripts and steps they need")
    rst.add_argument("task", nargs="+")
    rst.add_argument("--json", action="store_true")
    rst.set_defaults(fn=cmd_restore)

    ak = sub.add_parser("ask", help="ask an agent to find or write the task for a request; prints what to run")
    ak.add_argument("request", nargs="*")
    ak.add_argument("--task", help="follow up on a task (resumes its agent conversation when possible)")
    ak.add_argument("--resume", metavar="ASK_ID", help="continue an earlier (e.g. interrupted) ask")
    ak.add_argument("--agent", help="agent from retinue.json (default from retinue.json)")
    ak.add_argument("--new", action="store_true", help="start a fresh conversation even with --task")
    ak.add_argument("--no-grill", action="store_true", help="no interview: the agent takes defaults and records them")
    ak.add_argument("--no-split", action="store_true", help="one agent only, even when the request has parts")
    ak.add_argument("--max-agents", type=int, metavar="N", help="at most N agents working at once (default: no limit)")
    ak.add_argument(
        "--budget",
        type=float,
        metavar="USD",
        help="spending reminder for the whole ask, all agents together (default: from `opsquire init --budget`)",
    )
    ak.add_argument("--strict-budget", action="store_true", help="make --budget a hard cap instead of a reminder")
    ak.set_defaults(fn=cmd_ask)

    wt = sub.add_parser("wait", help="wait for the next finished run of a task, then print it (for agents)")
    wt.add_argument("task")
    wt.add_argument("--since", help="UTC ISO time; default: newer than the latest run that exists now")
    wt.add_argument(
        "--timeout",
        type=float,
        default=540,
        help="seconds (default 540); exit 124 on timeout, 4 when the task was abandoned or superseded instead",
    )
    wt.add_argument("--json", action="store_true")
    wt.set_defaults(fn=cmd_wait)

    ct = sub.add_parser("contract", help="print the agent contract (packaged AGENTS.md + the home's LOCAL.md)")
    ct.add_argument("--write", action="store_true", help="write it to the home as AGENTS.md and CLAUDE.md")
    ct.set_defaults(fn=cmd_contract)

    ini = sub.add_parser("init", help="set up a machine: home, local rules, config, credentials, skill, first task")
    ini.add_argument("path", nargs="?", help="the home to use (default: the configured or default home)")
    ini.add_argument("--budget", type=float, metavar="USD", help="spending reminder per `opsquire ask`")
    ini.add_argument(
        "--offboard-days",
        type=float,
        metavar="N",
        help="settled tasks leave tasks/ N days after their last activity (default 7; 0: never automatically)",
    )
    ini.add_argument("--dry-run", action="store_true")
    ini.set_defaults(fn=cmd_init)

    ig = sub.add_parser(
        "integrate", help="install the opsquire skill for Claude agents; make the home a local-only git repo"
    )
    ig.add_argument("--dry-run", action="store_true")
    ig.set_defaults(fn=cmd_integrate)

    rp = sub.add_parser(
        "repo", help="change a code/config repo as a GitHub PR: list, checkout <repo>, overlap <copy>, pr <copy>"
    )
    rp.add_argument("action", choices=["list", "checkout", "overlap", "pr"])
    rp.add_argument("target", nargs="?", help="repo name (checkout) or copy path (overlap, pr)")
    rp.add_argument("--title", help="PR title (first line) and commit message")
    rp.add_argument("--body-file", help="PR description, markdown")
    rp.add_argument("--branch", help="branch name (default: <branch_prefix><title words>)")
    rp.add_argument("--base", help="checkout: branch to copy and open the PR against (default: the repo's base)")
    rp.add_argument("--json", action="store_true")
    rp.set_defaults(fn=cmd_repo)

    pc = sub.add_parser(
        "piece", help="fan-out for chat agents: open <name> (worktree for one subagent), land <name>, drop <name>, ls"
    )
    pc.add_argument("action", choices=["open", "land", "drop", "ls"])
    pc.add_argument("target", nargs="?", help="piece name (open), or name, group/name or worktree path (land, drop)")
    pc.add_argument("--request", default="", help="open: the piece's self-contained request, recorded on its branch")
    pc.add_argument("--group", help="open: pieces of one request share a group (default: the OPSQUIRE_AUTHOR session)")
    pc.add_argument("--delete", action="store_true", help="drop: discard the piece and its branch, do not park it")
    pc.add_argument("--json", action="store_true")
    pc.set_defaults(fn=cmd_piece)

    jg = sub.add_parser("judge", help="let an agent decide how a task ended from its record, and close it")
    jg.add_argument("task")
    jg.add_argument("--agent", help="agent from retinue.json (default from retinue.json)")
    jg.add_argument("--json", action="store_true")
    jg.set_defaults(fn=cmd_judge)

    ms = sub.add_parser(
        "milestones",
        help="what happened that others would want to know: changing tasks closed, applies that failed, thread results",
    )
    ms.add_argument("--since", help="date, ISO time, HH:MM (UTC today) or 2h/30m/1d; default: midnight UTC today")
    ms.add_argument("--until", help="ISO prefix")
    ms.add_argument("--thread", help="one thread only")
    ms.add_argument("--kind", help="comma-separated: done, failed, result (default: all)")
    ms.add_argument("--json", action="store_true")
    ms.set_defaults(fn=cmd_milestones)

    th = sub.add_parser("threads", help="tasks grouped into the threads (stories) they belong to, with suggestions")
    th.add_argument("--json", action="store_true")
    th.set_defaults(fn=cmd_threads)

    ui = sub.add_parser("ui", help="serve the home's picture on localhost: threads, tasks, runs, scripts, locks")
    ui.add_argument("--port", type=int, default=DEFAULT_PORT)
    ui.add_argument("--open", action="store_true", help="open it in the browser")
    ui.set_defaults(fn=cmd_ui)

    dc = sub.add_parser("docs", help="regenerate docs/: the catalog and an API page per general script")
    dc.set_defaults(fn=cmd_docs)

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
        if a.cmd in ("new", "lint", "run"):
            sweep_after_command()
        if a.cmd in ("new", "lint", "run", "offboard", "restore"):
            refresh_docs()
        return code
    except OpsquireError as e:
        print(f"opsquire: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nopsquire: interrupted", file=sys.stderr)
        return EXIT_CODES["interrupted"]
    except BrokenPipeError:
        return 0
