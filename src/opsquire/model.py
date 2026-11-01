from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = 1
PACKAGE_DIR = Path(__file__).resolve().parent
DATA_DIR = PACKAGE_DIR / "data"
HELPERS_DIR = DATA_DIR / "helpers"
TEMPLATES_DIR = DATA_DIR / "templates"
DEFAULT_HOME = Path.home() / ".local" / "share" / "opsquire"

TASK_ID_RE = re.compile(r"^\d{8}-\d{4}_[a-z0-9][a-z0-9-]*_[a-z0-9][a-z0-9-]*$")
GENERAL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*_[a-z0-9][a-z0-9-]*$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
LOCK_MODES = ("exclusive", "shared", "none")
INTERPRETERS = {".py": [sys.executable, "-u"], ".sh": ["/bin/bash"]}
IGNORED_PARTS = {"__pycache__", ".mypy_cache", ".ruff_cache"}
LINT_FILE = ".lint.json"


class OpsquireError(Exception):
    pass


HEARTBEAT_FILE = "heartbeat"
HEARTBEAT_EVERY = 5.0
HEARTBEAT_STALE = 30.0


class Heartbeat:
    """Refreshes <run dir>/heartbeat while a run is in progress, so any reader (even in another PID namespace,
    like an agent sandbox) can tell a live run from a dead one."""

    def __init__(self, run_dir: Path) -> None:
        self.path = run_dir / HEARTBEAT_FILE
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.beat, daemon=True)

    def beat(self) -> None:
        while True:
            with contextlib.suppress(OSError):
                self.path.write_text(iso(now()) + "\n")
            if self.stop.wait(HEARTBEAT_EVERY):
                return

    def __enter__(self) -> Heartbeat:
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop.set()
        self.thread.join(timeout=2)


def pid_namespace() -> str:
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return ""


def run_alive(pid: int, host: str, run_dir: Path, pid_ns: str = "") -> bool:
    """Same host and PID namespace: ask the kernel (instant, exact). Otherwise (e.g. reading from an agent sandbox):
    trust the heartbeat; runs from before heartbeats existed fall back to the pid check."""
    if host != socket.gethostname():
        return True
    beat = run_dir / HEARTBEAT_FILE
    if beat.exists() and pid_ns != pid_namespace():
        try:
            return time.time() - beat.stat().st_mtime < HEARTBEAT_STALE
        except OSError:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "opsquire" / "config.json"


def home_dir() -> Path:
    env = os.environ.get("OPSQUIRE_HOME")
    if env:
        return Path(env).expanduser().resolve()
    try:
        configured = read_json(config_path()).get("home")
    except (OSError, ValueError, AttributeError):
        configured = None
    return Path(configured).expanduser().resolve() if isinstance(configured, str) else DEFAULT_HOME


def main_home() -> Path:
    """The operator's home even inside a parallel piece's worktree (whose OPSQUIRE_VAR points back at it)."""
    env = os.environ.get("OPSQUIRE_VAR")
    return Path(env).parent if env else home_dir()


def var_dir() -> Path:
    """Run records live in the main home; agents of a parallel ask work in worktrees and get OPSQUIRE_VAR."""
    env = os.environ.get("OPSQUIRE_VAR")
    return Path(env) if env else home_dir() / "var"


def runs_dir() -> Path:
    return var_dir() / "runs"


def locks_dir() -> Path:
    return var_dir() / "locks"


def creds_path() -> Path:
    return Path(os.environ.get("OPSQUIRE_CREDS", str(Path.home() / ".creds")))


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def compact(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%S") + f"{t.microsecond // 1000:03d}Z"


def parse_iso(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt.timezone.utc)


def read_json(path: Path) -> Any:
    with path.open() as f:
        return json.load(f)


def write_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_json(path: Path, data: Any) -> None:
    write_text(path, json.dumps(data, indent=2, sort_keys=False) + "\n")


@dataclass
class Meta:
    id: str
    kind: str
    created: str
    author: str
    purpose: str
    area: str
    entry: str
    changes_prod: bool
    targets: list[str]
    lock: str
    lock_reason: str
    credentials: dict[str, str]
    uses: list[str]
    rollback: str
    links: list[str] = field(default_factory=list)
    superseded_by: str | None = None
    abandoned_reason: str | None = None
    archived: dict[str, Any] | None = None
    interpreter: str | None = None
    steps: list[dict[str, Any]] | None = None

    def command(self, entry: Path) -> list[str]:
        if self.interpreter and entry.suffix == ".py":
            return [str(Path(self.interpreter).expanduser()), "-u"]
        return list(INTERPRETERS[entry.suffix])

    @property
    def modes(self) -> list[str]:
        return ["dry", "apply"] if self.changes_prod else ["read"]

    def to_json(self) -> dict[str, Any]:
        return {"schema": SCHEMA, **self.__dict__}


REQUIRED: dict[str, type] = {
    "id": str,
    "kind": str,
    "created": str,
    "author": str,
    "purpose": str,
    "area": str,
    "entry": str,
    "changes_prod": bool,
    "targets": list,
    "lock": str,
    "lock_reason": str,
    "credentials": dict,
    "uses": list,
    "rollback": str,
}


def validate_meta(raw: Any, folder: Path) -> tuple[Meta | None, list[str]]:
    errors: list[str] = []
    if not isinstance(raw, dict):
        return None, ["meta.json must be a JSON object"]
    if raw.get("schema") != SCHEMA:
        errors.append(f"schema must be {SCHEMA}")
    for key, typ in REQUIRED.items():
        if not isinstance(raw.get(key), typ):
            errors.append(f"{key} missing or not {typ.__name__}")
    if errors:
        return None, errors
    meta = Meta(
        **{k: raw[k] for k in REQUIRED},
        links=list(raw.get("links") or []),
        superseded_by=raw.get("superseded_by"),
        abandoned_reason=raw.get("abandoned_reason"),
        archived=raw.get("archived"),
        interpreter=raw.get("interpreter"),
        steps=raw.get("steps"),
    )
    if meta.archived is not None and not (
        isinstance(meta.archived, dict)
        and isinstance(meta.archived.get("note"), str)
        and meta.archived["note"].strip()
        and isinstance(meta.archived.get("history"), list)
    ):
        errors.append("archived must be an object with a non-empty note and a history list")
    if meta.interpreter is not None:
        interp = Path(str(meta.interpreter)).expanduser()
        if not isinstance(meta.interpreter, str) or not meta.entry.endswith(".py"):
            errors.append("interpreter is a path string and only applies to main.py tasks")
        elif not (interp.is_file() and os.access(interp, os.X_OK)):
            errors.append(f"interpreter {meta.interpreter!r} is not an executable file")
    if meta.kind not in ("task", "general", "plan"):
        errors.append("kind must be task, general or plan")
    id_re = GENERAL_ID_RE if meta.kind == "general" else TASK_ID_RE
    if not id_re.match(meta.id):
        errors.append(f"id {meta.id!r} does not match the {meta.kind} naming convention")
    elif meta.id.split("_")[-2] != meta.area:
        errors.append(f"area {meta.area!r} differs from the area in the id")
    if meta.id != folder.name:
        errors.append(f"id {meta.id!r} differs from folder name {folder.name!r}")
    try:
        parse_iso(meta.created)
    except ValueError:
        errors.append("created must look like 2026-10-30T17:20:00.000Z")
    if not meta.purpose.strip():
        errors.append("purpose is empty")
    if not meta.rollback.strip():
        errors.append('rollback is empty (use "read-only" when nothing changes)')
    if meta.kind == "plan":
        errors += validate_plan(meta)
    else:
        entry = folder / meta.entry
        if not entry.is_file():
            errors.append(f"entry {meta.entry!r} not found")
        elif entry.suffix not in INTERPRETERS:
            errors.append(f"entry must end in one of {sorted(INTERPRETERS)}")
        if meta.steps is not None:
            errors.append("steps are only allowed on plans")
    if not meta.targets or not all(isinstance(t, str) and re.match(r"^[a-z0-9][a-z0-9_.-]*$", t) for t in meta.targets):
        errors.append("targets must be a non-empty list of lowercase names (hosts, books, systems)")
    if meta.lock not in LOCK_MODES:
        errors.append(f"lock must be one of {LOCK_MODES}")
    if meta.lock == "none" and not meta.lock_reason.strip():
        errors.append("lock none needs a lock_reason")
    if meta.changes_prod and meta.lock == "shared" and not meta.lock_reason.strip():
        errors.append("a prod-changing task with a shared lock needs a lock_reason")
    for env, key in meta.credentials.items():
        if not ENV_NAME_RE.match(env) or not isinstance(key, str) or not key:
            errors.append(f"credentials entry {env!r} must map an UPPER_CASE env name to a dotted key")
    for use in meta.uses:
        if not isinstance(use, str) or not (home_dir() / "general" / use / "meta.json").is_file():
            errors.append(f"uses {use!r} is not a general script")
    return (meta if not errors else None), errors


STEP_KEYS = {"task": str, "args": list, "on_failure": str, "verify": bool}


def validate_plan(meta: Meta) -> list[str]:
    errors: list[str] = []
    if meta.entry:
        errors.append('a plan has no entry script; set entry to ""')
    if meta.credentials or meta.uses or meta.interpreter:
        errors.append("credentials, uses and interpreter belong on the step tasks, not the plan")
    if not isinstance(meta.steps, list) or not meta.steps:
        return errors + ["a plan needs a non-empty steps list"]
    targets: set[str] = set()
    changes = False
    for i, step in enumerate(meta.steps, 1):
        if not isinstance(step, dict) or not isinstance(step.get("task"), str):
            errors.append(f"step {i} must be an object with a task id")
            continue
        for key, value in step.items():
            if key not in STEP_KEYS or not isinstance(value, STEP_KEYS[key]):
                errors.append(f"step {i}: {key!r} is not one of {sorted(STEP_KEYS)} with the right type")
        for field_name in ("task", "on_failure"):
            ref = step.get(field_name)
            if not isinstance(ref, str):
                continue
            folder = task_folder(ref)
            if folder and peek_kind(folder) == "plan":
                errors.append(f"step {i}: {ref!r} must be a live task, not a plan (plans do not nest)")
                continue
            sub = load_task(folder) if folder else None
            if sub is None or sub.meta is None:
                errors.append(f"step {i}: {field_name} {ref!r} is not a valid task (use the full task id)")
            elif sub.meta.kind != "task" or sub.meta.archived or sub.meta.superseded_by or sub.meta.abandoned_reason:
                errors.append(f"step {i}: {ref!r} must be a live task, not a {sub.meta.kind} or retired task")
            elif field_name == "task":
                targets |= set(sub.meta.targets)
                changes |= sub.meta.changes_prod
    if not errors:
        if sorted(meta.targets) != sorted(targets):
            errors.append(f"plan targets must be the union of its steps' targets: {sorted(targets)}")
        if meta.changes_prod != changes:
            errors.append(f"plan changes_prod must be {changes} (true if any step changes prod)")
    return errors


def peek_kind(folder: Path) -> str:
    try:
        raw = read_json(folder / "meta.json")
    except (OSError, ValueError):
        return ""
    return str(raw.get("kind", "")) if isinstance(raw, dict) else ""


def task_folder(task_id: str) -> Path | None:
    matches = [p for p in (home_dir() / "tasks").glob(f"*/{task_id}") if p.is_dir()]
    return matches[0] if len(matches) == 1 else None


@dataclass
class Task:
    folder: Path
    raw: dict[str, Any]
    meta: Meta | None
    errors: list[str]

    @property
    def id(self) -> str:
        return self.folder.name

    @property
    def kind(self) -> str:
        if self.folder.parent.name == "general":
            return "general"
        return "plan" if self.raw.get("kind") == "plan" else "task"

    def files(self) -> list[Path]:
        roots = [self.folder]
        if self.meta:
            roots += [home_dir() / "general" / u for u in self.meta.uses]
            for step in self.meta.steps or []:
                folder = task_folder(str(step.get("task")))
                if folder:
                    roots.append(folder)
        out: list[Path] = []
        for root in roots:
            for p in sorted(root.rglob("*")):
                if p.is_file() and p.name != LINT_FILE and not (IGNORED_PARTS & set(p.parts)):
                    out.append(p)
        return out

    def file_hashes(self) -> dict[str, str]:
        home = home_dir()
        return {str(p.relative_to(home)): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.files()}

    def content_hash(self) -> str:
        h = hashlib.sha256()
        for name, digest in self.file_hashes().items():
            h.update(f"{name}\0{digest}\n".encode())
        return h.hexdigest()


def load_task(folder: Path) -> Task:
    meta_path = folder / "meta.json"
    try:
        raw = read_json(meta_path)
    except (OSError, json.JSONDecodeError) as e:
        return Task(folder, {}, None, [f"meta.json unreadable: {e}"])
    meta, errors = validate_meta(raw, folder)
    return Task(folder, raw if isinstance(raw, dict) else {}, meta, errors)


def all_task_folders() -> list[Path]:
    home = home_dir()
    tasks = sorted(p for p in (home / "tasks").glob("*/*") if p.is_dir())
    general = sorted(p for p in (home / "general").glob("*") if p.is_dir())
    return tasks + general


def resolve_task(query: str) -> Task:
    folders = all_task_folders()
    q = query.rstrip("/").split("/")[-1]
    exact = [f for f in folders if f.name == q]
    if exact:
        return load_task(exact[0])
    matches = [f for f in folders if q in f.name]
    if len(matches) == 1:
        return load_task(matches[0])
    if not matches:
        raise OpsquireError(f"no task matches {query!r}")
    names = "\n  ".join(m.name for m in matches)
    raise OpsquireError(f"{query!r} matches several tasks:\n  {names}")
