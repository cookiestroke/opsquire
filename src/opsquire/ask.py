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
2. Otherwise create one with `opsquire new ... ` (set --purpose, --target, --changes-prod/--rollback, --cred ENV=key,
   --uses) and write the script. Prod-changing scripts must do nothing in dry mode and call confirm before changes.
3. `opsquire lint <task>` must pass before you finish.
4. Never run `opsquire run`, never use ssh/scp, never read or ask for credential values. You cannot ask questions
   interactively: if you need a fact or a credential key name, stop and ask for it on a NEED line.
5. For questions about past runs, use `opsquire runs/last/log ... --json` and answer from the records.

End your final message with plain lines, nothing after them:
RUN: <exact command>      (one per command, in order; first the dry/read run)
NEED: <question>          (instead of RUN when blocked)"""

ACTION_RE = re.compile(r"^(RUN|NEED):\s*(.+?)\s*$", re.M)


@dataclass
class AskResult:
    ask_id: str = ""
    text: str = ""
    session: str | None = None
    actions: list[tuple[str, str]] = field(default_factory=list)
    exit_code: int = 0
    cost_usd: float | None = None
    interrupted: bool = False


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


def ask(
    prompt: str, agent_name: str | None, task_id: str | None, new_session: bool, resume_ask: str | None = None
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
    values = {"prompt": prompt, "system": SYSTEM_PROMPT.replace("{home}", home), "home": home}
    argv = build_argv(agent, values, session)
    env = {
        **os.environ,
        "OPSQUIRE_AUTHOR": f"opsquire-ask/{name} {res.ask_id}",
        "PATH": os.pathsep.join([os.path.dirname(os.path.abspath(sys.argv[0])), os.environ.get("PATH", "")]),
    }

    def progress(msg: str) -> None:
        sys.stderr.write(f"  · {msg}\n")
        sys.stderr.flush()

    sys.stderr.write(f"opsquire ask via {name}{' (resuming ' + session + ')' if session else ''} ...\n")
    try:
        proc = subprocess.Popen(argv, cwd=home, env=env, stdout=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise OpsquireError(f"agent command not found: {argv[0]}") from None
    assert proc.stdout
    try:
        with proc.stdout:
            parser(proc.stdout, progress, res)
        code = proc.wait()
    except KeyboardInterrupt:
        stop(proc)
        res.interrupted = True
        code = 130
    res.exit_code = 130 if res.interrupted else (res.exit_code or code)
    res.actions = ACTION_RE.findall(res.text)
    mentioned = sorted(
        set(re.findall(r"\b\d{8}-\d{4}_[a-z0-9-]+_[a-z0-9-]+\b", res.text)) | ({task_id} if task_id else set())
    )
    record = {
        "ask_id": res.ask_id,
        "agent": name,
        "status": "interrupted" if res.interrupted else "ok" if res.exit_code == 0 else "failed",
        "started": iso(started),
        "ended": iso(now()),
        "prompt": prompt,
        "session": res.session,
        "resumed": bool(session),
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
