"""`opsquire ui`: a local, read-mostly web view of the home on localhost. One page (data/ui.html) reads the state
(threads, tasks, runs, asks, scripts, locks) from /api/state, follows changes over /api/stream (server-sent events:
the server watches the home's meta.json, run.json and locks files), tails a run's output.log and shows a task's
design.md or script. Its one write is closing a task, the same code as `opsquire close`. Nothing runs from here:
run commands are shown to copy."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlparse

from .close import close_task
from .jobs import Jobs
from .model import OpsquireError, home_dir, iso, now, runs_dir, task_folder, var_dir
from .state import state

DEFAULT_PORT = 7771
WATCH_INTERVAL_S = 1.0
PING_INTERVAL_S = 15.0
MAX_LOG_BYTES = 512 * 1024
VIEWABLE = {"design.md", "main.py", "main.sh", "meta.json", "test_main.py"}


def page() -> bytes:
    return resources.files("opsquire").joinpath("data/ui.html").read_bytes()


def fingerprint() -> str:
    """Changes when any task meta, run record or lock changes: the cheapest signal that the picture moved."""
    home = home_dir()
    latest = 0.0
    count = 0
    for pattern, root in (("*/*/meta.json", home / "tasks"), ("*/meta.json", home / "general")):
        for p in root.glob(pattern):
            try:
                latest = max(latest, p.stat().st_mtime)
                count += 1
            except OSError:
                continue
    runs = runs_dir()
    if runs.is_dir():
        for p in runs.glob("*/*/run.json"):
            try:
                latest = max(latest, p.stat().st_mtime)
                count += 1
            except OSError:
                continue
    lock_file = var_dir() / "locks" / "locks.json"
    if lock_file.is_file():
        latest = max(latest, lock_file.stat().st_mtime)
    return f"{count}:{latest:.3f}"


def log_tail(run_id: str, offset: int | None, until: int | None = None) -> dict[str, Any]:
    """A slice of a run's output.log: the last MAX_LOG_BYTES when `offset` is None (first open), the bytes from
    `offset` on (tailing a running run), or the bytes in [offset, until) (loading earlier output). `start` says where
    the text begins so the page can ask for what precedes it."""
    root = runs_dir()
    found = [p for p in root.glob(f"*/{run_id}") if p.is_dir()] if root.is_dir() else []
    if not found:
        raise OpsquireError(f"no run {run_id}")
    log = found[0] / "output.log"
    status = None
    try:
        with (found[0] / "run.json").open() as f:
            status = json.load(f).get("status")
    except (OSError, ValueError):
        pass
    if not log.is_file():
        return {"text": "", "start": 0, "offset": 0, "status": status}
    size = log.stat().st_size
    start = max(0, size - MAX_LOG_BYTES) if offset is None else max(0, offset)
    stop = min(size, until) if until is not None else min(size, start + MAX_LOG_BYTES)
    with log.open("rb") as f:
        f.seek(start)
        data = f.read(max(0, stop - start))
    return {"text": data.decode("utf-8", "replace"), "start": start, "offset": start + len(data), "status": status}


def task_file(task_id: str, name: str) -> dict[str, Any]:
    if name not in VIEWABLE or "/" in task_id or task_id.startswith("."):
        raise OpsquireError(f"{name!r} is not a viewable file")
    folder = task_folder(task_id) or (home_dir() / "general" / task_id)
    if not folder.is_dir():
        raise OpsquireError(f"no task {task_id}")
    path = folder / name
    if not path.is_file():
        return {"text": None}
    return {"text": path.read_text(errors="replace")}


class Handler(BaseHTTPRequestHandler):
    server_version = "opsquire-ui"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        if os.environ.get("OPSQUIRE_UI_LOG"):
            super().log_message(format, *args)

    def send_json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            if url.path in ("/", "/index.html"):
                body = page()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/api/state":
                self.send_json({**state(), "jobs": self.jobs.views()})
            elif url.path == "/api/job":
                self.send_json(self.jobs.read(q.get("id", ""), int(q.get("offset") or 0)))
            elif url.path == "/api/log":
                offset, until = q.get("offset") or None, q.get("until") or None
                self.send_json(
                    log_tail(q.get("run", ""), int(offset) if offset else None, int(until) if until else None)
                )
            elif url.path == "/api/file":
                self.send_json(task_file(q.get("task", ""), q.get("name", "")))
            elif url.path == "/api/stream":
                self.stream()
            else:
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except OpsquireError as e:
            self.send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self) -> None:
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self.send_json({"error": "body must be JSON"}, HTTPStatus.BAD_REQUEST)
            return
        if not isinstance(body, dict):
            self.send_json({"error": "body must be a JSON object"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            if url.path == "/api/run":
                job = self.jobs.start(str(body.get("task", "")), str(body.get("mode", "")))
                self.send_json({"job_id": job.job_id})
            elif url.path == "/api/judge":
                job = self.jobs.start(str(body.get("task", "")), "judge")
                self.send_json({"job_id": job.job_id})
            elif url.path == "/api/answer":
                self.jobs.answer(str(body.get("job", "")), str(body.get("text", "")))
                self.send_json({"ok": True})
            elif url.path == "/api/interrupt":
                self.jobs.interrupt(str(body.get("job", "")))
                self.send_json({"ok": True})
            elif url.path == "/api/close":
                run = body.get("run")
                closed = close_task(
                    str(body.get("task", "")),
                    str(body.get("outcome", "")),
                    str(body.get("note", "")),
                    str(run) if run else None,
                )
                self.send_json(closed)
            else:
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except OpsquireError as e:
            busy = "already has a job" in str(e)
            self.send_json({"error": str(e)}, HTTPStatus.CONFLICT if busy else HTTPStatus.BAD_REQUEST)

    @property
    def jobs(self) -> Jobs:
        server: UIServer = self.server  # type: ignore[assignment]
        return server.jobs

    def stream(self) -> None:
        """Server-sent events: `change` when the home moved, `ping` to keep the connection alive."""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        server: UIServer = self.server  # type: ignore[assignment]
        seen = fingerprint()
        last_ping = time.monotonic()
        self.wfile.write(f"event: hello\ndata: {json.dumps({'generated': iso(now())})}\n\n".encode())
        self.wfile.flush()
        while not server.stopping.is_set():
            moved = server.jobs.changed.wait(WATCH_INTERVAL_S)
            current = fingerprint()
            if current != seen or moved:
                server.jobs.changed.clear()
                seen = current
                self.wfile.write(f"event: change\ndata: {json.dumps({'generated': iso(now())})}\n\n".encode())
                self.wfile.flush()
                last_ping = time.monotonic()
            elif time.monotonic() - last_ping > PING_INTERVAL_S:
                self.wfile.write(b"event: ping\ndata: {}\n\n")
                self.wfile.flush()
                last_ping = time.monotonic()


class UIServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, Handler)
        self.stopping = threading.Event()
        self.jobs = Jobs()


def serve(port: int = DEFAULT_PORT, host: str = "127.0.0.1", on_ready: Callable[[], None] | None = None) -> None:
    """Serve until interrupted. Binds localhost only: the page can close tasks, and the home is the operator's."""
    try:
        server = UIServer((host, port))
    except OSError as e:
        raise OpsquireError(f"cannot listen on {host}:{port}: {e.strerror or e}") from None
    if on_ready is not None:
        on_ready()
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.stopping.set()
        server.jobs.stop_all()
        server.server_close()


def url(port: int, host: str = "127.0.0.1") -> str:
    return f"http://{host}:{port}/"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
