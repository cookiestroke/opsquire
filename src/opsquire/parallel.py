"""Parallel asks: the first agent split the request into independent pieces; each piece gets its own agent in its own
git worktree of the home, interview questions from all of them reach the operator through one labelled queue, and
every finished piece is committed and merged back. Unfinished pieces are parked on their branch, resumable."""

from __future__ import annotations

import datetime as dt
import queue
import re
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import handover, homegit, repos
from .ask import (
    PIECE,
    PIECE_MODEL,
    RESOLVE,
    Agents,
    Answerer,
    AskResult,
    Budget,
    BudgetSpent,
    Chat,
    Conversation,
    agent_env,
    billing,
    converse,
    mentioned_tasks,
    model_for,
    pick_agent,
    save_record,
    say,
    system_prompt,
    tty_answer,
)
from .lint import lint_task, save_lint
from .model import OpsquireError, home_dir, iso, now, resolve_task, var_dir, write_json
from .plan import plan_meta

RUN_TASK_RE = re.compile(r"^opsquire run (\S+)")


@dataclass
class Piece:
    n: int
    total: int
    request: str
    branch: str
    worktree: str
    name: str = ""
    status: str = "pending"
    session: str | None = None
    waiting: str | None = None
    recommend: str = ""
    text: str = ""
    actions: list[list[str]] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    rounds: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float = 0.0

    @property
    def label(self) -> str:
        return f"[{self.n}/{self.total} {self.name or kebab(self.request)}]"

    @property
    def handed(self) -> list[str]:
        """Task ids this piece hands over, in order (from its RUN lines)."""
        ids: list[str] = []
        for kind, text in self.actions:
            m = RUN_TASK_RE.match(text)
            if kind == "RUN" and m and m.group(1) not in ids:
                ids.append(m.group(1))
        return ids


@dataclass
class Question:
    piece: Piece
    question: str
    recommend: str
    done: threading.Event = field(default_factory=threading.Event)
    answer: str | None = None


class Fanout:
    def __init__(
        self,
        ask_id: str,
        whole: str,
        name: str,
        agent: dict[str, Any],
        grill: bool,
        max_agents: int | None,
        budget: Budget,
    ) -> None:
        self.ask_id, self.whole, self.name, self.agent, self.grill = ask_id, whole, name, agent, grill
        self.budget = budget
        self.home = home_dir()
        self.base = homegit.branch(self.home)
        self.slots = threading.BoundedSemaphore(max_agents) if max_agents else None
        self.questions: queue.Queue[Question] = queue.Queue()
        self.stop = threading.Event()
        self.merge_lock = threading.Lock()
        self.reply: str | None = None

    def ask_operator(self, piece: Piece) -> Answerer:
        def answer(question: str, recommend: str) -> str | None:
            if self.stop.is_set():
                return None
            q = Question(piece, question, recommend)
            self.questions.put(q)
            while not q.done.wait(0.2):
                if self.stop.is_set():
                    return None
            return q.answer

        return answer

    def prepare(self, piece: Piece) -> None:
        wt = Path(piece.worktree)
        homegit.add_worktree(self.home, wt, piece.branch)
        env = {**self.env(), "OPSQUIRE_HOME": str(wt)}
        for cmd in (["contract", "--write", "--force"], ["docs"]):
            subprocess.run([sys.executable, "-m", "opsquire", *cmd], env=env, capture_output=True, check=False)

    def env(self) -> dict[str, str]:
        return agent_env(self.name, self.ask_id, {"OPSQUIRE_VAR": str(var_dir())})

    def conversation(self, piece: Piece) -> Conversation:
        wt = piece.worktree
        context = PIECE.format(total=piece.total, piece=piece.request, whole=self.whole)
        conv = Conversation(
            self.name,
            self.agent,
            wt,
            system_prompt(wt, self.grill, split=False, piece=context),
            {**self.env(), "OPSQUIRE_HOME": wt, "OPSQUIRE_ASK_ID": f"{self.ask_id}-{piece.n}"},
            session=piece.session,
            label=piece.label,
            model=model_for(PIECE_MODEL, self.agent),
        )
        return conv

    def work(self, piece: Piece, first: str | None) -> None:
        """Thread body: talk to the piece's agent until it hands over, then land it. `first` is the first message;
        None means "answer the question it is waiting on", "" means "it is ready, only land it"."""
        try:
            conv = self.conversation(piece)
            message = first or None
            if first is None:
                reply = self.reply or self.ask_operator(piece)(str(piece.waiting), piece.recommend or "your call")
                if reply is None:
                    piece.status = "waiting"
                    return
                piece.rounds.append({"question": piece.waiting, "recommended": piece.recommend, "answer": reply})
                message = f"Answer: {reply}"
            if message is not None:
                piece.waiting = None
                self.talk(piece, conv, message)
                if piece.status != "ready":
                    return
            self.land(piece, conv)
        except BudgetSpent as e:
            piece.status, piece.waiting = "waiting", f"{e}; resume with a new --budget"
        except OpsquireError as e:
            piece.status, piece.text = "failed", str(e)
        except Exception as e:  # a crashed piece must never take the others down
            piece.status, piece.text = "failed", f"{type(e).__name__}: {e}"

    def talk(self, piece: Piece, conv: Conversation, message: str) -> AskResult:
        if self.slots:
            self.slots.acquire()
        try:
            res = converse(
                conv, message, self.grill, self.ask_operator(piece), self.budget, piece.rounds, stopped=self.stop.is_set
            )
        finally:
            if self.slots:
                self.slots.release()
        piece.session, piece.cost_usd = conv.session, piece.cost_usd + conv.cost
        conv.cost = 0.0
        piece.text = res.text or piece.text
        if res.actions:
            piece.actions = [list(a) for a in res.actions]
        piece.task_ids = mentioned_tasks(piece.text)
        if res.interrupted or self.stop.is_set():
            piece.status = "interrupted"
        elif res.exit_code:
            piece.status = "failed"
        elif res.waiting:
            piece.status, piece.waiting, piece.recommend = "waiting", res.waiting, res.recommend
        elif any(k == "RUN" for k, _ in res.actions):
            piece.status = "ready"
        else:
            need = [t for k, t in res.actions if k == "NEED"]
            piece.status, piece.waiting = "waiting", need[-1] if need else "the agent handed over no RUN lines"
        return res

    def land(self, piece: Piece, conv: Conversation) -> None:
        """Commit the piece and merge it into the home; on a clash its agent gets one turn to resolve it."""
        wt = Path(piece.worktree)
        homegit.commit(wt, None, f"ask {self.ask_id} piece {piece.n} ({piece.name}): built\n\n{piece.request}")
        message = f"ask {self.ask_id}: merge piece {piece.n} ({piece.name})"
        with self.merge_lock:
            if homegit.merge_into(self.home, piece.branch, message):
                self.merged(piece)
                return
        conflicts = homegit.merge_from(wt, self.base)
        if conflicts:
            self.talk(piece, conv, RESOLVE.format(files=", ".join(conflicts)))
            left = homegit.has_markers(wt, conflicts)
            if piece.status != "ready" or left:
                homegit.git(wt, "merge", "--abort", check=False)
                why = f"conflicts left in {', '.join(left)}" if left else f"resolving failed ({piece.status})"
                piece.status, piece.waiting = "clash", f"could not merge into the home: {why}"
                return
            homegit.commit(wt, None, f"ask {self.ask_id} piece {piece.n} ({piece.name}): resolve merge")
        with self.merge_lock:
            if homegit.merge_into(self.home, piece.branch, message):
                self.merged(piece)
                return
        piece.status, piece.waiting = "clash", "could not merge into the home (it changed again, or has local edits)"

    def merged(self, piece: Piece) -> None:
        """Lint the handed-over tasks again in the home (under the merge lock): lint results are not versioned, so
        the worktree's do not come along, and this checks the merged files themselves."""
        piece.status = "merged"
        repos.drop(f"{self.ask_id}-{piece.n}")
        for tid in piece.handed:
            task = resolve_task(tid)
            result = lint_task(task)
            save_lint(task, result)
            if result.interrupted:
                say(f"  ! {piece.label} {tid} merged, but its lint was interrupted: run `opsquire lint {tid}`")
            elif not result.ok:
                say(f"  ! {piece.label} {tid} does not pass lint in the home after merging:\n{result.output}")

    def park(self, piece: Piece) -> None:
        """Keep an unfinished piece's work on its branch and remove its worktree, so nothing is left lying around."""
        wt = Path(piece.worktree)
        if wt.exists():
            try:
                homegit.commit(
                    wt, None, f"ask {self.ask_id} piece {piece.n} ({piece.name}): unfinished ({piece.status})"
                )
            except homegit.GitError as e:
                say(f"  ! {piece.label} work could not be saved to {piece.branch}: {e}")
        homegit.remove_worktree(self.home, wt)
        if piece.status == "merged":
            homegit.delete_branch(self.home, piece.branch)

    def run(self, pieces: list[Piece], firsts: dict[int, str | None]) -> bool:
        """Run every piece; the main thread answers the question queue. False when interrupted."""
        homegit.exclude_worktrees(self.home)
        ready = []
        for p in pieces:
            try:
                self.prepare(p)
                ready.append(p)
            except OpsquireError as e:
                p.status, p.text = "failed", f"could not create its worktree: {e}"
        threads = [threading.Thread(target=self.work, args=(p, firsts.get(p.n)), daemon=True) for p in ready]
        for t in threads:
            t.start()
        interrupted = False
        try:
            while any(t.is_alive() for t in threads):
                try:
                    q = self.questions.get(timeout=0.2)
                except queue.Empty:
                    continue
                q.answer = tty_answer(f"{q.piece.label} {q.question}", q.recommend)
                q.done.set()
        except KeyboardInterrupt:
            interrupted = True
            say("\nopsquire: stopping every agent; unfinished pieces are kept on their branches")
            self.stop.set()
            Agents.stop_all()
            for t in threads:
                t.join(timeout=30)
        for p in pieces:
            if interrupted and p.status not in ("merged", "waiting", "failed", "clash"):
                p.status = "interrupted"
            self.park(p)
        return not interrupted


def kebab(text: str, limit: int = 30) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    out = ""
    for w in words:
        if len(out) + len(w) + 1 > limit:
            break
        out = f"{out}-{w}" if out else w
    return out or "piece"


def split_pieces(lines: list[str]) -> list[tuple[str, str]]:
    """(name, request) per SPLIT line. Agents name pieces as `name | request`; without a name, a piece is named by
    the words its request does not share with the others, so labels stay distinct."""
    parsed = []
    for line in lines:
        m = re.match(r"([a-z0-9][a-z0-9-]{0,29})\s*\|\s*(.+)$", line.strip())
        parsed.append((m.group(1), m.group(2)) if m else ("", line.strip()))
    word_sets = [set(re.findall(r"[a-z0-9]+", r.lower())) for _, r in parsed]
    common = set.intersection(*word_sets) if word_sets else set()
    named = []
    for name, request in parsed:
        if not name:
            name = kebab(" ".join(w for w in re.findall(r"[a-z0-9]+", request.lower()) if w not in common))
        named.append((name, request))
    return named


def piece_for(ask_id: str, home: Path, n: int, total: int, name: str, request: str) -> Piece:
    return Piece(
        n=n,
        total=total,
        request=request,
        branch=f"ask/{ask_id}/{n}",
        worktree=str(home / homegit.WORKTREES / ask_id / str(n)),
        name=name,
    )


def run_lines(task_id: str) -> list[str]:
    """The commands that hand a task over: dry then apply for a changing one, a single read run otherwise."""
    meta = resolve_task(task_id).meta
    changes = bool(meta and meta.changes_prod)
    return [f"opsquire run {task_id}"] + ([f"opsquire run {task_id} --apply"] if changes else [])


def bundle(fan: Fanout, pieces: list[Piece]) -> str | None:
    """One plan over every merged piece's tasks, in piece order (a piece's own plan contributes its steps)."""
    steps: list[dict[str, Any]] = []
    areas: set[str] = set()
    for p in pieces:
        if p.status != "merged":
            continue
        for tid in p.handed:
            task = resolve_task(tid)
            if task.meta is None:
                continue
            areas.add(task.meta.area)
            steps += [dict(s) for s in task.meta.steps or []] if task.kind == "plan" else [{"task": task.id}]
    if sum(1 for p in pieces if p.status == "merged" and p.handed) < 2 or not steps:
        return None
    t = now()
    area = areas.pop() if len(areas) == 1 else "multi"
    names = "-".join(p.name or f"piece{p.n}" for p in pieces if p.status == "merged" and p.handed)
    name = f"{t:%Y%m%d-%H%M}_{area}_{kebab('all ' + names.replace('-', ' '), 48)}"
    folder = fan.home / "tasks" / f"{t:%Y-%m}" / name
    purpose = f"Every piece of ask {fan.ask_id} in one command: {fan.whole[:200]}"
    meta = plan_meta(name, t, area, purpose, f"opsquire-ask/{fan.name} {fan.ask_id}", steps)
    folder.mkdir(parents=True)
    write_json(folder / "meta.json", meta.to_json())
    plan = resolve_task(name)
    result = lint_task(plan)
    save_lint(plan, result)
    if not result.ok:
        say(f"  ! the bundled plan {name} did not pass lint:\n{result.output}")
        return None
    homegit.commit(fan.home, [folder], f"ready: {name}")
    return name


def summary(pieces: list[Piece], plan: str | None) -> str:
    lines: list[str] = []
    for p in pieces:
        state = {"merged": "ready", "waiting": f"waiting: {p.waiting}", "clash": f"clash: {p.waiting}"}.get(
            p.status, p.status
        )
        lines.append(f"{p.label} {state}")
        if p.status == "merged":
            lines += [f"  {k}: {t}" for k, t in p.actions]
        elif p.status == "failed" and p.text:
            lines.append(f"  {p.text.strip().splitlines()[-1][:300]}")
    if plan:
        lines += ["", "Every ready piece in one plan (in order):"]
        lines += [f"RUN: {cmd}" for cmd in run_lines(plan)]
    return "\n".join(lines)


def finish(fan: Fanout, res: AskResult, pieces: list[Piece], started: dt.datetime, ok: bool) -> AskResult:
    plan = None
    if ok:
        try:
            plan = bundle(fan, pieces)
        except OpsquireError as e:
            say(f"  ! could not bundle the pieces into one plan: {e}")
    res.text = summary(pieces, plan)
    res.pieces = [asdict(p) for p in pieces]
    res.cost_usd = (res.cost_usd or 0) + sum(p.cost_usd for p in pieces)
    open_pieces = [p for p in pieces if p.status != "merged"]
    res.interrupted = not ok
    res.waiting = f"{len(open_pieces)} piece(s) not finished" if open_pieces and ok else None
    res.exit_code = 130 if not ok else 1 if any(p.status in ("failed", "clash") for p in pieces) else 0
    res.task_ids = sorted({t for p in pieces for t in p.task_ids} | ({plan} if plan else set()))
    res.actions = [(k, t) for p in pieces if p.status == "merged" for k, t in p.actions]
    if plan:
        res.actions += [("RUN", cmd) for cmd in run_lines(plan)]
    status = "interrupted" if not ok else "waiting" if res.waiting else "ok" if res.exit_code == 0 else "failed"
    if status == "ok":
        handover.record_run_lines(res.actions, fan.whole, f"ask {res.ask_id}")
    piece_model = model_for(PIECE_MODEL, fan.agent)
    spent = {"cost_usd": res.cost_usd, "billing": billing(), "model": f"pieces on {piece_model}" if piece_model else ""}
    Chat.add("agent", res.text.strip(), actions=[{"kind": k, "text": t} for k, t in res.actions], **spent)
    Chat.add("end", status, waiting=res.waiting or "")
    save_record(
        {
            **spent,
            "ask_id": res.ask_id,
            "agent": fan.name,
            "status": status,
            "started": iso(started),
            "ended": iso(now()),
            "prompt": fan.whole,
            "session": res.session,
            "grill": fan.grill,
            "rounds": res.rounds,
            "split": [p.request for p in pieces],
            "pieces": res.pieces,
            "plan": plan,
            "waiting_on": res.waiting,
            "exit_code": res.exit_code,
            "cost_usd": res.cost_usd,
            "task_ids": res.task_ids,
            "actions": [{"kind": k, "text": t} for k, t in res.actions],
            "text": res.text,
        }
    )
    return res


def fan_out(
    res: AskResult,
    whole: str,
    name: str,
    agent: dict[str, Any],
    grill: bool,
    max_agents: int | None,
    budget: Budget,
    started: dt.datetime,
) -> AskResult:
    fan = Fanout(res.ask_id, whole, name, agent, grill, max_agents, budget)
    homegit.commit(fan.home, None, f"snapshot before ask {res.ask_id}")
    named = split_pieces(res.split)
    pieces = [piece_for(res.ask_id, fan.home, i, len(named), n, r) for i, (n, r) in enumerate(named, 1)]
    total = len(pieces)
    say(f"opsquire: split into {total} pieces, one agent each:")
    for p in pieces:
        say(f"  {p.label} {p.request}")
    ok = fan.run(pieces, {p.n: p.request for p in pieces})
    return finish(fan, res, pieces, started, ok)


def resume_parallel(rec: dict[str, Any], reply: str | None, max_agents: int | None, budget: Budget) -> AskResult:
    name, agent = pick_agent(str(rec.get("agent")))
    pieces = [Piece(**p) for p in rec["pieces"]]
    todo = [p for p in pieces if p.status != "merged"]
    if not todo:
        raise OpsquireError(f"ask {rec['ask_id']}: every piece is already merged")
    fan = Fanout(
        str(rec["ask_id"]), str(rec.get("prompt", "")), name, agent, bool(rec.get("grill", True)), max_agents, budget
    )
    waiting = [p for p in todo if p.status == "waiting"]
    if reply is not None and len(waiting) == 1:
        fan.reply = reply
    firsts: dict[int, str | None] = {}
    for p in todo:
        if p.status == "waiting" and p.waiting and p.session:
            firsts[p.n] = None
        elif p.status == "clash" and p.actions:
            firsts[p.n] = ""
        else:
            firsts[p.n] = "Continue where you stopped." if p.session else p.request
        p.status = "ready" if firsts[p.n] == "" else "pending"
    homegit.commit(fan.home, None, f"snapshot before resuming ask {fan.ask_id}")
    res = AskResult(ask_id=fan.ask_id, session=rec.get("session"), rounds=list(rec.get("rounds") or []))
    res.cost_usd = float(rec.get("cost_usd") or 0) - sum(p.cost_usd for p in pieces)
    ok = fan.run(todo, firsts)
    started = dt.datetime.fromisoformat(str(rec["started"]).replace("Z", "+00:00"))
    return finish(fan, res, pieces, started, ok)
