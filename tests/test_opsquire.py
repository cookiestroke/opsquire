from __future__ import annotations

import json
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
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


def run_in_pty(cmd: list[str], env: dict[str, str], answers: list[tuple[str, str]]) -> subprocess.CompletedProcess[str]:
    pid, fd = pty.fork()
    if pid == 0:
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

    def cli(self, *args: str, answers: list[tuple[str, str]] | None = None) -> subprocess.CompletedProcess[str]:
        if answers is None:
            return subprocess.run([*CLI, *args], env=self.env, capture_output=True, text=True, timeout=120)
        return run_in_pty([*CLI, *args], self.env, answers)

    def new(self, name: str, body: str, *extra: str, lang: str = "py", area: str = "web1") -> Path:
        r = self.cli("new", area, name, "--purpose", f"test {name}", "--lang", lang, *extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        folder = Path(r.stdout.strip())
        (folder / f"main.{lang}").write_text(body)
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
            (ROOT / "src/opsquire/data/templates/main.py").read_text(),
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
        self.assertEqual(json.loads(self.cli("show", "uses-general", "--json").stdout)["status"], "draft")

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
        r = self.cli("run", "prompt", "--apply", answers=[("[y/N]", "\x03")])
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
        r = self.cli("run", "bashprompt", "--apply", answers=[("[y/N]", "\x03")])
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
        banned = [w[::-1] for w in ("tsj", "vedpactsj", "03.271", "radnaM", "ldvb", "dovb", "inimeg", "sgol-tsj")]
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

    CONFIRM_BODY = (Path(__file__).resolve().parent.parent / "src/opsquire/data/templates/main.py").read_text()

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
        r = self.cli("run", plan.name, "--apply", answers=[("[y/N]", "n")])
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
            r = self.cli("run", name, "--apply", answers=[("[y/N]", "y")])
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
        self.new("beating", "import time\n\nimport opsquire_helpers as sq\n\nsq.run(lambda: time.sleep(8))\n")
        p = self.start("run", "beating")
        deadline = time.monotonic() + 30
        beats: list[Path] = []
        while time.monotonic() < deadline and not beats:
            beats = list((self.home / "var/runs").rglob("heartbeat"))
            time.sleep(0.2)
        self.assertTrue(beats)
        first = beats[0].stat().st_mtime
        time.sleep(6)
        self.assertGreater(beats[0].stat().st_mtime, first)
        held = json.loads(self.cli("locks", "--json").stdout)
        self.assertTrue(held["web1"][0]["alive"])
        self.assertEqual(held["web1"][0]["pid_ns"], self.own_ns())
        p.communicate(timeout=60)
        self.assertEqual(p.returncode, 0)

    PASSING_TEST = (
        "import unittest\n\nfrom logic import double\n\n\nclass T(unittest.TestCase):\n"
        "    def test_double(self) -> None:\n        self.assertEqual(double(2), 4)\n\n"
        "    def test_zero(self) -> None:\n        self.assertEqual(double(0), 0)\n"
    )

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
        self.assertIn("**web1_shared**: shared logic (tests: 2; used by 1", catalog)
        self.assertRegex(catalog, r"\d{8}-\d{4}_web1_user \[draft, task\]: test user \(uses web1_shared\)")
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

    def fake_orca(self, running: bool, registered: str = "") -> Path:
        bin_dir = self.tmp / "orca-bin"
        bin_dir.mkdir(exist_ok=True)
        cli = bin_dir / "orca-ide"
        cli.write_text(f'#!/bin/sh\nexec {sys.executable} {ROOT / "tests/fake_orca.py"} "$@"\n')
        cli.chmod(0o755)
        log = self.tmp / "orca-calls.jsonl"
        self.env |= {"PATH": f"{bin_dir}:{self.env['PATH']}", "FAKE_ORCA_LOG": str(log)}
        self.env |= {"FAKE_ORCA_RUNNING": "1" if running else "0", "FAKE_ORCA_REGISTERED": registered}
        self.env["CLAUDE_CONFIG_DIR"] = str(self.tmp / "claude")
        return log

    def orca_calls(self, log: Path) -> list[list[str]]:
        return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []

    def test_integrate_installs_the_skill_idempotently(self) -> None:
        self.fake_orca(running=False)
        skill = self.tmp / "claude/skills/opsquire/SKILL.md"
        r = self.cli("integrate", "--dry-run")
        self.assertIn("claude skill: would be installed", r.stdout)
        self.assertFalse(skill.exists())
        r = self.cli("integrate")
        self.assertIn("claude skill: installed", r.stdout)
        self.assertIn("orca: not running", r.stdout)
        self.assertEqual(skill.read_text(), (ROOT / "src/opsquire/data/skills/opsquire/SKILL.md").read_text())
        self.assertIn("claude skill: unchanged", self.cli("integrate").stdout)

    def test_integrate_registers_the_home_in_orca_once(self) -> None:
        log = self.fake_orca(running=True)
        r = self.cli("integrate")
        self.assertIn(f"orca: registered {self.home}", r.stdout)
        self.assertIn(["repo", "add", "--path", str(self.home), "--json"], self.orca_calls(log))
        self.fake_orca(running=True, registered=str(self.home))
        self.assertIn("orca: home already registered", self.cli("integrate").stdout)

    def test_ask_via_orca_launches_a_visible_agent_on_the_home(self) -> None:
        log = self.fake_orca(running=True)
        r = self.cli("ask", "--agent", "orca", "restart", "web1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Launched a Claude agent in Orca (terminal)", r.stdout)
        launch = [c for c in self.orca_calls(log) if c[:2] == ["agent", "launch"]][0]
        self.assertIn(f"path:{self.home}", launch)
        self.assertIn("--focus", launch)
        prompt = launch[launch.index("--prompt") + 1]
        self.assertIn("Request: restart web1", prompt)
        self.assertIn("AGENTS.md", prompt)
        rec = json.loads(next((self.home / "var/asks").glob("*.json")).read_text())
        self.assertEqual((rec["agent"], rec["status"]), ("orca", "ok"))

    def test_ask_via_orca_explains_a_refused_launch(self) -> None:
        self.fake_orca(running=False)
        r = self.cli("ask", "--agent", "orca", "hi")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Orca is not running", r.stdout + r.stderr)
        self.fake_orca(running=True)
        self.env["FAKE_ORCA_UNREGISTERED"] = "1"
        r = self.cli("ask", "--agent", "orca", "hi")
        self.assertEqual(r.returncode, 1)
        self.assertIn("run `opsquire integrate`", r.stdout + r.stderr)

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


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
