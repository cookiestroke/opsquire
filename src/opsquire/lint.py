from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import (
    DATA_DIR,
    HELPERS_DIR,
    LINT_FILE,
    Task,
    home_dir,
    iso,
    load_task,
    now,
    read_json,
    task_folder,
    write_json,
)

RUFF_CONFIG = DATA_DIR / "ruff.toml"
MYPY_CONFIG = DATA_DIR / "mypy.ini"


@dataclass
class LintResult:
    ok: bool
    content_hash: str
    output: str
    tests: int = 0
    interrupted: bool = False


class Interrupted(Exception):
    """A lint tool was stopped by the operator's Ctrl-C (the signal reaches the whole process group), so its exit
    says nothing about the files."""


TEST_TIMEOUT = 600


def find_tool(name: str) -> str | None:
    override = os.environ.get(f"OPSQUIRE_{name.upper()}")
    if override:
        return override
    candidates = [Path(sys.executable).parent / name, Path(sysconfig.get_path("scripts", f"{os.name}_user")) / name]
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return shutil.which(name)


def _run(cmd: list[str], env: dict[str, str] | None = None) -> tuple[bool, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=300)
    if proc.returncode == -signal.SIGINT:
        raise Interrupted(Path(cmd[0]).name)
    text = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, f"$ {' '.join(cmd)}\n{text}" if text else f"$ {' '.join(cmd)}\nok"


def lint_task(task: Task) -> LintResult:
    """Lint the task's files, run its tests and say whether it is ready. Ctrl-C while a tool runs is an interruption,
    never a verdict: the main thread gets its KeyboardInterrupt as usual, a worker thread gets a result marked
    interrupted that callers report as such and never save."""
    try:
        return _lint_task(task)
    except Interrupted as e:
        if threading.current_thread() is threading.main_thread():
            raise KeyboardInterrupt from e
        return LintResult(False, task.content_hash(), f"interrupted: {e} was stopped by Ctrl-C", interrupted=True)


def _lint_task(task: Task) -> LintResult:
    content_hash = task.content_hash()
    if task.errors or task.meta is None:
        return LintResult(False, content_hash, "meta.json:\n  " + "\n  ".join(task.errors))
    if task.meta.kind == "plan":
        return lint_plan(task, content_hash)
    if task.meta.archived:
        return LintResult(False, content_hash, "archived: historical record kept byte-for-byte, not linted or runnable")
    files = task.files()
    py = [str(p) for p in files if p.suffix == ".py"]
    sh = [str(p) for p in files if p.suffix == ".sh"]
    other = [p for p in files if p.suffix not in (".py", ".sh", ".json", ".md", ".txt", ".sql", ".toml")]
    ok = True
    parts: list[str] = []
    if other:
        ok = False
        parts.append("unsupported files (no linter): " + ", ".join(str(p) for p in other))
    if py:
        ruff, mypy = find_tool("ruff"), find_tool("mypy")
        if not ruff or not mypy:
            return LintResult(False, content_hash, "ruff and mypy must be installed (pipx inject opsquire ruff mypy)")
        uses = [str(home_dir() / "general" / u) for u in task.meta.uses]
        by_folder: dict[str, list[str]] = {}
        for f in py:
            by_folder.setdefault(str(Path(f).parent), []).append(f)
        mypy_cmd = [mypy, "--config-file", str(MYPY_CONFIG)]
        # mypy refuses a MYPYPATH under its own site-packages, which is where an installed opsquire keeps its helpers
        # when mypy shares its environment, so it reads a copy.
        with tempfile.TemporaryDirectory(prefix="opsquire-mypy-") as helpers:
            shutil.copy(HELPERS_DIR / "opsquire_helpers.py", helpers)
            env = {**os.environ, "MYPYPATH": os.pathsep.join([helpers, *uses])}
            for cmd in (
                [ruff, "check", "--no-cache", "--config", str(RUFF_CONFIG), *py],
                *([*mypy_cmd, *files] for files in by_folder.values()),
            ):
                passed, out = _run(cmd, env)
                ok &= passed
                parts.append(out)
    if sh:
        shellcheck = find_tool("shellcheck")
        if not shellcheck:
            return LintResult(False, content_hash, "shellcheck must be installed")
        passed, out = _run([shellcheck, "-x", "-P", str(HELPERS_DIR), *sh])
        ok &= passed
        parts.append(out)
    tests, tests_ok, tests_out = run_tests(task)
    ok &= tests_ok
    if tests_out:
        parts.append(tests_out)
    return LintResult(ok, content_hash, "\n\n".join(parts), tests)


def run_tests(task: Task) -> tuple[int, bool, str]:
    """unittest discovery in every folder holding test_*.py (the task's own and the general scripts it uses).
    Tests must be offline: no ssh, no network, no credentials."""
    assert task.meta
    folders = sorted({p.parent for p in task.files() if p.name.startswith("test_") and p.suffix == ".py"})
    python = task.meta.command(Path("main.py"))[0]
    uses = [str(home_dir() / "general" / u) for u in task.meta.uses]
    total, ok, report = 0, True, []
    for folder in folders:
        path = os.pathsep.join([str(folder), str(HELPERS_DIR), *uses, os.environ.get("PYTHONPATH", "")])
        env = {**os.environ, "PYTHONPATH": path, "PYTHONDONTWRITEBYTECODE": "1", "OPSQUIRE_TESTING": "1"}
        cmd = [python, "-m", "unittest", "discover", "-s", str(folder), "-t", str(folder), "-p", "test_*.py"]
        try:
            r = subprocess.run(cmd, cwd=folder, capture_output=True, text=True, env=env, timeout=TEST_TIMEOUT)
            code, text = r.returncode, r.stdout + r.stderr
        except subprocess.TimeoutExpired:
            code, text = 1, f"timed out after {TEST_TIMEOUT}s"
        if code == -signal.SIGINT:
            raise Interrupted("unittest")
        m = re.search(r"Ran (\d+) tests?", text)
        ran = int(m.group(1)) if m else 0
        passed = code == 0 and ran > 0
        total += ran
        ok &= passed
        head = f"$ unittest {folder}: {'ok' if passed else 'FAILED'} ({ran} tests)"
        report.append(head if passed else head + "\n" + text.strip()[-3000:])
    return total, ok, "\n".join(report)


def lint_plan(plan: Task, content_hash: str) -> LintResult:
    assert plan.meta
    steps: list[LintResult] = []
    report: list[str] = []
    for step in plan.meta.steps or []:
        folder = task_folder(step["task"])
        sub = lint_task(load_task(folder)) if folder else LintResult(False, "", f"{step['task']} not found")
        steps.append(sub)
        report.append(f"== step {step['task']}: {'ok' if sub.ok else 'FAILED'}\n{sub.output}")
    return LintResult(
        all(s.ok for s in steps),
        content_hash,
        "\n\n".join(report),
        sum(s.tests for s in steps),
        interrupted=any(s.interrupted for s in steps),
    )


def save_lint(task: Task, result: LintResult) -> None:
    if result.interrupted:
        return
    try:
        write_json(
            task.folder / LINT_FILE,
            {
                "ok": result.ok,
                "content_hash": result.content_hash,
                "checked": iso(now()),
                "tests": result.tests,
                "output": result.output,
            },
        )
    except OSError:
        pass


def last_lint(task: Task) -> dict[str, Any] | None:
    """The task's last lint stamp, whatever files it was taken on."""
    try:
        data = read_json(task.folder / LINT_FILE)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def cached_lint_ok(task: Task) -> bool | None:
    data = last_lint(task)
    if data is None:
        return None
    try:
        current = task.content_hash()
    except OSError:
        return None
    if data.get("content_hash") != current:
        return None
    return bool(data.get("ok"))
