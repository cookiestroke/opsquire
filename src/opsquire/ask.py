from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from .model import DATA_DIR, OpsquireError, compact, home_dir, iso, now, read_json, var_dir, write_json

SYSTEM_PROMPT = """You are running inside `opsquire ask` for the operator.
The operator will run whatever you prepare in their own terminal.
Read {home}/AGENTS.md first and follow it exactly. The `opsquire` command is on your PATH.

Turn the request into runnable opsquire task(s):
1. Search first: `opsquire find <keywords>` (and `opsquire show <id>` on candidates). Reuse a fitting task; if a general
   script fits, create a thin task that calls it with this run's arguments. Archived tasks are reference only.
2. If the request needs several tasks in order (for example check, change, verify), reuse or create each task, then
   create a plan with `opsquire new <area> <name> --plan --step <task-id> --step <task-id> ...` (mark verify steps with
   "verify": true and a rollback task with "on_failure" in its meta.json steps) and hand over the plan's command only.
3. Otherwise create one task with `opsquire new ... ` (set --purpose, --target, --changes-prod/--rollback,
   --cred ENV=key, --uses, --interpreter when it needs a project venv) and write the script. Prod-changing scripts must
   do nothing in dry mode and call confirm before changes. Use sq.run(main, cleanup=...) for graceful termination.
4. `opsquire lint <task-or-plan>` must pass before you finish.
5. Never run `opsquire run`, never use ssh/scp, never read or ask for credential values.
6. For questions about past runs, use `opsquire runs/last/log ... --json` and answer from the records.
{grill}
End your final message with plain lines, nothing after them:
RUN: <exact command>      (one per command, in order; first the dry/read run)
NEED: <question>          (instead of RUN when blocked on something only the operator can give)"""

GRILL_ON = """
Interview before building (only when something new must be built; reusing an existing task needs no interview):
- Ask exactly ONE question per message, about the decision other decisions depend on first (scope, targets,
  order, failure handling, rollback, what counts as done). Research the code, configs and records first; never ask
  what you can look up.
- End that message with exactly two lines and nothing after them:
  ASK: <the question>
  RECOMMEND: <your recommended answer, short>
- The operator's answer comes back as your next message. Stop asking once the open decisions are settled.
- Before writing scripts, save the decisions to design.md in the task (or plan) folder: question, answer, why.
"""

GRILL_OFF = """
Do not ask questions. Where a decision is open, take the safest reasonable default and record each assumption in
design.md in the task folder.
"""

ACTION_RE = re.compile(r"^(RUN|NEED):\s*(.+?)\s*$", re.M)
ASK_RE = re.compile(r"^ASK:\s*(.+?)\s*$", re.M)
RECOMMEND_RE = re.compile(r"^RECOMMEND:\s*(.+?)\s*$", re.M)
MAX_ROUNDS = 15


@dataclass
class AskResult:
    ask_id: str = ""
    text: str = ""
    session: str | None = None
    actions: list[tuple[str, str]] = field(default_factory=list)
    exit_code: int = 0
    cost_usd: float | None = None
    interrupted: bool = False
    rounds: list[dict[str, Any]] = field(default_factory=list)
    waiting: str | None = None


def agents_config() -> dict[str, Any]:
    override = os.environ.get("OPSQUIRE_RETINUE")
    local = home_dir() / "retinue.json"
    path = Path(override) if override else local if local.is_file() else DATA_DIR / "retinue.json"
    try:
        cfg: dict[str, Any] = read_json(path)
    except (OSError, ValueError) as e:
        raise OpsquireError(f"cannot read agent config {path}: {e}") from None
    return cfg


def pick_agent(name: str | None) -> tuple[str, dict[str, Any]]:
    cfg = agents_config()
    name = name or str(cfg.get("default", ""))
    agent = cfg.get("agents", {}).get(name)
    if not isinstance(agent, dict):
        raise OpsquireError(f"unknown agent {name!r}; configured: {', '.join(cfg.get('agents', {}))}")
    if not agent.get("enabled"):
        raise OpsquireError(f"agent {name!r} is disabled: {agent.get('reason', 'no reason given')}")
    return name, agent


def build_argv(agent: dict[str, Any], values: dict[str, str], session: str | None) -> list[str]:
    def fill(s: str) -> str:
        for k, v in values.items():
            s = s.replace("{" + k + "}", v)
        return s

    argv: list[str] = []
    for item in agent["argv"]:
        if item == "{allowed_tools*}":
            argv += [fill(t) for t in agent.get("allowed_tools", [])]
        else:
            argv.append(fill(item))
    if session:
        if not agent.get("resume_argv"):
            raise OpsquireError("this agent cannot resume a conversation")
        argv += [fill(i).replace("{session}", session) for i in agent["resume_argv"]]
    return argv


def _lines(stream: IO[str]) -> Iterator[str]:
    yield from iter(stream.readline, "")


def parse_claude_stream(stream: IO[str], progress: Callable[[str], None], res: AskResult) -> None:
    for line in _lines(stream):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "system" and ev.get("session_id"):
            res.session = ev["session_id"]
        if ev.get("type") == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    inp = block.get("input", {})
                    detail = inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
                    progress(f"{block.get('name')}: {str(detail)[:110]}")
        if ev.get("type") == "result":
            res.text = str(ev.get("result") or "")
            res.session = ev.get("session_id") or res.session
            res.cost_usd = ev.get("total_cost_usd")
            if ev.get("is_error"):
                res.exit_code = 1


def parse_text(stream: IO[str], progress: Callable[[str], None], res: AskResult) -> None:
    for line in _lines(stream):
        res.text += line


PARSERS: dict[str, Callable[[IO[str], Callable[[str], None], AskResult], None]] = {
    "claude-stream-json": parse_claude_stream,
    "text": parse_text,
}


def asks_dir() -> Path:
    return var_dir() / "asks"


def ask_records() -> list[dict[str, Any]]:
    d = asks_dir()
    if not d.is_dir():
        return []
    recs: list[dict[str, Any]] = []
    for path in sorted(d.glob("*.json")):
        try:
            recs.append(read_json(path))
        except (OSError, ValueError):
            continue
    return recs


def session_for_task(task_id: str) -> str | None:
    for rec in reversed(ask_records()):
        if rec.get("session") and task_id in (rec.get("task_ids") or []):
            return str(rec["session"])
    return None


def session_for_ask(ask_id: str) -> tuple[str, str]:
    matches = [r for r in ask_records() if ask_id in str(r.get("ask_id"))]
    if len(matches) != 1:
        raise OpsquireError(f"{ask_id!r} matches {len(matches)} asks; use the full ask id")
    rec = matches[0]
    if not rec.get("session"):
        raise OpsquireError(f"ask {rec['ask_id']} has no agent session to resume")
    return str(rec["session"]), str(rec.get("agent"))


def stop(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def tty_answer(question: str, recommend: str) -> str | None:
    try:
        with open("/dev/tty", "w") as out, open("/dev/tty") as inp:
            out.write(f"\n? {question}\n  [Enter = {recommend}] > ")
            out.flush()
            line = inp.readline()
    except OSError:
        return None
    if not line:
        return None
    return line.strip() or recommend


def ask(
    prompt: str,
    agent_name: str | None,
    task_id: str | None,
    new_session: bool,
    resume_ask: str | None = None,
    grill: bool = True,
) -> AskResult:
    session = None
    if resume_ask:
        session, agent_name = session_for_ask(resume_ask)
    name, agent = pick_agent(agent_name)
    parser = PARSERS.get(str(agent.get("output")))
    if parser is None:
        raise OpsquireError(f"agent {name!r} has unknown output format {agent.get('output')!r}")
    started = now()
    res = AskResult(ask_id=f"{compact(started)}_{name}")
    home = str(home_dir())
    if task_id and not new_session and not session:
        session = session_for_task(task_id)
    if task_id and not session:
        prompt = f"This is about opsquire task {task_id}.\n\n{prompt}"
    system = SYSTEM_PROMPT.replace("{home}", home).replace("{grill}", GRILL_ON if grill else GRILL_OFF)
    env = {
        **os.environ,
        "OPSQUIRE_AUTHOR": f"opsquire-ask/{name} {res.ask_id}",
        "PATH": os.pathsep.join([os.path.dirname(os.path.abspath(sys.argv[0])), os.environ.get("PATH", "")]),
    }
    first_prompt = prompt
    resumed = bool(session)
    turn = AskResult()
    try:
        message = prompt
        for _ in range(MAX_ROUNDS):
            turn = AskResult()
            code = run_turn(
                agent, parser, name, {"prompt": message, "system": system, "home": home}, session, env, turn
            )
            res.text, res.cost_usd = turn.text, (res.cost_usd or 0) + (turn.cost_usd or 0)
            res.session = turn.session or session
            session = res.session
            res.exit_code = turn.exit_code or code
            res.actions = ACTION_RE.findall(res.text)
            questions = ASK_RE.findall(res.text)
            if res.exit_code or res.actions or not questions or not grill:
                break
            question = questions[-1]
            recommends = RECOMMEND_RE.findall(res.text)
            recommend = recommends[-1] if recommends else "your call"
            if not session:
                res.waiting = question
                break
            answer = tty_answer(question, recommend)
            if answer is None:
                res.waiting = question
                break
            res.rounds.append({"question": question, "recommended": recommend, "answer": answer})
            message = f"Answer: {answer}"
        else:
            res.waiting = "too many questions in one ask; continue with --resume"
    except KeyboardInterrupt:
        res.interrupted = True
        res.exit_code = 130
        res.session = res.session or turn.session
    mentioned = sorted(
        set(re.findall(r"\b\d{8}-\d{4}_[a-z0-9-]+_[a-z0-9-]+\b", res.text)) | ({task_id} if task_id else set())
    )
    status = (
        "interrupted" if res.interrupted else "waiting" if res.waiting else "ok" if res.exit_code == 0 else "failed"
    )
    record = {
        "ask_id": res.ask_id,
        "agent": name,
        "status": status,
        "started": iso(started),
        "ended": iso(now()),
        "prompt": first_prompt,
        "session": res.session,
        "resumed": resumed,
        "grill": grill,
        "rounds": res.rounds,
        "waiting_on": res.waiting,
        "exit_code": res.exit_code,
        "cost_usd": res.cost_usd,
        "task_ids": mentioned,
        "actions": [{"kind": k, "text": t} for k, t in res.actions],
        "text": res.text,
    }
    try:
        asks_dir().mkdir(parents=True, exist_ok=True)
        write_json(asks_dir() / f"{res.ask_id}.json", record)
    except OSError:
        sys.stderr.write("  (ask not recorded: var/ is not writable here)\n")
    return res


def run_turn(
    agent: dict[str, Any],
    parser: Callable[[IO[str], Callable[[str], None], AskResult], None],
    name: str,
    values: dict[str, str],
    session: str | None,
    env: dict[str, str],
    turn: AskResult,
) -> int:
    argv = build_argv(agent, values, session)

    def progress(msg: str) -> None:
        sys.stderr.write(f"  · {msg}\n")
        sys.stderr.flush()

    sys.stderr.write(f"opsquire ask via {name}{' (resuming ' + session + ')' if session else ''} ...\n")
    try:
        proc = subprocess.Popen(
            argv, cwd=values["home"], env=env, stdout=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL
        )
    except FileNotFoundError:
        raise OpsquireError(f"agent command not found: {argv[0]}") from None
    assert proc.stdout
    try:
        with proc.stdout:
            parser(proc.stdout, progress, turn)
        return proc.wait()
    except KeyboardInterrupt:
        stop(proc)
        raise
