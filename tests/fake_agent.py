import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

argv = sys.argv[1:]
prompt = argv[0] if argv else ""
system = argv[argv.index("--sys") + 1] if "--sys" in argv else ""
piece_match = re.search(r"^Your piece: (.+)$", system, re.M)
piece = piece_match.group(1) if piece_match else ""
fresh = f"sess-{re.sub(r'[^a-z0-9]+', '-', piece.lower()).strip('-')}" if piece else "sess-new"
session = argv[argv.index("--resume") + 1] if "--resume" in argv else fresh
print(json.dumps({"type": "system", "subtype": "init", "session_id": session}), flush=True)
if os.environ.get("FAKE_LOG"):
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(json.dumps({"piece": piece, "prompt": prompt, "argv": argv, "cwd": os.getcwd()}) + "\n")


def result(text: str, cost: float = 0.01) -> None:
    print(json.dumps({"type": "result", "result": text, "session_id": session, "total_cost_usd": cost}), flush=True)
    sys.exit(0)


def opsquire(*args: str) -> str:
    r = subprocess.run([sys.executable, "-m", "opsquire", *args], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"opsquire {' '.join(args)} failed:\n{r.stdout}{r.stderr}")
    return r.stdout.strip()


if os.environ.get("FAKE_SLEEP") and (("slow" in piece) if piece else not os.environ.get("FAKE_SPLIT")):
    try:
        time.sleep(float(os.environ["FAKE_SLEEP"]))
    except KeyboardInterrupt:
        sys.exit(130)

if "You judge how an operations task ended" in system:
    verdict = os.environ.get("FAKE_JUDGE", "CLOSE: done | the apply run landed it on web1")
    assert "Purpose:" in prompt and "Runs (" in prompt, prompt[:200]
    result(f"Looked at the record.\n{verdict}", cost=0.02)

if os.environ.get("FAKE_SPLIT") and "Parallel work comes first" in system:
    result("\n".join(f"SPLIT: {p}" for p in json.loads(os.environ["FAKE_SPLIT"])))

if piece:
    name = re.sub(r"[^a-z0-9]+", "-", piece.lower()).strip("-")[:30].strip("-")
    home = Path(os.environ["OPSQUIRE_HOME"])
    if prompt.startswith("While you worked"):
        files = re.search(r"conflict markers in: (.+?)\. Resolve", prompt.replace("\n", " "), re.S)
        assert files
        for f in files.group(1).split(", "):
            path = home / f
            keep = [ln for ln in path.read_text().splitlines() if not ln.startswith(("<<<<<<<", "=======", ">>>>>>>"))]
            path.write_text("\n".join(keep) + "\n")
        done = next(home.glob(f"tasks/*/*_web1_{name}"))
        opsquire("lint", done.name)
        result(f"Resolved.\nRUN: opsquire run {done.name}")
    if "question" in piece and "--resume" not in argv:
        result(f"About {piece}.\nASK: which window for {name}?\nRECOMMEND: tonight")
    if "need" in piece and "--resume" not in argv:
        result("Blocked.\nNEED: the book name")
    if "fail" in piece:
        print(json.dumps({"type": "result", "result": "", "session_id": session, "is_error": True}), flush=True)
        sys.exit(1)
    if "clash" in piece:
        shared = home / "general" / "web1_shared"
        shared.mkdir(parents=True, exist_ok=True)
        (shared / "notes.txt").write_text(f"written by {name}\n")
    task = opsquire("new", "web1", name, "--purpose", f"piece {name}")
    Path(task, "main.py").write_text("print(1)\n")
    opsquire("lint", Path(task).name)
    tid = Path(task).name
    result(f"Built {tid}.\nRUN: opsquire run {tid}", cost=float(os.environ.get("FAKE_COST", "0.01")))

grill = int(os.environ.get("FAKE_GRILL", "0"))
if grill and "Do not ask questions" not in system:
    state = Path(os.environ["FAKE_STATE"])
    seen = json.loads(state.read_text()) if state.exists() else []
    seen.append(prompt)
    state.write_text(json.dumps(seen))
    if len(seen) <= grill:
        n = len(seen)
        text = f"Thinking about decision {n}.\nASK: question {n}?\nRECOMMEND: rec {n}"
        print(json.dumps({"type": "result", "result": text, "session_id": session}), flush=True)
        sys.exit(0)

pr_line = ""
if os.environ.get("FAKE_REPO"):
    copy = Path(opsquire("repo", "checkout", os.environ["FAKE_REPO"]))
    (copy / "README.md").write_text("changed by the agent\n")
    notes = Path(os.environ["FAKE_STATE"]).with_name("pr-body.md")
    notes.write_text("Why: test\n")
    pr_line = (
        "\n" + opsquire("repo", "pr", str(copy), "--title", "Agent change", "--body-file", str(notes)).splitlines()[0]
    )

tool = {"type": "tool_use", "name": "Bash", "input": {"command": "opsquire find web1 restart"}}
print(json.dumps({"type": "assistant", "message": {"content": [tool]}}), flush=True)
text = (
    f"Reusing 20261030-1749_web1_restart-bots. author={os.environ.get('OPSQUIRE_AUTHOR')} argv={json.dumps(argv)}\n"
    "RUN: opsquire run 20261030-1749_web1_restart-bots\n"
    "RUN: opsquire run 20261030-1749_web1_restart-bots --apply" + pr_line
)
print(json.dumps({"type": "result", "result": text, "session_id": session, "total_cost_usd": 0.01}), flush=True)
