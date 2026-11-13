"""`opsquire ui`: the operator's console on localhost. One page (data/ui.html) reads the state (threads, tasks, runs,
asks, scripts, locks) from /api/state and follows changes over /api/stream (server-sent events: one watcher thread
looks at every file of the home's tasks, general scripts, runs, locks, asks and thread results), tails a run's
output.log and shows a task's design.md or script. Its writes are closing a task (the same code as `opsquire close`),
runs started from the page (jobs.py) and which chats the operator has read (chatread.py). A running server keeps the
Python it started with, so it also compares that code with what is on disk and installed now, and the page tells the
operator to restart it when they differ."""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
import traceback
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import metadata, resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__, chatread, tools
from .close import close_task
from .jobs import Jobs
from .model import (
    IGNORED_PARTS,
    PACKAGE_DIR,
    OpsquireError,
    home_dir,
    iso,
    now,
    runs_dir,
    task_folder,
    var_dir,
    write_json,
)
from .snapshots import Snapshots
from .state import state

DEFAULT_PORT = 7771
WATCH_INTERVAL_S = 1.0
HEALTH_EVERY_S = 5.0
HEALTH_STALE_S = 20.0
PING_INTERVAL_S = 15.0
MAX_LOG_BYTES = 512 * 1024
VIEWABLE = {"design.md", "main.py", "main.sh", "verify.py", "verify.sh", "meta.json", "test_main.py"}
PAGE = "data/ui.html"


def page() -> bytes:
    return resources.files("opsquire").joinpath("data/ui.html").read_bytes()


def fingerprint() -> str:
    """Changes when anything the page shows may have moved: any file of a task or general script (its lint stamp
    and scripts included, since they decide draft or ready), run records, locks, asks and thread results. A new run is
    a new folder, which changes its task's runs folder; a finished run's record never changes again, so only each
    task's latest one is read."""
    home, var = home_dir(), var_dir()
    paths: list[str] = [str(chatread.path())]
    for root in (home / "tasks", home / "general", home / "threads", var / "asks", var / "locks"):
        for folder, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in IGNORED_PARTS]
            paths += (os.path.join(folder, f) for f in files)
    for task_runs in runs_dir().glob("*/"):
        paths.append(str(task_runs))
        latest = max(task_runs.glob("*/run.json"), default=None, key=lambda p: p.parent.name)
        if latest:
            paths.append(str(latest))
    h = hashlib.sha256()
    for path in sorted(paths):
        try:
            st = os.stat(path)
        except OSError:
            continue
        h.update(f"{path}\0{st.st_mtime_ns}\0{st.st_size}\n".encode())
    return h.hexdigest()


def code_hashes() -> dict[str, str]:
    """The server's code as it is on disk now: the package's modules and the page, by content, so a touch or a checkout
    that lands back on the same code is no change. Helpers and templates are read fresh by every run, so they never go
    stale in a running server and are left out."""
    hashes: dict[str, str] = {}
    for path in [*PACKAGE_DIR.glob("*.py"), PACKAGE_DIR / PAGE]:
        try:
            hashes[path.relative_to(PACKAGE_DIR).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return hashes


def installed_version() -> str:
    """The version installed now; __version__ is the one this process read when it started."""
    try:
        return metadata.version("opsquire")
    except metadata.PackageNotFoundError:
        return __version__


def code_status(loaded: dict[str, str]) -> dict[str, Any]:
    """How the code the server started with (`loaded`) compares with what is there now. `page` is the page a reload
    would get, so a tab can tell it is older than that."""
    current, installed = code_hashes(), installed_version()
    changed = sorted(name for name in loaded.keys() | current.keys() if loaded.get(name) != current.get(name))
    return {
        "stale": bool(changed) or installed != __version__,
        "changed": changed,
        "version": __version__,
        "installed": installed,
        "page": current.get(PAGE),
    }


def tools_spec() -> dict[str, Any]:
    from .ask import pick_agent

    spec: dict[str, Any] = pick_agent(None)[1].get("tools") or {}
    return spec


LISTED: dict[str, Any] = {"servers": [], "at": ""}


def tools_view(refresh: bool) -> dict[str, Any]:
    """Settings: the default agent's servers (listed by its CLI on refresh, which can take seconds) with the rules."""
    spec = tools_spec()
    if refresh:
        LISTED.update(servers=tools.listed_servers(spec), at=iso(now()))
    return {**tools.view(spec, LISTED["servers"]), "listed_at": LISTED["at"]}


def ask_chat(ask_id: str, offset: int) -> dict[str, Any]:
    """The conversation an ask belongs to, from line `offset` on, for the console's chat. A follow-up writes into
    its conversation's chat (the first ask's), so any ask of it reads the whole conversation."""
    if not re.fullmatch(r"[0-9A-Za-z_.-]+", ask_id):
        raise OpsquireError("bad ask id")
    folder = var_dir() / "asks"
    with contextlib.suppress(OSError, ValueError, AttributeError):
        conv = str(json.loads((folder / f"{ask_id}.json").read_text()).get("copies") or ask_id)
        if re.fullmatch(r"[0-9A-Za-z_.-]+", conv):
            ask_id = conv
    path = folder / f"{ask_id}.chat.jsonl"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        lines = []
    items = []
    for line in lines[offset:]:
        with contextlib.suppress(ValueError):
            items.append(json.loads(line))
    return {"items": items, "offset": len(lines)}


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
        zipped = len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or "")
        if zipped:
            body = gzip.compress(body, compresslevel=5)
        self.send_response(status)
        if zipped:
            self.send_header("Content-Encoding", "gzip")
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
                server: UIServer = self.server  # type: ignore[assignment]
                try:
                    answer = server.snapshots.answer(server.look(), q.get("since", ""))
                except Exception as e:
                    server.health["state_error"] = f"{type(e).__name__}: {e}"
                    raise
                extra = {
                    "jobs": self.jobs.views(),
                    "start_problems": self.jobs.problems,
                    "code": code_status(server.code),
                }
                self.send_json({**answer, **extra})
            elif url.path == "/api/tools":
                self.send_json(tools_view(q.get("refresh") == "1"))
            elif url.path == "/api/askchat":
                self.send_json(ask_chat(q.get("id", ""), int(q.get("offset") or 0)))
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
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            self.send_json({"error": f"{type(e).__name__}: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

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
                before = set(self.jobs.jobs)
                expect = body.get("expect")
                if expect is not None and not (isinstance(expect, list) and all(isinstance(x, str) for x in expect)):
                    raise OpsquireError("expect must be a list of strings")
                job = self.jobs.start(str(body.get("task", "")), str(body.get("mode", "")), expect)
                self.send_json({"job_id": job.job_id, "joined": job.job_id in before})
            elif url.path == "/api/judge":
                job = self.jobs.start(str(body.get("task", "")), "judge")
                self.send_json({"job_id": job.job_id})
            elif url.path == "/api/ask":
                job = self.jobs.start_ask(
                    str(body.get("request", "")),
                    str(body.get("resume", "")),
                    str(body.get("model", "")),
                    str(body.get("agent", "")),
                )
                self.send_json({"job_id": job.job_id})
            elif url.path == "/api/tools":
                tools.update(body, tools_spec())
                self.send_json(tools_view(False))
            elif url.path == "/api/answer":
                self.jobs.answer(str(body.get("job", "")), str(body.get("text", "")))
                self.send_json({"ok": True})
            elif url.path == "/api/interrupt":
                self.jobs.interrupt(str(body.get("job", "")))
                self.send_json({"ok": True})
            elif url.path == "/api/chatread":
                from .state import load_asks

                chatread.mark(str(body.get("conversation", "")), load_asks())
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
            busy = "job running" in str(e) or "too many jobs" in str(e)
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
        seen = server.generation
        self.wfile.write(f"event: hello\ndata: {json.dumps({'generated': iso(now())})}\n\n".encode())
        self.wfile.flush()
        while not server.stopping.is_set():
            current = server.wait_change(seen, PING_INTERVAL_S)
            if current != seen:
                seen = current
                version = server.snapshots.version(current)
                self.wfile.write(f"event: change\ndata: {json.dumps({'version': version})}\n\n".encode())
            else:
                self.wfile.write(b"event: ping\ndata: {}\n\n")
            self.wfile.flush()


class UIServer(ThreadingHTTPServer):
    """Serves the page; one watcher thread notices when the home, a job or the code on disk moved and bumps
    `generation`, which every open stream waits on, so no stream misses a change another one saw."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, Handler)
        self.stopping = threading.Event()
        self.jobs = Jobs()
        self.code = code_hashes()
        chatread.start()
        self.generation = 0
        self.followed = -1
        self.seen: tuple[str, dict[str, Any]] | None = None
        self.moved = threading.Condition()
        self.health: dict[str, Any] = {"state_s": None, "state_at": None, "state_error": ""}
        self.snapshots = Snapshots(self.timed_state)
        self.started = iso(now())
        self.beaten = 0.0
        threading.Thread(target=self.watch, daemon=True).start()

    def timed_state(self) -> dict[str, Any]:
        t0 = time.monotonic()
        built = state()
        self.health.update(state_s=round(time.monotonic() - t0, 3), state_at=iso(now()), state_error="")
        self.beaten = 0.0
        if self.seen:
            self.beat(self.seen[1])
        return built

    def health_file(self) -> Path:
        return var_dir() / "ui" / f"{self.server_address[1]}.json"

    def beat(self, code: dict[str, Any]) -> None:
        """What `opsquire doctor` reads to tell whether this console is up and current, from any shell: the agent
        sandboxes that cannot open its port read this file instead."""
        if time.monotonic() - self.beaten < HEALTH_EVERY_S:
            return
        self.beaten = time.monotonic()
        host, port = str(self.server_address[0]), self.server_address[1]
        record = {
            "pid": os.getpid(),
            "url": f"http://{host}:{port}/",
            "started": self.started,
            "beat": iso(now()),
            "stale_code": code["changed"],
            "jobs": len([j for j in self.jobs.views() if j.get("status") != "done"]),
            **self.health,
        }
        with contextlib.suppress(OSError):
            self.health_file().parent.mkdir(parents=True, exist_ok=True)
            write_json(self.health_file(), record)

    def watch(self) -> None:
        while not self.stopping.is_set():
            job_moved = self.jobs.changed.wait(WATCH_INTERVAL_S)
            self.jobs.changed.clear()
            try:
                seen = self.look(force=job_moved)
                if seen != self.followed:
                    self.followed = seen
                    self.jobs.follow_up()
            except Exception:
                traceback.print_exc(file=sys.stderr)

    def look(self, force: bool = False) -> int:
        """Bump `generation` when the home, a job or the code moved since the last look, and return it. The watcher
        looks every second; a state request looks first too, so it never answers with a home older than itself."""
        current = (fingerprint(), code_status(self.code))
        self.beat(current[1])
        with self.moved:
            if force or current != self.seen:
                self.seen = current
                self.generation += 1
                self.moved.notify_all()
            return self.generation

    def wait_change(self, after: int, timeout: float) -> int:
        with self.moved:
            self.moved.wait_for(lambda: self.generation != after or self.stopping.is_set(), timeout)
            return self.generation

    def stop(self) -> None:
        self.stopping.set()
        with contextlib.suppress(OSError):
            self.health_file().unlink()
        with self.moved:
            self.moved.notify_all()


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
        server.stop()
        server.jobs.stop_all()
        server.server_close()


def url(port: int, host: str = "127.0.0.1") -> str:
    return f"http://{host}:{port}/"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
