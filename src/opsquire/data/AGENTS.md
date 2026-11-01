# Opsquire: contract for agents

Read this before writing or reading anything in the opsquire home (`opsquire home` prints it).
It applies to every agent (Claude Code, Codex, agents inside Orca or similar tools) and does not
depend on any agent's memory. Local rules for this home are at the end.

## Why this exists

Agents prepare, the operator acts. When work needs something agents must not reach (production
hosts, production data, credentials, live changes), the agent writes a task here and the operator
runs it in their own terminal with one command. The runner records everything; the agent then
reads the record and continues.

Agents write `tasks/` and `general/`. Only the runner writes `var/` (run records, locks, asks);
it is write-denied to agents, so a run record is always real.

## Layout of the home

```
tasks/YYYY-MM/YYYYMMDD-HHMM_<area>_<task>/   task: meta.json + main.py|main.sh (+ payload files, design.md)
general/<area>_<verb-noun>/                   reusable script with arguments, called by tasks, never run directly
var/runs/<task-id>/<run-id>/                  one folder per run (run-id = UTC time + mode)
    run.json        summary: read this first
    events.jsonl    structured events, one JSON object per line
    output.log      every line, UTC-timestamped, secrets redacted
    snapshot/       exact copy of every file that ran
var/locks/locks.json                          held locks
var/asks/<ask-id>.json                        every `opsquire ask`, with its agent session
LOCAL.md                                      local rules, appended to this contract
retinue.json                                  optional: this home's agents (overrides the packaged one)
```

`<area>` is the host, service or system. Areas and names are lowercase kebab-case. All timestamps
are UTC ISO-8601 with milliseconds: `2026-10-30T17:20:01.123Z`.

## Docs: read these before code

`docs/CATALOG.md` lists every general script (purpose, test count, users, run record) and every task;
`docs/<general-id>.md` gives a general script's API (module and function docstrings, signatures), what its tests
guarantee, and its run history. They are generated from code, tests, meta.json and run records after every
`new`, `lint` and `run` (or `opsquire docs`), so they are current; never edit them. Read them first and open
code only to change it. Give general scripts a module docstring and docstrings on public functions: that text
is what the next agent reads instead of the code.

## Finding things (use `--json` for structured output)

```
opsquire find <words...>             ranked search over ids, areas, targets, purposes, archived history
opsquire ls [--area x] [--status draft|ready|done|superseded|abandoned|archived]
opsquire show <task>                 meta + every run
opsquire runs [<task>] [--status failed] [--area x] [--since 2026-10-30]
opsquire last                        most recent run
opsquire log <run|task> [--events]   output.log (or events.jsonl) of a run, latest run for a task
opsquire wait <task> [--timeout s]   block until the operator's next run of <task> finishes, then print it
opsquire locks
```

`<task>` may be the full id or any unique part of it. To answer "did it work", read `run.json`:
`status`, `targets`, `last_error`, `results`. Read `output.log` only when you need detail.

## Writing a task

1. `opsquire find <keywords>` first; reuse a task or a general script instead of duplicating it.
   The second time a task is needed, move its logic into `general/` with arguments.
2. Scaffold: `opsquire new <area> <name> --purpose "..." [--changes-prod --rollback "..."]
   [--target a --target b] [--cred ENV=dotted.key] [--uses <general-id>] [--lang py|sh]`
   (`--general` for reusable scripts). Set `OPSQUIRE_AUTHOR` (e.g. `claude-code <session id>`).
3. Edit `main.py` / `main.sh`. Today Python (ruff + `mypy --strict`) and bash (shellcheck) are linted;
   other languages need a linter wired into `opsquire/lint.py` first.
4. `opsquire lint <task>` must pass: ruff, `mypy --strict`, shellcheck, and every `test_*.py` (unittest) in the
   task and in the general scripts it uses. The runner re-checks the exact files at run time and blocks on
   failure. Write offline tests (no ssh, network or credentials) for any logic that decides what gets stopped,
   started or changed; a test file with no tests fails.
5. Hand the operator exactly one command per step: `opsquire run <task-id>` (dry/read), then
   `opsquire run <task-id> --apply`.
6. Then run `opsquire wait <task-id> --json` yourself and continue from the record. Never ask the
   operator to paste output.

### Plans: several tasks, one command

When work needs several tasks in order (check, change, verify), create or reuse each task, then a plan:
`opsquire new <area> <name> --plan --step <task-id> --step <task-id> ...`. The operator runs the plan
like any task: `opsquire run <plan>` (every step in its dry/read mode), then `opsquire run <plan> --apply`.

- Before step 1, the plan checks every step's lint and credentials and locks the union of all targets
  (exclusive if any step is), so it fails before doing anything rather than halfway through.
- Steps run in order; each leaves its own normal run record (`plan_run` links it to the plan's record,
  which lists the steps). The plan stops at the first step that is not `ok`; the rest are `skipped`.
- In a step's meta entry: `"args": [...]` passes arguments, `"verify": true` marks a check whose dry-run
  result is meaningless (it sees the state before the change; the plan record warns), and
  `"on_failure": "<task-id>"` names a rollback task that runs only in apply mode, only when that step
  `failed` or was `partial`, with its own confirms. Nothing is rolled back automatically otherwise.
- Plans do not nest, take no `--` arguments, and their targets and `changes_prod` must match their steps.

### meta.json

| field | meaning |
|---|---|
| `schema` | always `1` |
| `id`, `kind`, `area`, `created`, `author` | identity; `id` equals the folder name |
| `purpose` | the why; one or two sentences |
| `entry` | `main.py` or `main.sh` |
| `interpreter` | optional python for `main.py` (e.g. a project venv that has the needed packages); default is the python opsquire runs on, never whatever `python3` is first on PATH. Recorded in `run.json` |
| `changes_prod` | `false` = read-only (mode `read`); `true` = modes `dry` (default) and `apply` |
| `targets` | hosts/services/systems touched; used for locks and per-target status |
| `lock`, `lock_reason` | `exclusive` (default for changes), `shared` (default for read-only), or `none`. `none` needs a reason; so does `shared` on a changing task |
| `credentials` | `{"ENV_NAME": "dotted.key"}`; the runner reads the credentials file (`OPSQUIRE_CREDS`, default `~/.creds`) and passes only these. Ask the operator for key names, never values |
| `uses` | general scripts this task calls (`$OPSQUIRE_GENERAL/<id>/main.py`) |
| `rollback` | how to undo, or `read-only` |
| `links` | chat threads, PRs, incident links |
| `superseded_by`, `abandoned_reason` | set by hand to retire a task |
| `steps` | plans only: `[{"task": id, "args": [...], "verify": bool, "on_failure": id}]`; a plan has `entry: ""` and no credentials, uses or interpreter |
| `archived` | `{"note": ..., "history": [{when, by, command, source, outcome}]}` for scripts that ran before opsquire; kept byte-for-byte, never linted or runnable |

### What a script gets

Environment: `OPSQUIRE_MODE` (`read`, `dry` or `apply`), `OPSQUIRE_RUN_ID`, `OPSQUIRE_TASK_ID`,
`OPSQUIRE_TARGETS` (space-separated), `OPSQUIRE_GENERAL`, `OPSQUIRE_HELPERS`, plus the declared
credentials. Arguments after `--` in `opsquire run <task> -- ...` are passed through. A dry run must
not change anything.

### Structured events

Print one line `@@OPSQUIRE {json}` to stdout. This works from remote payloads piped over a shell
too, because only stdout comes back. Helpers do it for you locally:

```python
import opsquire_helpers as sq  # bash: source "$OPSQUIRE_HELPERS/opsquire.sh"

sq.step("Stopping services")  # sq_step "..."
sq.target("db2", "failed", "refused")  # ok | failed | skipped | unchanged    (sq_target db2 failed "...")
sq.result("db1 restarted on v1.2.3")  # sq_result "..."
sq.warn("...")
sq.error("...")
sq.data("counts", rows=12)
sq.progress("download", done, total, "B")  # live bar, logged every 10%  (sq_progress label done total [unit])
if sq.APPLY:
    sq.confirm("Restart all four?")  # sq_confirm "...": asks on the terminal, exits 3 on no
sq.run(main, cleanup=undo_scratch)  # bash: redefine sq_cleanup() after sourcing
```

Remote payload (no helpers): `print("@@OPSQUIRE " + json.dumps({"event": "target", "target": "db2", "status": "ok", "msg": "..."}), flush=True)`.

Anything that can take more than a few seconds (downloads, waits, loops over many hosts or files) reports
`progress`: the operator sees one updating bar with speed and time left instead of a silent terminal (a silent
download got interrupted twice before this existed). Unit `"B"` means bytes; other units are shown as counts.

Report every target with a `target` event. If a script reports none, all targets inherit the run
outcome. If it reports some, the unreported ones become `skipped`.

## Graceful termination (every script, every tool)

Ctrl-C, SIGTERM and SIGHUP must end cleanly: no traceback, exit code 130, scratch work undone
(`sq.run(main, cleanup=...)` / `sq_cleanup`), and a final report of how far it got. Nothing else may
be left behind: no temp files, half-written files, half-created folders, held locks or orphaned
processes. The one thing that stays is the record of the interrupted run.

## Two ways in

- **From a chat**: whenever the operator asks for anything script-shaped (see the local rules),
  follow "Writing a task", hand over the command, then `opsquire wait`.
- **From the operator's terminal**: `opsquire ask "<request>"` starts a headless agent in the home that
  does the same and prints `RUN:` lines (or `NEED:` when blocked). `opsquire ask --task <id> "..."`
  resumes that task's conversation; `opsquire ask --resume <ask-id> ["answer"]` continues an interrupted
  or waiting ask.

### The interview (grill), on by default

When something new must be built (no existing task fits), the agent interviews the operator before
building: one question per message, about the decision others depend on first, ending with
`ASK: <question>` and `RECOMMEND: <answer>` lines. `opsquire ask` shows it on the terminal; Enter accepts
the recommendation. Questions answerable from code, configs or records are researched, not asked. The
decisions go into the task's `design.md` before any script is written. Reuse needs no interview.
`--no-grill` makes the agent take safe defaults and record them in `design.md` instead. Without a
terminal, the ask stops with status `waiting` and prints how to answer with `--resume`.

### Agents everywhere: the skill, and Orca

`opsquire integrate` installs the `opsquire` skill into Claude Code's user skills, so agents in any folder or
worktree (Orca included) load this contract when they need tooling, and registers the home as an Orca workspace.
`opsquire ask --agent orca "<request>"` launches a visible Claude agent in Orca on the home with the same rules;
the conversation and any interview continue in Orca, and the ask record keeps Orca's launch receipt.

### Adding an agent tool (the retinue)

Agents are configured in `retinue.json`, not code. An entry has `enabled`, `argv` (placeholders
`{prompt}`, `{system}`, `{home}`, `{session}`, and `{allowed_tools*}` which expands the
`allowed_tools` list), optional `resume_argv`, and `output`: `claude-stream-json` (live tool
progress, session id, cost) or `text` (whole stdout is the answer). New output formats are one parser
function in `opsquire/ask.py`. Only enable an agent whose own sandbox keeps it away from what the
local rules protect; otherwise set `"enabled": false` with a `reason`.

## Statuses

Task (derived, never stored): `draft` (lint not passing) → `ready` (lint passes on current files)
→ `done` (an `apply` run, or a `read` run for read-only tasks, finished `ok`); `superseded` /
`abandoned` / `archived` from meta.

Run: `running`, `ok`, `partial` (some targets failed), `failed`, `aborted` (answered no),
`interrupted` (Ctrl-C, terminal closed, or runner vanished), `blocked` (lint, lock or missing
credential; nothing ran). `opsquire run` and `opsquire wait` exit 0 ok, 1 failed/partial, 2 blocked,
3 aborted, 130 interrupted; `wait` exits 124 on timeout.

Target: `ok`, `failed`, `skipped`, `unchanged`.

## Locks

Every run locks its targets: exclusive blocks everything else, shared allows other shared runs.
A held lock blocks the run (`blocked`). Only the operator can override, with `opsquire run <task>
--break-lock` and typing `BREAK`; the override is recorded in `run.json`. Locks of dead runs are
reclaimed automatically and logged. Choose `shared`/`none` only with a researched `lock_reason`.
Locks cover runs started through opsquire on this machine only.

## Rules

- Never put secrets in scripts, meta.json, or arguments. Never print credential values; the runner
  redacts known values, but do not rely on it.
- Never dump logs wholesale into output; print counts, statuses and short summaries.
- Never write under `var/`, never edit a run record, never run `opsquire run` yourself for a task
  that needs what agents must not reach (it will fail; that is intended).
- Prefer idempotent scripts that check state first and report `unchanged`.
