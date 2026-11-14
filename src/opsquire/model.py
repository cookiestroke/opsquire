from __future__ import annotations

import contextlib
import datetime as dt
import functools
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
IGNORED_PARTS = {"__pycache__", ".mypy_cache", ".ruff_cache", ".claude"}
LINT_FILE = ".lint.json"
OFFBOARDED = "offboarded"
ORIGIN_ENV = "OPSQUIRE_ORIGIN"
ORIGIN_CONSOLE = "console"
ORIGIN_TERMINAL = "terminal"


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


def run_origin() -> str:
    """Where this run was started, for its record: the console's jobs say so in the environment; anything else is a
    command line. OPSQUIRE_AUTHOR cannot tell them apart, since a console started with it set passes it on."""
    return ORIGIN_CONSOLE if os.environ.get(ORIGIN_ENV) == ORIGIN_CONSOLE else ORIGIN_TERMINAL


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


GENERIC_AUTHOR_WORDS = {"claude", "code", "codex", "chat", "opsquire", "ask", "migrated", "by", "unknown", "test"}


def author_key(author: str) -> str:
    """What identifies the session in an author string, or "" when it names only the tool ("claude-code (chat)")."""
    text = re.sub(r"\d{4}-\d{2}-\d{2}", " ", author.lower())
    words = [w for w in re.findall(r"[a-z0-9]+", text) if w not in GENERIC_AUTHOR_WORDS]
    return " ".join(words)


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "opsquire" / "config.json"


def update_config(**values: Any) -> None:
    """Merge values into the config file (home, ask budget), creating it when needed."""
    try:
        current = read_json(config_path())
    except (OSError, ValueError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    config_path().parent.mkdir(parents=True, exist_ok=True)
    write_json(config_path(), {**current, **values})


@functools.lru_cache(maxsize=16)
def resolved(path: str) -> Path:
    return Path(path).expanduser().resolve()


def home_dir() -> Path:
    env = os.environ.get("OPSQUIRE_HOME")
    if env:
        return resolved(env)
    try:
        configured = by_file("config", config_path(), functools.partial(read_json, config_path())).get("home")
    except (OSError, ValueError, AttributeError):
        configured = None
    return resolved(configured) if isinstance(configured, str) else DEFAULT_HOME


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
    return dt.datetime.now(dt.UTC)


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def compact(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%S") + f"{t.microsecond // 1000:03d}Z"


def parse_iso(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt.UTC)


DEFAULT_TIME_ZONE = "UTC"


def valid_time_zone(name: object) -> bool:
    if name in ("UTC", "local"):
        return True
    if not isinstance(name, str) or not name:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def configured_time_zone() -> str:
    """Where people read times: `time_zone` in the config file, `UTC`, `local` (this machine's zone) or a zone name
    such as `America/New_York`. Records and JSON stay in UTC whatever it says."""
    try:
        value = read_json(config_path()).get("time_zone")
    except (OSError, ValueError, AttributeError):
        value = None
    return str(value) if valid_time_zone(value) else DEFAULT_TIME_ZONE


def in_zone(t: dt.datetime, zone: str) -> dt.datetime:
    if zone == "UTC":
        return t.astimezone(dt.UTC)
    if zone == "local":
        return t.astimezone()
    return t.astimezone(ZoneInfo(zone))


def zone_label(t: dt.datetime) -> str:
    return "UTC" if t.utcoffset() == dt.timedelta(0) and t.tzname() in ("UTC", "GMT", None) else str(t.tzname())


def shown(stamp: object, zone: str, seconds: bool = True) -> str:
    """A recorded UTC time as people read it, in `zone`, with the zone named; anything unparsable as given."""
    text = str(stamp or "")
    try:
        t = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.UTC)
    t = in_zone(t, zone)
    return f"{t:%Y-%m-%d %H:%M}" + (f":{t:%S}" if seconds else "") + f" {zone_label(t)}"


def wall_clock(day: dt.datetime, hour: int, minute: int, zone: str) -> dt.datetime:
    """`hour:minute` on the date of `day` in `zone`, as an aware time."""
    local = in_zone(day, zone)
    return attach_zone(dt.datetime(local.year, local.month, local.day, hour, minute), zone)


def attach_zone(naive: dt.datetime, zone: str) -> dt.datetime:
    """A time typed without a zone, read in `zone`."""
    if zone == "UTC":
        return naive.replace(tzinfo=dt.UTC)
    if zone == "local":
        return naive.astimezone()
    return naive.replace(tzinfo=ZoneInfo(zone))


def read_json(path: Path) -> Any:
    with path.open() as f:
        return json.load(f)


def write_text(path: Path, text: str) -> None:
    """Atomic: a temporary file of this thread's own, then a rename, so threads writing one file never share it."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(text)
        tmp.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
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
    outcome: dict[str, Any] | None = None
    thread: str | None = None
    verify: str | None = None
    stages: list[str] | None = None

    def command(self, entry: Path) -> list[str]:
        if self.interpreter and entry.suffix == ".py":
            return [str(Path(self.interpreter).expanduser()), "-u"]
        return list(INTERPRETERS[entry.suffix])

    @property
    def modes(self) -> list[str]:
        if not self.changes_prod:
            return ["read"]
        return ["dry", "apply", "verify"] if self.verify else ["dry", "apply"]

    def to_json(self) -> dict[str, Any]:
        return {"schema": SCHEMA, **self.__dict__}


STAGE_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")


def stage_of(meta: Meta, argv: list[str]) -> str | None:
    """The stage a run with these arguments belongs to: its first argument, or the first stage when it has none."""
    if not meta.stages:
        return None
    if not argv:
        return meta.stages[0]
    return argv[0] if argv[0] in meta.stages else None


def is_last_stage(meta: Meta, argv: list[str]) -> bool:
    return not meta.stages or stage_of(meta, argv) == meta.stages[-1]


def next_stage(meta: Meta, runs: list[dict[str, Any]]) -> str | None:
    """The stage the task's next run is for: the one after its latest ok apply, the first when none applied yet, and
    the last once the last applied."""
    if not meta.stages:
        return None
    for r in reversed(runs):
        if r.get("mode") == "apply" and r.get("status") == "ok":
            done = stage_of(meta, [str(x) for x in r.get("argv") or []])
            if done is not None:
                i = meta.stages.index(done)
                return meta.stages[min(i + 1, len(meta.stages) - 1)]
    return meta.stages[0]


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
        outcome=raw.get("outcome"),
        thread=raw.get("thread"),
        verify=raw.get("verify"),
        stages=raw.get("stages"),
    )
    if meta.stages is not None:
        names = meta.stages
        good = isinstance(names, list) and all(isinstance(n, str) and STAGE_RE.match(n) for n in names)
        if not good or len(names) < 2 or len(set(names)) != len(names) or not meta.changes_prod:
            errors.append(
                'stages names the first argument of each stage of a changing task, in order: ["build", "deploy"]'
            )
    if meta.verify is not None:
        check = folder / str(meta.verify)
        if not isinstance(meta.verify, str) or not meta.changes_prod:
            errors.append("verify names the check script of a changing task")
        elif not check.is_file() or check.suffix not in INTERPRETERS:
            errors.append(f"verify {meta.verify!r} not found or not .py/.sh")
    if meta.thread is not None and not (isinstance(meta.thread, str) and THREAD_RE.match(meta.thread)):
        errors.append("thread must be a lowercase kebab-case name of at most 40 characters")
    if meta.outcome is not None:
        errors += validate_outcome(meta.outcome)
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
            gone = isinstance(use, str) and (home_dir() / OFFBOARDED / "general" / use).is_dir()
            hint = f" (it is offboarded: `opsquire restore {use}` brings it back)" if gone else ""
            errors.append(f"uses {use!r} is not a general script{hint}")
    return (meta if not errors else None), errors


OUTCOME_STATES = ("done", "partial", "abandoned")
THREAD_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def decision(raw: dict[str, Any]) -> dict[str, Any] | None:
    """The recorded decision that no run of this task is coming (abandoned or superseded): who, when and why. None
    for an open task, a task that ended done or partial, and a supersession written by hand with no record."""
    outcome = raw.get("outcome")
    if raw.get("abandoned_reason") and isinstance(outcome, dict) and outcome.get("state") == "abandoned":
        return {
            "state": "abandoned",
            "by": str(outcome.get("closed_by") or "unknown"),
            "at": str(outcome.get("closed") or ""),
            "note": str(outcome.get("note") or ""),
        }
    record = raw.get("superseded")
    if raw.get("superseded_by") and isinstance(record, dict):
        return {
            "state": "superseded",
            "by": str(record.get("by") or "unknown"),
            "at": str(record.get("at") or ""),
            "note": str(record.get("note") or ""),
            "superseded_by": str(raw["superseded_by"]),
        }
    return None


def decision_text(task_id: str, d: dict[str, Any]) -> str:
    what = f"superseded by {d['superseded_by']}" if d["state"] == "superseded" else "abandoned"
    return f"{task_id} was {what} ({d['by']}, {d['at']})" + (f": {d['note']}" if d["note"] else "")


def validate_outcome(outcome: Any) -> list[str]:
    """Errors in a task's closing `outcome` (written by `opsquire close`): a state, a note, who and when."""
    if not isinstance(outcome, dict):
        return ["outcome must be an object"]
    errors: list[str] = []
    if outcome.get("state") not in OUTCOME_STATES:
        errors.append(f"outcome.state must be one of {OUTCOME_STATES}")
    for key in ("note", "closed_by", "closed"):
        if not isinstance(outcome.get(key), str) or not outcome[key].strip():
            errors.append(f"outcome.{key} missing or empty")
    return errors


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
        """Content hashes of every file that runs. meta.json is hashed without its annotations (`outcome`,
        `judged`): closing or judging a task does not change what it does, so it must not make it draft."""
        home = home_dir()
        return {
            str(p.relative_to(home)): by_file("sha256", p, functools.partial(content_digest, p)) for p in self.files()
        }

    def content_hash(self) -> str:
        h = hashlib.sha256()
        for name, digest in self.file_hashes().items():
            h.update(f"{name}\0{digest}\n".encode())
        return h.hexdigest()


ANNOTATIONS = ("outcome", "judged", "handover", "hold")


FILE_CACHE: dict[tuple[str, str], tuple[tuple[int, int], Any]] = {}


def by_file[T](kind: str, path: Path, build: Callable[[], T]) -> T:
    """What build() derives from one file, computed again only when the file's mtime or size changed: the console
    rebuilds its state from the same thousands of unchanged files on every refresh."""
    st = os.stat(path)
    stamp = (st.st_mtime_ns, st.st_size)
    hit = FILE_CACHE.get((kind, str(path)))
    if hit and hit[0] == stamp:
        value: T = hit[1]
        return value
    fresh = build()
    FILE_CACHE[(kind, str(path))] = (stamp, fresh)
    return fresh


def code_version(files: dict[str, str]) -> str:
    """What a task's run record or current files do, for comparing runs: the hashes of `file_hashes` without the
    Markdown, since closing a task appends its `## Result` to design.md. "" when the record has no files."""
    code = {name: digest for name, digest in files.items() if not name.endswith(".md")}
    if not code:
        return ""
    return hashlib.sha256(json.dumps(code, sort_keys=True).encode()).hexdigest()


def content_digest(path: Path) -> str:
    return hashlib.sha256(content_for_hash(path)).hexdigest()


def content_for_hash(path: Path) -> bytes:
    data = path.read_bytes()
    if path.name != "meta.json":
        return data
    try:
        raw = json.loads(data)
    except ValueError:
        return data
    if not isinstance(raw, dict):
        return data
    return json.dumps({k: v for k, v in raw.items() if k not in ANNOTATIONS}, sort_keys=True).encode()


def load_task(folder: Path) -> Task:
    """A task as its folder holds it now. Parsed again only when meta.json or the folder's file list changed (a
    verify script appearing, say), which is what validation reads; each caller gets its own copy of the raw meta."""
    try:
        stamp = os.stat(folder).st_mtime_ns
        task = by_file(f"task:{stamp}", folder / "meta.json", functools.partial(read_task, folder))
    except OSError:
        task = read_task(folder)
    return Task(task.folder, dict(task.raw), task.meta, list(task.errors))


def read_task(folder: Path) -> Task:
    meta_path = folder / "meta.json"
    try:
        raw = read_json(meta_path)
    except (OSError, json.JSONDecodeError) as e:
        return Task(folder, {}, None, [f"meta.json unreadable: {e}"])
    meta, errors = validate_meta(raw, folder)
    return Task(folder, raw if isinstance(raw, dict) else {}, meta, errors)


def all_task_folders() -> list[Path]:
    home = home_dir()
    tasks = sorted(p for p in (home / "tasks").glob("*/*") if p.is_dir() and not p.name.startswith("."))
    general = sorted(p for p in (home / "general").glob("*") if p.is_dir() and not p.name.startswith("."))
    return tasks + general


def offboarded_folders() -> list[Path]:
    """Settled tasks that left tasks/ (and general scripts taken out by hand): same layout, under offboarded/."""
    root = home_dir() / OFFBOARDED
    return sorted(p for p in root.glob("*/*") if p.is_dir() and not p.name.startswith("."))


def is_offboarded(folder: Path) -> bool:
    return folder.parent.parent.name == OFFBOARDED


def match_folder(folders: list[Path], query: str) -> Path | None:
    q = query.rstrip("/").split("/")[-1]
    exact = [f for f in folders if f.name == q]
    if exact:
        return exact[0]
    matches = [f for f in folders if q in f.name]
    if len(matches) > 1:
        names = "\n  ".join(m.name for m in matches)
        raise OpsquireError(f"{query!r} matches several tasks:\n  {names}")
    return matches[0] if matches else None


def resolve_task(query: str, offboarded: bool = False) -> Task:
    """The task a command names. Commands that only read a task (show, log, runs) and restore pass offboarded=True;
    for every other command an offboarded task is not there to work on, and the error says how to bring it back."""
    folder = match_folder(all_task_folders(), query)
    if folder:
        return load_task(folder)
    folder = match_folder(offboarded_folders(), query)
    if folder is None:
        raise OpsquireError(f"no task matches {query!r}")
    if not offboarded:
        raise OpsquireError(
            f"{folder.name} is offboarded (settled and idle): `opsquire restore {folder.name}` brings it back, "
            f"`opsquire show {folder.name}` reads it"
        )
    return load_task(folder)
