"""Every opsquire feature, end to end, through the real CLI in a scratch home: what an operator and their agents do in
a day, with a pseudo-terminal answering the prompts. Targets are made up and nothing leaves the scratch directory.

    python scripts/full_power.py      exit 0 when every check passes; the scratch home is removed, or kept on failure
"""

from __future__ import annotations

import json
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CLI = [sys.executable, "-m", "opsquire"]
TMP = Path(tempfile.mkdtemp(prefix="opsquire-full-power-", dir=os.environ.get("TMPDIR")))
HOME = TMP / "home"
ENV = {
    **os.environ,
    "OPSQUIRE_HOME": str(HOME),
    "OPSQUIRE_CREDS": str(TMP / "creds.json"),
    "OPSQUIRE_AUTHOR": "e2e-agent (chat)",
    "XDG_CONFIG_HOME": str(TMP / "cfg"),
    "HOME": str(TMP),
    "CLAUDE_CONFIG_DIR": str(TMP / "claude"),
    "PYTHONUSERBASE": str(Path.home() / ".local"),
    "PYTHONPATH": str(ROOT / "src"),
    "PATH": os.environ["PATH"] + os.pathsep + str(Path.home() / ".local/bin"),
}
CHANGING = ("--changes-prod", "--rollback", "none needed", "--no-verify")
results: list[bool] = []


def in_pty(cmd: list[str], env: dict[str, str], answers: list[tuple[str, str]]) -> tuple[int, str]:
    """Run cmd on a pseudo-terminal, typing each answer when the last line of output shows its prompt."""
    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe(cmd[0], cmd, env)
    out, pending, deadline = b"", list(answers), time.monotonic() + 120
    while time.monotonic() < deadline:
        if not select.select([fd], [], [], 0.5)[0]:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
        if pending and pending[0][0].encode() in out.rsplit(b"\n", 1)[-1]:
            os.write(fd, (pending.pop(0)[1] + "\n").encode())
    if time.monotonic() >= deadline:
        os.kill(pid, signal.SIGKILL)
    _, status = os.waitpid(pid, 0)
    os.close(fd)
    return os.waitstatus_to_exitcode(status), out.decode(errors="replace")


def cli(*args: str, answers: list[tuple[str, str]] | None = None, author: str = "") -> tuple[int, str, str]:
    env = dict(ENV, OPSQUIRE_AUTHOR=author) if author else ENV
    if answers is not None:
        code, out = in_pty([*CLI, *args], env, answers)
        return code, out, ""
    r = subprocess.run([*CLI, *args], env=env, capture_output=True, text=True, timeout=300)
    return r.returncode, r.stdout, r.stderr


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"\n    {detail[-800:]}"), flush=True)


def as_list(text: str) -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = json.loads(text)
    return data


def runs(task: str) -> list[dict[str, Any]]:
    return as_list(cli("runs", task, "--json")[1])


def events(rec: dict[str, Any]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(rec["paths"]["events"]).read_text().splitlines()]


def meta(folder: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((folder / "meta.json").read_text())
    return data


def offered(task_id: str) -> tuple[str, str] | None:
    code = "import json\nfrom opsquire.state import state\nprint(json.dumps(state()['inbox']))"
    items = as_list(subprocess.run([sys.executable, "-c", code], env=ENV, capture_output=True, text=True).stdout)
    flat = items + [s for it in items for s in it.get("steps") or []]
    found = next((x for x in flat if x["task_id"] == task_id), None)
    return (str(found["next"]), str(found["args"])) if found else None


def new(area: str, name: str, purpose: str, *extra: str) -> Path:
    code, out, err = cli("new", area, name, "--purpose", purpose, *extra)
    if code:
        raise SystemExit(f"opsquire new {area} {name} failed: {err}")
    return Path(out.strip().splitlines()[-1])


def script(folder: Path, body: str) -> None:
    (folder / "main.py").write_text(body)
    (folder / "test_main.py").unlink(missing_ok=True)


def home_from_nothing() -> None:
    code, out, err = cli("init", str(HOME), "--budget", "1")
    check("init builds a home with its contract", code == 0 and (HOME / "AGENTS.md").is_file(), out + err)
    check("the contract carries its stamp", "sha256:" in (HOME / "AGENTS.md").read_text().splitlines()[-1])
    demo = re.search(r"first task: created and ready \((\S+)\)", out)
    check("init's first task runs", bool(demo) and cli("run", demo.group(1) if demo else "")[0] == 0, out)


def read_task() -> Path:
    read = new("web1", "inventory", "what runs on web1 and web2", "--target", "web1", "--target", "web2")
    check("read scaffold: main.py and design.md", {"main.py", "design.md"} <= {p.name for p in read.iterdir()})
    code, out, _ = cli("lint", read.name)
    check("read scaffold lints", code == 0, out)
    code, out, _ = cli("run", read.name)
    kinds = {e["event"] for e in events(runs(read.name)[-1])}
    check("read run reports data, progress, targets", code == 0 and {"data", "progress", "target"} <= kinds, out)
    return read


STAGED_PLAN = '''def plan_for(state: dict[str, Any]) -> list[str]:
    """The changes a target needs, decided from its state alone (test_main.py covers it); empty when it is right."""
    return [] if state["target"] == "web2" else [f"{STAGE} config v2"]'''

STAGED_TEST = """import unittest

import main


class Decisions(unittest.TestCase):
    def test_web2_is_already_right(self) -> None:
        self.assertEqual(main.plan_for({"target": "web2"}), [])

    def test_web1_gets_the_config(self) -> None:
        self.assertEqual(len(main.plan_for({"target": "web1"})), 1)


if __name__ == "__main__":
    unittest.main()
"""

VERIFY_OK = """import opsquire_helpers as sq


def main() -> None:
    for name in sq.TARGETS:
        sq.target(name, "ok", "config v2 live")


if __name__ == "__main__":
    sq.run(main)
"""


def staged_task() -> None:
    staged = new(
        "web1", "roll-config", "roll the config out in two stages", "--changes-prod", "--rollback", "the old config",
        "--target", "web1", "--target", "web2", "--stage", "build", "--stage", "deploy",
    )  # fmt: skip
    body = (staged / "main.py").read_text()
    start = body.index("def plan_for")
    body = body[:start] + STAGED_PLAN + body[body.index("\n\n\n", start) :]
    body = body.replace("from typing import Any\n", "import sys\nfrom typing import Any\n")
    stage_line = 'STAGE = sys.argv[1] if len(sys.argv) > 1 else "build"\n'
    body = body.replace("import opsquire_helpers as sq\n", "import opsquire_helpers as sq\n\n" + stage_line)
    (staged / "main.py").write_text(body)
    (staged / "test_main.py").write_text(STAGED_TEST)
    (staged / "verify.py").write_text(VERIFY_OK)
    code, out, _ = cli("lint", staged.name)
    check("staged task lints with its 2 tests", code == 0 and "(2 tests)" in out, out)
    check("the inbox offers the whole pipeline", offered(staged.name) == ("apply", ""), str(offered(staged.name)))
    code, out, _ = cli("run", staged.name, "--apply", "--", "build")
    check(
        "one stage applied with no dry run is blocked", code == 2 and runs(staged.name)[-1]["status"] == "blocked", out
    )
    check("the blocked run keeps the inbox item", offered(staged.name) == ("apply", ""))
    code, out, _ = cli("run", staged.name, "--apply", answers=[("[y/N]", "y"), ("[y/N]", "y")])
    modes = [(r["mode"], r.get("argv")) for r in runs(staged.name) if r["status"] != "blocked"]
    walked = modes == [
        ("dry", ["build"]),
        ("apply", ["build"]),
        ("dry", ["deploy"]),
        ("apply", ["deploy"]),
        ("verify", []),
    ]
    check("one apply walks build and deploy, then verifies", code == 0 and walked, str(modes) + out[-600:])
    check("each stage asked its own question once", out.count("[y/N]") == 2, out[-600:])
    targets = next(r for r in runs(staged.name) if r.get("argv") == ["build"] and r["mode"] == "dry")["targets"]
    check("dry: web1 would change, web2 is left", targets["web2"]["status"] == "unchanged")
    closed = (meta(staged).get("outcome") or {}).get("state") == "done"
    check("the task closes done after the last stage", closed)
    check("design.md gets its Result", "## Result" in (staged / "design.md").read_text())
    check("the closed task leaves the inbox", offered(staged.name) is None)


def handover_and_plan() -> None:
    vacuum = new("db1", "vacuum", "vacuum db1", *CHANGING)
    reindex = new("db1", "reindex", "reindex db1", *CHANGING)
    check("both steps lint", all(cli("lint", t.name)[0] == 0 for t in (vacuum, reindex)))
    code, out, err = cli("handover", vacuum.name, "--why", "bloat at 40%")
    check("handover names the other item on the same machine", code == 0 and reindex.name in err, out + err)
    check("handover withdraw", "withdrawn" in cli("handover", vacuum.name, "--withdraw")[1])
    plan = new("db1", "maintenance", "vacuum then reindex db1", "--plan", "--step", vacuum.name, "--step", reindex.name)
    check("the plan lints", cli("lint", plan.name)[0] == 0)
    check("the plan waits in the inbox", offered(plan.name) == ("dry", ""))
    check("a plan apply with no dry run is blocked", cli("run", plan.name, "--apply")[0] == 2)
    check("the plan's dry run", cli("run", plan.name)[0] == 0)
    code, out, _ = cli("run", plan.name, "--apply", answers=[("[y/N]", "y"), ("[y/N]", "y")])
    both = all(runs(t.name)[-1]["mode"] == "apply" for t in (vacuum, reindex))
    check("the plan apply runs both steps", code == 0 and both, out)


def decisions() -> None:
    old = new("web3", "old-way", "the old way", *CHANGING)
    newer = new("web3", "new-way", "the new way", *CHANGING)
    cli("lint", old.name)
    cli("lint", newer.name)
    superseded = cli("supersede", old.name, "--by", newer.name, "--note", "covers web3", author="operator-x")[0]
    check("supersede", superseded == 0)
    check("a superseded task refuses to run", "superseded by" in cli("run", old.name)[2])
    code, _, err = cli("wait", old.name, "--timeout", "20")
    check("wait exits 4 on a decision", code == 4, err)
    check("the author hears the decision", "was superseded by" in cli("new", "web3", "next", "--purpose", "more")[2])
    check("reopen", cli("reopen", old.name, "--note", "needed after all")[0] == 0)


def asking_and_failing() -> None:
    twice = new("web4", "ask-twice", "asks per host", *CHANGING)
    script(twice, "import opsquire_helpers as sq\n\nif sq.APPLY:\n    sq.confirm('a?')\n    sq.confirm('b?')\n")
    cli("lint", twice.name)
    cli("run", twice.name)
    cli("run", twice.name, "--apply", answers=[("a? [y/N]", "y"), ("b? [y/N]", "y")])
    warns = [str(e["msg"]) for e in events(runs(twice.name)[-1]) if e["event"] == "warn"]
    check("a second yes is warned in the record", any("second yes" in w for w in warns), str(warns))

    bad = new("web5", "breaks", "fails on apply", *CHANGING)
    script(
        bad,
        "import opsquire_helpers as sq\n\n\ndef main() -> None:\n    if sq.APPLY:\n"
        "        raise RuntimeError('disk full on web5')\n    sq.target('web5', 'ok')\n\n\nsq.run(main)\n",
    )
    cli("lint", bad.name)
    cli("run", bad.name)
    cli("run", bad.name, "--apply")
    last_error = str(runs(bad.name)[-1].get("last_error"))
    check("a failed apply names its cause", "disk full on web5" in last_error, last_error)
    check("runs --status failed lists it", bad.name in cli("runs", "--status", "failed")[1])
    check("log prints the run", "disk full" in cli("log", bad.name)[1])


def history(read: Path) -> None:
    check("last", cli("last")[0] == 0)
    check("find", "roll-config" in cli("find", "roll", "config")[1])
    check("threads", cli("threads")[0] == 0)
    check("milestones", cli("milestones", "--since", "2h")[0] == 0)
    check("the catalog is regenerated", "roll-config" in (HOME / "docs/CATALOG.md").read_text())
    check("offboard --due --dry", cli("offboard", "--due", "--dry", "--days", "0.0001")[0] == 0)
    check("locks", cli("locks")[0] == 0)
    with (HOME / "LOCAL.md").open("a") as f:
        f.write("\n### Rule: full power marker\n")
    _, _, err = cli("lint", read.name)
    rewritten = "AGENTS.md rewritten" in err and "full power marker" in (HOME / "AGENTS.md").read_text()
    check("the contract follows LOCAL.md", rewritten, err)


def console() -> None:
    port = "18793"
    ui = subprocess.Popen([*CLI, "ui", "--port", port], env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        body = ""
        for _ in range(40):
            try:
                body = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2).read().decode()
                break
            except OSError:
                time.sleep(0.3)
        state: dict[str, Any] = json.loads(body) if body else {}
        check("the console serves the inbox", "inbox" in state, body[:300])
    finally:
        ui.terminate()
        ui.wait(10)


def main() -> int:
    read: list[Path] = []
    stages: list[Callable[[], None]] = [
        home_from_nothing,
        lambda: read.append(read_task()),
        staged_task,
        handover_and_plan,
        decisions,
        asking_and_failing,
        lambda: history(read[0]),
        console,
    ]
    try:
        for stage in stages:
            stage()
    except KeyboardInterrupt:
        shutil.rmtree(TMP, ignore_errors=True)
        print("\ninterrupted; scratch home removed")
        return 130
    if all(results):
        shutil.rmtree(TMP, ignore_errors=True)
        print(f"\n{len(results)}/{len(results)} checks passed")
        return 0
    print(f"\n{sum(results)}/{len(results)} checks passed; scratch home kept for a look: {HOME}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
