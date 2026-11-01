from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from . import homegit, pinned
from .model import DATA_DIR, OpsquireError, compact, home_dir, iso, now, read_json, var_dir, write_json

SYSTEM_PROMPT = """You are running inside `opsquire ask` for the operator.
The operator will run whatever you prepare in their own terminal.
Read {home}/AGENTS.md first and follow it exactly. The `opsquire` command is on your PATH.
{split}
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

SPLIT_ON = """
Parallel work comes first: if the request holds two or more pieces that can be built independently (no piece needs
another's result, and no two would edit the same general script), do not build anything yourself. After at most a
quick `opsquire find`, answer with only these lines, one per piece, and nothing else:
SPLIT: <short-kebab-name> | <a self-contained request for that piece, carrying every detail it needs>
The name (at most 30 characters, e.g. umm-disk-usage) labels the piece for the operator; make each one distinct.
Each piece then gets its own agent working in parallel. If the request is one piece of work, carry on below.
"""

PIECE = """
You are one of {total} agents working in parallel on one request; build only your piece, the others do the rest.
Your piece: {piece}
The whole request, for context only: {whole}
"""

RESOLVE = """While you worked, other pieces of the same request were merged into the home. Merging them into your
copy left conflict markers in: {files}. Resolve every conflict keeping the intent of both sides, run `opsquire lint`
on each task you created or changed, then end with your RUN lines again."""

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
SPLIT_RE = re.compile(r"^SPLIT:\s*(.+?)\s*$", re.M)
TASK_ID_IN_TEXT = re.compile(r"\b\d{8}-\d{4}_[a-z0-9-]+_[a-z0-9-]+\b")
MAX_ROUNDS = 15
DEFAULT_BUDGET_USD = 15.0

Answerer = Callable[[str, str], "str | None"]


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
    task_ids: list[str] = field(default_factory=list)
    waiting: str | None = None
    recommend: str = ""
    split: list[str] = field(default_factory=list)
    pieces: list[dict[str, Any]] = field(default_factory=list)


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


def build_argv(agent: dict[str, Any], values: dict[str, str], session: str | None, cap: float | None) -> list[str]:
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
    if cap is not None:
        if not agent.get("budget_argv"):
            raise OpsquireError("this agent cannot enforce a budget; drop --strict-budget")
        argv += [fill(i).replace("{budget}", f"{cap:.2f}") for i in agent["budget_argv"]]
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


def say(msg: str) -> None:
    with pinned.LOCK:
        if not pinned.line(msg):
            sys.stderr.write(msg + "\n")
            sys.stderr.flush()


class BudgetSpent(OpsquireError):
    pass


class Budget:
    """One spending pool for an ask and all its pieces. The limit is a reminder (a notice when passed) unless
    strict, in which case each agent turn is capped at what is left and nothing starts once it is spent."""

    def __init__(self, limit: float, strict: bool) -> None:
        self.limit, self.strict = limit, strict
        self.spent = 0.0
        self.noticed = False
        self.lock = threading.Lock()

    def cap(self) -> float | None:
        if not self.strict:
            return None
        with self.lock:
            left = self.limit - self.spent
        if left <= 0.005:
            raise BudgetSpent(f"budget of ${self.limit:.2f} spent")
        return left

    def add(self, cost: float | None) -> None:
        with self.lock:
            self.spent += cost or 0
            crossed = not self.strict and not self.noticed and self.spent >= self.limit
            self.noticed = self.noticed or crossed
        if crossed:
            say(f"  ! ${self.spent:.2f} spent so far, past the ${self.limit:.2f} reminder (still running)")


class Agents:
    """Every agent process alive in this ask, so Ctrl-C can stop them all."""

    procs: set[subprocess.Popen[str]] = set()
    lock = threading.Lock()

    @classmethod
    def stop_all(cls) -> None:
        with cls.lock:
            procs = list(cls.procs)
        for proc in procs:
            stop(proc)


@dataclass
class Conversation:
    """One agent conversation in one folder (the home, or a parallel piece's worktree)."""

    name: str
    agent: dict[str, Any]
    home: str
    system: str
    env: dict[str, str]
    session: str | None = None
    label: str = ""
    cost: float = 0.0

    def progress(self, msg: str) -> None:
        say(f"  · {self.label + ' ' if self.label else ''}{msg}")

    def turn(self, message: str, budget: Budget) -> AskResult:
        parser = PARSERS.get(str(self.agent.get("output")))
        if parser is None:
            raise OpsquireError(f"agent {self.name!r} has unknown output format {self.agent.get('output')!r}")
        values = {"prompt": message, "system": self.system, "home": self.home}
        argv = build_argv(self.agent, values, self.session, budget.cap())
        resuming = f" (resuming {self.session})" if self.session else ""
        say(f"opsquire ask via {self.name}{' ' + self.label if self.label else ''}{resuming} ...")
        res = AskResult()
        try:
            proc = subprocess.Popen(
                argv, cwd=self.home, env=self.env, stdout=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL
            )
        except FileNotFoundError:
            raise OpsquireError(f"agent command not found: {argv[0]}") from None
        with Agents.lock:
            Agents.procs.add(proc)
        assert proc.stdout
        try:
            with proc.stdout:
                parser(proc.stdout, self.progress, res)
            code = proc.wait()
        except KeyboardInterrupt:
            stop(proc)
            self.session = res.session or self.session
            raise
        finally:
            with Agents.lock:
                Agents.procs.discard(proc)
        res.exit_code = res.exit_code or code
        self.session = res.session or self.session
        res.session = self.session
        self.cost += res.cost_usd or 0
        budget.add(res.cost_usd)
        res.actions = ACTION_RE.findall(res.text)
        return res


def converse(
    conv: Conversation,
    message: str,
    grill: bool,
    answer: Answerer,
    budget: Budget,
    rounds: list[dict[str, Any]],
    allow_split: bool = False,
    stopped: Callable[[], bool] = lambda: False,
) -> AskResult:
    """Run turns until the agent hands over RUN/NEED lines, asks something nobody answers, or splits."""
    res = AskResult()
    for _ in range(MAX_ROUNDS):
        if stopped():
            res.interrupted = True
            return res
        res = conv.turn(message, budget)
        if res.exit_code or stopped():
            res.interrupted = stopped()
            return res
        pieces = SPLIT_RE.findall(res.text)
        if allow_split and len(pieces) >= 2 and not res.actions:
            res.split = pieces
            return res
        questions = ASK_RE.findall(res.text)
        if res.actions or not questions or not grill:
            return res
        recommends = RECOMMEND_RE.findall(res.text)
        res.recommend = recommends[-1] if recommends else "your call"
        question = questions[-1]
        reply = answer(question, res.recommend) if res.session else None
        if reply is None:
            res.waiting = question
            return res
        rounds.append({"question": question, "recommended": res.recommend, "answer": reply})
        message = f"Answer: {reply}"
    res.waiting = "too many questions in one ask; continue with --resume"
    return res


def system_prompt(home: str, grill: bool, split: bool, piece: str = "") -> str:
    text = SYSTEM_PROMPT.replace("{home}", home).replace("{grill}", GRILL_ON if grill else GRILL_OFF)
    return text.replace("{split}", SPLIT_ON if split else piece)


def agent_env(name: str, ask_id: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    return {
        **os.environ,
        "OPSQUIRE_AUTHOR": f"opsquire-ask/{name} {ask_id}",
        "PATH": os.pathsep.join([os.path.dirname(os.path.abspath(sys.argv[0])), os.environ.get("PATH", "")]),
        **(extra or {}),
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


def save_record(record: dict[str, Any]) -> None:
    try:
        asks_dir().mkdir(parents=True, exist_ok=True)
        write_json(asks_dir() / f"{record['ask_id']}.json", record)
    except OSError:
        say("  (ask not recorded: var/ is not writable here)")


def session_for_task(task_id: str) -> str | None:
    for rec in reversed(ask_records()):
        if rec.get("session") and task_id in (rec.get("task_ids") or []):
            return str(rec["session"])
    return None


def find_ask(query: str) -> dict[str, Any]:
    records = ask_records()
    by_session = [r for r in records if r.get("session") == query]
    matches = by_session[-1:] or [r for r in records if query in str(r.get("ask_id"))]
    if len(matches) != 1:
        raise OpsquireError(f"{query!r} matches {len(matches)} asks; use the ask id or session id it printed")
    return matches[0]


def stop(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def tty_answer(question: str, recommend: str) -> str | None:
    """Ask the operator on the terminal (pinned below the progress when it can be); None without a terminal."""
    try:
        line = pinned.ask(question, recommend)
    except OSError:
        return None
    if line is None:
        return None
    answer = line.strip() or recommend
    say(f"? {question}\n  -> {answer}")
    return answer


def mentioned_tasks(text: str, extra: str | None = None) -> list[str]:
    return sorted(set(TASK_ID_IN_TEXT.findall(text)) | ({extra} if extra else set()))


def ask(
    prompt: str,
    agent_name: str | None,
    task_id: str | None,
    new_session: bool,
    resume_ask: str | None = None,
    grill: bool = True,
    split: bool = True,
    max_agents: int | None = None,
    budget: Budget | None = None,
    reply_given: bool = False,
) -> AskResult:
    budget = budget or Budget(DEFAULT_BUDGET_USD, strict=False)
    session = None
    if resume_ask:
        rec = find_ask(resume_ask)
        if rec.get("pieces"):
            from .parallel import resume_parallel

            return resume_parallel(rec, prompt if reply_given else None, max_agents, budget)
        if not rec.get("session"):
            raise OpsquireError(f"ask {rec['ask_id']} has no agent session to resume")
        session, agent_name = str(rec["session"]), str(rec.get("agent"))
    name, agent = pick_agent(agent_name)
    started = now()
    res = AskResult(ask_id=f"{compact(started)}_{name}")
    home = home_dir()
    if task_id and not new_session and not session:
        session = session_for_task(task_id)
    if task_id and not session:
        prompt = f"This is about opsquire task {task_id}.\n\n{prompt}"
    can_split = split and not session and not task_id and homegit.is_repo(home)
    conv = Conversation(name, agent, str(home), system_prompt(str(home), grill, can_split), agent_env(name, res.ask_id))
    conv.session = session
    try:
        turn = converse(conv, prompt, grill, tty_answer, budget, res.rounds, allow_split=can_split)
    except KeyboardInterrupt:
        turn = AskResult(interrupted=True, exit_code=130)
    except BudgetSpent as e:
        turn = AskResult(waiting=f"{e}; continue with --resume and a new --budget")
    res.text, res.exit_code, res.actions = turn.text, turn.exit_code, turn.actions
    res.waiting, res.interrupted, res.split = turn.waiting, turn.interrupted, turn.split
    res.session, res.cost_usd = conv.session, conv.cost
    if res.split:
        from .parallel import fan_out

        return fan_out(res, prompt, name, agent, grill, max_agents, budget, started)
    res.task_ids = mentioned_tasks(res.text, task_id)
    if res.task_ids:
        note = homegit.commit_quietly(home, [home / "tasks", home / "general"], f"ask {res.ask_id}: {prompt[:60]}")
        if note:
            say(f"  {note}")
    status = (
        "interrupted" if res.interrupted else "waiting" if res.waiting else "ok" if res.exit_code == 0 else "failed"
    )
    save_record(
        {
            "ask_id": res.ask_id,
            "agent": name,
            "status": status,
            "started": iso(started),
            "ended": iso(now()),
            "prompt": prompt,
            "session": res.session,
            "resumed": bool(session),
            "grill": grill,
            "rounds": res.rounds,
            "waiting_on": res.waiting,
            "exit_code": res.exit_code,
            "cost_usd": res.cost_usd,
            "task_ids": res.task_ids,
            "actions": [{"kind": k, "text": t} for k, t in res.actions],
            "text": res.text,
        }
    )
    return res
