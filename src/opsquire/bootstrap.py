"""`opsquire init [path]`: everything a fresh machine needs before the first run, in one idempotent command.
The home with its folders and local rules, the configuration (home and ask budget), the credentials file, the
agent skill and the home's local git repository, the contract, a check of the tools the lint gate and the agents
need, and a first read-only task so the first `opsquire run` has something to run. Nothing that exists is
overwritten; every step says what it did."""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path
from typing import Any

from . import contract
from .ask import DEFAULT_BUDGET_USD, configured_budget
from .integrate import integrate
from .lint import find_tool, lint_task, save_lint
from .model import (
    DATA_DIR,
    TASK_ID_RE,
    all_task_folders,
    config_path,
    creds_path,
    home_dir,
    iso,
    load_task,
    now,
    read_json,
    update_config,
    write_text,
)
from .offboard import CONFIG_KEY, days_text, keep_days

DEMO_NAME = "demo_hello"
DEMO_MAIN = '''"""The first task of this home: read-only, it says hello and counts what is here."""

from pathlib import Path

import opsquire_helpers as sq


def main() -> None:
    sq.step("Looking around")
    home = Path(__file__).resolve().parents[3]
    tasks = sum(1 for _ in home.glob("tasks/*/*/meta.json"))
    scripts = sum(1 for _ in home.glob("general/*/meta.json"))
    sq.data("home", path=str(home), tasks=tasks, general_scripts=scripts)
    for name in sq.TARGETS:
        sq.target(name, "ok", f"hello from {name}")
    sq.result("the first run is recorded; `opsquire last` shows it")


if __name__ == "__main__":
    sq.run(main)
'''
TOOLS = (
    ("git", "the home's history and parallel asks (apt install git)"),
    ("ruff", "lint gate (pipx inject opsquire ruff)"),
    ("mypy", "lint gate (pipx inject opsquire mypy)"),
    ("shellcheck", "lint gate for shell tasks (apt install shellcheck)"),
    ("gh", "repos and PRs (`opsquire repo`)"),
    ("claude", "`opsquire ask` with the default agent (Claude Code)"),
)


def init(path: str | None, budget: float | None, dry: bool, offboard_days: float | None = None) -> list[str]:
    lines: list[str] = []
    would = "would be " if dry else ""
    if path:
        target = Path(path).expanduser().resolve()
        if not dry:
            update_config(home=str(target))
        lines.append(f"home: {would}set to {target} (in {config_path()})")
        os.environ["OPSQUIRE_HOME"] = str(target)
    home = home_dir()
    for sub in ("tasks", "general"):
        folder = home / sub
        if folder.is_dir():
            lines.append(f"{sub}/: present")
        else:
            if not dry:
                folder.mkdir(parents=True)
            lines.append(f"{sub}/: {would}created ({folder})")
    local = home / "LOCAL.md"
    if local.is_file():
        lines.append(f"local rules: present ({local})")
    else:
        if not dry:
            write_text(local, (DATA_DIR / "LOCAL.template.md").read_text())
        lines.append(f"local rules: {would}written from the template ({local}); edit it: operator, areas, tools")
    current = configured_budget()
    chosen = budget if budget is not None else current
    if budget is not None and not dry:
        update_config(ask_budget_usd=budget)
    lines.append(
        f"ask budget: ${chosen:.2f} per ask as a reminder"
        + (
            " (default; `opsquire init --budget N` changes it)"
            if budget is None and current == DEFAULT_BUDGET_USD
            else ""
        )
        + "; `opsquire ask --strict-budget` makes it a hard cap"
    )
    if offboard_days is not None and not dry:
        update_config(**{CONFIG_KEY: offboard_days})
    days = offboard_days if offboard_days is not None else keep_days()
    lines.append(
        f"offboarding: settled tasks leave tasks/ {days_text(days)} days after their last activity"
        if days > 0
        else "offboarding: off; settled tasks stay until `opsquire offboard <task>`"
    )
    lines.append(credentials_line(dry))
    for name, use in TOOLS:
        found = find_tool(name) if name in ("ruff", "mypy", "shellcheck") else shutil.which(name)
        lines.append(f"tool {name}: {'found' if found else 'MISSING'}; {use}")
    lines += integrate(dry)
    if dry:
        lines.append(f"contract: would be written ({home / 'AGENTS.md'})")
    else:
        if contract.state(home) == "edited":
            lines.append(f"contract: not written: {contract.EDITED}")
        else:
            contract.write_contract(home)
            lines.append(f"contract: written ({home / 'AGENTS.md'})")
    first, ready = demo_task(home, dry)
    lines.append(first)
    if not dry:
        lines.append(
            '\nnext: run the first task; then `opsquire ui --open` for the console, or `opsquire ask "<what you want>"`'
            if ready
            else "\nnext: install what is MISSING above, then run the first task"
        )
    return lines


def credentials_line(dry: bool) -> str:
    path = creds_path()
    if path.exists():
        try:
            data = read_json(path)
        except OSError as e:
            return f"credentials: {path} exists but cannot be read from here ({type(e).__name__}); fine in a sandbox"
        except ValueError:
            return f"credentials: {path} is not valid JSON; the runner will refuse tasks that need it"
        keys = len(leaves(data))
        return f"credentials: present ({path}, {keys} value{'s' if keys != 1 else ''})"
    if dry:
        return f"credentials: would create an empty {path} (mode 600); fill it with the values tasks declare"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600)
    path.write_text("{}\n")
    path.chmod(0o600)
    return f"credentials: created empty {path} (mode 600); fill it with the values tasks declare"


def leaves(node: Any) -> list[Any]:
    if isinstance(node, dict):
        return [x for v in node.values() for x in leaves(v)]
    if isinstance(node, list):
        return [x for v in node for x in leaves(v)]
    return [node]


def demo_task(home: Path, dry: bool) -> tuple[str, bool]:
    """The line about the first task, and whether it can run now."""
    existing = [f for f in all_task_folders() if f.name.endswith(f"_{DEMO_NAME}")]
    if existing:
        name = existing[0].name
        if lint_task(load_task(existing[0])).ok:
            return f"first task: present ({name}); run it: opsquire run {name}", True
        return f"first task: present ({name}) but its lint did not pass; {after_tools(name)}", False
    t = now()
    name = f"{t:%Y%m%d-%H%M}_{DEMO_NAME}"
    assert TASK_ID_RE.match(name)
    folder = home / "tasks" / f"{t:%Y-%m}" / name
    if dry:
        return f"first task: would be created ({name})", True
    from .cli import build_task

    args = argparse.Namespace(
        area="demo",
        name="hello",
        general=False,
        plan=False,
        lang="py",
        purpose="say hello and count what is in this home (read-only)",
        changes_prod=False,
        no_verify=True,
        target=None,
        lock=None,
        lock_reason=None,
        cred=None,
        uses=None,
        rollback=None,
        link=None,
        interpreter=None,
        thread=None,
        author="opsquire-init",
    )
    folder.mkdir(parents=True)
    build_task(args, name, t, folder)
    write_text(folder / "main.py", DEMO_MAIN)
    write_text(
        folder / "design.md", f"# {name}\n\nCreated by `opsquire init` on {iso(t)[:10]} as the first task to run.\n"
    )
    task = load_task(folder)
    result = lint_task(task)
    save_lint(task, result)
    if result.ok:
        return f"first task: created and ready ({name}); run it: opsquire run {name}", True
    return f"first task: created ({name}) but its lint did not pass; {after_tools(name)}", False


def after_tools(name: str) -> str:
    return f"install the missing tools, then: opsquire lint {name}"
