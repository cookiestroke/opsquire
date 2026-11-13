"""Tool approvals for ask agents.

`opsquire approve-server --ask <id>` is a stdio MCP server with one tool, `approve`, which the agent's CLI calls (its
retinue `tools.approval_argv`) before any tool that is not already allowed. It decides from the home's tools.json:
allow and off answer at once; confirm writes a request into the ask's approvals folder and waits. The `opsquire ask`
that started the agent watches that folder and asks the operator, on the terminal or (through the job) in the console,
and writes the answer back. No operator, or no answer, is a denial.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, TextIO

from . import pinned, tools
from .model import read_json, var_dir, write_json

POLL_S = 0.3
YES = {"y", "yes", "allow", "ok"}
DETAIL_KEYS = (
    "channel_id",
    "channel",
    "chat_id",
    "to",
    "recipient",
    "repo",
    "title",
    "text",
    "message",
    "body",
    "command",
)


def folder(ask_key: str) -> Path:
    return var_dir() / "asks" / f"{ask_key}.approvals"


def detail(tool_input: dict[str, Any], limit: int = 400) -> str:
    """The parts of a tool's input an operator needs to judge it: where it goes and what it says."""
    picked = {k: tool_input[k] for k in DETAIL_KEYS if k in tool_input} or tool_input
    text = "; ".join(f"{k}={v if isinstance(v, str) else json.dumps(v)}" for k, v in picked.items())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _deny(why: str) -> dict[str, Any]:
    return {"behavior": "deny", "message": why}


def judge(ask_key: str, spec: dict[str, Any], tool: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    tools.note_seen(tool)
    choice, label = tools.decide(tool, tool_input, spec, tools.load())
    if choice == "allow":
        return {"behavior": "allow", "updatedInput": tool_input}
    if choice == "off":
        return _deny(f"{label} is switched off in the operator's tool settings")
    box = folder(ask_key)
    box.mkdir(parents=True, exist_ok=True)
    n = f"{time.time_ns()}"
    write_json(box / f"{n}.req.json", {"tool": tool, "label": label, "detail": detail(tool_input)})
    answer = box / f"{n}.ans.json"
    while not answer.is_file():
        time.sleep(POLL_S)
        if not box.is_dir():
            return _deny("the ask ended before the operator answered")
    try:
        got = read_json(answer)
    finally:
        for f in (answer, box / f"{n}.req.json"):
            f.unlink(missing_ok=True)
    if got.get("allow"):
        return {"behavior": "allow", "updatedInput": tool_input}
    return _deny(f"the operator denied {label}" + (f": {got['why']}" if got.get("why") else ""))


TOOL = {
    "name": "approve",
    "description": "Ask opsquire whether a tool call may run.",
    "inputSchema": {
        "type": "object",
        "properties": {"tool_name": {"type": "string"}, "input": {"type": "object"}, "tool_use_id": {"type": "string"}},
        "required": ["tool_name", "input"],
    },
}


def serve(ask_key: str, spec: dict[str, Any], inp: TextIO = sys.stdin, out: TextIO = sys.stdout) -> None:
    """A minimal MCP server over newline-delimited JSON-RPC on stdin and stdout."""

    def reply(msg_id: Any, result: Any = None, error: dict[str, Any] | None = None) -> None:
        body = {"jsonrpc": "2.0", "id": msg_id, **({"error": error} if error else {"result": result})}
        out.write(json.dumps(body) + "\n")
        out.flush()

    for line in inp:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method, msg_id = msg.get("method"), msg.get("id")
        if msg_id is None:
            continue
        params = msg.get("params") or {}
        if method == "initialize":
            reply(
                msg_id,
                {
                    "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": tools.OWN_SERVER, "version": "1"},
                },
            )
        elif method == "ping":
            reply(msg_id, {})
        elif method == "tools/list":
            reply(msg_id, {"tools": [TOOL]})
        elif method == "tools/call" and params.get("name") == "approve":
            args = params.get("arguments") or {}
            try:
                decision = judge(ask_key, spec, str(args.get("tool_name", "")), dict(args.get("input") or {}))
            except Exception as e:  # noqa: BLE001 - any failure here must deny, never allow
                decision = _deny(f"opsquire could not decide: {e}")
            reply(msg_id, {"content": [{"type": "text", "text": json.dumps(decision)}]})
        else:
            reply(msg_id, error={"code": -32601, "message": f"unknown method {method}"})


class Active:
    """The ask whose operator answers approvals right now ("" outside an ask, as for the judge, which then runs
    without the approval server and keeps the agent's own permissions)."""

    key = ""


def config_path(key: str, agent: str) -> Path:
    return folder(key) / f"{agent}.mcp.json"


def argv(spec: dict[str, Any], agent: str) -> list[str]:
    """The extra arguments for an agent in the active ask: the approval server with the home's added servers, and
    the servers switched off."""
    if not Active.key or not spec.get("approval_argv"):
        return []
    cfg = tools.load()
    path = config_path(Active.key, agent)
    cmd = [sys.executable, "-m", "opsquire", "approve-server", "--ask", Active.key, "--agent", agent]
    write_json(path, tools.mcp_config(cfg, cmd))
    out = [str(a).replace("{mcp_config}", str(path)) for a in spec["approval_argv"]]
    pattern = str(spec.get("server_tools", "{server}"))
    for sid in tools.disabled_servers(cfg):
        out += [str(a).replace("{server_tools}", pattern.replace("{server}", sid)) for a in spec.get("deny_argv") or []]
    return out


class Watcher:
    """Inside `opsquire ask`: turns the approval requests of one ask into questions for the operator, one at a time.
    Leaving it removes the ask's approvals folder, which also denies any request still waiting."""

    def __init__(self, ask_key: str, on_request: Any = None, on_answer: Any = None) -> None:
        self.key = ask_key
        self.box = folder(ask_key)
        self.stop_event = threading.Event()
        self.on_request, self.on_answer = on_request, on_answer
        self.thread = threading.Thread(target=self._loop, name="opsquire-approvals", daemon=True)

    def __enter__(self) -> Watcher:
        self.box.mkdir(parents=True, exist_ok=True)
        Active.key = self.key
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        Active.key = ""
        self.stop_event.set()
        self.thread.join(timeout=2)
        shutil.rmtree(self.box, ignore_errors=True)

    def _loop(self) -> None:
        while not self.stop_event.wait(POLL_S):
            for req in sorted(self.box.glob("*.req.json")) if self.box.is_dir() else []:
                answer = req.with_name(req.name.replace(".req.json", ".ans.json"))
                if answer.exists():
                    continue
                with contextlib.suppress(OSError, ValueError):
                    self._handle(req, answer, read_json(req))

    def _handle(self, req: Path, answer: Path, r: dict[str, Any]) -> None:
        question = f"Allow {r.get('label')}?" + (f" {r['detail']}" if r.get("detail") else "")
        if self.on_request:
            self.on_request(r)
        try:
            line = pinned.ask(question, "deny")
        except OSError:
            line = None
        allow = line is not None and line.strip().lower() in YES
        write_json(answer, {"allow": allow, "why": "" if line is not None else "no operator to answer"})
        if self.on_answer:
            self.on_answer(r, allow)
