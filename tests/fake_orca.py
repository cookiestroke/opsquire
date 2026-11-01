import json
import os
import sys

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
elif args[:2] == ["agent", "launch"]:
    if not running:
        print(json.dumps({"ok": False, "error": {"code": "runtime_unavailable", "message": "Orca is not running"}}))
        sys.exit(1)
    print(json.dumps({"ok": True, "result": {"outcome": {"kind": "terminal"}, "receipt": {"detail": "fake"}}}))
else:
    print(json.dumps({"ok": False, "error": "unknown"}))
    sys.exit(2)
