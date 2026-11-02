import base64
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
state_path = Path(os.environ["FAKE_GH_STATE"])
state = json.loads(state_path.read_text()) if state_path.exists() else {"refs": {}, "commits": [], "prs": []}
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")


def save() -> None:
    state_path.write_text(json.dumps(state))


if args[:3] == ["api", "-X", "POST"]:
    fields = {k: v for k, v in (args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "-f")}
    state["refs"][fields["ref"].removeprefix("refs/heads/")] = fields["sha"]
    save()
    print("{}")
elif args[0] == "api" and "/git/ref/heads/" in args[1]:
    branch = args[1].split("/git/ref/heads/", 1)[1]
    if branch not in state["refs"]:
        print("Not Found", file=sys.stderr)
        sys.exit(1)
    print(state["refs"][branch])
elif args[:2] == ["api", "graphql"]:
    inp = json.loads(sys.stdin.read())["variables"]["input"]
    branch = inp["branch"]["branchName"]
    if state["refs"].get(branch) != inp["expectedHeadOid"]:
        print(json.dumps({"errors": [{"message": "expectedHeadOid mismatch"}]}))
        sys.exit(0)
    oid = f"c{len(state['commits']) + 1:039d}"
    changes = inp["fileChanges"]
    state["commits"].append(
        {
            "branch": branch,
            "headline": inp["message"]["headline"],
            "body": inp["message"]["body"],
            "added": {a["path"]: base64.b64decode(a["contents"]).decode() for a in changes.get("additions", [])},
            "deleted": [d["path"] for d in changes.get("deletions", [])],
        }
    )
    state["refs"][branch] = oid
    save()
    print(json.dumps({"data": {"createCommitOnBranch": {"commit": {"oid": oid}}}}))
elif args[:2] == ["pr", "list"]:
    head = args[args.index("--head") + 1]
    print(json.dumps([{"url": p["url"]} for p in state["prs"] if p["head"] == head]))
elif args[:2] == ["pr", "create"]:
    url = f"https://github.com/{args[args.index('--repo') + 1]}/pull/{len(state['prs']) + 1}"
    state["prs"].append({"url": url, "head": args[args.index("--head") + 1], "base": args[args.index("--base") + 1]})
    save()
    print(url)
else:
    print(f"fake gh: unhandled {args}", file=sys.stderr)
    sys.exit(2)
