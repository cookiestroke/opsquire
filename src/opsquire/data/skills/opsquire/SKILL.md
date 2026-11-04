---
name: opsquire
description: Use for ANY tooling need: a script, check, query, deploy, restart, install, report, or repeated procedure, especially anything touching hosts, production systems, credentials or live services. Opsquire is the operator's tool where agents prepare scripts and the operator runs them with one command, with a permanent record. Use it before writing any ad-hoc script or handing over shell commands.
---

The operator runs all tooling through Opsquire. Agents prepare; the operator runs; every run is recorded.

1. Run `opsquire home` to find the home, then read `<home>/AGENTS.md` (the contract, including the operator's local
   rules) and `<home>/docs/CATALOG.md` (everything that already exists). Follow the contract exactly; it wins over
   this summary.
2. Search before writing: `opsquire find <keywords>`. Reuse a task or a general script; create a task with
   `opsquire new` only when nothing fits, and interview the operator first (one question at a time, each with a
   recommended answer) when something new must be built.
3. Independent pieces (none needs another's result, no shared general script) are built at the same time: open one
   piece per part with `opsquire piece open <name> --request "..." --json`, start one subagent per piece with the
   printed brief (it works under the piece's path and env and builds only its piece), then `opsquire piece land
   <name>` each one as it reports. Read-only research can go to subagents without a piece.
4. `opsquire lint <task>` must pass (ruff, mypy --strict, shellcheck, and the task's unit tests).
5. Never run `opsquire run` yourself for work that needs what agents must not reach (production, ssh,
   credentials); hand the operator the exact command, one per step: `opsquire run <task>` first (dry or read),
   then `opsquire run <task> --apply`.
6. Then run `opsquire wait <task> --json` and continue from the run record. Never ask the operator to paste output.

Purely local work (editing code, tests, analysing local files) needs no Opsquire task.
