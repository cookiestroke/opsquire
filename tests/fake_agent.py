import json
import os
import sys
import time

argv = sys.argv[1:]
session = argv[argv.index("--resume") + 1] if "--resume" in argv else "sess-new"
print(json.dumps({"type": "system", "subtype": "init", "session_id": session}), flush=True)
if os.environ.get("FAKE_SLEEP"):
    try:
        time.sleep(float(os.environ["FAKE_SLEEP"]))
    except KeyboardInterrupt:
        sys.exit(130)
tool = {"type": "tool_use", "name": "Bash", "input": {"command": "opsquire find web1 restart"}}
print(json.dumps({"type": "assistant", "message": {"content": [tool]}}), flush=True)
text = (
    f"Reusing 20261030-1749_web1_restart-bots. author={os.environ.get('OPSQUIRE_AUTHOR')} argv={json.dumps(argv)}\n"
    "RUN: opsquire run 20261030-1749_web1_restart-bots\n"
    "RUN: opsquire run 20261030-1749_web1_restart-bots --apply"
)
print(json.dumps({"type": "result", "result": text, "session_id": session, "total_cost_usd": 0.01}), flush=True)
