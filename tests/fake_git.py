import os
import sys

args = sys.argv[1:]
if "rev-parse" in args:
    print("true" if os.environ.get("FAKE_GIT_REPO") == "1" else "", end="")
    sys.exit(0 if os.environ.get("FAKE_GIT_REPO") == "1" else 128)
if "remote" in args:
    print(os.environ.get("FAKE_GIT_REMOTES", ""))
    sys.exit(0)
sys.exit(1)
