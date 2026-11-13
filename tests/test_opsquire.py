from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import gzip
import io
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


CLI = [sys.executable, "-m", "opsquire"]
BASH_HEADER = (
    '#!/usr/bin/env bash\nset -euo pipefail\n# shellcheck source=opsquire.sh\nsource "$OPSQUIRE_HELPERS/opsquire.sh"\n'
)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


ASKING_BODY = """import opsquire_helpers as sq


def main() -> None:
    sq.step("Pre-checks (read-only)")

    if sq.APPLY:
        sq.confirm("Apply the change?")
        sq.step("Apply")

    for name in sq.TARGETS:
        sq.target(name, "ok")
    sq.result("done")


if __name__ == "__main__":
    sq.run(main)
"""


def run_in_pty(
    cmd: list[str], env: dict[str, str], answers: list[tuple[str, str]], size: tuple[int, int] | None = None
) -> subprocess.CompletedProcess[str]:
    pid, fd = pty.fork()
    if pid == 0:
        if size:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", size[0], size[1], 0, 0))
        os.execvpe(cmd[0], cmd, env)
    out = b""
    pending = list(answers)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.5)
        if not ready:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
        if pending and pending[0][0].encode() in out.rsplit(b"\n", 1)[-1]:
            answer = pending.pop(0)[1]
            os.write(fd, (answer if answer == "\x03" else answer + "\n").encode())
    if time.monotonic() >= deadline:
        os.kill(pid, signal.SIGKILL)
    _, status = os.waitpid(pid, 0)
    os.close(fd)
    return subprocess.CompletedProcess(cmd, os.waitstatus_to_exitcode(status), out.decode(errors="replace"), "")


class OpsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR")))
        self.home = self.tmp / "home"
        (self.home / "tasks").mkdir(parents=True)
        (self.home / "general").mkdir()
        self.creds = self.tmp / "creds.json"
        self.creds.write_text(json.dumps({"influx": {"prod": {"token": "SECRET-TOKEN-123456"}}}))
        self.env = {
            **os.environ,
            "OPSQUIRE_HOME": str(self.home),
            "OPSQUIRE_CREDS": str(self.creds),
            "OPSQUIRE_AUTHOR": "test",
            "PATH": os.environ["PATH"] + ":" + str(Path.home() / ".local/bin"),
            "PYTHONPATH": str(ROOT / "src"),
        }

    def tearDown(self) -> None:
        litter = [str(p) for p in self.home.rglob("*.tmp")] if self.home.exists() else []
        self.assertEqual(litter, [], "temp files left behind")
        locks_file = self.home / "var/locks/locks.json"
        if locks_file.is_file():
            held = json.loads(locks_file.read_text())
            alive = [h for hs in held.values() for h in hs if h["task_id"] != "other" and pid_alive(h["pid"])]
            self.assertEqual(alive, [], "locks left held by live processes")
        shutil.rmtree(self.tmp, ignore_errors=True)

    CHANGING = ("--changes-prod", "--rollback", "none", "--no-verify")
    NO_DRY = ("--skip-dry", "the dry run is not what this test is about")

    def cli(self, *args: str, answers: list[tuple[str, str]] | None = None) -> subprocess.CompletedProcess[str]:
        if answers is None:
            return subprocess.run([*CLI, *args], env=self.env, capture_output=True, text=True, timeout=120)
        return run_in_pty([*CLI, *args], self.env, answers)

    def new(self, name: str, body: str, *extra: str, lang: str = "py", area: str = "web1") -> Path:
        r = self.cli("new", area, name, "--purpose", f"test {name}", "--lang", lang, *extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        folder = Path(r.stdout.strip())
        (folder / f"main.{lang}").write_text(body)
        (folder / "test_main.py").unlink(missing_ok=True)
        return folder

    def last(self, task: str) -> dict[str, Any]:
        r = self.cli("runs", task, "--json")
        recs: list[dict[str, Any]] = json.loads(r.stdout)
        return recs[-1]

    def events(self, rec: dict[str, Any]) -> list[dict[str, Any]]:
        return [json.loads(line) for line in Path(rec["paths"]["events"]).read_text().splitlines()]

    def test_read_only_ok_with_redaction(self) -> None:
        self.new(
            "read-ok",
            "import os\n\nimport opsquire_helpers as sq\n\n"
            "print('token', os.environ['TOKEN'])\nsq.target('web1', 'ok', 'fine')\n",
            "--cred",
            "TOKEN=influx.prod.token",
        )
        r = self.cli("run", "read-ok")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("SECRET-TOKEN-123456", r.stdout + r.stderr)
        rec = self.last("read-ok")
        self.assertEqual((rec["status"], rec["mode"], rec["targets"]["web1"]["status"]), ("ok", "read", "ok"))
        self.assertEqual(rec["redactions"], 1)
        self.assertEqual(rec["credentials_used"], ["TOKEN"])
        log = Path(rec["paths"]["output"]).read_text()
        self.assertIn("[REDACTED:influx.prod.token]", log)
        self.assertNotIn("SECRET-TOKEN-123456", log + Path(rec["paths"]["events"]).read_text())
        self.assertTrue(Path(rec["paths"]["snapshot"]).is_dir())
        self.assertEqual(json.loads(self.cli("ls", "--json").stdout)[0]["status"], "done")

    def test_prod_task_dry_apply_and_abort(self) -> None:
        self.new(
            "restart",
            ASKING_BODY,
            "--changes-prod",
            "--rollback",
            "restart again",
        )
        self.assertEqual(self.cli("run", "restart").returncode, 0)
        self.assertEqual(self.last("restart")["mode"], "dry")
        self.assertEqual(json.loads(self.cli("show", "restart", "--json").stdout)["status"], "ready")

        r = self.cli("run", "restart", "--apply", answers=[("[y/N]", "n")])
        self.assertEqual(r.returncode, 3, r.stdout)
        rec = self.last("restart")
        self.assertEqual((rec["status"], rec["targets"]["web1"]["status"]), ("aborted", "skipped"))

        r = self.cli("run", "restart", "--apply", answers=[("[y/N]", "y")])
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.last("restart")["status"], "ok")
        self.assertEqual(json.loads(self.cli("show", "restart", "--json").stdout)["status"], "done")

    def test_partial_multi_host(self) -> None:
        body = (
            "import sys\n\nimport opsquire_helpers as sq\n\n"
            "sq.target('db1', 'ok')\nsq.target('db2', 'failed', 'supervisor refused')\nsys.exit(1)\n"
        )
        self.new("multi", body, "--target", "db1", "--target", "db2", "--target", "db3", area="db")
        r = self.cli("run", "multi")
        self.assertEqual(r.returncode, 1)
        rec = self.last("multi")
        self.assertEqual(rec["status"], "partial")
        self.assertEqual(
            {k: v["status"] for k, v in rec["targets"].items()}, {"db1": "ok", "db2": "failed", "db3": "skipped"}
        )
        self.assertEqual(rec["last_error"], "supervisor refused")

    def test_crash_is_failed(self) -> None:
        self.new("crash", "raise RuntimeError('boom')\n")
        self.assertEqual(self.cli("run", "crash").returncode, 1)
        rec = self.last("crash")
        self.assertEqual((rec["status"], rec["targets"]["web1"]["status"]), ("failed", "failed"))
        self.assertIn("RuntimeError: boom", Path(rec["paths"]["output"]).read_text())

    def test_lint_failure_blocks_without_running(self) -> None:
        self.new("badtypes", "def f(x):\n    return x\n\nopen('/tmp/should-not-exist-opsquire-test', 'w')\n")
        r = self.cli("run", "badtypes")
        self.assertEqual(r.returncode, 2)
        rec = self.last("badtypes")
        self.assertEqual(rec["status"], "blocked")
        self.assertIn("lint or tests failed", rec["last_error"])
        self.assertFalse(any(e["event"] == "spawn" for e in self.events(rec)))

    def test_missing_credential_blocks(self) -> None:
        self.new("nocred", "print('hi')\n", "--cred", "X=influx.prod.missing")
        self.assertEqual(self.cli("run", "nocred").returncode, 2)
        self.assertIn("influx.prod.missing", self.last("nocred")["last_error"])

    def test_bash_task_and_remote_payload_pattern(self) -> None:
        body = (
            BASH_HEADER + 'sq_step "local driver"\n'
            "bash -c 'python3 -u -' <<'PY'\n"
            "import json\n"
            "print('remote says hi')\n"
            "event = {'event': 'target', 'target': 'web1', 'status': 'unchanged', 'msg': 'already running'}\n"
            "print('@@OPSQUIRE ' + json.dumps(event))\n"
            "PY\n"
        )
        self.new("bashremote", body, lang="sh")
        r = self.cli("run", "bashremote")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rec = self.last("bashremote")
        self.assertEqual((rec["status"], rec["targets"]["web1"]["status"]), ("ok", "unchanged"))
        self.assertIn("remote says hi", Path(rec["paths"]["output"]).read_text())

    def write_lock(self, target: str, pid: int, mode: str = "exclusive") -> None:
        d = self.home / "var/locks"
        d.mkdir(parents=True, exist_ok=True)
        holder = {
            "run_id": "R1",
            "task_id": "other",
            "pid": pid,
            "host": os.uname().nodename,
            "mode": mode,
            "since": "x",
        }
        (d / "locks.json").write_text(json.dumps({target: [holder]}))

    def test_lock_conflict_break_and_stale(self) -> None:
        self.new("locked", "print('ran')\n")
        self.write_lock("web1", os.getpid())
        self.assertEqual(self.cli("run", "locked").returncode, 2)
        self.assertIn("locked by other", self.last("locked")["last_error"])

        self.assertEqual(self.cli("run", "locked", "--break-lock", answers=[("Type BREAK", "nope")]).returncode, 2)
        r = self.cli("run", "locked", "--break-lock", answers=[("Type BREAK", "BREAK")])
        self.assertEqual(r.returncode, 0, r.stdout)
        rec = self.last("locked")
        self.assertEqual(rec["lock"]["broken"][0]["run_id"], "R1")
        self.assertEqual(json.loads(self.cli("locks", "--json").stdout), {})

        dead = subprocess.Popen(["true"])
        dead.wait()
        self.write_lock("web1", dead.pid)
        self.assertEqual(self.cli("run", "locked").returncode, 0)
        self.assertEqual(self.last("locked")["lock"]["stale_reclaimed"][0]["pid"], dead.pid)

    def test_shared_locks_coexist_but_block_exclusive(self) -> None:
        self.new("reader", "print('read')\n")
        self.write_lock("web1", os.getpid(), mode="shared")
        self.assertEqual(self.cli("run", "reader").returncode, 0)
        self.new("writer", "print('w')\n", "--changes-prod", "--rollback", "none needed")
        self.assertEqual(self.cli("run", "writer").returncode, 2)

    def test_ctrl_c_and_kill_9(self) -> None:
        self.new("slow", "import time\n\nprint('start', flush=True)\ntime.sleep(30)\n")
        p = subprocess.Popen(
            [*CLI, "run", "slow"], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
        )
        assert p.stdout
        p.stdout.readline()
        time.sleep(0.3)
        os.killpg(p.pid, signal.SIGINT)
        p.communicate(timeout=20)
        self.assertEqual(p.returncode, 130)
        self.assertEqual(self.last("slow")["status"], "interrupted")

        p = subprocess.Popen(
            [*CLI, "run", "slow"], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
        )
        assert p.stdout
        p.stdout.readline()
        os.killpg(p.pid, signal.SIGKILL)
        p.communicate(timeout=20)
        rec = self.last("slow")
        self.assertEqual(rec["status"], "interrupted")
        self.assertIn("vanished", rec["last_error"])

    def test_guards(self) -> None:
        self.new("ro", "print(1)\n")
        self.assertIn("read-only", self.cli("run", "ro", "--apply").stderr)
        r = self.cli("new", "Bad_Area", "x", "--purpose", "p")
        self.assertEqual(r.returncode, 2)
        self.assertIn("kebab-case", r.stderr)
        g = self.cli("new", "supervisor", "restart-stack", "--general", "--purpose", "reusable")
        self.assertEqual(g.returncode, 0, g.stderr)
        self.assertIn("not run directly", self.cli("run", "supervisor_restart-stack").stderr)
        folder = Path(
            self.cli(
                "new", "web1", "uses-general", "--purpose", "p", "--uses", "supervisor_restart-stack"
            ).stdout.strip()
        )
        self.assertEqual(self.cli("run", "uses-general").returncode, 0)
        files = self.last("uses-general")["files"]
        self.assertTrue(any(f.startswith("general/supervisor_restart-stack/") for f in files), files)
        meta = json.loads((folder / "meta.json").read_text())
        meta["id"] = "renamed"
        (folder / "meta.json").write_text(json.dumps(meta))
        shown = json.loads(self.cli("show", "uses-general", "--json").stdout)
        self.assertEqual(shown["status"], "done", "it ran ok; a later meta error does not undo that")
        self.assertIn("differs from folder name", " ".join(shown["meta_errors"]))
        self.assertEqual(self.cli("run", "uses-general").returncode, 2)
        never_ran = self.new("never-ran", "print(1)\n")
        self.assertEqual(self.cli("lint", "never-ran").returncode, 0)
        (never_ran / "meta.json").write_text(
            json.dumps(json.loads((never_ran / "meta.json").read_text()) | {"id": "x"})
        )
        self.assertEqual(json.loads(self.cli("show", "never-ran", "--json").stdout)["status"], "draft")

    def test_archived_is_listed_but_never_runs_or_lints(self) -> None:
        body = "import os\nos.system('touch should-not-run')\n"
        folder = self.new("legacy", body, "--changes-prod", "--rollback", "x")
        meta = json.loads((folder / "meta.json").read_text())
        meta["archived"] = {"note": "ran before ops", "history": [{"when": "2026-10-23", "by": "codex"}]}
        (folder / "meta.json").write_text(json.dumps(meta))
        self.assertEqual(json.loads(self.cli("ls", "--status", "archived", "--json").stdout)[0]["id"], folder.name)
        r = self.cli("run", "legacy", "--apply")
        self.assertEqual(r.returncode, 2)
        self.assertIn("archived", r.stderr)
        self.assertFalse((folder / "should-not-run").exists())
        self.assertEqual(self.cli("lint", "legacy").returncode, 1)
        meta["archived"] = {"note": ""}
        (folder / "meta.json").write_text(json.dumps(meta))
        errors = json.loads(self.cli("show", "legacy", "--json").stdout)["meta_errors"]
        self.assertIn("archived must be", " ".join(errors))

    def test_find_ranks_matches_and_skips_unrelated(self) -> None:
        self.new("restart-bots", "print(1)\n", "--target", "web1")
        self.new("pnl-report", "print(1)\n", area="hv2")
        hits = json.loads(self.cli("find", "restart", "web1", "--json").stdout)
        self.assertEqual([h["id"].split("_", 1)[1] for h in hits], ["web1_restart-bots"])
        self.assertEqual(hits[0]["matched"], ["restart", "web1"])
        self.assertEqual(json.loads(self.cli("find", "nothing-like-this", "--json").stdout), [])

    def agents_file(self, **override: Any) -> None:
        fake = str(Path(__file__).parent / "fake_agent.py")
        agent = {"enabled": True, "argv": [sys.executable, fake, "{prompt}", "--sys", "{system}", "{allowed_tools*}"]}
        agent |= {"resume_argv": ["--resume", "{session}"], "output": "claude-stream-json"}
        agent |= {"budget_argv": ["--max-budget-usd", "{budget}"]}
        agent |= {"allowed_tools": ["Edit(/{home}/tasks/**)"]} | override
        cfg = {"default": "fake", "agents": {"fake": agent, "off": {"enabled": False, "reason": "not reviewed"}}}
        path = self.tmp / "retinue.json"
        path.write_text(json.dumps(cfg))
        self.env["OPSQUIRE_RETINUE"] = str(path)

    def test_ask_records_actions_and_resumes_by_task(self) -> None:
        self.agents_file()
        r = self.cli("ask", "restart", "web1", "bots")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("RUN: opsquire run 20261030-1749_web1_restart-bots --apply", r.stdout)
        self.assertEqual(r.stdout.count("RUN: opsquire run 20261030-1749_web1_restart-bots --apply"), 1)
        self.assertIn("follow up: opsquire ask --resume ", r.stderr)
        self.assertIn("or: opsquire ask --task 20261030-1749_web1_restart-bots", r.stderr)
        self.assertIn("Bash: opsquire find web1 restart", r.stderr)
        self.assertIn(f"Edit(/{self.home}/tasks/**)", r.stdout)
        self.assertIn("author=opsquire-ask/fake", r.stdout)
        recs = [json.loads(p.read_text()) for p in sorted((self.home / "var/asks").glob("*.json"))]
        self.assertEqual(recs[0]["session"], "sess-new")
        self.assertEqual(recs[0]["task_ids"], ["20261030-1749_web1_restart-bots"])
        self.assertEqual([a["kind"] for a in recs[0]["actions"]], ["RUN", "RUN"])

        task = self.new("restart-bots", "print(1)\n")
        rec = json.loads(next((self.home / "var/asks").glob("*.json")).read_text())
        rec["task_ids"] = [task.name]
        rec["session"] = "sess-42"
        (self.home / "var/asks" / "x-later.json").write_text(json.dumps(rec))
        r = self.cli("ask", "--task", "restart-bots", "how", "did", "it", "go")
        self.assertIn('"--resume", "sess-42"', r.stdout)
        r = self.cli("ask", "--task", "restart-bots", "--new", "again")
        self.assertNotIn("--resume", r.stdout)
        self.assertIn(f"This is about opsquire task {task.name}", r.stdout)

    def test_ask_refuses_disabled_and_reports_agent_failure(self) -> None:
        self.agents_file()
        r = self.cli("ask", "--agent", "off", "hi")
        self.assertEqual(r.returncode, 2)
        self.assertIn("disabled: not reviewed", r.stderr)
        self.agents_file(argv=[sys.executable, "-c", "import sys; sys.exit(4)"])
        r = self.cli("ask", "hi")
        self.assertEqual(r.returncode, 1)
        self.assertIn("agent failed (exit 4)", r.stderr)

    def start(self, *args: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [*CLI, *args],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

    def test_ctrl_c_before_script_records_interrupted_and_cleans_up(self) -> None:
        self.new("early", "print(1)\n", "--changes-prod", "--rollback", "x")
        p = self.start("run", "early")
        deadline = time.monotonic() + 20
        while not list((self.home / "var/runs").rglob("run.json")) and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.2)
        os.killpg(p.pid, signal.SIGINT)
        out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 130, err)
        self.assertNotIn("Traceback", out + err)
        rec = self.last("early")
        self.assertEqual(rec["status"], "interrupted")
        self.assertIn("before the script started", rec["last_error"])
        self.assertEqual(rec["targets"]["web1"]["status"], "skipped")

    def test_ctrl_c_at_python_prompt_runs_cleanup_without_traceback(self) -> None:
        body = (
            "from pathlib import Path\n\nimport opsquire_helpers as sq\n\nSCRATCH = Path('scratch.txt')\n\n\n"
            "def main() -> None:\n    SCRATCH.write_text('x')\n    sq.step('half way')\n    sq.confirm('Go?')\n\n\n"
            "sq.run(main, cleanup=lambda: SCRATCH.unlink(missing_ok=True))\n"
        )
        folder = self.new("prompt", body, "--changes-prod", "--rollback", "x")
        r = self.cli("run", "prompt", "--apply", *self.NO_DRY, answers=[("[y/N]", "\x03")])
        self.assertEqual(r.returncode, 130, r.stdout)
        self.assertNotIn("Traceback", r.stdout)
        rec = self.last("prompt")
        self.assertEqual(rec["status"], "interrupted")
        log = Path(rec["paths"]["output"]).read_text()
        self.assertIn("half way", log)
        self.assertIn("interrupted by the operator", log)
        self.assertFalse((folder / "scratch.txt").exists())

    def test_ctrl_c_at_bash_prompt_runs_sq_cleanup(self) -> None:
        body = BASH_HEADER + 'sq_cleanup() { rm -f scratch.txt; }\ntouch scratch.txt\nsq_confirm "Go?"\n'
        folder = self.new("bashprompt", body, "--changes-prod", "--rollback", "x", lang="sh")
        r = self.cli("run", "bashprompt", "--apply", *self.NO_DRY, answers=[("[y/N]", "\x03")])
        self.assertEqual(r.returncode, 130, r.stdout)
        self.assertEqual(self.last("bashprompt")["status"], "interrupted")
        self.assertFalse((folder / "scratch.txt").exists())

    def test_new_is_all_or_nothing(self) -> None:
        sys.path.insert(0, str(ROOT / "src"))
        from opsquire import cli

        with (
            mock.patch.dict(os.environ, {"OPSQUIRE_HOME": str(self.home)}),
            mock.patch("shutil.copy", side_effect=KeyboardInterrupt),
        ):
            self.assertEqual(cli.main(["new", "web1", "half", "--purpose", "p"]), 130)
        self.assertEqual(list((self.home / "tasks").iterdir()), [])

    def test_ask_interrupt_then_resume(self) -> None:
        self.agents_file()
        self.env["FAKE_SLEEP"] = "30"
        p = self.start("ask", "do", "something")
        assert p.stderr
        p.stderr.readline()
        time.sleep(1.0)
        os.killpg(p.pid, signal.SIGINT)
        _, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 130, err)
        self.assertNotIn("Traceback", err)
        rec = json.loads(next((self.home / "var/asks").glob("*.json")).read_text())
        self.assertEqual((rec["status"], rec["session"]), ("interrupted", "sess-new"))
        self.assertIn(f"--resume {rec['ask_id']}", err)
        del self.env["FAKE_SLEEP"]
        r = self.cli("ask", "--resume", rec["ask_id"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('"--resume", "sess-new"', r.stdout)
        r = self.cli("ask", "--resume", "sess-new", "more")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('"--resume", "sess-new"', r.stdout)

    def test_wait_returns_the_next_finished_run(self) -> None:
        self.new("waited", "import time\n\ntime.sleep(1)\nprint('done')\n")
        self.assertEqual(self.cli("run", "waited").returncode, 0)
        first = self.last("waited")["run_id"]
        w = self.start("wait", "waited", "--timeout", "60", "--json")
        time.sleep(1.5)
        self.assertEqual(self.cli("run", "waited").returncode, 0)
        out, err = w.communicate(timeout=90)
        self.assertEqual(w.returncode, 0, err)
        rec = json.loads(out)
        self.assertNotEqual(rec["run_id"], first)
        self.assertEqual(rec["status"], "ok")
        self.assertIn("started, waiting for it to finish", err)
        r = self.cli("wait", "waited", "--timeout", "1")
        self.assertEqual(r.returncode, 124)

    def test_repo_contains_nothing_employer_specific(self) -> None:
        banned = [
            w[::-1].lower() for w in ("tsj", "vedpactsj", "03.271", "radnaM", "ldvb", "dovb", "inimeg", "sgol-tsj")
        ]
        listed = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        hits = []
        for name in listed:
            text = (ROOT / name).read_text(errors="ignore").lower()
            hits += [f"{name}: {w}" for w in banned if w in text]
        self.assertEqual(hits, [])

    def test_agent_sandbox_stubs_are_not_script_files(self) -> None:
        folder = self.new("stubbed", "print('ok')\n")
        before = self.cli("lint", "stubbed")
        self.assertEqual(before.returncode, 0, before.stdout + before.stderr)
        stubs = folder / ".claude"
        stubs.mkdir()
        for name in ("agents", "settings.json", "loop.md"):
            (stubs / name).write_text("")
        after = self.cli("lint", "stubbed")
        self.assertEqual(after.returncode, 0, after.stdout + after.stderr)
        self.assertNotIn("unsupported files", after.stdout + after.stderr)

    def test_lint_ignores_active_venv_and_project_configs(self) -> None:
        self.new("pinned", "import os\n\nprint(os.getcwd())\n")
        hostile = self.tmp / "project"
        (hostile / "bin").mkdir(parents=True)
        (hostile / "mypy.ini").write_text("[mypy]\ninstall_types = True\nnon_interactive = True\n")
        (hostile / "pyproject.toml").write_text('[tool.ruff.lint]\nselect = ["ALL"]\n')
        for tool in ("mypy", "ruff"):
            fake = hostile / "bin" / tool
            fake.write_text("#!/bin/sh\necho fake tool >&2\nexit 1\n")
            fake.chmod(0o755)
        env = {**self.env, "PATH": f"{hostile / 'bin'}:{self.env['PATH']}"}
        r = subprocess.run([*CLI, "lint", "pinned"], env=env, cwd=hostile, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("fake tool", r.stdout + r.stderr)
        self.assertIn("mypy.ini", r.stdout)

    def test_task_python_is_pinned_unless_meta_names_one(self) -> None:
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "python3"
        fake.write_text("#!/bin/sh\necho WRONG PYTHON\nexit 1\n")
        fake.chmod(0o755)
        env = {**self.env, "PATH": f"{bin_dir}:{self.env['PATH']}"}
        self.new("pinned-py", "print('ran')\n")
        r = subprocess.run([*CLI, "run", "pinned-py"], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("WRONG PYTHON", r.stdout)
        self.assertEqual(self.last("pinned-py")["interpreter"], sys.executable)

        venv_python = bin_dir / "venv-python"
        venv_python.write_text(f'#!/bin/sh\necho VENV PYTHON\nexec {sys.executable} "$@"\n')
        venv_python.chmod(0o755)
        self.new("venv-py", "print('ran')\n", "--interpreter", str(venv_python))
        r = subprocess.run([*CLI, "run", "venv-py"], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("VENV PYTHON", r.stdout)
        self.assertEqual(self.last("venv-py")["interpreter"], str(venv_python))

        folder = self.new("bad-interp", "print(1)\n", "--interpreter", str(self.tmp / "missing"))
        self.assertIn(
            "not an executable file",
            " ".join(json.loads(self.cli("show", folder.name, "--json").stdout)["meta_errors"]),
        )

    CONFIRM_BODY = ASKING_BODY

    def make_plan(self, *steps: Path, name: str = "flow", extra: dict[str, Any] | None = None) -> Path:
        args = [a for s in steps for a in ("--step", s.name)]
        r = self.cli("new", "web1", name, "--plan", "--purpose", "check, change, verify", *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        folder = Path(r.stdout.strip())
        if extra:
            meta = json.loads((folder / "meta.json").read_text())
            for i, step_extra in extra.items():
                meta["steps"][int(i)].update(step_extra)
            (folder / "meta.json").write_text(json.dumps(meta))
        return folder

    def plan_rec(self, plan: Path) -> dict[str, Any]:
        return self.last(plan.name)

    def test_plan_dry_then_apply_runs_steps_in_order_under_one_lock(self) -> None:
        check = self.new("check", "print('checked')\n")
        change = self.new("change", self.CONFIRM_BODY, "--changes-prod", "--rollback", "undo")
        verify = self.new("verify", "print('verified')\n")
        plan = self.make_plan(check, change, verify, extra={"2": {"verify": True}})
        r = self.cli("run", plan.name)
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = self.plan_rec(plan)
        self.assertEqual((rec["status"], rec["mode"]), ("ok", "dry"))
        self.assertEqual(
            [(s["status"], s["mode"]) for s in rec["steps"]], [("ok", "read"), ("ok", "dry"), ("ok", "read")]
        )
        self.assertIn("verify step", rec["warnings"][0])
        step_rec = self.last("change")
        self.assertEqual((step_rec["plan_run"], step_rec["lock"]["held_by_plan_run"]), (rec["run_id"], rec["run_id"]))

        r = self.cli("run", plan.name, "--apply", answers=[("[y/N]", "y")])
        self.assertEqual(r.returncode, 0, r.stdout)
        rec = self.plan_rec(plan)
        self.assertEqual([s["mode"] for s in rec["steps"]], ["read", "apply", "read"])
        self.assertEqual(rec["warnings"], [])
        self.assertEqual(json.loads(self.cli("show", plan.name, "--json").stdout)["status"], "done")
        self.assertEqual(json.loads(self.cli("ls", "--kind", "plan", "--json").stdout)[0]["id"], plan.name)

    def test_plan_stops_at_failure_and_runs_rollback_only_in_apply(self) -> None:
        marker = self.tmp / "rolled-back"
        ok = self.new("prep", "print(1)\n")
        bad = self.new("bad", "import sys\n\nsys.exit(1)\n", "--changes-prod", "--rollback", "undo")
        after = self.new("after", "print(1)\n")
        undo = self.new("undo", f"from pathlib import Path\n\nPath({str(marker)!r}).touch()\n")
        plan = self.make_plan(ok, bad, after, extra={"1": {"on_failure": undo.name}})
        self.assertEqual(self.cli("run", plan.name).returncode, 1)
        self.assertFalse(marker.exists())
        r = self.cli("run", plan.name, "--apply")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("no dry run covers the apply of step", self.plan_rec(plan)["last_error"])
        self.assertFalse(marker.exists(), "a refused plan runs no step and no rollback")
        r = self.cli("run", plan.name, "--apply", "--skip-dry", "its dry run fails the same way")
        self.assertEqual(r.returncode, 1, r.stderr)
        rec = self.plan_rec(plan)
        self.assertEqual(rec["status"], "failed")
        self.assertEqual([s["status"] for s in rec["steps"]], ["ok", "failed", "skipped"])
        self.assertEqual(rec["steps"][1]["rollback"]["status"], "ok")
        self.assertTrue(marker.exists())
        self.assertIn("step 2", rec["last_error"])

    def test_plan_abort_skips_the_rest(self) -> None:
        change = self.new("change", self.CONFIRM_BODY, "--changes-prod", "--rollback", "undo")
        verify = self.new("verify", "print(1)\n")
        plan = self.make_plan(change, verify)
        r = self.cli("run", plan.name, "--apply", *self.NO_DRY, answers=[("[y/N]", "n")])
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertEqual([s["status"] for s in self.plan_rec(plan)["steps"]], ["aborted", "skipped"])

    def test_plan_blocks_before_any_step_on_lock_or_lint(self) -> None:
        first = self.new("first", "print(1)\n")
        broken = self.new("broken", "def f(x):\n    return x\n")
        plan = self.make_plan(first, broken)
        self.assertEqual(self.cli("run", plan.name).returncode, 2)
        rec = self.plan_rec(plan)
        self.assertIn("fails lint", rec["last_error"])
        self.assertEqual(self.cli("runs", "first", "--json").stdout.strip(), "[]")

        (broken / "main.py").write_text("print(1)\n")
        self.write_lock("web1", os.getpid())
        self.assertEqual(self.cli("run", plan.name).returncode, 2)
        self.assertIn("locked by other", self.plan_rec(plan)["last_error"])
        self.assertEqual(self.cli("runs", "first", "--json").stdout.strip(), "[]")

    def test_plan_ctrl_c_mid_step_releases_locks(self) -> None:
        body = "import time\n\nimport opsquire_helpers as sq\n\n\ndef main() -> None:\n"
        body += "    print('start', flush=True)\n    time.sleep(30)\n\n\nsq.run(main)\n"
        slow = self.new("slow", body)
        later = self.new("later", "print(1)\n")
        plan = self.make_plan(slow, later)
        p = self.start("run", plan.name)
        assert p.stdout
        p.stdout.readline()
        time.sleep(0.3)
        os.killpg(p.pid, signal.SIGINT)
        _, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 130, err)
        self.assertNotIn("Traceback", err)
        self.assertEqual([s["status"] for s in self.plan_rec(plan)["steps"]], ["interrupted", "skipped"])

    def test_plan_validation(self) -> None:
        a = self.new("a", "print(1)\n")
        plan = self.make_plan(a)
        meta = json.loads((plan / "meta.json").read_text())
        meta["targets"] = ["elsewhere"]
        meta["steps"].append({"task": plan.name})
        (plan / "meta.json").write_text(json.dumps(meta))
        errors = " ".join(json.loads(self.cli("show", plan.name, "--json").stdout)["meta_errors"])
        self.assertIn("plans do not nest", errors)
        self.assertEqual(self.cli("new", "web1", "empty", "--plan", "--purpose", "p").returncode, 2)
        self.assertIn("no arguments", self.cli("run", plan.name, "--", "x").stderr)

    def grill_env(self, questions: int) -> None:
        self.agents_file()
        self.env["FAKE_GRILL"] = str(questions)
        self.env["FAKE_STATE"] = str(self.tmp / "fake-state.json")

    def test_grill_asks_one_question_at_a_time_and_enter_accepts(self) -> None:
        self.grill_env(2)
        r = self.cli("ask", "build", "something", "new", answers=[("[Enter =", ""), ("[Enter =", "my own answer")])
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("RUN: opsquire run 20261030-1749_web1_restart-bots --apply", r.stdout)
        rec = json.loads(next((self.home / "var/asks").glob("*.json")).read_text())
        self.assertEqual([x["answer"] for x in rec["rounds"]], ["rec 1", "my own answer"])
        self.assertEqual(rec["status"], "ok")
        seen = json.loads((self.tmp / "fake-state.json").read_text())
        self.assertEqual(seen[1:], ["Answer: rec 1", "Answer: my own answer"])

    def test_questions_are_pinned_below_the_progress_on_a_real_terminal(self) -> None:
        self.grill_env(2)
        self.env["TERM"] = "xterm"
        cmd = [*CLI, "ask", "build", "something", "new"]
        r = run_in_pty(cmd, self.env, [("[Enter = rec 1]", ""), ("[Enter = rec 2]", "mine")], size=(24, 80))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("\x1b[1;21r", r.stdout)
        self.assertEqual(r.stdout.count("\x1b[r"), 2)
        self.assertIn("-> rec 1", r.stdout)
        self.assertIn("RUN: opsquire run 20261030-1749_web1_restart-bots --apply", r.stdout)
        rec = json.loads(next((self.home / "var/asks").glob("*.json")).read_text())
        self.assertEqual([x["answer"] for x in rec["rounds"]], ["rec 1", "mine"])

    def test_progress_while_a_question_is_pinned_scrolls_above_it(self) -> None:
        sys.path.insert(0, str(ROOT / "src"))
        from opsquire import pinned

        out = io.StringIO()
        self.assertFalse(pinned.line("nothing pinned"))
        pinned.Pin.out, pinned.Pin.bottom, pinned.Pin.cols = out, 20, 12
        try:
            self.assertTrue(pinned.line("a long progress\nline"))
        finally:
            pinned.Pin.out = None
        self.assertEqual(out.getvalue(), "\x1b7\x1b[20;1H\na long prog\x1b8")
        block = pinned.layout("which host?", "web1 " * 30, 60)
        self.assertEqual(block[-1], "  [Enter = recommended] > ")
        self.assertTrue(all(len(x) < 60 for x in block))

    def test_grill_without_terminal_waits_and_resumes(self) -> None:
        self.grill_env(1)
        p = self.start("ask", "build", "it")
        _, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0, err)
        self.assertIn("waiting on: question 1?", err)
        rec = json.loads(next((self.home / "var/asks").glob("*.json")).read_text())
        self.assertEqual(rec["status"], "waiting")
        r = self.cli("ask", "--resume", rec["ask_id"], "go", "with", "rec")
        self.assertIn("RUN:", r.stdout)

    def test_grill_ctrl_c_at_question_keeps_session(self) -> None:
        self.grill_env(2)
        r = self.cli("ask", "build", "it", answers=[("[Enter =", "\x03")])
        self.assertEqual(r.returncode, 130, r.stdout)
        self.assertNotIn("Traceback", r.stdout)
        rec = json.loads(next((self.home / "var/asks").glob("*.json")).read_text())
        self.assertEqual((rec["status"], rec["session"]), ("interrupted", "sess-new"))

    def test_no_grill_skips_questions(self) -> None:
        self.grill_env(3)
        r = self.cli("ask", "--no-grill", "build", "it")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("RUN:", r.stdout)
        self.assertIn("Do not ask questions", r.stdout)

    def test_prompt_appears_after_everything_printed_before_it(self) -> None:
        py = "import opsquire_helpers as sq\n\nfor i in range(300):\n    print(f'plan line {i}')\nsq.confirm('Go?')\n"
        sh = BASH_HEADER + 'for i in $(seq 0 299); do echo "plan line $i"; done\nsq_confirm "Go?"\n'
        for name, body, lang in (("order-py", py, "py"), ("order-sh", sh, "sh")):
            self.new(name, body, "--changes-prod", "--rollback", "x", lang=lang)
            r = self.cli("run", name, "--apply", *self.NO_DRY, answers=[("[y/N]", "y")])
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertLess(r.stdout.index("plan line 299"), r.stdout.index(">>> Go? [y/N]"), name)

    def fake_running(self, pid_ns: str, beat_age: float | None) -> str:
        self.new("ghost", "print(1)\n")
        self.assertEqual(self.cli("run", "ghost").returncode, 0)
        rec = self.last("ghost")
        run_dir = Path(rec["paths"]["dir"])
        dead = subprocess.Popen(["true"])
        dead.wait()
        rec |= {"status": "running", "pid": dead.pid, "pid_ns": pid_ns}
        (run_dir / "run.json").write_text(json.dumps(rec))
        beat = run_dir / "heartbeat"
        if beat_age is None:
            beat.unlink()
        else:
            os.utime(beat, (time.time() - beat_age, time.time() - beat_age))
        return str(self.last("ghost")["status"])

    def own_ns(self) -> str:
        return os.readlink("/proc/self/ns/pid")

    def test_liveness_from_another_pid_namespace_uses_the_heartbeat(self) -> None:
        self.assertEqual(self.fake_running("pid:[1]", 2), "running")

    def test_liveness_stale_heartbeat_means_dead(self) -> None:
        self.assertEqual(self.fake_running("pid:[1]", 120), "interrupted")

    def test_liveness_same_namespace_trusts_the_kernel(self) -> None:
        self.assertEqual(self.fake_running(self.own_ns(), 2), "interrupted")

    def test_live_run_beats_and_locks_follow_it(self) -> None:
        body = "import time\n\nimport opsquire_helpers as sq\n\n\ndef main() -> None:\n"
        body += "    print('start', flush=True)\n    time.sleep(120)\n\n\nsq.run(main)\n"
        self.new("beating", body)
        p = self.start("run", "beating")
        assert p.stdout
        p.stdout.readline()
        beat = next((self.home / "var/runs").rglob("heartbeat"))
        first = beat.stat().st_mtime
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and beat.stat().st_mtime == first:
            time.sleep(0.2)
        self.assertGreater(beat.stat().st_mtime, first)
        held = json.loads(self.cli("locks", "--json").stdout)
        self.assertTrue(held["web1"][0]["alive"])
        self.assertEqual(held["web1"][0]["pid_ns"], self.own_ns())
        os.killpg(p.pid, signal.SIGINT)
        p.communicate(timeout=60)
        self.assertEqual(p.returncode, 130)
        self.assertEqual(json.loads(self.cli("locks", "--json").stdout), {})

    PASSING_TEST = (
        "import unittest\n\nfrom logic import double\n\n\nclass T(unittest.TestCase):\n"
        "    def test_double(self) -> None:\n        self.assertEqual(double(2), 4)\n\n"
        "    def test_zero(self) -> None:\n        self.assertEqual(double(0), 0)\n"
    )

    def test_ctrl_c_during_a_lint_tool_is_an_interruption_not_a_verdict(self) -> None:
        """Ctrl-C reaches the lint tools too (same process group); their death must not read as a lint failure, in
        the main thread or in an ask's worker thread, and no lint stamp may record it."""
        folder = self.new("tooled", "print(1)\n")
        fake = self.tmp / "ruff-sigint"
        fake.write_text("#!/bin/sh\nkill -INT $$\n")
        fake.chmod(0o755)
        self.env["OPSQUIRE_RUFF"] = str(fake)
        r = self.cli("lint", "tooled")
        self.assertEqual(r.returncode, 130, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertIn("opsquire: interrupted", r.stderr)
        self.assertFalse((folder / ".lint.json").exists())

        sys.path.insert(0, str(ROOT / "src"))
        import threading

        from opsquire.lint import lint_task, save_lint
        from opsquire.model import resolve_task

        results: list[Any] = []
        with mock.patch.dict(os.environ, {"OPSQUIRE_HOME": str(self.home), "OPSQUIRE_RUFF": str(fake)}):
            task = resolve_task("tooled")
            t = threading.Thread(target=lambda: results.append(lint_task(task)))
            t.start()
            t.join(timeout=30)
            save_lint(task, results[0])
        self.assertEqual((results[0].ok, results[0].interrupted), (False, True))
        self.assertIn("interrupted: ruff-sigint was stopped by Ctrl-C", results[0].output)
        self.assertNotIn("Traceback", results[0].output)
        self.assertFalse((folder / ".lint.json").exists())

    def test_unit_tests_gate_lint_and_runs(self) -> None:
        folder = self.new("tested", "from logic import double\n\nprint(double(21))\n")
        (folder / "logic.py").write_text("def double(x: int) -> int:\n    return x * 2\n")
        (folder / "test_logic.py").write_text(self.PASSING_TEST)
        r = self.cli("lint", "tested")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("LINT OK (2 tests)", r.stdout)
        self.assertEqual(self.cli("run", "tested").returncode, 0)
        self.assertEqual(self.last("tested")["tests"], 2)
        self.assertEqual(list(folder.rglob("__pycache__")), [])

        (folder / "logic.py").write_text("def double(x: int) -> int:\n    return x * 3\n")
        r = self.cli("run", "tested")
        self.assertEqual(r.returncode, 2, r.stderr)
        rec = self.last("tested")
        self.assertEqual(rec["status"], "blocked")
        self.assertIn("lint or tests failed", rec["last_error"])
        self.assertIn("FAILED (2 tests)", r.stderr)

    def test_tests_of_used_general_scripts_run_too_and_empty_test_files_fail(self) -> None:
        g = Path(self.cli("new", "web1", "shared", "--general", "--purpose", "shared logic").stdout.strip())
        (g / "logic.py").write_text("def double(x: int) -> int:\n    return x * 2\n")
        (g / "test_logic.py").write_text(self.PASSING_TEST)
        self.new("user", "print(1)\n", "--uses", "web1_shared")
        self.assertIn("LINT OK (2 tests)", self.cli("lint", "user").stdout)
        empty = self.new("empty-tests", "print(1)\n")
        (empty / "test_nothing.py").write_text("import unittest\n\n\nclass T(unittest.TestCase):\n    pass\n")
        r = self.cli("lint", "empty-tests")
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAILED (0 tests)", r.stdout)

    def test_terminal_is_quiet_by_default_and_full_with_verbose(self) -> None:
        body = "import opsquire_helpers as sq\n\nsq.step('working')\nsq.data('internal', rows=3)\nprint('plain line')\n"
        self.new("quiet", body)
        r = self.cli("run", "quiet")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout + r.stderr
        for hidden in ("[spawn]", "[lint]", "[lock]", "[data]", "@@OPSQUIRE"):
            self.assertNotIn(hidden, out)
        self.assertIn("[step] working", out)
        self.assertIn("plain line", out)
        self.assertRegex(r.stderr, r"OK in [0-9.]+s; details: opsquire log \S+quiet")
        log = Path(self.last("quiet")["paths"]["output"]).read_text()
        self.assertIn("[spawn]", log)
        self.assertIn("@@OPSQUIRE data: internal", log)
        loud = self.cli("run", "quiet", "-v")
        self.assertIn("[spawn]", loud.stderr)
        self.assertIn("[data] internal", loud.stdout)

    def test_docs_catalog_and_api_pages_follow_the_code(self) -> None:
        g = Path(self.cli("new", "web1", "shared", "--general", "--purpose", "shared logic").stdout.strip())
        code = '"""Doubling helpers."""\n\n\ndef double(x: int) -> int:\n    """Twice x."""\n    return x * 2\n'
        (g / "logic.py").write_text(code)
        (g / "test_logic.py").write_text(self.PASSING_TEST)
        self.new("user", "print(1)\n", "--uses", "web1_shared")
        self.assertEqual(self.cli("lint", "web1_shared").returncode, 0)
        catalog = (self.home / "docs/CATALOG.md").read_text()
        self.assertIn("**web1_shared**: shared logic (lint-ok; tests: 2; used by 1; never run)", catalog)
        self.assertRegex(catalog, r"\d{8}-\d{4}_web1_user \[draft, task; never run\]: test user \(uses web1_shared\)")
        self.assertEqual(self.cli("run", "web1_user").returncode, 0)
        self.assertEqual(self.cli("run", "web1_user").returncode, 0)
        catalog = (self.home / "docs/CATALOG.md").read_text()
        self.assertIn("used by 1; runs 2, 2 ok read)", catalog)
        self.assertRegex(catalog, r"web1_user \[done, task; runs 2, 2 ok read\]")
        page = (self.home / "docs/web1_shared.md").read_text()
        self.assertIn("- runs (it and its users): runs 2, 2 ok read, last 20", page)
        page = (self.home / "docs/web1_shared.md").read_text()
        self.assertIn("`double(x: int) -> int`: Twice x.", page)
        self.assertIn("T: double", page)
        self.assertIn("Doubling helpers.", page)
        shutil.rmtree(g)
        self.assertEqual(self.cli("docs").returncode, 0)
        self.assertFalse((self.home / "docs/web1_shared.md").exists())

    PROGRESS_BODY = (
        "import json\n\n"
        "for i in range(51):\n"
        "    ev = {'event': 'progress', 'msg': 'download', 'done': i * 2 << 20, 'total': 100 << 20, 'unit': 'B'}\n"
        "    print('@@OPSQUIRE ' + json.dumps(ev), flush=True)\n"
        "    if i == 25:\n"
        "        print('halfway note', flush=True)\n"
    )

    def test_progress_without_terminal_prints_every_ten_percent_and_logs_sparingly(self) -> None:
        self.new("prog", self.PROGRESS_BODY)
        r = self.cli("run", "prog")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = [x for x in r.stdout.splitlines() if x.startswith("download [")]
        self.assertLessEqual(len(lines), 12)
        self.assertIn("100/100 MB 100%", lines[-1])
        self.assertIn("halfway note", r.stdout)
        rec = self.last("prog")
        events = [e for e in self.events(rec) if e["event"] == "progress"]
        self.assertTrue(2 <= len(events) <= 12, len(events))
        self.assertEqual(events[-1]["done"], events[-1]["total"])

    def test_progress_on_a_terminal_redraws_one_line_and_steps_print_above_it(self) -> None:
        self.new("progtty", self.PROGRESS_BODY)
        r = self.cli("run", "progtty", answers=[])
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("\r\x1b[K", r.stdout)
        self.assertIn("█", r.stdout)
        self.assertRegex(r.stdout, r"\r\x1b\[Khalfway note")

    def test_bash_progress_helper(self) -> None:
        body = BASH_HEADER + "sq_progress files 3 10\nsq_progress files 10 10\n"
        self.new("progsh", body, lang="sh")
        r = self.cli("run", "progsh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("files [", r.stdout)
        self.assertIn("10/10 100%", r.stdout)

    def test_progress_formatting(self) -> None:
        sys.path.insert(0, str(ROOT / "src"))
        from opsquire import progress

        self.assertEqual(progress.human(113 << 20, "B"), "113 MB")
        self.assertEqual(progress.human(1.5 * (1 << 20), "B"), "1.5 MB")
        self.assertEqual(progress.amount(113 << 20, 181 << 20, "B"), "113/181 MB")
        self.assertEqual(progress.amount(900 << 10, 2 << 20, "B"), "900 KB/2.0 MB")
        self.assertEqual(progress.amount(3, 10, "files"), "3/10 files")
        self.assertEqual(progress.duration(42), "~42 s")
        self.assertEqual(progress.duration(300), "~5 min")

    def git_home(self) -> None:
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.com\n")
        self.env |= {"GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1"}
        self.env |= {"HOME": str(self.tmp / "user"), "CLAUDE_CONFIG_DIR": str(self.tmp / "claude")}
        self.env["PYTHONUSERBASE"] = os.environ.get("PYTHONUSERBASE", str(Path.home() / ".local"))
        r = self.cli("integrate")
        self.assertIn("home repo: initialized a local git repository", r.stdout, r.stderr)

    def git(self, *args: str) -> str:
        r = subprocess.run(["git", "-C", str(self.home), *args], env=self.env, capture_output=True, text=True)
        return r.stdout

    def asks(self) -> list[dict[str, Any]]:
        return [json.loads(p.read_text()) for p in sorted((self.home / "var/asks").glob("*.json"))]

    def split(self, *pieces: str) -> Path:
        self.git_home()
        self.agents_file()
        log = self.tmp / "agent-log.jsonl"
        self.env |= {"FAKE_SPLIT": json.dumps(list(pieces)), "FAKE_LOG": str(log)}
        return log

    def piece_env(self, opened: subprocess.CompletedProcess[str]) -> dict[str, str]:
        self.assertEqual(opened.returncode, 0, opened.stdout + opened.stderr)
        info = json.loads(opened.stdout)
        self.assertTrue(info["worktree"].startswith(str(self.home / ".worktrees/")), info)
        self.assertEqual(info["env"]["OPSQUIRE_VAR"], str(self.home / "var"))
        self.assertIn(info["worktree"], info["brief"])
        env: dict[str, str] = {**self.env, **info["env"]}
        return env

    def piece_task(self, env: dict[str, str], name: str, body: str) -> str:
        r = subprocess.run(
            [*CLI, "new", "web1", name, "--purpose", f"piece {name}"], env=env, capture_output=True, text=True
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        folder = Path(r.stdout.strip())
        self.assertTrue(str(folder).startswith(env["OPSQUIRE_HOME"]), folder)
        (folder / "main.py").write_text(body)
        return folder.name

    def test_piece_open_and_land_puts_a_subagents_task_into_the_home(self) -> None:
        self.git_home()
        self.env["OPSQUIRE_AUTHOR"] = "claude-code 26f643b0"
        env = self.piece_env(self.cli("piece", "open", "alpha", "--json", "--request", "print alpha"))
        self.assertEqual(json.loads(self.cli("piece", "ls", "--json").stdout)[0]["group"], "claude-code-26f643b0")
        self.assertIn("print alpha", self.cli("piece", "ls").stdout)
        tid = self.piece_task(env, "alpha", "print('alpha')\n")
        self.assertEqual(list(self.home.glob(f"tasks/*/{tid}")), [])
        r = self.cli("piece", "land", "alpha")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"{tid}: ready", r.stdout)
        self.assertEqual(json.loads(self.cli("show", tid, "--json").stdout)["status"], "ready")
        self.assertEqual(
            json.loads(self.cli("show", tid, "--json").stdout)["meta"]["author"], "claude-code 26f643b0 piece alpha"
        )
        self.assertFalse((self.home / ".worktrees").exists())
        self.assertEqual(self.git("branch", "--list", "piece/*"), "")
        self.assertEqual(self.git("status", "--short"), "")
        self.assertEqual(self.cli("piece", "ls").stdout.count("\n"), 1)
        r = self.cli("run", tid)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.last(tid)["status"], "ok")

    def test_piece_clash_is_reported_and_lands_after_the_markers_are_resolved(self) -> None:
        self.git_home()
        r = self.cli("new", "web1", "shared", "--general", "--purpose", "shared", "--lang", "py")
        self.assertEqual(r.returncode, 0, r.stderr)
        shared = Path(r.stdout.strip()) / "main.py"
        self.assertEqual(shared.parent, self.home / "general/web1_shared", r.stdout)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "shared")
        one = self.piece_env(self.cli("piece", "open", "one", "--group", "g", "--json"))
        two = self.piece_env(self.cli("piece", "open", "two", "--group", "g", "--json"))
        (Path(one["OPSQUIRE_HOME"]) / "general/web1_shared/main.py").write_text('"""one."""\n')
        (Path(two["OPSQUIRE_HOME"]) / "general/web1_shared/main.py").write_text('"""two."""\n')
        self.assertEqual(self.cli("piece", "land", "g/one").returncode, 0)
        r = self.cli("piece", "land", "two")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("conflicts in: general/web1_shared/main.py", r.stdout)
        self.assertEqual(shared.read_text(), '"""one."""\n')
        clashed = Path(two["OPSQUIRE_HOME"]) / "general/web1_shared/main.py"
        self.assertIn("<<<<<<<", clashed.read_text())
        r = self.cli("piece", "land", "two")
        self.assertEqual(r.returncode, 1)
        clashed.write_text('"""one and two."""\n')
        r = self.cli("piece", "land", "two")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(shared.read_text(), '"""one and two."""\n')
        self.assertFalse((self.home / ".worktrees").exists())
        self.assertEqual(self.git("status", "--short"), "")

    def test_piece_drop_parks_the_work_and_open_resumes_it(self) -> None:
        self.git_home()
        env = self.piece_env(self.cli("piece", "open", "slow", "--group", "g", "--json", "--request", "a slow piece"))
        tid = self.piece_task(env, "slow", "print('slow')\n")
        r = self.cli("piece", "drop", "slow")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("parked on piece/g/slow", r.stdout)
        self.assertFalse((self.home / ".worktrees").exists())
        self.assertIn("parked", self.cli("piece", "ls").stdout)
        self.assertIn("is parked", self.cli("piece", "land", "slow").stderr)
        env = self.piece_env(self.cli("piece", "open", "slow", "--group", "g", "--json"))
        self.assertEqual(json.loads(self.cli("piece", "ls", "--json").stdout)[0]["request"], "a slow piece")
        self.assertTrue((Path(env["OPSQUIRE_HOME"]) / f"tasks/{tid[:4]}-{tid[4:6]}/{tid}/main.py").is_file())
        self.assertEqual(self.cli("piece", "land", "slow").returncode, 0)
        self.assertEqual(json.loads(self.cli("show", tid, "--json").stdout)["status"], "ready")
        env = self.piece_env(self.cli("piece", "open", "gone", "--group", "g", "--json"))
        self.assertIn("discarded", self.cli("piece", "drop", "gone", "--delete").stdout)
        self.assertEqual(self.git("branch", "--list", "piece/*"), "")

    def test_pieces_do_not_nest(self) -> None:
        self.git_home()
        env = self.piece_env(self.cli("piece", "open", "outer", "--group", "g", "--json"))
        r = subprocess.run([*CLI, "piece", "open", "inner"], env=env, capture_output=True, text=True)
        self.assertIn("pieces do not nest", r.stderr)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already open", self.cli("piece", "open", "outer", "--group", "g").stderr)

    def test_unreadable_creds_file_blocks_only_tasks_that_declare_credentials(self) -> None:
        self.creds.chmod(0o000)
        self.new("no-creds", "import opsquire_helpers as sq\n\nsq.result('fine')\n")
        r = self.cli("run", "no-creds")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rec = self.last("no-creds")
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["counts"]["warn"], 1)
        self.assertIn("not readable", r.stdout + r.stderr)
        self.new("with-creds", "print('x')\n", "--cred", "TOKEN=influx.prod.token")
        r = self.cli("run", "with-creds")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(self.last("with-creds")["status"], "blocked")
        self.assertIn("cannot read", self.last("with-creds")["last_error"])

    def test_state_infers_threads_from_sessions_links_and_mentions(self) -> None:
        self.env["OPSQUIRE_AUTHOR"] = "claude-code 26f643b0"
        a = self.new("alpha", "print('a')\n", "--link", "https://github.com/x/y/pull/1").name
        b = self.new("beta", "print('b')\n").name
        self.env["OPSQUIRE_AUTHOR"] = "claude-code (chat)"
        c = self.new("gamma", "print('c')\n", "--link", "https://github.com/x/y/pull/1/").name
        d = self.new("delta", "print('d')\n", "--purpose", f"follows {c} to verify").name
        e = self.new("eps", "print('e')\n").name
        f = self.new("zeta", "print('f')\n", "--thread", "named-story").name
        g = self.new("eta", "print('g')\n", "--thread", "named-story", "--link", "https://slack/1").name
        h = self.new("theta", "print('h')\n", "--thread", "other-story", "--link", "https://slack/1").name
        r = self.cli("threads", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        state = json.loads(r.stdout)
        of = {tid: t["thread"] for tid, t in state["tasks"].items()}
        self.assertEqual(len({of[a], of[b], of[c], of[d]}), 1, of)
        self.assertEqual(of[a], a)
        self.assertNotEqual(of[e], of[a])
        self.assertEqual((of[f], of[g], of[h]), ("named-story", "named-story", "other-story"))
        self.assertTrue(state["tasks"][f]["thread_explicit"])
        titles = {th["id"]: th["title"] for th in state["threads"]}
        self.assertEqual(titles["named-story"], "named story")
        self.assertEqual(titles[a], "alpha (web1)")
        self.assertEqual({th["state"] for th in state["threads"]}, {"open"})
        self.assertEqual(self.cli("new", "web1", "bad", "--purpose", "p", "--thread", "Bad Name").returncode, 2)

    def test_state_suggestions_and_thread_state(self) -> None:
        tid = self.new(
            "deploy",
            "import opsquire_helpers as sq\n\nsq.target('web1', 'ok')\n",
            "--changes-prod",
            "--rollback",
            "none",
        ).name
        for _ in range(3):
            self.assertEqual(self.cli("run", tid).returncode, 0)
        self.assertEqual(self.cli("run", tid, "--apply", answers=[("", "y")]).returncode, 0)
        state = json.loads(self.cli("threads", "--json").stdout)
        kinds = [s["kind"] for s in state["tasks"][tid]["suggestions"]]
        self.assertEqual(kinds, ["rerun-heavy"], state["tasks"][tid]["suggestions"])
        self.assertEqual(state["threads"][0]["state"], "done")
        meta_path = self.home / f"tasks/{tid[:4]}-{tid[4:6]}/{tid}/meta.json"
        meta = json.loads(meta_path.read_text())
        del meta["outcome"]
        meta_path.write_text(json.dumps(meta))
        (meta_path.parent / "design.md").unlink()
        state = json.loads(self.cli("threads", "--json").stdout)
        kinds = [s["kind"] for s in state["tasks"][tid]["suggestions"]]
        self.assertEqual(kinds, ["awaiting-close", "rerun-heavy"])
        self.assertEqual(state["threads"][0]["state"], "open")
        self.assertEqual(self.cli("close", tid, "--outcome", "done", "--note", "landed by hand").returncode, 0)
        state = json.loads(self.cli("threads", "--json").stdout)
        kinds = [s["kind"] for s in state["tasks"][tid]["suggestions"]]
        self.assertEqual(kinds, ["no-result", "rerun-heavy"])
        self.assertEqual(state["threads"][0]["state"], "done")
        old = self.new("old", "print('x')\n").name
        meta = json.loads((self.home / f"tasks/{old[:4]}-{old[4:6]}/{old}/meta.json").read_text())
        meta["created"] = "2026-01-01T00:00:00.000Z"
        (self.home / f"tasks/{old[:4]}-{old[4:6]}/{old}/meta.json").write_text(json.dumps(meta))
        state = json.loads(self.cli("threads", "--json").stdout)
        self.assertEqual([s["kind"] for s in state["tasks"][old]["suggestions"]], ["stale"])
        self.assertEqual(state["runs"][state["tasks"][tid]["runs"][0]]["targets"], {"web1": "ok"})

    def test_ui_serves_state_stream_log_file_and_close(self) -> None:
        import http.client

        tid = self.new("served", "import opsquire_helpers as sq\n\nsq.result('served fine')\n").name
        self.assertEqual(self.cli("run", tid).returncode, 0)
        (self.home / f"tasks/{tid[:4]}-{tid[4:6]}/{tid}/design.md").write_text("# d\n")
        port = free_port()
        proc = subprocess.Popen([*CLI, "ui", "--port", str(port)], env=self.env, stdout=subprocess.PIPE, text=True)
        try:
            self.assertIn(f"http://127.0.0.1:{port}/", proc.stdout.readline() if proc.stdout else "")
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)

            def get(path: str) -> tuple[int, Any]:
                conn.request("GET", path)
                resp = conn.getresponse()
                body = resp.read()
                return resp.status, (
                    json.loads(body) if resp.getheader("Content-Type", "").startswith("application/json") else body
                )

            status, page = get("/")
            self.assertEqual(status, 200)
            self.assertIn(b"<title>Opsquire</title>", page)
            status, state = get("/api/state")
            self.assertEqual(status, 200)
            self.assertEqual(state["tasks"][tid]["status"], "done")
            run_id = state["tasks"][tid]["runs"][0]
            status, log = get(f"/api/log?run={run_id}")
            self.assertEqual((status, log["status"], log["start"]), (200, "ok", 0))
            self.assertIn("served fine", log["text"])
            self.assertEqual(get(f"/api/log?run={run_id}&offset={log['offset']}")[1]["text"], "")
            head = get(f"/api/log?run={run_id}&offset=0&until=10")[1]
            self.assertEqual((len(head["text"]), head["offset"]), (10, 10))
            self.assertEqual(state["tasks"][tid]["entry"], "main.py")
            self.assertEqual(get("/api/log?run=nope")[0], 400)
            self.assertEqual(get(f"/api/file?task={tid}&name=design.md")[1]["text"], "# d\n")
            self.assertEqual(get(f"/api/file?task={tid}&name=../LOCAL.md")[0], 400)
            self.assertEqual(get(f"/api/file?task={tid}&name=nothing.md")[0], 400)
            body = json.dumps({"task": tid, "outcome": "partial", "note": "half done", "run": run_id})
            conn.request("POST", "/api/close", body, {"Content-Type": "application/json"})
            resp = conn.getresponse()
            closed = json.loads(resp.read())
            self.assertEqual(
                (resp.status, closed["outcome"]["state"], closed["outcome"]["run"]), (200, "partial", run_id)
            )
            self.assertTrue(any("Result" in n for n in closed["notes"]), closed)
            conn.request(
                "POST",
                "/api/close",
                json.dumps({"task": tid, "outcome": "nope", "note": "x"}),
                {"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            self.assertEqual(resp.status, 400)
            resp.read()
            self.assertEqual(json.loads(self.cli("show", tid, "--json").stdout)["status"], "partial")
            stream = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            stream.request("GET", "/api/stream")
            sresp = stream.getresponse()
            self.assertIn(b"event: hello", sresp.readline() + sresp.readline())
            self.new("later", "print('later')\n")
            deadline = time.time() + 5
            seen = b""
            while b"event: change" not in seen and time.time() < deadline:
                seen += sresp.readline()
            self.assertIn(b"event: change", seen)
            stream.close()
            conn.close()
        finally:
            proc.send_signal(signal.SIGINT)
            self.assertEqual(proc.wait(timeout=10), 0)

    def test_ui_streams_every_change_that_moves_the_inbox_to_every_page(self) -> None:
        import http.client

        folder = self.new("handed", "print('v1')\n")
        port = free_port()
        proc = subprocess.Popen([*CLI, "ui", "--port", str(port)], env=self.env, stdout=subprocess.PIPE, text=True)
        try:
            self.assertIn(f"http://127.0.0.1:{port}/", proc.stdout.readline() if proc.stdout else "")
            streams = []
            for _ in range(2):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                conn.request("GET", "/api/stream")
                resp = conn.getresponse()
                self.assertIn(b"event: hello", resp.readline() + resp.readline())
                streams.append((conn, resp))

            def inbox() -> list[dict[str, Any]]:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                conn.request("GET", "/api/state")
                items: list[dict[str, Any]] = json.loads(conn.getresponse().read())["inbox"]
                conn.close()
                return items

            def every_page_hears_a_change() -> None:
                for _, resp in streams:
                    seen = b""
                    while b"event: change" not in seen:
                        line = resp.readline()
                        self.assertTrue(line, "stream closed")
                        seen += line

            self.assertEqual(inbox(), [])
            self.assertEqual(self.cli("lint", folder.name).returncode, 0)
            every_page_hears_a_change()
            self.assertEqual([(i["task_id"], i["changed_since_lint"]) for i in inbox()], [(folder.name, False)])
            (folder / "main.py").write_text("print('v2')\n")
            every_page_hears_a_change()
            self.assertEqual([(i["task_id"], i["changed_since_lint"]) for i in inbox()], [(folder.name, True)])
            for conn, _ in streams:
                conn.close()
        finally:
            proc.send_signal(signal.SIGINT)
            self.assertEqual(proc.wait(timeout=10), 0)

    def test_ok_apply_closes_the_task_itself_and_close_can_amend(self) -> None:
        body = "import opsquire_helpers as sq\n\nsq.target('web1', 'ok', 'restarted')\nsq.result('web1 on v2')\n"
        tid = self.new("auto", body, "--changes-prod", "--rollback", "none").name
        self.assertEqual(self.cli("run", tid).returncode, 0)
        meta_path = self.home / f"tasks/{tid[:4]}-{tid[4:6]}/{tid}/meta.json"
        self.assertIsNone(json.loads(meta_path.read_text())["outcome"])
        r = self.cli("run", tid, "--apply", answers=[("", "y")])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("closed done", r.stdout)
        outcome = json.loads(meta_path.read_text())["outcome"]
        self.assertEqual((outcome["state"], outcome["closed_by"]), ("done", "opsquire run (auto)"))
        self.assertIn("ok on web1", outcome["note"])
        self.assertIn("web1 on v2", outcome["note"])
        runs = json.loads(self.cli("runs", tid, "--json").stdout)
        self.assertEqual([r["mode"] for r in runs][-2:], ["apply", "verify"])
        self.assertEqual(outcome["run"], runs[-2]["run_id"])
        design = (meta_path.parent / "design.md").read_text()
        self.assertIn("## Result", design)
        self.assertIn("- web1 on v2", design)
        state = json.loads(self.cli("threads", "--json").stdout)
        self.assertEqual(state["tasks"][tid]["suggestions"], [])
        self.assertEqual(state["threads"][0]["state"], "done")
        self.assertEqual(
            self.cli("close", tid, "--outcome", "partial", "--note", "web1 rolled back later").returncode, 0
        )
        self.assertEqual(json.loads(meta_path.read_text())["outcome"]["state"], "partial")

    def test_ui_inbox_and_jobs_run_tasks_with_prompts(self) -> None:
        import http.client

        body = (
            "import opsquire_helpers as sq\n\nif sq.APPLY:\n    sq.confirm('Really change web1?')\n"
            "sq.target('web1', 'ok')\nsq.result('changed' if sq.APPLY else 'would change')\n"
        )
        tid = self.new("job", body, "--changes-prod", "--rollback", "none").name
        self.assertEqual(self.cli("lint", tid).returncode, 0)
        port = free_port()
        proc = subprocess.Popen([*CLI, "ui", "--port", str(port)], env=self.env, stdout=subprocess.PIPE, text=True)
        try:
            self.assertIn("opsquire ui", proc.stdout.readline() if proc.stdout else "")
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)

            def call(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
                conn.request(method, path, json.dumps(body) if body else None, {"Content-Type": "application/json"})
                resp = conn.getresponse()
                return resp.status, json.loads(resp.read())

            def wait_job(job: str, status: str) -> dict[str, Any]:
                deadline = time.time() + 30
                while time.time() < deadline:
                    j: dict[str, Any] = call("GET", f"/api/job?id={job}&offset=0")[1]
                    if j["status"] == status:
                        return j
                    time.sleep(0.2)
                self.fail(f"job {job} never reached {status}: {j}")

            _, state = call("GET", "/api/state")
            self.assertEqual([(i["task_id"], i["next"]) for i in state["inbox"]], [(tid, "dry")])
            self.assertEqual(state["jobs"], [])
            self.assertEqual(self.cli("run", tid).returncode, 0)
            status, started = call("POST", "/api/run", {"task": tid, "mode": "apply"})
            self.assertEqual(status, 200, started)
            j = wait_job(started["job_id"], "waiting")
            self.assertIn("Really change web1? [y/N]", j["prompt"])
            self.assertIn("Really change web1?", j["text"])
            status, again = call("POST", "/api/run", {"task": tid, "mode": "apply"})
            self.assertEqual((status, again["job_id"], again["joined"]), (200, started["job_id"], True))
            status, other = call("POST", "/api/run", {"task": tid, "mode": "dry"})
            self.assertEqual(status, 409)
            self.assertIn("already has a apply job running", other["error"])
            self.assertEqual(call("POST", "/api/answer", {"job": started["job_id"], "text": "n"})[0], 200)
            j = wait_job(started["job_id"], "done")
            self.assertEqual(j["exit_code"], 3)
            self.assertEqual(self.last(tid)["status"], "aborted")
            self.assertEqual(j["run_id"], self.last(tid)["run_id"])
            _, state = call("GET", "/api/state")
            self.assertEqual([(i["task_id"], i["next"]) for i in state["inbox"]], [(tid, "dry")])
            status, started = call("POST", "/api/run", {"task": tid, "mode": "dry"})
            j = wait_job(started["job_id"], "done")
            self.assertEqual((j["exit_code"], self.last(tid)["status"]), (0, "ok"))
            _, state = call("GET", "/api/state")
            self.assertEqual([(i["task_id"], i["next"]) for i in state["inbox"]], [(tid, "apply")])
            status, started = call("POST", "/api/run", {"task": tid, "mode": "apply"})
            j = wait_job(started["job_id"], "waiting")
            self.assertEqual(call("POST", "/api/answer", {"job": started["job_id"], "text": "y"})[0], 200)
            j = wait_job(started["job_id"], "done")
            self.assertEqual(j["exit_code"], 0, j["text"])
            self.assertIn("closed done", j["text"])
            _, state = call("GET", "/api/state")
            self.assertEqual(state["inbox"], [])
            self.assertEqual(state["tasks"][tid]["status"], "done")
            self.assertEqual(state["tasks"][tid]["outcome"]["closed_by"], "opsquire run (auto)")
            self.assertEqual(len(state["jobs"]), 3)
            self.assertEqual(call("POST", "/api/run", {"task": tid, "mode": "read"})[0], 400)
            conn.close()
        finally:
            proc.send_signal(signal.SIGINT)
            self.assertEqual(proc.wait(timeout=15), 0)

    def test_ui_job_runs_the_handed_over_args_the_inbox_shows(self) -> None:
        import http.client

        show = "import sys\n\nimport opsquire_helpers as sq\n\nsq.result(repr(sys.argv[1:]))\n"
        read = self.new("reused", show).name
        change = self.new(
            "flip",
            show.replace("sq.result", "sq.confirm('go?') if sq.APPLY else None\nsq.result"),
            "--changes-prod",
            "--rollback",
            "none",
        ).name
        for tid in (read, change):
            self.assertEqual(self.cli("lint", tid).returncode, 0)
        self.assertEqual(self.cli("run", read).returncode, 0)
        with_space = ["focus", "two words"]
        self.assertEqual(self.cli("handover", read, "--", *with_space).returncode, 0)
        self.assertEqual(self.cli("run", change, "--", "web1").returncode, 0)
        self.assertEqual(self.cli("handover", change, "--apply", "--", "web1").returncode, 0)
        port = free_port()
        proc = subprocess.Popen([*CLI, "ui", "--port", str(port)], env=self.env, stdout=subprocess.PIPE, text=True)
        try:
            self.assertIn("opsquire ui", proc.stdout.readline() if proc.stdout else "")
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)

            def call(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                conn.request(method, path, json.dumps(body) if body else None, {"Content-Type": "application/json"})
                return json.loads(conn.getresponse().read())

            def run(tid: str, mode: str, **extra: Any) -> dict[str, Any]:
                job = call("POST", "/api/run", {"task": tid, "mode": mode, **extra})["job_id"]
                deadline = time.time() + 30
                while time.time() < deadline:
                    j: dict[str, Any] = call("GET", f"/api/job?id={job}&offset=0")
                    if j["status"] == "waiting":
                        call("POST", "/api/answer", {"job": job, "text": "y"})
                    if j["status"] == "done":
                        return j
                    time.sleep(0.2)
                self.fail(f"job {job} never finished: {j}")

            inbox = {i["task_id"]: i for i in call("GET", "/api/state")["inbox"]}
            self.assertEqual(inbox[read]["command"], f"opsquire run {read} -- focus 'two words'")
            self.assertEqual(inbox[read]["args"], "focus 'two words'")

            j = run(read, "read", args=["from", "the", "page"])
            self.assertEqual(j["args"], "focus 'two words'")
            rec = self.last(read)
            self.assertEqual((rec["argv"], rec["results"]), (with_space, [repr(with_space)]))
            self.assertNotIn(read, {i["task_id"] for i in call("GET", "/api/state")["inbox"]})
            self.assertEqual(run(read, "read")["args"], "")
            self.assertEqual(self.last(read)["argv"], [])

            run(change, "dry")
            self.assertEqual(self.last(change)["argv"], [], "a dry run is not the handed-over apply")
            self.assertEqual(self.cli("handover", change, "--apply", "--", "web1").returncode, 0)
            folder = next(self.home.glob(f"tasks/*/{change}"))
            (folder / "main.py").write_text((folder / "main.py").read_text() + "\n")
            self.assertEqual(run(change, "apply")["exit_code"], 2, "edited after its dry run: refused")
            refused = self.last(change)
            self.assertEqual((refused["status"], refused["argv"]), ("blocked", ["web1"]))
            self.assertIn("the scripts changed since its last dry run", refused["last_error"])
            waiting = next(i for i in call("GET", "/api/state")["inbox"] if i["task_id"] == change)
            self.assertEqual((waiting["next"], waiting["args"]), ("apply", "web1"), "a refused run keeps the handover")
            self.assertEqual(self.cli("run", change, "--", "web1").returncode, 0)
            waiting = next(i for i in call("GET", "/api/state")["inbox"] if i["task_id"] == change)
            self.assertEqual((waiting["next"], waiting["args"], waiting["handed"]), ("apply", "web1", None))
            self.assertEqual(run(change, "apply")["exit_code"], 0)
            runs = json.loads(self.cli("runs", change, "--json").stdout)
            applied = next(r for r in reversed(runs) if r["mode"] == "apply")
            self.assertEqual(
                (applied["status"], applied["argv"]), ("ok", ["web1"]), "the apply takes the arguments of its dry run"
            )

            conn.close()
        finally:
            proc.send_signal(signal.SIGINT)
            self.assertEqual(proc.wait(timeout=15), 0)

    def test_ui_says_when_its_code_is_older_than_what_is_on_disk(self) -> None:
        import http.client

        code = self.tmp / "code"
        shutil.copytree(ROOT / "src/opsquire", code / "opsquire", ignore=shutil.ignore_patterns("__pycache__"))
        self.env["PYTHONPATH"] = str(code)
        jobs_py = code / "opsquire/jobs.py"
        original = jobs_py.read_text()
        port = free_port()

        def start() -> subprocess.Popen[str]:
            ui = subprocess.Popen([*CLI, "ui", "--port", str(port)], env=self.env, stdout=subprocess.PIPE, text=True)
            self.assertIn("opsquire ui", ui.stdout.readline() if ui.stdout else "")
            return ui

        def status() -> Any:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/api/state")
            code_status = json.loads(conn.getresponse().read())["code"]
            conn.close()
            return code_status

        proc = start()
        try:
            first = status()
            self.assertEqual((first["stale"], first["changed"]), (False, []))

            os.utime(jobs_py)
            (code / "opsquire/data/templates/main.py").write_text("# read fresh by every run\n")
            (code / "opsquire/data/helpers/opsquire_helpers.py").write_text("# read fresh by every run\n")
            self.assertFalse(status()["stale"], "same modules, and files the console does not keep")

            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            conn.request("GET", "/api/stream")
            stream = conn.getresponse()
            self.assertEqual(stream.readline(), b"event: hello\n")
            stream.readline(), stream.readline()
            jobs_py.write_text(original + "\n# newer\n")
            self.assertEqual(stream.readline(), b"event: change\n", "an open page hears about it by itself")
            conn.close()
            newer = status()
            self.assertEqual((newer["stale"], newer["changed"]), (True, ["jobs.py"]))

            page = code / "opsquire/data/ui.html"
            page.write_text(page.read_text() + "\n")
            (code / "opsquire/added.py").write_text("")
            newer = status()
            self.assertEqual(newer["changed"], ["added.py", "data/ui.html", "jobs.py"])
            self.assertNotEqual(newer["page"], first["page"], "a tab can tell a reload would give it a newer page")

            (code / "opsquire/added.py").unlink()
            page.write_text(page.read_text()[:-1])
            jobs_py.write_text(original)
            back = status()
            self.assertEqual((back["stale"], back["changed"]), (False, []), "back on the code it loaded")

            jobs_py.write_text(original + "\n# newer\n")
            self.assertTrue(status()["stale"])
        finally:
            proc.send_signal(signal.SIGINT)
            self.assertEqual(proc.wait(timeout=15), 0)
        proc = start()
        try:
            self.assertEqual(status()["stale"], False, "a restart picks up the code on disk")
        finally:
            proc.send_signal(signal.SIGINT)
            self.assertEqual(proc.wait(timeout=15), 0)

    def test_ui_code_status_sees_a_newer_install(self) -> None:
        from opsquire import __version__, ui

        loaded = ui.code_hashes()
        self.assertIn("jobs.py", loaded)
        self.assertIn("data/ui.html", loaded)
        self.assertNotIn("data/templates/main.py", loaded)
        with mock.patch("opsquire.ui.installed_version", return_value=__version__):
            self.assertFalse(ui.code_status(loaded)["stale"])
        with mock.patch("opsquire.ui.installed_version", return_value="99.0.0"):
            newer = ui.code_status(loaded)
        self.assertEqual((newer["stale"], newer["changed"], newer["installed"]), (True, [], "99.0.0"))
        gone = ui.code_status({**loaded, "removed.py": "x"})
        self.assertEqual((gone["stale"], gone["changed"]), (True, ["removed.py"]))

    def test_ui_page_has_the_restart_banner(self) -> None:
        from opsquire import ui

        page = ui.page().decode()
        self.assertIn("function renderStale()", page)
        self.assertIn("renderStale(), renderLive()", page)
        self.assertIn("opsquire ui --port ${location.port}", page)

    def test_run_records_whether_the_console_or_a_terminal_started_it(self) -> None:
        import http.client

        tid = self.new("origin", "import opsquire_helpers as sq\n\nsq.result('ran')\n").name
        self.assertEqual(self.cli("lint", tid).returncode, 0)
        plan = self.cli("new", "web1", "both", "--plan", "--purpose", "p", "--step", tid)
        self.assertEqual(plan.returncode, 0, plan.stderr)
        plan_id = next(p.name for p in self.home.glob("tasks/*/*") if p.name.endswith("_both"))

        self.assertEqual(self.cli("run", tid).returncode, 0)
        self.assertEqual(self.last(tid)["origin"], "terminal")
        self.assertEqual(self.cli("run", plan_id).returncode, 0)
        self.assertEqual(self.last(plan_id)["origin"], "terminal")

        self.env["OPSQUIRE_ORIGIN"] = "made up"
        self.assertEqual(self.cli("run", tid).returncode, 0)
        self.assertEqual(self.last(tid)["origin"], "terminal", "only the console's own word counts")
        del self.env["OPSQUIRE_ORIGIN"]

        self.assertEqual(self.env["OPSQUIRE_AUTHOR"], "test", "the author says nothing about the console here")
        port = free_port()
        proc = subprocess.Popen([*CLI, "ui", "--port", str(port)], env=self.env, stdout=subprocess.PIPE, text=True)
        try:
            self.assertIn("opsquire ui", proc.stdout.readline() if proc.stdout else "")
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)

            def call(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
                conn.request(method, path, json.dumps(body) if body else None, {"Content-Type": "application/json"})
                return json.loads(conn.getresponse().read())

            def run(task: str) -> None:
                job = call("POST", "/api/run", {"task": task, "mode": "read"})["job_id"]
                deadline = time.time() + 30
                while time.time() < deadline:
                    j = call("GET", f"/api/job?id={job}&offset=0")
                    if j["status"] == "done":
                        self.assertEqual(j["exit_code"], 0, j["text"])
                        return
                    time.sleep(0.2)
                self.fail(f"job {job} never finished: {j}")

            run(tid)
            rec = self.last(tid)
            self.assertEqual((rec["origin"], rec["cwd"]), ("console", os.getcwd()), "same cwd, different origin")
            run(plan_id)
            self.assertEqual(self.last(plan_id)["origin"], "console")
            self.assertEqual(self.last(tid)["origin"], "console", "a plan's step started where the plan did")

            runs = call("GET", "/api/state")["runs"]
            self.assertEqual(runs[self.last(tid)["run_id"]]["origin"], "console")
            self.assertEqual({r["origin"] for r in runs.values()}, {"console", "terminal"})
            conn.close()
        finally:
            proc.send_signal(signal.SIGINT)
            self.assertEqual(proc.wait(timeout=15), 0)

        self.assertEqual(self.cli("run", tid).returncode, 0)
        table = self.cli("runs", tid).stdout
        self.assertIn("ORIGIN", table)
        self.assertIn("console", table)
        self.assertIn("terminal", table)
        self.assertIn("origin=terminal", self.cli("log", tid).stdout)
        self.assertIn("origin=console", self.cli("log", plan_id).stdout, "a plan's record has no exit code of its own")

        folder = next(self.home.glob(f"var/runs/{tid}/*"))
        older = json.loads((folder / "run.json").read_text())
        del older["origin"]
        (folder / "run.json").write_text(json.dumps(older))
        self.assertEqual(self.cli("runs", tid).returncode, 0)

    def test_run_line_keeps_quoted_args_together(self) -> None:
        tid = self.new("quoted", "print(1)\n").name
        self.assertEqual(self.cli("lint", tid).returncode, 0)
        code = (
            "import sys\nfrom opsquire import handover\n"
            "print(handover.record_run_lines([('RUN', sys.argv[1]), ('RUN', sys.argv[2])], 'why', 'by'))\n"
        )
        run = f"opsquire run {tid} -- focus 'two words'"
        unbalanced = f"opsquire run {tid} -- 'open"
        r = subprocess.run([sys.executable, "-c", code, unbalanced, run], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), repr([tid]), r.stderr)
        item = json.loads(self.cli("threads", "--json").stdout)["inbox"][0]
        self.assertEqual((item["command"], item["args"]), (run, "focus 'two words'"))

    def test_run_records_usage_and_state_shows_agent_work(self) -> None:
        tid = self.new("usage", "import opsquire_helpers as sq\n\nsq.result('x' * 10)\n").name
        self.assertEqual(self.cli("run", tid).returncode, 0)
        rec = self.last(tid)
        self.assertGreater(rec["usage"]["max_rss_mb"], 1)
        self.assertGreaterEqual(rec["usage"]["cpu_s"], 0)
        state = json.loads(self.cli("threads", "--json").stdout)
        self.assertEqual(state["runs"][rec["run_id"]]["usage"], rec["usage"])
        self.assertTrue(state["tasks"][tid]["settled"])
        self.assertEqual(state["tasks"][tid]["agents"]["asks"], 0)
        self.assertGreater(state["tasks"][tid]["agents"]["operator_s"], 0)
        self.agents_file()
        r = self.cli("ask", "--no-grill", "restart", "web1")
        self.assertEqual(r.returncode, 0, r.stderr)
        ask = self.asks()[-1]
        made = self.new("after-ask", "print(1)\n").name
        ask["task_ids"] = [made]
        ask["ended"] = "2999-01-01T00:00:00.000Z"
        ask["cost_usd"] = 1.5
        ask["pieces"] = [{"name": "a", "status": "merged"}, {"name": "b", "status": "merged"}]
        Path(self.home / "var/asks" / "patched.json").write_text(json.dumps(ask))
        state = json.loads(self.cli("threads", "--json").stdout)
        self.assertEqual(state["tasks"][made]["agents"], {"asks": 1, "pieces": 2, "cost_usd": 1.5, "operator_s": 0.0})

    def test_judge_closes_from_the_record_or_keeps_it_open(self) -> None:
        self.agents_file()
        body = "import opsquire_helpers as sq\n\nsq.target('web1', 'failed', 'refused')\n"
        tid = self.new("judged", body, "--changes-prod", "--rollback", "none").name
        (self.home / f"tasks/{tid[:4]}-{tid[4:6]}/{tid}/design.md").write_text("# why\n\nbecause\n")
        self.assertEqual(self.cli("run", tid, "--apply", *self.NO_DRY, answers=[("", "y")]).returncode, 1)
        self.env["FAKE_JUDGE"] = "KEEP: no evidence that web1 came back"
        r = self.cli("judge", tid)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("kept open: no evidence", r.stdout)
        self.assertIsNone(json.loads(self.cli("show", tid, "--json").stdout)["meta"]["outcome"])
        judged = json.loads(self.cli("threads", "--json").stdout)["tasks"][tid]["judged"]
        self.assertEqual((judged["decision"], judged["note"]), ("keep", "no evidence that web1 came back"))
        self.env["FAKE_JUDGE"] = "CLOSE: partial | web1 refused but web2 was done by hand; see run above"
        r = self.cli("judge", tid, "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual((out["decision"], out["cost_usd"]), ("partial", 0.02))
        meta = json.loads(self.cli("show", tid, "--json").stdout)["meta"]
        self.assertEqual((meta["outcome"]["state"], meta["outcome"]["closed_by"]), ("partial", "opsquire judge (fake)"))
        self.assertIn("web2 was done by hand", meta["outcome"]["note"])
        state = json.loads(self.cli("threads", "--json").stdout)
        self.assertTrue(state["tasks"][tid]["settled"])
        self.assertEqual(self.cli("judge", "nothing-like-this").returncode, 2)

    def test_ui_judge_job_ticks_a_task_off(self) -> None:
        import http.client

        self.agents_file()
        tid = self.new(
            "tick",
            "import opsquire_helpers as sq\n\nsq.target('web1', 'failed')\n",
            "--changes-prod",
            "--rollback",
            "none",
        ).name
        self.assertEqual(self.cli("run", tid, "--apply", *self.NO_DRY, answers=[("", "y")]).returncode, 1)
        port = free_port()
        proc = subprocess.Popen([*CLI, "ui", "--port", str(port)], env=self.env, stdout=subprocess.PIPE, text=True)
        try:
            self.assertIn("opsquire ui", proc.stdout.readline() if proc.stdout else "")
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)

            def call(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
                conn.request(method, path, json.dumps(body) if body else None, {"Content-Type": "application/json"})
                resp = conn.getresponse()
                return resp.status, json.loads(resp.read())

            _, state = call("GET", "/api/state")
            self.assertFalse(state["tasks"][tid]["settled"])
            self.assertEqual([i["task_id"] for i in state["inbox"]], [tid])
            status, started = call("POST", "/api/judge", {"task": tid})
            self.assertEqual(status, 200, started)
            deadline = time.time() + 30
            while time.time() < deadline:
                _, j = call("GET", f"/api/job?id={started['job_id']}&offset=0")
                if j["status"] == "done":
                    break
                time.sleep(0.2)
            self.assertEqual((j["mode"], j["exit_code"], j["run_id"]), ("judge", 0, None), j)
            self.assertIn("closed done", j["text"])
            _, state = call("GET", "/api/state")
            self.assertTrue(state["tasks"][tid]["settled"])
            self.assertEqual(state["tasks"][tid]["outcome"]["closed_by"], "opsquire judge (fake)")
            self.assertEqual(state["inbox"], [])
            self.assertEqual(state["jobs"][0]["mode"], "judge")
            conn.close()
        finally:
            proc.send_signal(signal.SIGINT)
            self.assertEqual(proc.wait(timeout=15), 0)

    def test_verify_check_is_scaffolded_runs_read_only_and_closes_when_all_ok(self) -> None:
        folder = self.new("deploy-v", "print('deploy')\n", "--changes-prod", "--rollback", "none")
        tid = folder.name
        self.assertTrue((folder / "verify.py").is_file())
        self.assertEqual(json.loads((folder / "meta.json").read_text())["verify"], "verify.py")
        self.assertEqual(self.cli("lint", tid).returncode, 0)
        r = self.cli("run", tid, "--apply", *self.NO_DRY, answers=[("", "y")])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.last(tid)["mode"], "verify")
        self.assertIn("closed done", r.stdout)
        self.assertIn("apply run", json.loads((folder / "meta.json").read_text())["outcome"]["note"])
        meta = json.loads((folder / "meta.json").read_text())
        meta["outcome"] = None
        (folder / "meta.json").write_text(json.dumps(meta))
        r = self.cli("run", tid, "--verify")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rec = self.last(tid)
        self.assertEqual(
            (rec["mode"], rec["status"], rec["entry"], rec["targets"]["web1"]["status"]),
            ("verify", "ok", "verify.py", "skipped"),
        )
        self.assertEqual(rec["lock"]["mode"], "shared")
        self.assertIsNone(json.loads((folder / "meta.json").read_text())["outcome"])
        self.assertNotIn("closed done", r.stdout)
        (folder / "verify.py").write_text(
            "import opsquire_helpers as sq\n\nfor name in sq.TARGETS:\n    sq.target(name, 'ok', 'v2 is live')\n"
        )
        r = self.cli("run", tid, "--verify")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("closed done", r.stdout)
        outcome = json.loads((folder / "meta.json").read_text())["outcome"]
        self.assertIn("verify run", outcome["note"])
        self.assertEqual(self.cli("run", tid, "--verify", "--apply").returncode, 0)
        plain = self.new("plain", "print(1)\n", "--changes-prod", "--rollback", "none", "--no-verify")
        self.assertFalse((plain / "verify.py").exists())
        self.assertEqual(self.cli("run", plain.name, "--verify").returncode, 2)
        read = self.new("ro", "print(1)\n")
        self.assertFalse((read / "verify.py").exists())

    def test_staged_apply_stays_open_until_its_verify_check_passes(self) -> None:
        body = (
            "import os\nimport sys\nfrom pathlib import Path\n\nimport opsquire_helpers as sq\n\n"
            "stage = sys.argv[1] if len(sys.argv) > 1 else 'build'\n"
            "if sq.APPLY and stage == 'deploy':\n    Path(os.environ['STAGED_LIVE']).write_text('v2')\n"
            "sq.target('web1', 'ok', stage)\n"
        )
        self.env["STAGED_LIVE"] = str(self.tmp / "staged-live")
        folder = self.new("staged", body, "--changes-prod", "--rollback", "none")
        tid = folder.name
        (folder / "verify.py").write_text(
            "import os\nfrom pathlib import Path\n\nimport opsquire_helpers as sq\n\n"
            "live = Path(os.environ['STAGED_LIVE']).is_file()\n"
            "sq.target('web1', 'ok' if live else 'failed', 'v2 live' if live else 'still v1')\n"
        )
        self.assertEqual(self.cli("lint", tid).returncode, 0)
        for stage in ("build", "deploy"):
            self.assertEqual(self.cli("run", tid, "--", stage).returncode, 0)
        r = self.cli("run", tid, "--apply", "--", "build", answers=[("", "y")])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("running the verify check", r.stdout)
        self.assertEqual((self.last(tid)["mode"], self.last(tid)["targets"]["web1"]["status"]), ("verify", "failed"))
        self.assertIsNone(json.loads((folder / "meta.json").read_text()).get("outcome"))
        r = self.cli("run", tid, "--apply", "--", "deploy", answers=[("", "y")])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("closed done", r.stdout)
        outcome = json.loads((folder / "meta.json").read_text())["outcome"]
        self.assertIn("verify run", outcome["note"])

    def test_inbox_asks_for_the_verify_check_after_an_apply_and_when_the_judge_says_so(self) -> None:
        self.agents_file()
        body = "import opsquire_helpers as sq\n\nsq.target('web1', 'failed', 'refused')\n"
        folder = self.new("needs-check", body, "--changes-prod", "--rollback", "none")
        tid = folder.name
        self.assertEqual(self.cli("run", tid, "--apply", *self.NO_DRY, answers=[("", "y")]).returncode, 1)
        state = json.loads(self.cli("threads", "--json").stdout)
        self.assertEqual(
            [(i["next"], i["command"]) for i in state["inbox"]], [("verify", f"opsquire run {tid} --verify")]
        )
        self.assertEqual(self.cli("run", tid, "--verify").returncode, 0)
        state = json.loads(self.cli("threads", "--json").stdout)
        self.assertEqual(state["inbox"], [])
        self.env["FAKE_JUDGE"] = "VERIFY: that web1 serves v2 on port 80"
        r = self.cli("judge", tid)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("needs the verify check: that web1 serves v2", r.stdout)
        state = json.loads(self.cli("threads", "--json").stdout)
        self.assertEqual(state["tasks"][tid]["judged"]["decision"], "verify")
        self.assertEqual([i["next"] for i in state["inbox"]], ["verify"])
        self.assertEqual(state["tasks"][tid]["verify"], "verify.py")
        other = self.new("no-check", body, "--changes-prod", "--rollback", "none", "--no-verify").name
        r = self.cli("judge", other)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no verify check yet; an agent must add one", r.stdout)
        self.assertEqual(json.loads(self.cli("threads", "--json").stdout)["inbox"][0]["task_id"], tid)

    def test_inbox_nests_the_waiting_steps_under_their_plan(self) -> None:
        a = self.new("stop-bots", self.CONFIRM_BODY, "--changes-prod", "--rollback", "start them", "--no-verify")
        b = self.new("start-bots", self.CONFIRM_BODY, "--changes-prod", "--rollback", "stop them", "--no-verify")
        plan = self.make_plan(a, b, name="cutover")
        for name in ("stop-bots", "start-bots", "cutover"):
            self.assertEqual(self.cli("lint", name).returncode, 0)
        inbox = json.loads(self.cli("threads", "--json").stdout)["inbox"]
        self.assertEqual([i["task_id"] for i in inbox], [plan.name])
        steps = [(s["task_id"], s["next"], s["steps"]) for s in inbox[0]["steps"]]
        self.assertEqual(steps, [(a.name, "dry", []), (b.name, "dry", [])])
        self.assertEqual(self.cli("run", "cutover").returncode, 0)
        inbox = json.loads(self.cli("threads", "--json").stdout)["inbox"]
        self.assertEqual([(i["task_id"], i["next"]) for i in inbox], [(plan.name, "apply")])
        self.assertEqual([s["next"] for s in inbox[0]["steps"]], ["apply", "apply"])
        self.assertEqual(self.cli("close", "cutover", "--outcome", "abandoned", "--note", "by hand").returncode, 0)
        inbox = json.loads(self.cli("threads", "--json").stdout)["inbox"]
        self.assertEqual(sorted((i["task_id"], i["steps"]) for i in inbox), sorted([(a.name, []), (b.name, [])]))

    def milestones(self, *args: str) -> list[dict[str, Any]]:
        r = self.cli("milestones", "--since", "2020-01-01", "--json", *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        items: list[dict[str, Any]] = json.loads(r.stdout)
        return items

    def test_milestones_report_closed_changing_tasks_failed_applies_and_thread_results(self) -> None:
        self.new("peek", "print(1)\n")
        good = self.new(
            "flip", self.CONFIRM_BODY, "--changes-prod", "--rollback", "flip back", "--no-verify", "--thread", "story"
        )
        bad = self.new(
            "boom",
            "import sys\n\nsys.exit(1)\n",
            "--changes-prod",
            "--rollback",
            "undo",
            "--no-verify",
            "--thread",
            "story",
        )
        for name in ("peek", "flip", "boom"):
            self.assertEqual(self.cli("lint", name).returncode, 0)
        self.assertEqual(self.cli("run", "peek").returncode, 0)
        self.assertEqual(self.cli("run", "flip").returncode, 0)
        self.assertEqual(self.cli("run", "flip", "--apply", answers=[("[y/N]", "y")]).returncode, 0)
        self.assertEqual(self.cli("run", "boom").returncode, 1)
        self.assertNotEqual(self.cli("run", "boom", "--apply", *self.NO_DRY, answers=[("[y/N]", "y")]).returncode, 0)
        (self.home / "threads").mkdir(exist_ok=True)
        (self.home / "threads/story.md").write_text("# story\n\nFirst paragraph.\n\nFlip is live, boom is not.\n")
        items = self.milestones()
        self.assertEqual(
            [(m["kind"], m["task_id"], m["thread"]) for m in items],
            [("done", good.name, "story"), ("failed", bad.name, "story"), ("result", None, "story")],
        )
        done, failed, result = items
        self.assertEqual((done["state"], done["by"]), ("done", "opsquire run (auto)"))
        self.assertEqual(done["text"], "done")
        self.assertEqual((failed["mode"], failed["state"], failed["failed_targets"]), ("apply", "failed", ["web1"]))
        self.assertEqual(result["text"], "Flip is live, boom is not.")
        self.assertEqual([m["kind"] for m in self.milestones("--kind", "failed,result")], ["failed", "result"])
        self.assertEqual(self.milestones("--thread", "other"), [])
        self.assertEqual(len(self.milestones("--thread", "story")), 3)
        self.assertEqual(self.cli("milestones", "--since", "2999-01-01").stdout.strip(), "no milestones in that window")
        text = self.cli("milestones", "--since", "2020-01-01").stdout
        self.assertIn("story\n", text)
        self.assertIn("apply failed   boom [web1]", text)
        self.assertIn("done           flip [web1]: done", text)
        self.assertIn("result         thread result [web1]: Flip is live, boom is not.", text)
        self.assertEqual(self.cli("milestones", "--kind", "nope").returncode, 2)

    def test_milestones_report_a_plan_once_and_never_a_dry_or_blocked_run(self) -> None:
        a = self.new("stop", self.CONFIRM_BODY, "--changes-prod", "--rollback", "start", "--no-verify")
        b = self.new("start", self.CONFIRM_BODY, "--changes-prod", "--rollback", "stop", "--no-verify")
        plan = self.make_plan(a, b, name="cutover")
        for name in ("stop", "start", "cutover"):
            self.assertEqual(self.cli("lint", name).returncode, 0)
        self.assertEqual(self.cli("run", "cutover").returncode, 0)
        self.assertEqual(self.milestones(), [])
        r = self.cli("run", "cutover", "--apply", answers=[("[y/N]", "y"), ("[y/N]", "y")])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        items = self.milestones()
        self.assertEqual([(m["kind"], m["task_id"]) for m in items], [("done", plan.name)])
        for step in (a, b):
            self.assertTrue(json.loads(self.cli("show", step.name, "--json").stdout)["meta"]["outcome"])

    def test_milestones_since_accepts_durations_clock_times_and_dates(self) -> None:
        sys.path.insert(0, str(ROOT / "src"))
        import datetime as dt

        from opsquire.milestones import since_arg
        from opsquire.model import OpsquireError

        at = dt.datetime(2026, 11, 5, 18, 30, tzinfo=dt.UTC)
        self.assertEqual(since_arg(None, at), "2026-11-05T00:00")
        self.assertEqual(since_arg("2h", at), "2026-11-05T16:30:00.000Z")
        self.assertEqual(since_arg("30m", at), "2026-11-05T18:00:00.000Z")
        self.assertEqual(since_arg("1d", at), "2026-11-04T18:30:00.000Z")
        self.assertEqual(since_arg("9:05", at), "2026-11-05T09:05")
        self.assertEqual(since_arg("2026-11-01", at), "2026-11-01")
        with self.assertRaises(OpsquireError):
            since_arg("yesterday", at)

    def test_handover_puts_a_reused_task_in_the_inbox_until_it_runs(self) -> None:
        folder = self.new("count", "print(1)\n")
        self.assertEqual(self.cli("lint", "count").returncode, 0)
        self.assertEqual(self.cli("run", "count").returncode, 0)
        self.assertEqual(json.loads(self.cli("threads", "--json").stdout)["inbox"], [])
        r = self.cli("handover", "count", "--why", "the host changed since", "--", "web1", "--fast")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"handed over: opsquire run {folder.name} -- web1 --fast", r.stdout)
        item = json.loads(self.cli("threads", "--json").stdout)["inbox"][0]
        self.assertEqual(
            (item["task_id"], item["next"], item["command"]),
            (folder.name, "read", f"opsquire run {folder.name} -- web1 --fast"),
        )
        self.assertEqual((item["handed"]["by"], item["handed"]["why"]), ("test", "the host changed since"))
        meta = json.loads((folder / "meta.json").read_text())
        self.assertEqual(meta["handover"]["args"], ["web1", "--fast"])
        self.assertEqual(json.loads(self.cli("show", "count", "--json").stdout)["status"], "done")
        self.assertEqual(self.cli("run", "count", "--", "web1", "--fast").returncode, 0)
        self.assertEqual(json.loads(self.cli("threads", "--json").stdout)["inbox"], [])
        self.assertIn("handover", json.loads((folder / "meta.json").read_text()))

        bad = self.cli("handover", "count", "--apply")
        self.assertEqual(bad.returncode, 2)
        self.assertIn("read-only; it has no apply run", bad.stderr)
        changing = self.new("flip", "print(1)\n", "--changes-prod", "--rollback", "flip back")
        self.assertEqual(self.cli("lint", "flip").returncode, 0)
        self.assertEqual(self.cli("run", "flip").returncode, 0)
        self.assertEqual([i["next"] for i in json.loads(self.cli("threads", "--json").stdout)["inbox"]], ["apply"])
        self.assertEqual(self.cli("handover", "flip", "--why", "dry again first").returncode, 0)
        item = json.loads(self.cli("threads", "--json").stdout)["inbox"][0]
        self.assertEqual(
            (item["task_id"], item["next"], item["command"]), (changing.name, "dry", f"opsquire run {changing.name}")
        )
        self.assertEqual(self.cli("handover", "flip", "--verify").returncode, 0)
        self.assertEqual(json.loads(self.cli("threads", "--json").stdout)["inbox"][0]["next"], "verify")
        general = self.cli("new", "web1", "lib", "--general", "--purpose", "g")
        self.assertEqual(general.returncode, 0)
        self.assertIn("general script", self.cli("handover", "web1_lib").stderr)

    def test_ask_records_a_handover_for_every_run_line(self) -> None:
        self.split("alpha task", "beta task")
        r = self.cli("ask", "two", "things")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        alpha = next(self.home.glob("tasks/*/*_web1_alpha-task"))
        meta = json.loads((alpha / "meta.json").read_text())
        self.assertEqual((meta["handover"]["mode"], meta["handover"]["why"]), ("read", "two things"))
        self.assertTrue(meta["handover"]["by"].startswith("ask "))
        top = json.loads(self.cli("threads", "--json").stdout)["inbox"]
        plan = next(i for i in top if i["steps"])
        inbox = {i["task_id"]: i for i in top + plan["steps"]}
        self.assertEqual((inbox[alpha.name]["next"], inbox[alpha.name]["handed"]["why"]), ("read", "two things"))
        self.assertIn(next(self.home.glob("tasks/*/*_web1_beta-task")).name, inbox)
        self.assertEqual(len(plan["steps"]), 2)
        self.assertEqual([i["task_id"] for i in top], [plan["task_id"]])

    def test_init_builds_a_working_home_from_nothing(self) -> None:
        user = self.tmp / "user"
        self.env |= {
            "HOME": str(user),
            "CLAUDE_CONFIG_DIR": str(self.tmp / "claude"),
            "XDG_CONFIG_HOME": str(self.tmp / "cfg"),
        }
        self.env["PYTHONUSERBASE"] = os.environ.get("PYTHONUSERBASE", str(Path.home() / ".local"))
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.com\n")
        self.env |= {"GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1"}
        del self.env["OPSQUIRE_HOME"]
        self.env["OPSQUIRE_CREDS"] = str(self.tmp / "fresh-creds.json")
        home = self.tmp / "fresh-home"
        dry = self.cli("init", str(home), "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertFalse(home.exists() or (self.tmp / "fresh-creds.json").exists())
        r = self.cli("init", str(home), "--budget", "5")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout
        for line in (
            f"home: set to {home}",
            "tasks/: created",
            "general/: created",
            "local rules: written from the template",
            "ask budget: $5.00 per ask as a reminder;",
            "credentials: created empty",
            "tool ruff: found",
            "claude skill: installed",
            "home repo: initialized a local git repository",
            "contract: written",
            "first task: created and ready (",
            "offboarding: settled tasks leave tasks/ 7 days after their last activity",
        ):
            self.assertIn(line, out)
        config = json.loads((self.tmp / "cfg/opsquire/config.json").read_text())
        self.assertEqual(config, {"home": str(home), "ask_budget_usd": 5.0})
        creds = self.tmp / "fresh-creds.json"
        self.assertEqual((json.loads(creds.read_text()), creds.stat().st_mode & 0o777), ({}, 0o600))
        self.assertIn("### What is script-shaped here", (home / "LOCAL.md").read_text())
        self.assertIn("## Local rules for this home", (home / "AGENTS.md").read_text())
        demo = re.search(r"first task: created and ready \((\S+)\)", out)
        assert demo
        run = self.cli("run", demo.group(1))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("hello from demo", run.stdout)
        self.assertEqual(json.loads(self.cli("last", "--json").stdout)["status"], "ok")
        (home / "LOCAL.md").write_text("mine\n")
        self.assertEqual(self.cli("init", "--offboard-days", "-1").returncode, 2)
        kept = self.cli("init", "--offboard-days", "30", "--dry-run")
        self.assertIn("offboarding: settled tasks leave tasks/ 30 days after", kept.stdout)
        self.assertNotIn("offboard_after_days", (self.tmp / "cfg/opsquire/config.json").read_text())
        never = self.cli("init", "--offboard-days", "0")
        self.assertIn("offboarding: off; settled tasks stay until `opsquire offboard <task>`", never.stdout)
        self.assertEqual(json.loads((self.tmp / "cfg/opsquire/config.json").read_text())["offboard_after_days"], 0.0)
        again = self.cli("init")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("offboarding: off;", again.stdout)
        for line in (
            "tasks/: present",
            "local rules: present",
            "credentials: present (",
            "claude skill: unchanged",
            f"first task: present ({demo.group(1)})",
            "ask budget: $5.00",
        ):
            self.assertIn(line, again.stdout)
        self.assertEqual((home / "LOCAL.md").read_text(), "mine\n")
        self.agents_file()
        ask = self.cli("ask", "--no-split", "--strict-budget", "one", "thing")
        self.assertEqual(ask.returncode, 0, ask.stdout + ask.stderr)
        self.assertIn('"--max-budget-usd", "5.00"', ask.stdout)

    def test_init_on_a_machine_with_no_git_identity_or_lint_tools(self) -> None:
        venv = self.tmp / "venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
        cli = [str(venv / "bin/python"), "-m", "opsquire"]
        tools = self.tmp / "tools"
        tools.mkdir()
        (tools / "git").symlink_to(shutil.which("git") or "git")
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text("[user]\n\tuseConfigOnly = true\n")
        env = {
            "PATH": str(tools),
            "HOME": str(self.tmp / "user"),
            "CLAUDE_CONFIG_DIR": str(self.tmp / "claude"),
            "XDG_CONFIG_HOME": str(self.tmp / "cfg"),
            "PYTHONUSERBASE": str(self.tmp / "no-user-packages"),
            "PYTHONPATH": str(ROOT / "src"),
            "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_CONFIG_NOSYSTEM": "1",
            "OPSQUIRE_CREDS": str(self.tmp / "fresh-creds.json"),
            "TMPDIR": str(self.tmp),
        }

        def run(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run([*cli, *args], env=env, capture_output=True, text=True, timeout=120)

        home = self.tmp / "fresh-home"
        r = run("init", str(home))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("tool ruff: MISSING", r.stdout)
        self.assertIn("home repo: initialized", r.stdout)
        self.assertIn("next: install what is MISSING above", r.stdout)
        self.assertNotIn("next: run the first task", r.stdout)
        authors = subprocess.run(
            ["git", "-C", str(home), "log", "--format=%an <%ae>"], capture_output=True, text=True, check=True
        )
        self.assertEqual(set(authors.stdout.split("\n")) - {""}, {"opsquire <opsquire@localhost>"})
        demo = re.search(r"first task: created \((\S+)\) but its lint did not pass", r.stdout)
        assert demo
        again = run("init")
        self.assertIn(f"first task: present ({demo.group(1)}) but its lint did not pass", again.stdout)
        self.assertNotIn("next: run the first task", again.stdout)
        blocked = run("run", demo.group(1))
        self.assertEqual(blocked.returncode, 2, blocked.stdout + blocked.stderr)
        self.assertIn("ruff and mypy must be installed", blocked.stdout + blocked.stderr)

        for tool in ("ruff", "mypy"):
            found = shutil.which(tool, path=f"{Path(sys.executable).parent}:{Path.home() / '.local/bin'}")
            assert found, tool
            (tools / tool).symlink_to(found)
        env["PYTHONUSERBASE"] = os.environ.get("PYTHONUSERBASE", str(Path.home() / ".local"))
        ready = run("init")
        self.assertIn(f"first task: present ({demo.group(1)}); run it", ready.stdout)
        self.assertIn("next: run the first task", ready.stdout)
        ran = run("run", demo.group(1))
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertIn("hello from demo", ran.stdout)

        (tools / "git").unlink()
        other = self.tmp / "home-without-git"
        no_git = run("init", str(other))
        self.assertEqual(no_git.returncode, 0, no_git.stdout + no_git.stderr)
        self.assertIn("tool git: MISSING", no_git.stdout)
        self.assertIn("home repo: skipped, git is not installed", no_git.stdout)
        self.assertIn("contract: written", no_git.stdout)

    def test_a_package_installed_next_to_mypy_can_lint_and_run(self) -> None:
        venv = self.tmp / "venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", "--system-site-packages", str(venv)], check=True)
        python = str(venv / "bin/python")
        where = "import site; print(site.getsitepackages()[0])"
        site = Path(subprocess.run([python, "-c", where], capture_output=True, text=True, check=True).stdout.strip())
        shutil.copytree(ROOT / "src/opsquire", site / "opsquire", ignore=shutil.ignore_patterns("__pycache__"))
        mypy_home = subprocess.run(
            [sys.executable, "-c", "import mypy, os; print(os.path.dirname(os.path.dirname(mypy.__file__)))"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        (site / "_lint_tools.pth").write_text(mypy_home + "\n")
        (venv / "bin/mypy").write_text(f"#!{python}\nfrom mypy.__main__ import console_entry\nconsole_entry()\n")
        (venv / "bin/mypy").chmod(0o755)
        ruff = shutil.which("ruff", path=f"{Path(sys.executable).parent}:{Path.home() / '.local/bin'}")
        assert ruff
        (venv / "bin/ruff").symlink_to(ruff)
        env = {k: v for k, v in self.env.items() if k != "PYTHONPATH"}
        env["TMPDIR"] = str(self.tmp)
        made = subprocess.run(
            [python, "-m", "opsquire", "new", "web1", "installed", "--purpose", "p", "--lang", "py"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(made.returncode, 0, made.stderr)
        (Path(made.stdout.strip()) / "main.py").write_text("import opsquire_helpers as sq\n\nsq.result(sq.__file__)\n")
        for args in (["lint", "installed"], ["run", "installed"]):
            r = subprocess.run([python, "-m", "opsquire", *args], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.last("installed")["results"], [str(site / "opsquire/data/helpers/opsquire_helpers.py")])
        self.assertEqual(list(self.tmp.glob("opsquire-mypy-*")), [])

    def test_integrate_installs_the_skill_and_makes_the_home_a_local_git_repo(self) -> None:
        self.env |= {"HOME": str(self.tmp / "user"), "CLAUDE_CONFIG_DIR": str(self.tmp / "claude")}
        self.env["PYTHONUSERBASE"] = os.environ.get("PYTHONUSERBASE", str(Path.home() / ".local"))
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.com\n")
        self.env |= {"GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1"}
        skills = [self.tmp / "claude/skills/opsquire/SKILL.md", self.tmp / "user/.claude/skills/opsquire/SKILL.md"]
        r = self.cli("integrate", "--dry-run")
        self.assertEqual(r.stdout.count("claude skill: would be installed"), 2)
        self.assertIn("home repo: would initialize", r.stdout)
        self.assertFalse(any(s.exists() for s in skills) or (self.home / ".git").exists())
        r = self.cli("integrate")
        self.assertEqual(r.stdout.count("claude skill: installed"), 2)
        want = (ROOT / "src/opsquire/data/skills/opsquire/SKILL.md").read_text().split("If your shell")[0]
        self.assertTrue(all(s.read_text().startswith(want) and "{{" not in s.read_text() for s in skills))
        self.assertIn("var/", (self.home / ".gitignore").read_text())
        self.assertEqual(len(self.git("log", "--oneline").splitlines()), 1)
        r = self.cli("integrate")
        self.assertEqual(r.stdout.count("claude skill: unchanged"), 2)
        self.assertIn("home repo: local-only git repository", r.stdout)
        self.git("remote", "add", "origin", "https://example.com/x.git")
        self.assertIn("refusing: it has remotes (origin)", self.cli("integrate").stdout)

    def test_lint_pass_commits_the_ready_version(self) -> None:
        self.git_home()
        folder = self.new("tidy", "print(1)\n")
        self.assertEqual(self.cli("lint", "tidy").returncode, 0)
        self.assertIn(f"ready: {folder.name}", self.git("log", "--format=%s"))
        self.assertEqual(self.git("status", "--short"), "")

    def test_lint_pass_commits_the_general_scripts_it_checked(self) -> None:
        self.git_home()
        g = Path(self.cli("new", "web1", "shared", "--general", "--purpose", "shared logic").stdout.strip())
        folder = self.new("user", "print(1)\n", "--uses", "web1_shared")
        self.assertEqual(self.cli("lint", "user").returncode, 0)
        (g / "main.py").write_text('"""Shared."""\n\nVALUE = 2\n')
        self.assertEqual(self.cli("lint", "user").returncode, 0)
        self.assertEqual(self.git("status", "--short"), "")
        self.assertIn("VALUE = 2", self.git("show", "HEAD:general/web1_shared/main.py"))
        self.assertIn(f"ready: {folder.name}", self.git("log", "-1", "--format=%s"))

    def test_home_commits_are_not_signed_even_when_the_operator_signs_everything(self) -> None:
        gpg = self.tmp / "gpg-that-fails"
        gpg.write_text("#!/bin/sh\necho 'gpg: signing failed: no passphrase' >&2\nexit 2\n")
        gpg.chmod(0o755)
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text(
            "[user]\n\tname = Test\n\temail = test@example.com\n\tsigningkey = ABC\n"
            f"[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = {gpg}\n"
        )
        self.env |= {"GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1"}
        self.env |= {"HOME": str(self.tmp / "user"), "CLAUDE_CONFIG_DIR": str(self.tmp / "claude")}
        self.env["PYTHONUSERBASE"] = os.environ.get("PYTHONUSERBASE", str(Path.home() / ".local"))
        self.assertIn("home repo: initialized", self.cli("integrate").stdout)
        folder = self.new("tidy", "print(1)\n")
        r = self.cli("lint", "tidy")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("home not committed", r.stdout + r.stderr)
        self.assertIn(f"ready: {folder.name}", self.git("log", "--format=%s"))
        self.assertEqual(set(self.git("log", "--format=%G?").split()), {"N"})
        self.assertEqual(self.git("config", "--global", "commit.gpgsign").strip(), "true")

    def test_parallel_ask_builds_each_piece_in_its_own_worktree_and_bundles_a_plan(self) -> None:
        log = self.split("alpha-build | alpha task", "beta task")
        r = self.cli("ask", "build", "alpha", "and", "beta")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("split into 2 pieces", r.stderr)
        self.assertIn("[1/2 alpha-build] ready", r.stdout)
        self.assertIn("[2/2 beta] ready", r.stdout)
        runs = re.findall(r"^\s*RUN: opsquire run (\S+)$", r.stdout, re.M)
        self.assertEqual(len(runs), 3, r.stdout)
        self.assertTrue(runs[-1].endswith("_web1_all-alpha-build-beta"), runs[-1])
        self.assertNotIn("--apply", r.stdout)
        plan = json.loads(next(self.home.glob(f"tasks/*/{runs[-1]}/meta.json")).read_text())
        self.assertEqual([s["task"] for s in plan["steps"]], runs[:2])
        for tid in runs:
            self.assertEqual(json.loads(self.cli("show", tid, "--json").stdout)["status"], "ready", tid)
        calls = [json.loads(x) for x in log.read_text().splitlines()]
        cwds = {c["piece"]: c["cwd"] for c in calls if c["piece"]}
        self.assertEqual(set(cwds), {"alpha task", "beta task"})
        self.assertTrue(all("/.worktrees/" in c for c in cwds.values()))
        self.assertFalse((self.home / ".worktrees").exists())
        self.assertEqual(self.git("branch", "--list", "ask/*"), "")
        self.assertEqual(self.git("status", "--short"), "")
        rec = self.asks()[-1]
        self.assertEqual([p["status"] for p in rec["pieces"]], ["merged", "merged"])
        self.assertEqual((rec["status"], rec["plan"]), ("ok", runs[-1]))

    def test_parallel_questions_reach_the_operator_one_at_a_time_with_labels(self) -> None:
        self.split("alpha question", "beta question")
        r = self.cli("ask", "two", "things", answers=[("[Enter = tonight]", ""), ("[Enter = tonight]", "tomorrow")])
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertRegex(r.stdout, r"\[\d/2 (alpha|beta)\] which window for")
        pieces = self.asks()[-1]["pieces"]
        self.assertEqual(sorted(p["rounds"][0]["answer"] for p in pieces), ["tomorrow", "tonight"])
        self.assertEqual([p["status"] for p in pieces], ["merged", "merged"])

    def test_parallel_merge_clash_is_resolved_by_the_piece_agent(self) -> None:
        self.split("alpha clash", "beta clash")
        r = self.cli("ask", "both", "touch", "shared")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        notes = (self.home / "general/web1_shared/notes.txt").read_text()
        self.assertIn("written by alpha-clash", notes)
        self.assertIn("written by beta-clash", notes)
        self.assertNotIn("<<<<<<<", notes)
        self.assertEqual([p["status"] for p in self.asks()[-1]["pieces"]], ["merged", "merged"])

    def test_parallel_waiting_piece_is_parked_on_its_branch_and_resumes(self) -> None:
        self.split("alpha task", "beta need")
        r = self.cli("ask", "two", "things")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("[2/2 beta-need] waiting: the book name", r.stdout)
        self.assertIn("continue with: opsquire ask --resume", r.stderr)
        rec = self.asks()[-1]
        self.assertEqual(self.git("branch", "--list", "ask/*").split(), [f"ask/{rec['ask_id']}/2"])
        self.assertFalse((self.home / ".worktrees").exists())
        r = self.cli("ask", "--resume", rec["ask_id"], "ldn1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("[2/2 beta-need] ready", r.stdout)
        rec = self.asks()[-1]
        self.assertEqual([p["status"] for p in rec["pieces"]], ["merged", "merged"])
        self.assertEqual(rec["pieces"][1]["rounds"][-1]["answer"], "ldn1")
        self.assertTrue(rec["plan"])
        self.assertEqual(self.git("branch", "--list", "ask/*"), "")

    def test_parallel_ctrl_c_parks_unfinished_pieces_cleanly(self) -> None:
        log = self.split("alpha task", "beta slow")
        self.env["FAKE_SLEEP"] = "60"
        p = self.start("ask", "two", "things")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and "beta slow" not in (log.read_text() if log.exists() else ""):
            time.sleep(0.1)
        time.sleep(1)
        os.killpg(p.pid, signal.SIGINT)
        out, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 130, err)
        self.assertNotIn("Traceback", out + err)
        rec = self.asks()[-1]
        self.assertEqual(rec["status"], "interrupted")
        self.assertEqual(rec["pieces"][1]["status"], "interrupted")
        self.assertIn(f"ask/{rec['ask_id']}/2", self.git("branch", "--list", "ask/*"))
        self.assertFalse((self.home / ".worktrees").exists())

    def test_no_split_and_strict_budget_reach_the_agent(self) -> None:
        self.split("alpha task", "beta task")
        r = self.cli("ask", "--no-split", "--strict-budget", "--budget", "3", "one", "thing")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Parallel work comes first", r.stdout)
        self.assertIn('"--max-budget-usd", "3.00"', r.stdout)
        self.assertNotIn("--max-budget-usd", self.cli("ask", "--no-split", "one", "thing").stdout)

    def test_budget_reminder_is_a_notice_by_default(self) -> None:
        self.split("alpha task", "beta task")
        self.env["FAKE_COST"] = "10"
        r = self.cli("ask", "two", "things")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stderr.count("past the $15.00 reminder"), 1)
        self.assertIn("$20.01 spent", r.stderr)

    def code_repo(self) -> Path:
        """A registered repo whose origin/main has three files, and a fake gh that records what reaches GitHub."""
        repo = self.tmp / "umm"
        repo.mkdir()
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.com\n")
        self.env |= {"GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1"}
        files = {"README.md": "hello\n", "src/app.py": "x = 1\n", "old.txt": "gone soon\n", "c/__pycache__/k.pyc": "o"}
        (repo / "link").symlink_to("missing-target")
        for name, text in files.items():
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text(text)
        for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "base"]):
            subprocess.run(["git", "-C", str(repo), *cmd], env=self.env, check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", "HEAD"], env=self.env, check=True
        )
        cfg = {"branch_prefix": "dev/", "repos": {"umm": {"path": str(repo), "github": "acme/umm", "base": "main"}}}
        (self.home / "repos.json").write_text(json.dumps(cfg))
        bin_dir = self.tmp / "gh-bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text(f'#!/bin/sh\nexec {sys.executable} {ROOT / "tests/fake_gh.py"} "$@"\n')
        (bin_dir / "gh").chmod(0o755)
        self.env |= {"PATH": f"{bin_dir}:{self.env['PATH']}", "FAKE_GH_STATE": str(self.tmp / "gh.json")}
        self.env["FAKE_GH_LOG"] = str(self.tmp / "gh.log")
        return repo

    def gh_state(self) -> dict[str, Any]:
        state: dict[str, Any] = json.loads((self.tmp / "gh.json").read_text())
        return state

    def test_repo_pr_pushes_a_copy_as_signed_commits_and_updates_the_pr(self) -> None:
        repo = self.code_repo()
        base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
        self.env["OPSQUIRE_ASK_ID"] = "ask-1"
        r = self.cli("repo", "checkout", "umm")
        self.assertEqual(r.returncode, 0, r.stderr)
        copy = Path(r.stdout.strip())
        self.assertEqual(copy, self.home / ".repos/ask-1/umm")
        self.assertEqual(self.cli("repo", "checkout", "umm").stdout.strip(), str(copy))
        (copy / "src/app.py").write_text("x = 2\n")
        (copy / "src/new.py").write_text("y = 1\n")
        (copy / "old.txt").unlink()
        (copy / "src/__pycache__").mkdir()
        (copy / "src/__pycache__/app.cpython-310.pyc").write_bytes(b"junk")
        body = self.tmp / "body.md"
        body.write_text("Why: because\n")
        r = self.cli("repo", "pr", str(copy), "--title", "Fix the app", "--body-file", str(body))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("PR: https://github.com/acme/umm/pull/1", r.stdout)
        state = self.gh_state()
        self.assertEqual(state["prs"][0]["head"], "dev/fix-the-app")
        first = state["commits"][0]
        self.assertEqual(first["added"], {"src/app.py": "x = 2\n", "src/new.py": "y = 1\n"})
        self.assertEqual(first["deleted"], ["old.txt"])
        calls = [json.loads(x) for x in (self.tmp / "gh.log").read_text().splitlines()]
        refs = [c for c in calls if c[:3] == ["api", "-X", "POST"]]
        self.assertIn(f"sha={base.strip()}", refs[0])
        (copy / "README.md").write_text("better\n")
        r = self.cli("repo", "pr", str(copy), "--title", "Fix the app")
        self.assertEqual(r.returncode, 0, r.stderr)
        state = self.gh_state()
        self.assertEqual(state["commits"][1]["added"], {"README.md": "better\n"})
        self.assertEqual(state["commits"][1]["deleted"], [])
        self.assertEqual(len(state["prs"]), 1)
        self.assertEqual((repo / "src/app.py").read_text(), "x = 1\n")
        self.assertEqual(self.cli("repo", "pr", str(copy), "--title", "Fix the app").returncode, 0)
        self.assertEqual(len(self.gh_state()["commits"]), 2)

    def test_repo_checkout_of_another_branch_commits_onto_it_only_while_it_is_unmoved(self) -> None:
        repo = self.code_repo()

        def git(*args: str) -> str:
            out = subprocess.run(["git", "-C", str(repo), *args], env=self.env, check=True, capture_output=True)
            return out.stdout.decode().strip()

        git("checkout", "-q", "-b", "peer/review")
        (repo / "src/app.py").write_text("x = 1\npeer = True\n")
        git("commit", "-qam", "peer")
        head = git("rev-parse", "HEAD")
        git("update-ref", "refs/remotes/origin/peer/review", head)
        state_path = self.tmp / "gh.json"
        state_path.write_text(
            json.dumps(
                {
                    "refs": {"peer/review": head},
                    "commits": [],
                    "prs": [{"url": "https://github.com/acme/umm/pull/7", "head": "peer/review", "base": "main"}],
                }
            )
        )
        self.env["OPSQUIRE_ASK_ID"] = "ask-1"
        copy = Path(self.cli("repo", "checkout", "umm", "--base", "peer/review").stdout.strip())
        self.assertEqual(copy, self.home / ".repos/ask-1/umm-peer-review")
        self.assertEqual((copy / "src/app.py").read_text(), "x = 1\npeer = True\n")
        (copy / "src/app.py").write_text("x = 1\npeer = True\nmine = True\n")
        body = self.tmp / "body.md"
        body.write_text("No PR is opened, so this explains the commit.\n")
        r = self.cli(
            "repo", "pr", str(copy), "--title", "Add mine", "--branch", "peer/review", "--body-file", str(body)
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("PR: https://github.com/acme/umm/pull/7", r.stdout)
        self.assertEqual(self.gh_state()["commits"][0]["body"], "No PR is opened, so this explains the commit.")
        gh = self.gh_state()
        self.assertEqual(gh["commits"][0]["branch"], "peer/review")
        self.assertEqual(gh["commits"][0]["added"], {"src/app.py": "x = 1\npeer = True\nmine = True\n"})
        self.assertEqual(len(gh["prs"]), 1)
        gh["refs"]["peer/review"] = "f" * 40
        state_path.write_text(json.dumps(gh))
        (copy / "README.md").write_text("later\n")
        r = self.cli("repo", "pr", str(copy), "--title", "Add mine", "--branch", "peer/review")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("expectedHeadOid mismatch", r.stderr)
        other = Path(self.cli("repo", "checkout", "umm", "--base", "peer/review").stdout.strip())
        self.assertEqual(other, copy)
        main_copy = Path(self.cli("repo", "checkout", "umm").stdout.strip())
        (main_copy / "README.md").write_text("direct\n")
        r = self.cli("repo", "pr", str(main_copy), "--title", "Direct", "--branch", "main")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refusing to commit straight onto main", r.stderr)

    def test_repo_checkout_refuses_a_base_that_is_not_a_branch_name(self) -> None:
        self.code_repo()
        for bad in ("--upload-pack=touch pwned", "a..b", "x y"):
            r = self.cli("repo", "checkout", "umm", f"--base={bad}")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("not a branch name", r.stderr)
        self.assertFalse((self.home / ".repos").exists())

    def test_repo_pr_onto_a_branch_without_a_pr_does_not_repeat_the_commit(self) -> None:
        repo = self.code_repo()
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
        subprocess.run(
            ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/peer/review", head.strip()],
            env=self.env,
            check=True,
        )
        (self.tmp / "gh.json").write_text(json.dumps({"refs": {"peer/review": head.strip()}, "commits": [], "prs": []}))
        copy = Path(self.cli("repo", "checkout", "umm", "--base", "peer/review").stdout.strip())
        (copy / "README.md").write_text("on top\n")
        for _ in range(2):
            r = self.cli("repo", "pr", str(copy), "--title", "On top", "--branch", "peer/review")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Pushed to peer/review (no open PR)", r.stdout)
        self.assertEqual(len(self.gh_state()["commits"]), 1)
        self.assertEqual(self.gh_state()["prs"], [])

    def test_repo_pr_from_another_base_opens_the_pr_against_that_base(self) -> None:
        repo = self.code_repo()
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout
        subprocess.run(
            ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/peer/review", head.strip()],
            env=self.env,
            check=True,
        )
        copy = Path(self.cli("repo", "checkout", "umm", "--base", "peer/review").stdout.strip())
        (copy / "README.md").write_text("on top\n")
        r = self.cli("repo", "pr", str(copy), "--title", "On top")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("dev/on-top -> peer/review", r.stdout)
        self.assertEqual(self.gh_state()["prs"][0]["base"], "peer/review")

    def test_chat_checkouts_never_share_a_copy(self) -> None:
        self.code_repo()
        first = self.cli("repo", "checkout", "umm")
        second = self.cli("repo", "checkout", "umm")
        self.assertEqual(first.returncode, 0, first.stderr)
        a, b = Path(first.stdout.strip()), Path(second.stdout.strip())
        self.assertNotEqual(a, b)
        self.assertEqual({a.parent, b.parent}, {self.home / ".repos/chat"})
        (a / "README.md").write_text("from session a\n")
        self.assertEqual((b / "README.md").read_text(), "hello\n")

    def test_repo_pr_refuses_a_new_title_on_a_copy_with_an_earlier_branch(self) -> None:
        self.code_repo()
        copy = Path(self.cli("repo", "checkout", "umm").stdout.strip())
        (copy / "README.md").write_text("first\n")
        self.assertEqual(self.cli("repo", "pr", str(copy), "--title", "First change").returncode, 0)
        (copy / "src/app.py").write_text("x = 3\n")
        r = self.cli("repo", "pr", str(copy), "--title", "Unrelated change")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("already pushed to branch dev/first-change", r.stderr)
        self.assertIn("--branch dev/first-change", r.stderr)
        self.assertEqual(len(self.gh_state()["commits"]), 1)
        state = json.loads((copy / ".opsquire-pr.json").read_text())
        del state["title"]
        (copy / ".opsquire-pr.json").write_text(json.dumps(state))
        r = self.cli("repo", "pr", str(copy), "--title", "First change")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("an earlier PR", r.stderr)
        r = self.cli("repo", "pr", str(copy), "--title", "Unrelated change", "--branch", "dev/first-change")
        self.assertEqual(r.returncode, 0, r.stderr)
        gh = self.gh_state()
        self.assertEqual([c["branch"] for c in gh["commits"]], ["dev/first-change"] * 2)
        self.assertEqual(gh["commits"][1]["added"], {"src/app.py": "x = 3\n"})
        r = self.cli("repo", "pr", str(copy), "--title", "Split out", "--branch", "dev/split-out")
        self.assertEqual(r.returncode, 0, r.stderr)
        gh = self.gh_state()
        self.assertEqual(gh["commits"][2]["branch"], "dev/split-out")
        self.assertEqual(gh["commits"][2]["added"], {"README.md": "first\n", "src/app.py": "x = 3\n"})
        self.assertEqual(len(gh["prs"]), 2)

    def test_ask_hands_over_its_pr_and_drops_its_repo_copy(self) -> None:
        self.code_repo()
        self.agents_file()
        self.env |= {"FAKE_REPO": "umm", "FAKE_STATE": str(self.tmp / "fake-state.json")}
        r = self.cli("ask", "--no-split", "change", "the", "readme")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("PR: https://github.com/acme/umm/pull/1", r.stdout)
        rec = json.loads(next((self.home / "var/asks").glob("*.json")).read_text())
        self.assertEqual([a["kind"] for a in rec["actions"]], ["RUN", "RUN", "PR"])
        self.assertEqual(self.gh_state()["commits"][0]["added"], {"README.md": "changed by the agent\n"})
        self.assertFalse((self.home / ".repos").exists())

    def test_home_commits_never_include_repo_copies(self) -> None:
        self.git_home()
        self.code_repo()
        self.assertEqual(self.cli("repo", "checkout", "umm").returncode, 0)
        self.new("tidy", "print(1)\n")
        self.assertEqual(self.cli("lint", "tidy").returncode, 0)
        r = self.cli("run", "tidy")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([f for f in self.git("ls-files").splitlines() if f.startswith(".repos")], [])

    def test_unwritable_var_gives_clear_error(self) -> None:
        self.new("rw", "print(1)\n")
        self.assertEqual(self.cli("run", "rw").returncode, 0)
        self.write_lock("other-target", os.getpid())
        var = self.home / "var"
        dirs = sorted((d for d in var.rglob("*") if d.is_dir()), reverse=True) + [var]
        for d in dirs:
            d.chmod(0o500)
        try:
            r = self.cli("run", "rw")
            self.assertEqual(r.returncode, 2)
            self.assertIn("written only by opsquire run", r.stderr)
            queries = [["ls", "--json"], ["show", "rw", "--json"], ["runs", "--json"], ["last", "--json"]]
            queries += [["log", "rw"], ["log", "rw", "--events"], ["locks", "--json"]]
            for query in queries:
                q = self.cli(*query)
                self.assertEqual(q.returncode, 0, f"{query}: {q.stderr}")
            self.assertIn("other-target", self.cli("locks", "--json").stdout)
        finally:
            for d in dirs:
                d.chmod(0o700)

    def test_close_records_outcome_and_shows_in_status_and_catalog(self) -> None:
        folder = self.new(
            "finish-by-hand",
            ASKING_BODY,
            "--changes-prod",
            "--rollback",
            "undo",
        )
        (folder / "design.md").unlink()
        self.assertEqual(self.cli("run", "finish-by-hand").returncode, 0)
        dry = self.last("finish-by-hand")["run_id"]
        r = self.cli("close", "finish-by-hand", "--outcome", "done", "--note", "restore finished by hand", "--run", dry)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no design.md", r.stderr)
        shown = json.loads(self.cli("show", "finish-by-hand", "--json").stdout)
        self.assertEqual(shown["status"], "done")
        outcome = shown["meta"]["outcome"]
        self.assertEqual((outcome["state"], outcome["closed_by"], outcome["run"]), ("done", "test", dry))
        self.assertEqual(self.cli("docs").returncode, 0)
        catalog = (self.home / "docs/CATALOG.md").read_text()
        self.assertIn("Closed done by test: restore finished by hand", catalog)
        (folder / "design.md").write_text("# d\n\n## Result\n\nlive\n")
        again = self.cli("close", "finish-by-hand", "--outcome", "partial", "--note", "one bot still down")
        self.assertNotIn("design.md", again.stderr)
        self.assertEqual(json.loads(self.cli("show", "finish-by-hand", "--json").stdout)["status"], "partial")

    def test_close_abandoned_sets_reason_and_rejects_bad_input(self) -> None:
        self.new("gave-up", "print('x')\n")
        self.assertNotEqual(self.cli("close", "gave-up", "--outcome", "done", "--note", "  ").returncode, 0)
        self.assertNotEqual(self.cli("close", "gave-up", "--outcome", "finished", "--note", "x").returncode, 0)
        bad_run = self.cli("close", "gave-up", "--outcome", "done", "--note", "x", "--run", "nope")
        self.assertNotEqual(bad_run.returncode, 0)
        self.assertIn("not a run of", bad_run.stderr)
        self.assertEqual(
            json.loads(self.cli("show", "gave-up", "--json").stdout)["meta"].get("outcome"),
            None,
            "failed closes write nothing",
        )
        self.assertEqual(
            self.cli("close", "gave-up", "--outcome", "abandoned", "--note", "superseded by hand").returncode, 0
        )
        shown = json.loads(self.cli("show", "gave-up", "--json").stdout)
        self.assertEqual(shown["status"], "abandoned")
        self.assertEqual(shown["meta"]["abandoned_reason"], "superseded by hand")

    def test_close_refuses_general_scripts(self) -> None:
        self.assertEqual(self.cli("new", "web1", "shared-bit", "--general", "--purpose", "g").returncode, 0)
        general = self.cli("close", "web1_shared-bit", "--outcome", "done", "--note", "x")
        self.assertNotEqual(general.returncode, 0)
        self.assertIn("general scripts are not closed", general.stderr)

    def console(self) -> tuple[subprocess.Popen[str], Any]:
        """A real `opsquire ui` on this home and a function that calls its API."""
        import http.client

        port = free_port()
        proc = subprocess.Popen([*CLI, "ui", "--port", str(port)], env=self.env, stdout=subprocess.PIPE, text=True)
        self.assertIn("opsquire ui", proc.stdout.readline() if proc.stdout else "")

        def call(path: str, body: dict[str, Any] | None = None) -> Any:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            conn.request("POST" if body else "GET", path, json.dumps(body) if body else None)
            data = json.loads(conn.getresponse().read())
            conn.close()
            return data

        return proc, call

    def await_job(self, call: Any, job: str, status: str) -> dict[str, Any]:
        deadline = time.time() + 60
        while time.time() < deadline:
            j: dict[str, Any] = call(f"/api/job?id={job}&offset=0")
            if j["status"] == status:
                return j
            time.sleep(0.1)
        self.fail(f"job {job} never reached {status}: {j}")

    def nested_task(self, child: str, lang: str) -> str:
        """A changing task that runs a general script as a plain child, knowing nothing about the prompt handshake."""
        made = self.cli("new", "web1", "child", "--general", "--purpose", "asks", "--lang", lang)
        self.assertEqual(made.returncode, 0, made.stderr)
        (Path(made.stdout.strip()) / f"main.{lang}").write_text(child)
        interpreter = "sys.executable" if lang == "py" else '"bash"'
        folder = self.new(
            "parent",
            "import os\nimport subprocess\nimport sys\n\n"
            f'child = os.environ["OPSQUIRE_GENERAL"] + "/web1_child/main.{lang}"\n'
            f"sys.exit(subprocess.run([{interpreter}, child], check=False).returncode)\n",
            "--changes-prod",
            "--rollback",
            "none",
            "--no-verify",
            "--uses",
            "web1_child",
        )
        lint = self.cli("lint", "parent")
        self.assertEqual(lint.returncode, 0, lint.stdout)
        return folder.name

    def test_a_nested_scripts_prompt_reaches_the_console_and_the_record(self) -> None:
        lines = "x" * 100
        children = {
            "py": "import opsquire_helpers as sq\n\n"
            f"for i in range(1, 41):\n    print(i, '{lines}')\nsq.confirm('Deploy web1?')\nsq.result('deployed')\n",
            "sh": BASH_HEADER + f'for i in $(seq 40); do echo "$i {lines}"; done\n'
            'sq_confirm "Deploy web1?"\nsq_result deployed\n',
        }
        for lang, child in children.items():
            with self.subTest(lang=lang):
                tid = self.nested_task(child, lang)
                self.assertEqual(self.cli("run", tid, answers=[("[y/N]", "y")]).returncode, 0)
                proc, call = self.console()
                try:
                    job = call("/api/run", {"task": tid, "mode": "apply"})["job_id"]
                    j = self.await_job(call, job, "waiting")
                    self.assertEqual(j["prompt"], ">>> Deploy web1? [y/N]")
                    self.assertTrue(j["text"].endswith("[y/N] "), j["text"][-200:])
                    self.assertIn(f"40 {lines}", j["text"])
                    rec = self.last(tid)
                    self.assertEqual((rec["status"], rec["waiting"]["question"]), ("running", "Deploy web1?"))
                    self.assertIn("waiting for the operator: Deploy web1?", Path(rec["paths"]["output"]).read_text())
                    self.assertIn('"event": "waiting"', Path(rec["paths"]["events"]).read_text())
                    table = self.cli("runs", tid).stdout.splitlines()[-1]
                    self.assertTrue(table.startswith("waiting"), table)
                    self.assertIn("asks the operator: Deploy web1?", table)
                    self.assertIn("waiting for the operator to answer", self.cli("log", tid).stdout)
                    view = call("/api/state")["runs"][rec["run_id"]]
                    self.assertEqual(view["waiting"]["question"], "Deploy web1?")
                    call("/api/answer", {"job": job, "text": "y"})
                    self.assertEqual(self.await_job(call, job, "done")["exit_code"], 0)
                    rec = self.last(tid)
                    self.assertEqual((rec["status"], rec["waiting"], rec["results"]), ("ok", None, ["deployed"]))
                finally:
                    proc.send_signal(signal.SIGINT)
                    self.assertEqual(proc.wait(timeout=15), 0)
                shutil.rmtree(next(self.home.glob(f"tasks/*/{tid}")))
                shutil.rmtree(self.home / "general/web1_child")
                shutil.rmtree(self.home / "var/runs" / tid)

    def test_console_keeps_a_prompt_that_output_landed_behind(self) -> None:
        from opsquire.jobs import Job, Jobs

        jobs, master_slave = Jobs(), os.pipe()
        job = Job(
            "j1", "t", "apply", "now", output=bytearray(b"plan 1\r\n\r\n>>> Deploy?\r\nnow [y/N] plan 2\r\nplan 3\r\n")
        )
        job.fd = master_slave[1]
        jobs.jobs[job.job_id] = job
        try:
            jobs.detect_helper_prompt(job)
            self.assertEqual((job.status, job.prompt), ("waiting", ">>> Deploy?\nnow [y/N]"))
            job.output += b"more output\r\n"
            jobs.detect_helper_prompt(job)
            self.assertEqual(job.status, "waiting")
            job.output += b"\r\n>>> Second? [y/N] "
            jobs.answer("j1", "y")
            self.assertEqual(os.read(master_slave[0], 10), b"y\n")
            self.assertEqual((job.status, job.prompt), ("waiting", ">>> Second? [y/N]"))
            jobs.answer("j1", "n")
            self.assertEqual((job.status, job.prompt), ("running", None))
            jobs.detect_helper_prompt(job)
            self.assertEqual(job.status, "running")
        finally:
            os.close(master_slave[0])
            os.close(master_slave[1])

    def test_a_run_stopped_at_its_prompt_says_so_and_leaves_nothing(self) -> None:
        tmpdir = self.tmp / "scratch"
        tmpdir.mkdir()
        self.env["TMPDIR"] = str(tmpdir)
        tid = self.nested_task("import opsquire_helpers as sq\n\nsq.run(lambda: sq.confirm('Deploy web1?'))\n", "py")
        r = self.cli("run", tid, "--apply", *self.NO_DRY, answers=[("[y/N]", "\x03")])
        self.assertEqual(r.returncode, 130, r.stdout)
        rec = self.last(tid)
        self.assertEqual((rec["status"], rec["waiting"]["question"]), ("interrupted", "Deploy web1?"))
        self.assertIn("waiting for the operator to answer: Deploy web1?", rec["last_error"])
        self.assertEqual([p.name for p in tmpdir.glob("opsquire-sync-*")], [])

    def test_handover_refuses_an_apply_that_already_ran_the_same_way(self) -> None:
        folder = self.new(
            "flip",
            "import sys\n\nimport opsquire_helpers as sq\n\nsq.result(' '.join(sys.argv[1:]))\n",
            "--changes-prod",
            "--rollback",
            "flip back",
            "--no-verify",
        )
        (folder / "design.md").write_text("# flip\n")
        self.assertEqual(self.cli("lint", "flip").returncode, 0)
        self.assertEqual(self.cli("run", "flip").returncode, 0)
        self.assertEqual(self.cli("handover", "flip", "--apply", "--why", "dry ok").returncode, 0)
        self.assertEqual(self.cli("run", "flip", "--apply").returncode, 0)
        applied = self.last("flip")
        self.assertIn("## Result", (folder / "design.md").read_text(), "closing changes the task's content hash")

        refused = self.cli("handover", "flip", "--apply", "--why", "dry ok")
        self.assertEqual(refused.returncode, 2)
        self.assertIn(applied["run_id"], refused.stderr)
        self.assertIn("--again", refused.stderr)
        self.assertEqual(json.loads(self.cli("threads", "--json").stdout)["inbox"], [])
        self.assertEqual(self.cli("handover", "flip", "--apply", "--again").returncode, 2, "--again needs a reason")

        again = self.cli("handover", "flip", "--apply", "--again", "--why", "rolled back at 09:00")
        self.assertEqual(again.returncode, 0, again.stderr)
        item = json.loads(self.cli("threads", "--json").stdout)["inbox"][0]
        self.assertIn(applied["run_id"], item["applied_already"])
        self.assertEqual(
            self.cli("handover", "flip", "--withdraw").stdout.strip(), f"{folder.name}: handover withdrawn"
        )
        self.assertEqual(json.loads(self.cli("threads", "--json").stdout)["inbox"], [])
        self.assertIn("no handover", self.cli("handover", "flip", "--withdraw").stdout)

        uncovered = self.cli("handover", "flip", "--apply", "--", "other")
        self.assertEqual(uncovered.returncode, 2, "other arguments need their own dry run")
        self.assertIn(f"opsquire run {folder.name} -- other", uncovered.stderr)
        self.assertEqual(self.cli("run", "flip", "--", "other").returncode, 0)
        self.assertEqual(self.cli("handover", "flip", "--apply", "--", "other").returncode, 0, "other arguments")
        repeated = self.cli("run", "flip", "--apply")
        self.assertEqual(repeated.returncode, 0)
        self.assertIn("this apply runs it again", repeated.stderr, "a typed command still says so")
        (folder / "main.py").write_text((folder / "main.py").read_text() + "\n")
        self.assertEqual(self.cli("handover", "flip", "--apply").returncode, 2, "other scripts need a dry run")
        self.assertEqual(self.cli("run", "flip").returncode, 0)
        self.assertEqual(self.cli("handover", "flip", "--apply").returncode, 0, "other scripts")

    def test_a_failed_apply_does_not_count_as_already_applied(self) -> None:
        self.new(
            "boom",
            "import sys\n\nimport opsquire_helpers as sq\n\nsys.exit(1 if sq.APPLY else 0)\n",
            "--changes-prod",
            "--rollback",
            "none",
            "--no-verify",
            "--target",
            "web1",
        )
        self.assertEqual(self.cli("lint", "boom").returncode, 0)
        self.assertEqual(self.cli("run", "boom").returncode, 0)
        self.assertEqual(self.cli("run", "boom", "--apply").returncode, 1)
        self.assertEqual(self.cli("handover", "boom", "--apply", "--why", "the host is back").returncode, 0)

    def test_an_ask_keeps_a_repeated_apply_out_of_the_inbox(self) -> None:
        self.new("flip", "print(1)\n", "--changes-prod", "--rollback", "flip back", "--no-verify")
        self.assertEqual(self.cli("lint", "flip").returncode, 0)
        self.assertEqual(self.cli("run", "flip", "--apply", *self.NO_DRY).returncode, 0)
        from opsquire import handover

        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(os.environ, {"OPSQUIRE_HOME": str(self.home)}),
            mock.patch("sys.stdout", out),
            mock.patch("sys.stderr", err),
        ):
            handed = handover.record_run_lines([("RUN", "opsquire run flip --apply")], "again", "ask 1")
        self.assertEqual(handed, [])
        self.assertIn("not put in the inbox", err.getvalue())

    def test_a_plan_that_applied_ok_is_not_handed_over_again(self) -> None:
        self.new("step", "print(1)\n", "--changes-prod", "--rollback", "none", "--no-verify")
        step = next(self.home.glob("tasks/*/*_web1_step")).name
        made = self.cli("new", "web1", "bundle", "--plan", "--step", step, "--purpose", "p", "--changes-prod")
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertEqual(self.cli("lint", "bundle").returncode, 0)
        self.assertEqual(self.cli("run", "bundle", "--apply", *self.NO_DRY).returncode, 0)
        refused = self.cli("handover", "bundle", "--apply", "--why", "again")
        self.assertEqual(refused.returncode, 2, refused.stdout)

    def test_new_and_handover_say_what_else_waits_on_the_target(self) -> None:
        waiting = self.new("replay", "print(1)\n", "--target", "db", "--target", "db_2", area="data")
        self.assertEqual(self.cli("lint", "replay").returncode, 0)
        new = self.cli("new", "db-2", "canary", "--purpose", "p", "--changes-prod", "--rollback", "none")
        self.assertEqual(new.returncode, 0)
        self.assertEqual(len(new.stdout.strip().splitlines()), 1, "stdout stays one path for scripts")
        self.assertIn(f"also waiting on db_2: {waiting.name}, read run", new.stderr)
        self.assertIn("one plan or task", new.stderr)
        canary = Path(new.stdout.strip())
        self.assertEqual(self.cli("lint", "canary").returncode, 0)
        handed = self.cli("handover", "canary", "--why", "go")
        self.assertIn(waiting.name, handed.stderr)
        as_json = json.loads(self.cli("handover", "canary", "--why", "go", "--json").stdout)
        self.assertEqual(as_json["waiting_on_same_target"][0]["task_id"], waiting.name)
        self.assertEqual(self.cli("new", "other-host", "x", "--purpose", "p").stderr, "", "nothing else waits there")
        inbox = {i["task_id"]: i for i in json.loads(self.cli("threads", "--json").stdout)["inbox"]}
        self.assertEqual(inbox[canary.name]["same_target"], [waiting.name])
        self.assertEqual(inbox[waiting.name]["same_target"], [canary.name])

    def test_a_failed_changing_task_leaves_the_inbox_until_it_changes(self) -> None:
        folder = self.new(
            "canary", "import sys\n\nsys.exit(1)\n", "--changes-prod", "--rollback", "none", "--no-verify"
        )
        self.assertEqual(self.cli("lint", "canary").returncode, 0)
        self.assertEqual(self.cli("run", "canary").returncode, 1)
        self.assertEqual(json.loads(self.cli("threads", "--json").stdout)["inbox"], [])
        (folder / "main.py").write_text("print(1)\n")
        self.assertEqual(self.cli("lint", "canary").returncode, 0)
        inbox = json.loads(self.cli("threads", "--json").stdout)["inbox"]
        self.assertEqual([i["next"] for i in inbox], ["dry"])
        (folder / "main.py").write_text("import sys\n\nsys.exit(1)\n")
        self.assertEqual(self.cli("lint", "canary").returncode, 0)
        self.assertEqual(self.cli("run", "canary").returncode, 1)
        self.assertEqual(self.cli("handover", "canary", "--why", "the host is back").returncode, 0)
        inbox = json.loads(self.cli("threads", "--json").stdout)["inbox"]
        self.assertEqual([i["next"] for i in inbox], ["dry"], "a handover brings it back")

    def test_new_takes_json_and_explains_a_bad_area(self) -> None:
        made = self.cli("new", "web1", "thing", "--purpose", "p", "--json")
        self.assertEqual(made.returncode, 0, made.stderr)
        data = json.loads(made.stdout)
        self.assertTrue((Path(data["path"]) / "meta.json").is_file())
        self.assertEqual((data["id"], data["waiting_on_same_target"]), (Path(data["path"]).name, []))
        closed = json.loads(self.cli("close", "thing", "--outcome", "abandoned", "--note", "n", "--json").stdout)
        self.assertEqual(closed["outcome"], "abandoned")

        bad = self.cli("new", "db_2", "Other_Name", "--purpose", "p")
        self.assertEqual((bad.returncode, bad.stdout), (2, ""))
        self.assertIn("nothing created", bad.stderr)
        self.assertIn("area 'db_2', use 'db-2'", bad.stderr)
        self.assertIn("name 'Other_Name', use 'other-name'", bad.stderr)
        self.assertIn("--target", bad.stderr)
        self.assertEqual(len(list(self.home.glob("tasks/*/*"))), 1)

    def test_contract_and_skill_say_how_to_call_opsquire_off_path(self) -> None:
        self.env["PATH"] = "/usr/bin:/bin"
        contract = self.cli("contract").stdout
        self.assertNotIn("{{", contract.split("## Local rules")[0])
        self.assertIn("-m opsquire", contract.split("## Layout")[0])
        self.env["CLAUDE_CONFIG_DIR"] = str(self.tmp / "claude")
        self.env["HOME"] = str(self.tmp)
        self.assertEqual(self.cli("integrate").returncode, 0)
        skill = (self.tmp / "claude/skills/opsquire/SKILL.md").read_text()
        self.assertNotIn("{{", skill)
        self.assertIn("-m opsquire", skill)

    def test_catalog_and_suggestions_count_script_versions(self) -> None:
        folder = self.new("replay", "print(1)\n")
        (folder / "design.md").write_text("# replay\n")
        for n in range(3):
            (folder / "main.py").write_text(f"print({n})\n")
            self.assertEqual(self.cli("lint", "replay").returncode, 0)
            self.assertEqual(self.cli("run", "replay").returncode, 0)
        (folder / "design.md").write_text("# replay\n\nnotes\n")
        self.assertEqual(self.cli("lint", "replay").returncode, 0)
        self.assertEqual(self.cli("run", "replay").returncode, 0)
        self.assertEqual(self.cli("docs").returncode, 0)
        catalog = (self.home / "docs/CATALOG.md").read_text()
        self.assertIn("runs 4, 4 ok read, 3 script versions", catalog, "design notes are not a version")
        view = json.loads(self.cli("threads", "--json").stdout)["tasks"][folder.name]
        self.assertEqual(view["stats"]["versions"], 3)
        self.assertIn("4 runs over 3 script versions", " ".join(s["text"] for s in view["suggestions"]))
        scripts = [line.split()[4] for line in self.cli("show", "replay").stdout.splitlines()[-4:]]
        self.assertEqual((len(set(scripts)), scripts[2] == scripts[3]), (3, True))

    def test_repo_warns_about_a_stale_copy_and_open_prs_on_the_same_files(self) -> None:
        repo = self.code_repo()
        origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], env=self.env, check=True)
        for cmd in (["remote", "add", "origin", str(origin)], ["push", "-q", "origin", "HEAD:refs/heads/main"]):
            subprocess.run(["git", "-C", str(repo), *cmd], env=self.env, check=True, capture_output=True)
        listed = json.loads(self.cli("repo", "list", "--json").stdout)[0]
        self.assertIn("up to date", listed["local"])
        fresh = self.cli("repo", "checkout", "umm")
        self.assertEqual(fresh.stderr, "", "origin matches")
        copy = Path(fresh.stdout.strip())

        other = self.tmp / "other"
        subprocess.run(["git", "worktree", "add", "-q", "--detach", str(other)], cwd=repo, env=self.env, check=True)
        (other / "moved.txt").write_text("x\n")
        for cmd in (
            ["add", "-A"],
            ["commit", "-q", "-m", "moved"],
            ["push", "-q", "origin", "HEAD:refs/heads/main"],
        ):
            subprocess.run(["git", "-C", str(other), *cmd], env=self.env, check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "update-ref", "refs/remotes/origin/main", "HEAD~0"], env=self.env, check=True
        )
        listed = json.loads(self.cli("repo", "list", "--json").stdout)[0]
        self.assertRegex(listed["local"], r"1 behind origin/main")
        with mock.patch.dict(os.environ, {"OPSQUIRE_HOME": str(self.home)}):
            from opsquire import repos

            note = repos.stale_copy_note(copy)
        self.assertIn("could not fetch", note)
        self.assertIn(f"git -C {repo} fetch origin main", note)

        (copy / "src/app.py").write_text("x = 3\n")
        (self.tmp / "gh.json").write_text(
            json.dumps(
                {
                    "refs": {},
                    "commits": [],
                    "prs": [],
                    "open_prs": [
                        {
                            "url": "https://github.com/acme/umm/pull/9",
                            "author": {"login": "colleague"},
                            "headRefName": "their-fix",
                            "files": [{"path": "src/app.py"}, {"path": "unrelated.py"}],
                        },
                        {
                            "url": "https://github.com/acme/umm/pull/10",
                            "author": {"login": "other"},
                            "headRefName": "elsewhere",
                            "files": [{"path": "unrelated.py"}],
                        },
                    ],
                }
            )
        )
        found = json.loads(self.cli("repo", "overlap", str(copy), "--json").stdout)
        self.assertEqual(
            found,
            [
                {
                    "url": "https://github.com/acme/umm/pull/9",
                    "author": "colleague",
                    "branch": "their-fix",
                    "files": ["src/app.py"],
                }
            ],
        )
        body = self.tmp / "body.md"
        body.write_text("why\n")
        opened = self.cli("repo", "pr", str(copy), "--title", "fix app", "--body-file", str(body))
        self.assertEqual(opened.returncode, 0, opened.stderr)
        self.assertIn("pull/9 (colleague) also changes src/app.py", opened.stderr)

    def test_a_recorded_end_survives_a_renamed_general_script(self) -> None:
        g = Path(self.cli("new", "web1", "shared", "--general", "--purpose", "shared logic").stdout.strip())
        made = {}
        for name in ("replaced", "dropped", "ran", "waiting"):
            made[name] = self.new(name, "print(1)\n", "--uses", "web1_shared")
            self.assertEqual(self.cli("lint", name).returncode, 0)
        meta = json.loads((made["replaced"] / "meta.json").read_text())
        (made["replaced"] / "meta.json").write_text(json.dumps(meta | {"superseded_by": made["ran"].name}))
        self.assertEqual(self.cli("close", "dropped", "--outcome", "abandoned", "--note", "not needed").returncode, 0)
        self.assertEqual(self.cli("run", "ran").returncode, 0)
        renamed = g.with_name("web1_common")
        g.rename(renamed)
        (renamed / "meta.json").write_text(
            json.dumps(json.loads((renamed / "meta.json").read_text()) | {"id": "web1_common"})
        )
        listed = {t["id"].split("_", 2)[-1]: t for t in json.loads(self.cli("ls", "--json").stdout)}
        self.assertIn("uses 'web1_shared' is not a general script", listed["replaced"]["meta_errors"])
        self.assertEqual(
            {name: listed[name]["status"] for name in made},
            {"replaced": "superseded", "dropped": "abandoned", "ran": "done", "waiting": "draft"},
        )
        st = json.loads(self.cli("threads", "--json").stdout)
        views = {name: st["tasks"][folder.name] for name, folder in made.items()}
        self.assertEqual(
            {name: v["settled"] for name, v in views.items()}, dict.fromkeys(made, True) | {"waiting": False}
        )
        self.assertEqual(views["dropped"]["outcome"]["state"], "abandoned")
        self.assertEqual(views["replaced"]["superseded_by"], made["ran"].name)
        self.assertEqual([s["kind"] for s in views["waiting"]["suggestions"]], ["broken"])
        self.assertIn("web1_shared", views["waiting"]["suggestions"][0]["text"])
        self.assertEqual(views["ran"]["suggestions"], [])
        self.assertEqual(self.cli("docs").returncode, 0)
        catalog = (self.home / "docs/CATALOG.md").read_text()
        self.assertRegex(catalog, r"web1_replaced \[superseded, task; never run\]")
        self.assertRegex(catalog, r"web1_dropped \[abandoned, task; never run\].* Closed abandoned by test: not needed")

    def test_a_general_script_shows_its_lint_and_a_task_lint_checks_a_changed_one(self) -> None:
        g = Path(self.cli("new", "web1", "shared", "--general", "--purpose", "shared logic").stdout.strip())
        good = '"""Doubling helpers."""\n\n\ndef double(x: int) -> int:\n    """Twice x."""\n    return x * 2\n'
        (g / "logic.py").write_text(good)
        (g / "test_logic.py").write_text(self.PASSING_TEST)

        def script() -> dict[str, Any]:
            rows: list[dict[str, Any]] = json.loads(self.cli("ls", "--kind", "general", "--json").stdout)
            return rows[0]

        self.assertEqual(script()["status"], "unchecked")
        self.assertEqual(self.cli("lint", "web1_shared").returncode, 0)
        self.assertEqual(script()["status"], "lint-ok")
        self.assertIn("(lint-ok; tests: 2; used by 0; never run)", (self.home / "docs/CATALOG.md").read_text())
        self.assertIn("- lint: lint-ok", (self.home / "docs/web1_shared.md").read_text())

        (g / "logic.py").write_text("import os\n" + good)
        self.assertEqual(script()["status"], "unchecked", "edited since its lint")
        self.new("user", "print(1)\n", "--uses", "web1_shared")
        self.assertIn("(unchecked; tests: 2; used by 1; never run)", (self.home / "docs/CATALOG.md").read_text())
        failed = self.cli("lint", "user")
        self.assertEqual(failed.returncode, 1)
        self.assertIn("web1_shared: LINT FAILED (general script, changed since its lint)", failed.stdout)
        self.assertEqual(script()["status"], "lint-failing")
        self.assertEqual(len(json.loads(self.cli("ls", "--status", "lint-failing", "--json").stdout)), 1)
        self.assertIn("(lint-failing; tests: 2; used by 1; never run)", (self.home / "docs/CATALOG.md").read_text())
        st = json.loads(self.cli("threads", "--json").stdout)
        self.assertEqual(st["scripts"]["web1_shared"]["lint"], "lint-failing")
        self.assertEqual(st["inbox"], [])

        (g / "logic.py").write_text(good)
        fixed = self.cli("lint", "user", "--json")
        self.assertEqual(fixed.returncode, 0, fixed.stdout)
        self.assertEqual(json.loads(fixed.stdout)["scripts"], {"web1_shared": True})
        self.assertEqual(script()["status"], "lint-ok")
        self.assertEqual(json.loads(self.cli("lint", "user", "--json").stdout)["scripts"], {}, "unchanged: not again")

    def test_a_dry_run_of_older_scripts_does_not_offer_the_apply(self) -> None:
        g = Path(self.cli("new", "web1", "shared", "--general", "--purpose", "shared logic").stdout.strip())
        folder = self.new(
            "canary", "print(1)\n", "--changes-prod", "--rollback", "none", "--no-verify", "--uses", "web1_shared"
        )

        def owed() -> list[str]:
            return [i["next"] for i in json.loads(self.cli("threads", "--json").stdout)["inbox"]]

        self.assertEqual(self.cli("lint", "canary").returncode, 0)
        self.assertEqual(owed(), ["dry"])
        self.assertEqual(self.cli("run", "canary").returncode, 0)
        self.assertEqual(owed(), ["apply"])
        (folder / "design.md").write_text("# canary\n\nnotes do not change what runs\n")
        self.assertEqual(self.cli("lint", "canary").returncode, 0)
        self.assertEqual(owed(), ["apply"])
        (g / "notes.txt").write_text("the general script it uses changed after the dry run\n")
        self.assertEqual(owed(), ["dry"], "changed since lint, and the dry run was of other files")
        self.assertEqual(self.cli("lint", "canary").returncode, 0)
        self.assertEqual(owed(), ["dry"])
        self.assertEqual(self.cli("run", "canary").returncode, 0)
        self.assertEqual(owed(), ["apply"])
        (folder / "main.py").write_text("print(2)\n")
        self.assertEqual(self.cli("lint", "canary").returncode, 0)
        self.assertEqual(owed(), ["dry"])

    def backdate(self, folder: Path, days: float) -> None:
        """Make a task look last touched `days` ago: its creation stamp and the time meta.json was written."""
        meta = json.loads((folder / "meta.json").read_text())
        then = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
        meta["created"] = then.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        (folder / "meta.json").write_text(json.dumps(meta))
        os.utime(folder / "meta.json", (then.timestamp(), then.timestamp()))

    def due_in(self, days: float) -> list[str]:
        code = (
            "import datetime as dt, json\nfrom opsquire.model import now\nfrom opsquire.offboard import due\n"
            f"print(json.dumps([t.id for t in due(now() + dt.timedelta(days={days}))]))"
        )
        r = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        ids: list[str] = json.loads(r.stdout)
        return ids

    def offboard_env(self, **config: Any) -> None:
        (self.tmp / "cfg/opsquire").mkdir(parents=True)
        (self.tmp / "cfg/opsquire/config.json").write_text(json.dumps(config))
        self.env["XDG_CONFIG_HOME"] = str(self.tmp / "cfg")

    def test_settled_tasks_leave_after_the_retention_and_come_back(self) -> None:
        self.offboard_env()
        self.git_home()
        seen = self.new("seen", "print('looked')\n", "--thread", "cleanup")
        self.assertEqual(self.cli("lint", "seen").returncode, 0)
        self.assertEqual(self.cli("run", "seen").returncode, 0)
        open_task = self.new("open", "print(1)\n", "--changes-prod", "--rollback", "none", "--no-verify")
        self.assertEqual(self.cli("lint", "open").returncode, 0)
        stale = self.new("stale", "print(1)\n", "--thread", "cleanup")
        fresh = self.new("fresh", "print(1)\n", "--thread", "cleanup")
        for name in ("stale", "fresh"):
            r = self.cli("close", name, "--outcome", "abandoned", "--note", "not needed after all")
            self.assertEqual(r.returncode, 0, r.stderr)
        self.backdate(stale, 8)
        self.assertEqual(self.due_in(0), [stale.name])
        self.assertEqual(self.due_in(6), [stale.name], "a read task that ran ok and a close both start the clock")
        self.assertEqual(self.due_in(8), sorted([seen.name, stale.name, fresh.name]), "the open task never leaves")

        dry = json.loads(self.cli("offboard", "--due", "--dry", "--json").stdout)
        self.assertEqual((dry["would_offboard"], dry["offboarded"], dry["days"]), ([stale.name], [], 7.0))
        self.assertTrue(stale.is_dir())

        stamp = self.home / "var/offboard.json"
        self.assertTrue(stamp.is_file(), "the commands above already swept today and found nothing")
        stamp.unlink()
        r = self.cli("lint", "open")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("offboarded 1 settled tasks idle 7+ days", r.stderr)
        gone = self.home / "offboarded" / stale.parent.name / stale.name
        self.assertTrue((gone / "main.py").is_file())
        self.assertFalse(stale.exists())
        log = subprocess.run(
            ["git", "-C", str(self.home), "log", "--format=%s", "--name-status", "-M", "-3"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertIn("offboarded 1 tasks idle 7+ days", log)
        self.assertRegex(log, rf"R100\ttasks/[\d-]+/{stale.name}/main.py\toffboarded/[\d-]+/{stale.name}/main.py")

        self.backdate(fresh, 8)
        r = self.cli("lint", "open")
        self.assertNotIn("offboarded", r.stderr, "the sweep rides at most once a day")
        self.assertTrue(fresh.is_dir())
        r = self.cli("offboard", "--due")
        self.assertEqual((r.returncode, r.stdout), (0, f"offboarded 1 idle 7+ days:\n  {fresh.name}\n"))
        self.assertEqual(self.cli("offboard", "--due").stdout, "offboarded 0 idle 7+ days\n")

        listed = {t["id"] for t in json.loads(self.cli("ls", "--json").stdout)}
        self.assertEqual(listed, {seen.name, open_task.name})
        hits = {h["id"]: h for h in json.loads(self.cli("find", "stale", "seen", "--json").stdout)}
        self.assertEqual((hits[stale.name]["offboarded"], hits[stale.name]["status"]), (True, "abandoned"))
        self.assertFalse(hits[seen.name]["offboarded"])
        self.assertIn("abandoned, offboarded", self.cli("find", "stale").stdout)
        shown = json.loads(self.cli("show", "stale", "--json").stdout)
        self.assertEqual((shown["offboarded"], shown["path"]), (True, str(gone)))
        self.assertEqual(self.cli("runs", "stale").returncode, 0)
        for cmd in (
            ["run", "stale"],
            ["lint", "stale"],
            ["handover", "stale"],
            ["close", "stale", "--outcome", "done"],
        ):
            r = self.cli(*cmd, "--note", "x") if cmd[0] == "close" else self.cli(*cmd)
            self.assertEqual(r.returncode, 2, cmd)
            self.assertIn(f"`opsquire restore {stale.name}` brings it back", r.stderr)
        catalog = (self.home / "docs/CATALOG.md").read_text()
        self.assertNotIn(stale.name, catalog)
        self.assertIn("## Offboarded\n\n2 settled tasks idle past the retention are in offboarded/", catalog)
        state = json.loads(self.cli("threads", "--json").stdout)
        self.assertEqual((state["offboarded"], sorted(state["tasks"])), (2, sorted([seen.name, open_task.name])))
        thread = next(th for th in state["threads"] if th["id"] == "cleanup")
        self.assertEqual((thread["tasks"], thread["offboarded"]), ([seen.name], 2))
        self.assertIn("(+2 offboarded)", self.cli("threads").stdout)
        ended = [m["task_id"] for m in json.loads(self.cli("milestones", "--since", "30d", "--json").stdout)]
        self.assertEqual(ended, [], "abandoned read tasks are no milestone, and reading them back does not fail")

        r = self.cli("restore", "stale")
        self.assertEqual((r.returncode, r.stdout), (0, f"restored: {stale.name}\n"))
        self.assertTrue((stale / "main.py").is_file())
        self.assertFalse(gone.exists())
        self.assertEqual(self.due_in(0), [], "restoring starts the idle clock again")
        self.assertIn(stale.name, (self.home / "docs/CATALOG.md").read_text())
        self.assertIn("is not offboarded", self.cli("restore", "stale").stderr)
        self.assertEqual(self.cli("restore", "fresh").returncode, 0)
        self.assertEqual(list((self.home / "offboarded").iterdir()), [])
        status = subprocess.run(
            ["git", "-C", str(self.home), "status", "--porcelain"], capture_output=True, text=True, check=True
        ).stdout
        self.assertEqual(status, "", "every move is committed")

    def test_offboarding_by_hand_refuses_what_is_still_alive(self) -> None:
        self.offboard_env(offboard_after_days=0)
        script = Path(self.cli("new", "web1", "shared", "--general", "--purpose", "shared logic").stdout.strip())
        user = self.new("user", "print(1)\n", "--uses", "web1_shared")
        self.assertEqual(self.cli("lint", "user").returncode, 0)
        r = self.cli("offboard", "user")
        self.assertEqual(r.returncode, 2)
        self.assertIn(f"nothing offboarded:\n  {user.name}: it is not settled (status ready)", r.stderr)
        self.assertEqual(self.cli("run", "user").returncode, 0)
        self.assertEqual(self.cli("handover", "user", "--why", "look again").returncode, 0)
        self.assertIn("a read run of it is waiting in the inbox", self.cli("offboard", "user").stderr)
        self.assertEqual(self.cli("handover", "user", "--withdraw").returncode, 0)
        r = self.cli("offboard", "web1_shared")
        self.assertEqual(r.returncode, 2)
        self.assertIn(f"web1_shared: active task {user.name} uses it", r.stderr)
        self.assertEqual(self.cli("offboard").returncode, 2)
        self.assertEqual(self.cli("offboard", "user", "--due").returncode, 2)
        self.assertIn("retention 0", self.cli("offboard", "--due").stderr)
        self.assertEqual(self.due_in(30), [user.name], "the home's retention of 0 only stops the sweep")
        self.backdate(user, 30)
        self.assertNotIn("offboarded", self.cli("lint", "web1_shared").stderr)
        self.assertTrue(user.is_dir(), "retention 0: nothing leaves on its own")

        self.assertEqual(self.cli("offboard", "user").returncode, 0)
        catalog = (self.home / "docs/CATALOG.md").read_text()
        self.assertIn("used by 0, 1 offboarded; runs 1, 1 ok read)", catalog)
        self.assertIn(f"- used by offboarded tasks: {user.name}", (self.home / "docs/web1_shared.md").read_text())
        scripts = json.loads(self.cli("threads", "--json").stdout)["scripts"]
        self.assertEqual((scripts["web1_shared"]["used_by"], scripts["web1_shared"]["used_by_offboarded"]), ([], 1))
        self.assertTrue(script.is_dir(), "a general script never leaves on its own")
        self.assertEqual(self.cli("offboard", "--due", "--days", "0").stdout, "offboarded 0 idle 0+ days\n")

        self.assertEqual(self.cli("offboard", "web1_shared").returncode, 0)
        self.assertTrue((self.home / "offboarded/general/web1_shared/main.py").is_file())
        self.assertTrue((self.home / "general").is_dir())
        self.assertFalse((self.home / "docs/web1_shared.md").exists())
        r = self.cli("new", "web1", "shared", "--general", "--purpose", "again")
        self.assertEqual(r.returncode, 2)
        self.assertIn("web1_shared exists and is offboarded: `opsquire restore web1_shared`", r.stderr)
        other = self.new("other", "print(1)\n")
        meta = json.loads((other / "meta.json").read_text())
        (other / "meta.json").write_text(json.dumps(meta | {"uses": ["web1_shared"]}))
        errors = json.loads(self.cli("show", "other", "--json").stdout)["meta_errors"]
        self.assertEqual(
            errors,
            [
                "uses 'web1_shared' is not a general script (it is offboarded: `opsquire restore web1_shared` "
                "brings it back)"
            ],
        )

        r = self.cli("restore", "user", "--json")
        self.assertEqual(json.loads(r.stdout)["restored"], ["web1_shared", user.name])
        shown = json.loads(self.cli("show", "user", "--json").stdout)
        self.assertEqual((shown["status"], shown["meta_errors"], shown["offboarded"]), ("done", [], False))
        self.assertEqual(self.cli("lint", "other").returncode, 0)

    def test_a_plan_and_its_steps_leave_together(self) -> None:
        self.offboard_env()
        first = self.new("first", "print(1)\n")
        second = self.new("second", "print(2)\n")
        for step in (first, second):
            self.assertEqual(self.cli("lint", step.name).returncode, 0)
        plan = self.make_plan(first, second)
        self.assertEqual(self.cli("lint", plan.name).returncode, 0)
        self.assertEqual(self.cli("run", plan.name).returncode, 0)
        everything = [plan.name, first.name, second.name]
        self.assertEqual(
            self.due_in(8), everything, "the plan first: an interrupted sweep leaves no plan without steps"
        )
        self.assertEqual(self.cli("handover", "second", "--why", "once more").returncode, 0)
        self.assertEqual(self.due_in(8), [], "one step has a run waiting, so the plan and the other step stay too")
        r = self.cli("offboard", "first")
        self.assertEqual(r.returncode, 2)
        self.assertIn(f"{first.name}: it is a step of plan {plan.name}, which stays", r.stderr)
        r = self.cli("offboard", plan.name)
        self.assertEqual(r.returncode, 2)
        self.assertIn(f"{second.name}: a read run of it is waiting in the inbox", r.stderr)
        self.assertTrue(first.is_dir() and plan.is_dir())
        self.assertEqual(self.cli("handover", "second", "--withdraw").returncode, 0)
        r = self.cli("offboard", plan.name, "--json")
        self.assertEqual(json.loads(r.stdout)["offboarded"], everything)
        self.assertEqual(json.loads(self.cli("ls", "--json").stdout), [])
        self.assertFalse(plan.parent.exists(), "an emptied month folder goes too")
        self.assertEqual(self.cli("restore", plan.name).stdout, f"restored: {second.name}, {first.name}, {plan.name}\n")
        self.assertEqual(self.cli("run", plan.name).returncode, 0)

    def test_an_apply_needs_an_ok_dry_run_of_the_same_scripts_and_arguments(self) -> None:
        body = "import sys\n\nimport opsquire_helpers as sq\n\nsq.target('web1', 'ok', ' '.join(sys.argv[1:]))\n"
        folder = self.new("guarded", body, "--changes-prod", "--rollback", "none", "--no-verify")
        tid = folder.name
        self.assertEqual(self.cli("lint", tid).returncode, 0)

        def refused(*args: str) -> str:
            r = self.cli("run", tid, "--apply", *args)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            rec = self.last(tid)
            self.assertEqual(
                (rec["status"], rec["mode"], rec["targets"]["web1"]["status"]), ("blocked", "apply", "skipped")
            )
            return str(rec["last_error"])

        never = refused()
        self.assertIn("it never had a dry run", never)
        self.assertIn(f"`opsquire run {tid}`", never)
        self.assertIn("--skip-dry", never)
        self.assertEqual(self.cli("handover", tid, "--apply").returncode, 2, "nor is it handed over")
        state = json.loads(self.cli("threads", "--json").stdout)
        self.assertEqual(
            [(i["task_id"], i["next"]) for i in state["inbox"]], [(tid, "dry")], "a refused run ran nothing"
        )

        self.assertEqual(self.cli("run", tid, "--", "one").returncode, 0)
        dry = self.last(tid)
        self.assertIn("had other arguments", refused())
        self.assertIn(f"`opsquire run {tid} -- two`", refused("--", "two"))
        item = json.loads(self.cli("threads", "--json").stdout)["inbox"][0]
        self.assertEqual((item["next"], item["command"]), ("apply", f"opsquire run {tid} --apply -- one"))
        self.assertEqual(self.cli("run", tid, "--apply", "--", "one").returncode, 0)
        self.assertEqual(self.last(tid)["dry"], {"run_id": dry["run_id"]})

        (folder / "main.py").write_text(body + "sq.step('new')\n")
        changed = refused("--", "one")
        self.assertIn("the scripts changed since its last dry run", changed)
        self.assertIn(dry["run_id"], changed)

        self.assertEqual(self.cli("run", tid, "--skip-dry", "why").returncode, 2, "only with --apply")
        self.assertEqual(self.cli("run", tid, "--apply", "--skip-dry", " ").returncode, 2, "the reason is required")
        r = self.cli("run", tid, "--apply", "--skip-dry", "hotfix, the host is down", "--", "one")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rec = self.last(tid)
        self.assertEqual((rec["status"], rec["dry"]), ("ok", {"skipped": "hotfix, the host is down"}))
        warned = [e["msg"] for e in self.events(rec) if e.get("level") == "warn"]
        self.assertTrue(any("applying without a dry run" in m and "hotfix" in m for m in warned), warned)

    def test_a_dry_run_that_failed_does_not_cover_an_apply(self) -> None:
        body = "import opsquire_helpers as sq\n\nsq.target('web1', 'failed', 'cannot reach it')\n"
        tid = self.new("shaky", body, "--changes-prod", "--rollback", "none", "--no-verify").name
        self.assertEqual(self.cli("run", tid).returncode, 1)
        dry = self.last(tid)
        r = self.cli("run", tid, "--apply")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(f"its dry run {dry['run_id']} of these scripts ended failed", self.last(tid)["last_error"])

    def test_a_plan_is_refused_whole_when_a_step_has_no_dry_run(self) -> None:
        marker = self.tmp / "applied"
        body = (
            "from pathlib import Path\n\nimport opsquire_helpers as sq\n\n"
            f"if sq.APPLY:\n    Path({str(marker)!r}).touch()\n"
        )
        first = self.new("first", body, "--changes-prod", "--rollback", "none", "--no-verify")
        second = self.new("second", "print(1)\n", "--changes-prod", "--rollback", "none", "--no-verify")
        plan = self.make_plan(first, second, name="both")
        self.assertEqual(self.cli("run", first.name).returncode, 0)
        r = self.cli("run", plan.name, "--apply")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        rec = self.plan_rec(plan)
        self.assertEqual(rec["status"], "blocked")
        self.assertIn(f"no dry run covers the apply of step {second.name}: it never had a dry run", rec["last_error"])
        self.assertFalse(marker.exists(), "the covered first step did not run either")
        self.assertEqual(self.cli("run", plan.name).returncode, 0)
        self.assertEqual(self.cli("run", plan.name, "--apply").returncode, 0)
        self.assertTrue(marker.exists())

    def test_last_error_names_the_cause_not_where_to_look(self) -> None:
        body = (
            "import traceback\n\nimport opsquire_helpers as sq\n\n"
            "try:\n    raise RuntimeError('no finished build of abc123')\n"
            "except RuntimeError:\n    sq.error(traceback.format_exc())\n"
            "sq.target('web1', 'failed', 'deploy failed, see the error')\n"
        )
        tid = self.new("cause", body).name
        self.assertEqual(self.cli("run", tid).returncode, 1)
        self.assertEqual(
            self.last(tid)["last_error"], "deploy failed, see the error: RuntimeError: no finished build of abc123"
        )
        two = (
            "import opsquire_helpers as sq\n\nsq.error('web1 answered slowly')\nsq.target('web1', 'ok', 'fine')\n"
            "sq.target('web2', 'failed', 'refused the config')\n"
        )
        tid = self.new("unrelated", two, "--target", "web1", "--target", "web2").name
        self.assertEqual(self.cli("run", tid).returncode, 1)
        self.assertEqual(self.last(tid)["last_error"], "refused the config", "an error before an ok target is not it")

    def inbox_ids(self) -> list[str]:
        code = "import json\nfrom opsquire.state import state\nprint(json.dumps(state()['inbox']))"
        r2 = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        items: list[dict[str, Any]] = json.loads(r2.stdout)
        return [str(it["task_id"]) for it in items]

    def test_a_waiting_task_can_be_superseded_and_reopened(self) -> None:
        old = self.new("old", "print('old')\n")
        newer = self.new("newer", "print('newer')\n")
        for name in ("old", "newer"):
            self.assertEqual(self.cli("lint", name).returncode, 0)
        self.assertIn(old.name, self.inbox_ids())
        self.assertNotEqual(self.cli("supersede", "old", "--by", "old").returncode, 0)
        r = self.cli("supersede", "old", "--by", "newer", "--note", "newer reads both hosts")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(old.name, self.inbox_ids())
        self.assertIn(newer.name, self.inbox_ids())
        r = self.cli("run", "old")
        self.assertEqual(r.returncode, 2)
        self.assertIn(f"{old.name} was superseded by {newer.name} (test, ", r.stderr)
        self.assertIn("newer reads both hosts", r.stderr)
        self.assertEqual(self.cli("runs", "old", "--json").stdout.strip(), "[]")
        self.assertNotEqual(self.cli("reopen", "old", "--note", " ").returncode, 0)
        r = self.cli("reopen", "old", "--note", "the newer one cannot reach web2")
        self.assertEqual(r.returncode, 0, r.stderr)
        meta = json.loads((old / "meta.json").read_text())
        self.assertNotIn("superseded_by", meta)
        self.assertEqual(meta["reopened"]["was"]["superseded_by"], newer.name)
        self.assertEqual(self.cli("run", "old").returncode, 0)

    def test_a_task_that_never_ran_expires_and_one_that_failed_waits_for_a_person(self) -> None:
        unrun = self.new("unrun", "print(1)\n")
        failed = self.new("failed", "raise SystemExit(1)\n", "--changes-prod", "--rollback", "none", "--no-verify")
        recent = self.new("recent", "print(1)\n")
        for name in ("unrun", "failed", "recent"):
            self.assertEqual(self.cli("lint", name).returncode, 0)
        self.assertEqual(self.cli("run", "failed", "--apply", *self.NO_DRY, answers=[("[y/N]", "y")]).returncode, 1)
        self.backdate(unrun, 15)
        self.backdate(failed, 15)
        self.backdate(recent, 4)
        (self.home / "var/offboard.json").unlink()
        r = self.cli("lint", "recent")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"abandoned 1 tasks that never ran in 14 days: {unrun.name}", r.stderr)
        self.assertIn(f"(oldest {recent.name}, 4 days)", r.stderr)
        outcome = json.loads((unrun / "meta.json").read_text())["outcome"]
        self.assertEqual((outcome["state"], outcome["closed_by"]), ("abandoned", "opsquire (expiry)"))
        self.assertIsNone(json.loads((failed / "meta.json").read_text()).get("outcome"))
        self.assertIsNone(json.loads((recent / "meta.json").read_text()).get("outcome"))
        self.assertNotIn("need a decision", self.cli("lint", "recent").stderr, "once a day, not on every command")

    def test_the_author_hears_what_became_of_its_tasks(self) -> None:
        self.env["OPSQUIRE_AUTHOR"] = "agent-x (chat)"
        waiting = self.new("waiting", "print(1)\n")
        self.assertEqual(self.cli("lint", "waiting").returncode, 0)
        self.env["OPSQUIRE_AUTHOR"] = "operator-y"
        r = self.cli("close", "waiting", "--outcome", "abandoned", "--note", "done by hand")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.cli("new", "web1", "unrelated", "--purpose", "by someone else")
        self.assertNotIn("was abandoned", r.stderr, "only the author hears it")
        self.env["OPSQUIRE_AUTHOR"] = "agent-x (chat)"
        r = self.cli("new", "web1", "next", "--purpose", "the next one")
        self.assertIn(f"{waiting.name} was abandoned (operator-y, ", r.stderr)
        self.assertIn("done by hand", r.stderr)
        r = self.cli("wait", "waiting", "--timeout", "30")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("no run is coming", r.stderr)

    def task_view(self, task_id: str) -> dict[str, Any]:
        code = f"import json\nfrom opsquire.state import state\nprint(json.dumps(state()['tasks'][{task_id!r}]))"
        r = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        view: dict[str, Any] = json.loads(r.stdout)
        return view

    def test_a_changing_task_should_ask_once_and_the_record_says_when_it_asked_twice(self) -> None:
        head = "import opsquire_helpers as sq\n\n"
        twice = self.new(
            "twice",
            head + "if sq.APPLY:\n    sq.confirm('web1?')\n    sq.confirm('web2?')\n",
            "--changes-prod",
            "--rollback",
            "none",
            "--no-verify",
        )
        silent = self.new(
            "silent", head + "sq.step('changing')\n", "--changes-prod", "--rollback", "none", "--no-verify"
        )
        self.new("reader", head + "sq.step('looking')\n")
        r = self.cli("lint", "silent")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("never ask before changing production", r.stdout)
        for name in ("twice", "reader"):
            r = self.cli("lint", name)
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertNotIn("never ask", r.stdout)
        self.assertTrue(self.task_view(twice.name)["asks_first"])
        self.assertFalse(self.task_view(silent.name)["asks_first"])
        r = self.cli("run", "twice", "--apply", *self.NO_DRY, answers=[("web1? [y/N]", "y"), ("web2? [y/N]", "y")])
        self.assertEqual(r.returncode, 0, r.stdout)
        warns = [e["msg"] for e in self.events(self.last("twice")) if e["event"] == "warn"]
        self.assertEqual(len([w for w in warns if "second yes in one run" in w]), 1)

    def inbox_item(self, task_id: str) -> dict[str, Any] | None:
        code = "import json\nfrom opsquire.state import state\nprint(json.dumps(state()['inbox']))"
        r = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        items: list[dict[str, Any]] = json.loads(r.stdout)
        return next((it for it in items if it["task_id"] == task_id), None)

    def test_a_staged_task_walks_its_stages_from_the_inbox_and_checks_only_after_the_last(self) -> None:
        head = "import sys\n\nimport opsquire_helpers as sq\n\n"
        folder = self.new(
            "staged",
            head + "stage = sys.argv[1] if len(sys.argv) > 1 else 'build'\nsq.target('web1', 'ok', stage)\n",
            "--changes-prod",
            "--rollback",
            "none",
        )
        (folder / "verify.py").write_text("import opsquire_helpers as sq\n\nsq.target('web1', 'ok', 'live')\n")
        meta = json.loads((folder / "meta.json").read_text())
        meta["stages"] = ["build", "build"]
        (folder / "meta.json").write_text(json.dumps(meta))
        self.assertNotEqual(self.cli("lint", "staged").returncode, 0, "stages must be distinct")
        meta["stages"] = ["build", "deploy"]
        (folder / "meta.json").write_text(json.dumps(meta))
        r = self.cli("lint", "staged")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        def offered() -> tuple[str, str]:
            item = self.inbox_item(folder.name)
            assert item is not None
            return str(item["next"]), str(item["args"])

        self.assertEqual(offered(), ("apply", ""), "one item runs every stage")
        r = self.cli("run", "staged", "--apply", "--", "build")
        self.assertEqual(r.returncode, 2, "one stage alone still needs its own dry run")
        self.assertEqual(self.cli("run", "staged", "--", "build").returncode, 0)
        r = self.cli("run", "staged", "--apply", "--", "build")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("stage build applied; `opsquire run", r.stdout)
        self.assertIsNone(json.loads((folder / "meta.json").read_text()).get("outcome"))
        self.assertEqual(offered(), ("apply", ""))
        r = self.cli("run", "staged", "--apply")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("stage 2/2 deploy: dry run", r.stdout)
        self.assertNotIn("stage 1/2", r.stdout, "it carries on from the stage left")
        modes = [(x["mode"], x["argv"]) for x in json.loads(self.cli("runs", "staged", "--json").stdout)]
        self.assertEqual(
            modes[-5:],
            [("dry", ["build"]), ("apply", ["build"]), ("dry", ["deploy"]), ("apply", ["deploy"]), ("verify", [])],
        )
        self.assertEqual(json.loads((folder / "meta.json").read_text())["outcome"]["state"], "done")
        self.assertIsNone(self.inbox_item(folder.name))

    def test_one_apply_walks_every_stage_and_stops_at_the_first_failure(self) -> None:
        body = (
            "import sys\n\nimport opsquire_helpers as sq\n\nstage = sys.argv[1]\n"
            "if sq.APPLY and stage == 'deploy':\n    raise SystemExit(1)\nsq.target('web1', 'ok', stage)\n"
        )
        folder = self.new(
            "piped", body, "--changes-prod", "--rollback", "none", "--stage", "build", "--stage", "deploy"
        )
        (folder / "verify.py").write_text("import opsquire_helpers as sq\n\nsq.target('web1', 'ok', 'live')\n")
        self.assertEqual(self.cli("lint", "piped").returncode, 0)
        r = self.cli("handover", "piped", "--apply", "--why", "roll it out")
        self.assertEqual(r.returncode, 0, "a staged apply makes its own dry runs: " + r.stderr)
        r = self.cli("run", "piped", "--apply")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("stopped at stage deploy (apply); later stages did not run", r.stdout)
        modes = [(x["mode"], x["argv"]) for x in json.loads(self.cli("runs", "piped", "--json").stdout)]
        self.assertEqual(modes, [("dry", ["build"]), ("apply", ["build"]), ("dry", ["deploy"]), ("apply", ["deploy"])])
        self.assertIsNone(json.loads((folder / "meta.json").read_text()).get("outcome"))

    def test_the_homes_contract_follows_local_rules_and_keeps_a_hand_edit(self) -> None:
        agents, local = self.home / "AGENTS.md", self.home / "LOCAL.md"
        local.write_text("### Rule: first\n")
        self.assertEqual(self.cli("contract", "--write").returncode, 0)
        self.assertIn("### Rule: first", agents.read_text())
        self.assertTrue(agents.read_text().rstrip().endswith("-->"))
        self.new("one", "print(1)\n")
        local.write_text("### Rule: first\n\n### Rule: second\n")
        r = self.cli("lint", "one")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("AGENTS.md rewritten", r.stderr)
        self.assertIn("### Rule: second", agents.read_text())
        self.assertNotIn("AGENTS.md", self.cli("lint", "one").stderr, "quiet when current")

        agents.write_text(agents.read_text().replace("### Rule: second", "### Rule: second\n\nonly here"))
        local.write_text("### Rule: first\n\n### Rule: third\n")
        r = self.cli("lint", "one")
        self.assertIn("edited by hand since opsquire wrote it: move the edit into LOCAL.md", r.stderr)
        self.assertIn("only here", agents.read_text())
        r = self.cli("contract", "--write")
        self.assertEqual(r.returncode, 2)
        self.assertIn("only here", agents.read_text())
        self.assertEqual(self.cli("contract", "--write", "--force").returncode, 0)
        self.assertNotIn("only here", agents.read_text())
        self.assertIn("### Rule: third", agents.read_text())

    def test_the_scaffold_lints_runs_and_asks_once_as_it_comes(self) -> None:
        r = self.cli("new", "web1", "look", "--purpose", "look around", "--target", "web1", "--target", "web2")
        read = Path(r.stdout.strip())
        r = self.cli("new", "web1", "change", "--purpose", "change it", "--changes-prod", "--rollback", "none")
        change = Path(r.stdout.strip())
        self.assertEqual(sorted(p.name for p in read.iterdir()), ["design.md", "main.py", "meta.json"])
        self.assertTrue((change / "test_main.py").is_file())
        self.assertIn("## Rollback", (change / "design.md").read_text())
        self.assertIn("change it", (change / "design.md").read_text())
        for folder in (read, change):
            r = self.cli("lint", folder.name)
            self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("(1 tests)", self.cli("lint", change.name).stdout)
        self.assertEqual(self.cli("run", read.name).returncode, 0)
        kinds = {e["event"] for e in self.events(self.last(read.name))}
        self.assertTrue({"data", "progress", "target", "result"} <= kinds, kinds)
        self.assertEqual(self.cli("run", change.name).returncode, 0)
        targets = self.last(change.name)["targets"]
        self.assertEqual(targets["web1"]["status"], "unchanged", "the starter plan changes nothing")
        r = self.cli("new", "web1", "bad", "--purpose", "x", "--changes-prod", "--rollback", "none", "--stage", "a")
        self.assertEqual(r.returncode, 2)
        r = self.cli(
            "new", "web1", "two", "--purpose", "x", "--changes-prod", "--rollback", "n", "--stage", "a", "--stage", "b"
        )
        self.assertEqual(json.loads((Path(r.stdout.strip()) / "meta.json").read_text())["stages"], ["a", "b"])

    def test_an_exception_in_a_script_is_the_runs_last_error(self) -> None:
        self.new(
            "boom",
            "import opsquire_helpers as sq\n\n\ndef main() -> None:\n    raise RuntimeError('disk full on web1')\n\n\n"
            "sq.run(main, cleanup=lambda: sq.step('cleaned up'))\n",
        )
        r = self.cli("run", "boom")
        self.assertEqual(r.returncode, 1, r.stdout)
        rec = self.last("boom")
        self.assertIn("RuntimeError: disk full on web1", rec["last_error"])
        self.assertIn("cleaned up", [e["msg"] for e in self.events(rec)])

    def test_a_read_that_failed_comes_back_to_the_inbox_once_it_is_fixed(self) -> None:
        folder = self.new("probe", "raise SystemExit(1)\n")
        self.assertEqual(self.cli("lint", "probe").returncode, 0)
        self.assertEqual(self.cli("run", "probe").returncode, 1)
        self.assertIsNone(self.inbox_item(folder.name), "not again with the same scripts")
        (folder / "main.py").write_text("print('fixed')\n")
        self.assertEqual(self.cli("lint", "probe").returncode, 0)
        item = self.inbox_item(folder.name)
        self.assertEqual(item and item["next"], "read")
        self.assertEqual(self.cli("run", "probe").returncode, 0)
        self.assertIsNone(self.inbox_item(folder.name), "a read that ended ok is done")

    def test_a_run_stamped_in_the_future_by_a_clock_jump_does_not_count_as_the_latest(self) -> None:
        folder = self.new("move", "print(1)\n", "--changes-prod", "--rollback", "none", "--no-verify")
        self.assertEqual(self.cli("lint", "move").returncode, 0)
        self.assertEqual(self.cli("run", "move").returncode, 0)
        first = self.last("move")
        jumped = Path(first["paths"]["dir"]).with_name("20991231T000000000Z_dry")
        shutil.copytree(first["paths"]["dir"], jumped)
        rec = json.loads((jumped / "run.json").read_text())
        rec.update(
            run_id=jumped.name, status="failed", started="2099-12-31T00:00:00.000Z", ended="2000-01-01T00:00:00.000Z"
        )
        (jumped / "run.json").write_text(json.dumps(rec))
        self.assertEqual(self.cli("run", "move").returncode, 0)
        recs = json.loads(self.cli("runs", "move", "--json").stdout)
        self.assertEqual(recs[0]["run_id"], jumped.name, "ordered by when it really ran")
        self.assertIn("system clock moved", recs[0]["clock_note"])
        item = self.inbox_item(folder.name)
        self.assertEqual(item and item["next"], "apply")

    def test_a_held_task_and_its_plans_steps_stay_out_of_the_inbox_until_handed_over(self) -> None:
        one = self.new("one", "print(1)\n", *self.CHANGING)
        two = self.new("two", "print(2)\n", *self.CHANGING)
        for name in ("one", "two"):
            self.assertEqual(self.cli("lint", name).returncode, 0)
        plan = self.make_plan(one, two, name="golive")
        self.assertEqual(self.cli("lint", plan.name).returncode, 0)
        self.assertIsNotNone(self.inbox_item(plan.name))
        self.assertNotEqual(self.cli("hold", plan.name).returncode, 0, "a hold says why")
        r = self.cli("hold", plan.name, "--why", "go live at 15:00")
        self.assertEqual(r.returncode, 0, r.stderr)
        for t in (plan, one, two):
            self.assertIsNone(self.inbox_item(t.name), t.name)
        self.assertEqual(self.cli("run", plan.name).returncode, 0, "a hold keeps it out of the inbox, not unrunnable")
        self.assertEqual(json.loads(self.cli("show", plan.name, "--json").stdout)["status"], "ready")
        r = self.cli("handover", plan.name, "--apply", "--why", "15:00")
        self.assertEqual(r.returncode, 0, r.stderr)
        item = self.inbox_item(plan.name)
        self.assertEqual(item and item["next"], "apply", "the dry run still covers it: a hold is not a change")
        self.assertEqual(self.cli("hold", one.name, "--why", "later", "--until", "2000-01-01T00:00:00Z").returncode, 0)
        self.assertEqual(self.cli("hold", two.name, "--why", "later").returncode, 0)
        self.assertEqual(self.cli("hold", two.name, "--release").returncode, 0)
        self.assertNotIn("hold", json.loads((two / "meta.json").read_text()))

    def test_new_can_create_a_task_held(self) -> None:
        r = self.cli("new", "web1", "later", "--purpose", "x", "--hold", "after the merge")
        folder = Path(r.stdout.strip())
        self.assertEqual(self.cli("lint", folder.name).returncode, 0)
        self.assertIsNone(self.inbox_item(folder.name))
        self.assertEqual(json.loads((folder / "meta.json").read_text())["hold"]["why"], "after the merge")

    def test_an_agent_reads_the_tasks_last_run_and_its_own_results_before_more_work(self) -> None:
        agent, operator = "claude-code 6d4a8e7e", "operator-z"
        self.env["OPSQUIRE_AUTHOR"] = agent
        folder = self.new("probe", "print('found 3 stale locks')\n")
        self.assertEqual(self.cli("lint", "probe").returncode, 0)
        self.assertEqual(self.cli("handover", "probe", "--why", "first look").returncode, 0, "nothing ran yet")
        self.env["OPSQUIRE_AUTHOR"] = operator
        self.assertEqual(self.cli("run", "probe").returncode, 0)
        self.assertEqual(self.last("probe")["handed_by"], agent)
        self.env["OPSQUIRE_AUTHOR"] = agent
        r = self.cli("new", "web1", "next", "--purpose", "more")
        self.assertEqual(r.returncode, 2)
        self.assertIn("runs you handed over finished and you have not read them", r.stderr)
        self.assertEqual(self.cli("unread").returncode, 1)
        hook = subprocess.run(
            [*CLI, "unread", "--hook"],
            env=self.env,
            input=json.dumps({"session_id": "6d4a8e7e-1111-2222", "stop_hook_active": False}),
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(hook.stdout)["decision"], "block", hook.stderr)
        again = subprocess.run(
            [*CLI, "unread", "--hook"],
            env=self.env,
            input=json.dumps({"session_id": "6d4a8e7e-1111-2222", "stop_hook_active": True}),
            capture_output=True,
            text=True,
        )
        self.assertEqual(again.stdout, "", "once per stop attempt, never a loop")
        self.assertEqual(self.cli("log", "probe").returncode, 0)
        self.assertEqual(self.cli("unread").returncode, 0)
        self.assertEqual(self.cli("new", "web1", "next", "--purpose", "more").returncode, 0)

        self.env["OPSQUIRE_AUTHOR"] = operator
        self.assertEqual(self.cli("run", "probe").returncode, 0)
        self.env["OPSQUIRE_AUTHOR"] = agent
        r = self.cli("handover", "probe", "--why", "again")
        self.assertEqual(r.returncode, 2)
        self.assertIn("read the task's latest run first", r.stderr)
        self.assertEqual(self.cli("runs", "probe", "--json").returncode, 0)
        self.assertEqual(self.cli("handover", folder.name, "--why", "again").returncode, 0)

    def test_an_agent_that_cannot_write_var_still_records_its_reads(self) -> None:
        """Agent sandboxes deny writes under var/; reading a run must still count, and a read that cannot be recorded
        says so instead of leaving the agent refused for no visible reason."""
        if os.geteuid() == 0:
            self.skipTest("root writes through read-only modes")
        agent, operator = "claude-code 2d39fd02", "operator-z"
        self.env["OPSQUIRE_AUTHOR"] = agent
        self.new("probe", "print('found 3 stale locks')\n")
        self.assertEqual(self.cli("lint", "probe").returncode, 0)
        self.env["OPSQUIRE_AUTHOR"] = operator
        self.assertEqual(self.cli("run", "probe").returncode, 0)
        self.env["OPSQUIRE_AUTHOR"] = agent
        var_dirs = [self.home / "var", *(p for p in (self.home / "var").rglob("*") if p.is_dir())]
        try:
            for d in var_dirs:
                d.chmod(0o555)
            r = self.cli("runs", "probe", "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(self.cli("log", self.last("probe")["run_id"]).returncode, 0)
            r = self.cli("handover", "probe", "--why", "again")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse((self.home / "var/reads").exists())

            other = "claude-code 7c1e9a40"
            self.env["OPSQUIRE_AUTHOR"] = other
            ledgers = self.home / ".reads"
            ledgers.chmod(0o555)
            try:
                r = self.cli("log", "probe")
            finally:
                ledgers.chmod(0o755)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(f"could not record that {other} read", r.stderr)
        finally:
            for d in var_dirs:
                d.chmod(0o755)

    def test_integrate_installs_the_stop_hook_once_and_keeps_other_settings(self) -> None:
        claude = self.tmp / "claude"
        claude.mkdir()
        (claude / "settings.json").write_text(json.dumps({"model": "x", "hooks": {"Stop": []}}))
        self.env["CLAUDE_CONFIG_DIR"] = str(claude)
        self.env["HOME"] = str(self.tmp)
        for _ in range(2):
            r = self.cli("integrate")
            self.assertEqual(r.returncode, 0, r.stderr)
        settings = json.loads((claude / "settings.json").read_text())
        self.assertEqual(settings["model"], "x")
        commands = [h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]]
        self.assertEqual(len(commands), 1)
        self.assertTrue(commands[0].endswith("unread --hook"))

    def test_doctor_sees_a_running_console_through_its_health_file(self) -> None:
        self.env["OPSQUIRE_AUTHOR"] = "claude-code 1234abcd"
        r = self.cli("doctor")
        self.assertEqual(r.returncode, 1)
        self.assertIn("console: not running", r.stdout)
        port = str(free_port())
        ui = subprocess.Popen(
            [*CLI, "ui", "--port", port], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        try:
            for _ in range(60):
                with contextlib.suppress(OSError):
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5).read()
                    if (self.home / f"var/ui/{port}.json").is_file():
                        break
                time.sleep(0.25)
            out = self.cli("doctor").stdout
            self.assertIn(f"console: up at http://127.0.0.1:{port}/", out)
            self.assertIn("last state build", out)
        finally:
            ui.send_signal(signal.SIGINT)
            ui.wait(20)
        self.assertFalse((self.home / f"var/ui/{port}.json").exists(), "a console that stops takes its file with it")

    def test_the_console_sends_only_what_changed_since_the_pages_version(self) -> None:
        for name in ("one", "two"):
            self.new(name, "print(1)\n")
            self.assertEqual(self.cli("lint", name).returncode, 0)
        port = str(free_port())
        ui = subprocess.Popen(
            [*CLI, "ui", "--port", port], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        def get(since: str = "") -> tuple[dict[str, Any], str]:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/state" + (f"?since={since}" if since else ""),
                headers={"Accept-Encoding": "gzip"},
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                raw, enc = r.read(), r.headers.get("Content-Encoding") or ""
            body: dict[str, Any] = json.loads(gzip.decompress(raw) if enc == "gzip" else raw)
            return body, enc

        try:
            for _ in range(80):
                with contextlib.suppress(OSError):
                    full, enc = get()
                    break
                time.sleep(0.25)
            self.assertEqual(full["kind"], "full")
            self.assertEqual(enc, "gzip")
            self.assertEqual(len(full["tasks"]), 2)
            self.assertEqual(get(full["version"])[0]["kind"], "same")
            self.assertEqual(self.cli("run", "one").returncode, 0)
            for _ in range(40):
                patch, _ = get(full["version"])
                if patch["kind"] != "same":
                    break
                time.sleep(0.25)
            self.assertEqual(patch["kind"], "patch")
            self.assertEqual(list(patch["changed"]["tasks"]), [next(t for t in full["tasks"] if "one" in t)])
            self.assertEqual(len(patch["changed"]["runs"]), 1)
            self.assertEqual(get("not-a-version")[0]["kind"], "full")
        finally:
            ui.send_signal(signal.SIGINT)
            ui.wait(20)

    def test_a_console_run_that_does_not_start_leaves_its_reason_on_the_task(self) -> None:
        import http.client

        folder = self.new("broken", "print(1)\n", "--changes-prod", "--rollback", "none", "--no-verify")
        self.assertEqual(self.cli("lint", "broken").returncode, 0)
        (folder / "main.py").write_text("def f(x):\n    return x\n")
        port = free_port()
        proc = subprocess.Popen([*CLI, "ui", "--port", str(port)], env=self.env, stdout=subprocess.PIPE, text=True)
        try:
            self.assertIn("opsquire ui", proc.stdout.readline() if proc.stdout else "")
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)

            def call(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
                conn.request(method, path, json.dumps(body) if body else None, {"Content-Type": "application/json"})
                resp = conn.getresponse()
                return resp.status, json.loads(resp.read())

            def problems() -> dict[str, Any]:
                deadline = time.time() + 30
                while time.time() < deadline:
                    found: dict[str, Any] = call("GET", "/api/state")[1]["start_problems"]
                    if found.get(folder.name):
                        return found
                    time.sleep(0.2)
                return {}

            status, started = call("POST", "/api/run", {"task": folder.name, "mode": "dry"})
            self.assertEqual(status, 200, started)
            why = problems().get(folder.name) or {}
            self.assertEqual(why.get("mode"), "dry")
            self.assertIn("lint", why.get("why", ""))
            status, refused = call("POST", "/api/run", {"task": folder.name, "mode": "verify"})
            self.assertEqual(status, 400)
            self.assertIn("not verify", call("GET", "/api/state")[1]["start_problems"][folder.name]["why"])
            (folder / "main.py").write_text("print(1)\n")
            status, started = call("POST", "/api/run", {"task": folder.name, "mode": "dry"})
            self.assertEqual(status, 200)
            self.assertNotIn(folder.name, call("GET", "/api/state")[1]["start_problems"])
        finally:
            proc.send_signal(signal.SIGINT)
            proc.wait(20)

    def test_an_ask_from_the_console_is_a_chat_with_its_interview_and_its_runs(self) -> None:
        import http.client

        self.agents_file()
        self.env |= {"FAKE_GRILL": "1", "FAKE_STATE": str(self.tmp / "fake-state.json")}
        port = free_port()
        proc = subprocess.Popen([*CLI, "ui", "--port", str(port)], env=self.env, stdout=subprocess.PIPE, text=True)
        try:
            self.assertIn("opsquire ui", proc.stdout.readline() if proc.stdout else "")
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)

            def call(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
                conn.request(method, path, json.dumps(body) if body else None, {"Content-Type": "application/json"})
                resp = conn.getresponse()
                return resp.status, json.loads(resp.read())

            def job_until(job: str, done: Any) -> dict[str, Any]:
                deadline = time.time() + 60
                while time.time() < deadline:
                    j: dict[str, Any] = call("GET", f"/api/job?id={job}&offset=0")[1]
                    if done(j):
                        return j
                    time.sleep(0.25)
                self.fail(f"job {job}: {j}")

            self.assertEqual(call("POST", "/api/ask", {"request": " "})[0], 400)
            status, started = call("POST", "/api/ask", {"request": "restart web1 bots"})
            self.assertEqual(status, 200, started)
            waiting = job_until(started["job_id"], lambda j: j["status"] == "waiting")
            self.assertTrue(waiting["ask_id"])
            self.assertEqual(call("POST", "/api/answer", {"job": started["job_id"], "text": ""})[0], 200)
            done = job_until(started["job_id"], lambda j: j["status"] == "done")
            self.assertEqual(done["exit_code"], 0, done["text"])
            chat = call("GET", f"/api/askchat?id={waiting['ask_id']}&offset=0")[1]["items"]
            kinds = [c["kind"] for c in chat]
            self.assertEqual(kinds[0], "you")
            self.assertEqual(chat[0]["text"], "restart web1 bots")
            question = next(c for c in chat if c["kind"] == "question")
            self.assertEqual((question["text"], question["recommend"]), ("question 1?", "rec 1"))
            self.assertIn({"kind": "you", "text": "rec 1"}, [{"kind": c["kind"], "text": c["text"]} for c in chat])
            reply = next(c for c in chat if c["kind"] == "agent")
            self.assertEqual([a["kind"] for a in reply["actions"]], ["RUN", "RUN"])
            self.assertEqual(kinds[-1], "end")
            self.assertEqual(chat[-1]["text"], "ok")
            status, again = call("POST", "/api/ask", {"request": "and the web2 bots?", "resume": waiting["ask_id"]})
            self.assertEqual(status, 200, again)
            job_until(again["job_id"], lambda j: j["status"] in ("waiting", "done"))
            follow = next(a for a in call("GET", "/api/state")[1]["asks"] if a["prompt"] == "and the web2 bots?")
            self.assertNotEqual(follow["ask_id"], waiting["ask_id"])
            self.assertEqual(follow["conversation"], waiting["ask_id"])
            whole = call("GET", f"/api/askchat?id={follow['ask_id']}&offset=0")[1]["items"]
            self.assertEqual(whole[0]["text"], "restart web1 bots")
            self.assertIn("and the web2 bots?", [c["text"] for c in whole if c["kind"] == "you"])
            self.assertEqual(call("GET", "/api/askchat?id=../x&offset=0")[0], 400)
        finally:
            proc.send_signal(signal.SIGINT)
            proc.wait(20)

    def test_an_ask_runs_on_the_chosen_model_and_says_how_it_was_billed(self) -> None:
        tiers = {t: {"id": f"model-{t}", "label": t.title()} for t in ("quick", "default", "deep")}
        self.agents_file(model_argv=["--model", "{model}"], models=tiers, billing={"key_env": "FAKE_PROVIDER_KEY"})
        log = self.tmp / "fake.log"
        self.env["FAKE_LOG"] = str(log)
        self.env.pop("FAKE_PROVIDER_KEY", None)
        self.assertEqual(self.cli("ask", "--model", "quick", "restart", "web1").returncode, 0)
        self.assertEqual(self.cli("ask", "restart", "web1", "again").returncode, 0)
        self.env["FAKE_PROVIDER_KEY"] = "test-key-not-real"
        self.assertEqual(self.cli("ask", "--model", "some-model-id", "one", "more").returncode, 0)
        argvs = [json.loads(line)["argv"] for line in log.read_text().splitlines()]
        models = [a[a.index("--model") + 1] for a in argvs]
        self.assertEqual(models, ["model-quick", "model-default", "some-model-id"])
        recs = sorted(
            (json.loads(p.read_text()) for p in (self.home / "var/asks").glob("*_fake.json")),
            key=lambda r: r["started"],
        )
        self.assertEqual(
            [(r["model"], r["billing"]) for r in recs],
            [("model-quick", "plan"), ("model-default", "plan"), ("some-model-id", "api-key")],
        )
        chat = (self.home / "var/asks" / f"{recs[0]['ask_id']}.chat.jsonl").read_text().splitlines()
        reply = next(json.loads(c) for c in chat if json.loads(c)["kind"] == "agent")
        self.assertEqual((reply["model"], reply["billing"], reply["agent"]), ("model-quick", "plan", "fake"))

    def test_an_agent_without_tiers_or_billing_runs_on_its_own_default(self) -> None:
        self.agents_file()
        log = self.tmp / "fake.log"
        self.env["FAKE_LOG"] = str(log)
        self.assertEqual(self.cli("ask", "--model", "deep", "restart", "web1").returncode, 0)
        argv = json.loads(log.read_text().splitlines()[-1])["argv"]
        self.assertNotIn("--model", argv)
        rec = json.loads(next((self.home / "var/asks").glob("*_fake.json")).read_text())
        self.assertEqual((rec["model"], rec["billing"], rec["agent"]), ("", "", "fake"))


class Bundle(unittest.TestCase):
    def test_payload_imports_its_modules_through_stdin(self) -> None:
        from opsquire.model import HELPERS_DIR

        sys.path.insert(0, str(HELPERS_DIR))
        try:
            import opsquire_helpers as sq
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory() as d:
            lib, payload = Path(d) / "shared_lib.py", Path(d) / "payload.py"
            lib.write_text("from __future__ import annotations\n\n\ndef twice(x: int) -> int:\n    return 2 * x\n")
            payload.write_text(
                "import sys\n\nimport shared_lib\n\nprint(shared_lib.twice(int(sys.argv[1])), __name__)\n"
                "raise ValueError('boom')\n"
            )
            r = subprocess.run(
                [sys.executable, "-I", "-u", "-", "21"],
                input=sq.bundle(payload, lib),
                capture_output=True,
                text=True,
                cwd=d,
                check=False,
            )
        self.assertEqual(r.stdout, "42 __main__\n")
        self.assertIn('File "payload.py", line 6, in <module>', r.stderr)
        self.assertIn("raise ValueError('boom')", r.stderr)


class ReleaseScripts(unittest.TestCase):
    """scripts/release.py and scripts/signed_commit.py: what the release workflow and every push to main rely on."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR")))
        self.env = {**os.environ}

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def load(name: str) -> Any:
        import importlib.util

        spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_release_bumps_and_rewrites_the_version(self) -> None:
        release = self.load("release")
        self.assertEqual(release.bump("0.1.9", "patch"), "0.1.10")
        self.assertEqual(release.bump("0.1.9", "minor"), "0.2.0")
        self.assertEqual(release.bump("0.1.9", "major"), "1.0.0")
        text = 'name = "opsquire"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n'
        self.assertEqual(release.current_version(text), "0.1.0")
        self.assertEqual(release.set_version("0.2.0", text), text.replace("0.1.0", "0.2.0"))
        with self.assertRaises(release.ReleaseError):
            release.set_version("0.2", text)
        with self.assertRaises(release.ReleaseError):
            release.current_version("name = x\n")
        pyproject = (ROOT / "pyproject.toml").read_text()
        self.assertEqual(release.current_version(), pyproject.split('version = "')[1].split('"')[0])

    def test_release_notes_group_commits_by_area_and_skip_release_commits(self) -> None:
        release = self.load("release")
        commits = [
            ("release: v0.2.0", ""),
            ("ui: run any task from its pane", "Why it matters.\n\nSecond paragraph is left out."),
            ("close: record how a task ended", ""),
            ("Merge branch fix", ""),
            ("ui: simple mode first", "One line."),
        ]
        text = release.notes("v0.2.0", "v0.1.0", commits, "acme/opsquire")
        self.assertEqual(
            text,
            "### close\n- record how a task ended\n\n"
            "### other\n- Merge branch fix\n\n"
            "### ui\n- run any task from its pane\n  Why it matters.\n- simple mode first\n  One line.\n\n"
            "**Full changelog**: https://github.com/acme/opsquire/compare/v0.1.0...v0.2.0\n",
        )
        first = release.notes("v0.1.0", None, [("release: v0.1.0", "")], "acme/opsquire")
        self.assertIn("No changes since the previous release.", first)
        self.assertIn("https://github.com/acme/opsquire/commits/v0.1.0", first)

    def signed_checkout(self) -> tuple[Path, Path]:
        """A bare 'GitHub' repository with one commit on main, a clone of it, and a fake gh that commits into the bare
        repository the way createCommitOnBranch would."""
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.com\n[init]\n\tdefaultBranch = main\n")
        self.env |= {"GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1"}
        bare, clone = self.tmp / "github" / "acme" / "opsquire.git", self.tmp / "clone"
        bare.parent.mkdir(parents=True)
        self.git(bare.parent, "init", "-q", "--bare", "-b", "main", str(bare))
        seed = self.tmp / "seed"
        seed.mkdir()
        self.git(seed, "init", "-q", "-b", "main")
        (seed / "pyproject.toml").write_text('version = "0.1.0"\n')
        (seed / "old.txt").write_text("old\n")
        self.git(seed, "add", "-A")
        self.git(seed, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "base")
        self.git(seed, "push", "-q", str(bare), "main")
        self.git(self.tmp, "clone", "-q", f"file://{bare}", str(clone))
        self.git(clone, "remote", "set-url", "origin", "https://github.com/acme/opsquire.git")
        self.git(clone, "config", f"url.file://{bare}.insteadOf", "https://github.com/acme/opsquire.git")
        bin_dir = self.tmp / "gh-bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text(f'#!/bin/sh\nexec {sys.executable} {ROOT / "tests/fake_gh.py"} "$@"\n')
        (bin_dir / "gh").chmod(0o755)
        self.env |= {
            "PATH": f"{bin_dir}:{self.env['PATH']}",
            "FAKE_GH_STATE": str(self.tmp / "gh.json"),
            "FAKE_GH_LOG": str(self.tmp / "gh.log"),
            "FAKE_GH_REMOTE": str(bare),
        }
        return bare, clone

    def git(self, cwd: Path, *args: str) -> str:
        r = subprocess.run(["git", "-C", str(cwd), *args], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def signed_commit(self, clone: Path, *args: str) -> subprocess.CompletedProcess[str]:
        cmd = [sys.executable, str(ROOT / "scripts/signed_commit.py"), "--checkout", str(clone), *args]
        return subprocess.run(cmd, env=self.env, capture_output=True, text=True)

    def test_signed_commit_sends_the_staged_files_and_moves_the_checkout_onto_the_commit(self) -> None:
        bare, clone = self.signed_checkout()
        (clone / "pyproject.toml").write_text('version = "0.2.0"\n')
        (clone / "new.txt").write_bytes(b"new \xff bytes\n")
        (clone / "old.txt").unlink()
        (clone / "unstaged.txt").write_text("not yet\n")
        self.git(clone, "add", "pyproject.toml", "new.txt", "old.txt")
        r = self.signed_commit(clone, "-m", "release: v0.2.0", "-m", "Second paragraph.")
        self.assertEqual(r.returncode, 0, r.stderr)
        oid = r.stdout.strip()
        self.assertEqual(self.git(bare, "rev-parse", "main"), oid)
        message = self.git(bare, "log", "-1", "--format=%s%n%n%b", "main")
        self.assertEqual(message, "release: v0.2.0\n\nSecond paragraph.")
        self.assertEqual(self.git(bare, "show", "main:pyproject.toml"), 'version = "0.2.0"')
        self.assertEqual(self.git(bare, "ls-tree", "--name-only", "main").split(), ["new.txt", "pyproject.toml"])
        self.assertEqual(self.git(clone, "rev-parse", "HEAD"), oid)
        self.assertEqual(self.git(clone, "rev-parse", "origin/main"), oid)
        self.assertEqual(self.git(clone, "status", "--porcelain"), "?? unstaged.txt")
        self.assertEqual((clone / "new.txt").read_bytes(), b"new \xff bytes\n")

    def test_signed_commit_refuses_nothing_staged_and_a_checkout_behind_origin(self) -> None:
        bare, clone = self.signed_checkout()
        nothing = self.signed_commit(clone, "-m", "x: nothing")
        self.assertEqual(nothing.returncode, 2)
        self.assertIn("nothing is staged", nothing.stderr)
        self.assertEqual(self.git(bare, "log", "--format=%s", "main"), "base")
        (clone / "a.txt").write_text("a\n")
        self.git(clone, "add", "a.txt")
        self.git(clone, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "local only")
        (clone / "b.txt").write_text("b\n")
        self.git(clone, "add", "b.txt")
        ahead = self.signed_commit(clone, "-m", "x: ahead")
        self.assertEqual(ahead.returncode, 2)
        self.assertIn("is not origin/main", ahead.stderr)
        self.assertEqual(self.git(bare, "log", "--format=%s", "main"), "base")
        self.assertEqual(self.git(clone, "status", "--porcelain"), "A  b.txt")


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
