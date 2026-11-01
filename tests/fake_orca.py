import json
import os
import re
import sys
from pathlib import Path

args = sys.argv[1:]
with open(os.environ["FAKE_ORCA_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
running = os.environ.get("FAKE_ORCA_RUNNING") == "1"
registered = os.environ.get("FAKE_ORCA_REGISTERED", "")
if args[:1] == ["status"]:
    print(json.dumps({"ok": True, "result": {"runtime": {"reachable": running}}}))
elif args[:2] == ["repo", "list"]:
    print(json.dumps({"ok": True, "result": {"repos": [{"path": registered}] if registered else []}}))
elif args[:2] == ["repo", "add"]:
    print(json.dumps({"ok": True, "result": {"repo": {"path": args[args.index("--path") + 1]}}}))
elif args[:2] == ["agent", "launch"] and os.environ.get("FAKE_ORCA_TURNS"):
    if os.environ.get("FAKE_ORCA_UNREGISTERED") == "1":
        print(json.dumps({"ok": False, "error": {"code": "selector_not_found", "message": "selector_not_found"}}))
        sys.exit(1)
    prompt = args[args.index("--prompt") + 1]
    answer = re.search(r"message to\s+(\S+\.md)", prompt)
    assert answer, prompt
    state = {"answer": answer.group(1), "turn": 0}
    Path(os.environ["FAKE_ORCA_STATE"]).write_text(json.dumps(state))
    turns = json.loads(os.environ["FAKE_ORCA_TURNS"])
    if os.environ.get("FAKE_ORCA_SILENT") != "1":
        Path(str(state["answer"])).write_text(turns[0])
    print(json.dumps({"ok": True, "result": {"launch": {"outcome": {"kind": "terminal", "handle": "term-1"}}}}))
elif args[:2] == ["terminal", "send"]:
    state = json.loads(Path(os.environ["FAKE_ORCA_STATE"]).read_text())
    state["turn"] += 1
    Path(os.environ["FAKE_ORCA_STATE"]).write_text(json.dumps(state))
    turns = json.loads(os.environ["FAKE_ORCA_TURNS"])
    Path(str(state["answer"])).write_text(turns[min(state["turn"], len(turns) - 1)])
    print(json.dumps({"ok": True, "result": {"accepted": True}}))
elif args[:2] == ["terminal", "wait"]:
    print(json.dumps({"ok": True, "result": {"terminal": {"status": "running"}, "satisfied": "tui-idle"}}))
elif args[:2] == ["terminal", "read"]:
    print(json.dumps({"ok": True, "result": {"terminal": {"tail": ["Do you want to create .asks/x.md?", "1. Yes"]}}}))
elif args[:2] == ["agent", "launch"]:
    if not running:
        print(json.dumps({"ok": False, "error": {"code": "runtime_unavailable", "message": "Orca is not running"}}))
        sys.exit(1)
    if os.environ.get("FAKE_ORCA_UNREGISTERED") == "1":
        print(json.dumps({"ok": False, "error": {"code": "selector_not_found", "message": "selector_not_found"}}))
        sys.exit(1)
    print(json.dumps({"ok": True, "result": {"outcome": {"kind": "terminal"}, "receipt": {"detail": "fake"}}}))
else:
    print(json.dumps({"ok": False, "error": "unknown"}))
    sys.exit(2)
