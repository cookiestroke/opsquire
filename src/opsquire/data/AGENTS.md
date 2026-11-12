# Opsquire: contract for agents

Read this before writing or reading anything in the opsquire home (`opsquire home` prints it).
It applies to every agent (Claude Code, Codex, or any similar tool) and does not
depend on any agent's memory. Local rules for this home are at the end. If your shell says `opsquire: command not
found`, call it as `{{opsquire_command}}`; the commands you hand the operator stay `opsquire ...`.

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
offboarded/YYYY-MM/<task-id>/                 settled tasks that sat idle past the retention (see Offboarding)
offboarded/general/<general-id>/              general scripts the operator took out by hand
var/runs/<task-id>/<run-id>/                  one folder per run (run-id = UTC time + mode)
    run.json        summary: read this first
    events.jsonl    structured events, one JSON object per line
    output.log      every line, UTC-timestamped, secrets redacted
    snapshot/       exact copy of every file that ran
var/locks/locks.json                          held locks
var/asks/<ask-id>.json                        every `opsquire ask`, with its agent session
repos.json                                    optional: repos agents may change through PRs
.repos/<ask-id>/<repo>/                       an ask's copies of those repos (not versioned)
.repos/chat/<repo>-<id>/                      a copy made outside an ask, one per checkout (not versioned)
LOCAL.md                                      local rules, appended to this contract
AGENTS.md                                     this contract, written by opsquire: never edit it; a new rule goes in
                                              LOCAL.md, and opsquire rewrites AGENTS.md on the next new, lint or run
retinue.json                                  optional: this home's agents (overrides the packaged one)
```

`<area>` is the host, service or system. Areas and names are lowercase kebab-case. All timestamps
are UTC ISO-8601 with milliseconds: `2026-10-30T17:20:01.123Z`.

## Docs: read these before code

`docs/CATALOG.md` lists every general script (purpose, lint, test count, users, run record) and every task;
`docs/<general-id>.md` gives a general script's API (module and function docstrings, signatures), what its tests
guarantee, and its run history. They are generated from code, tests, meta.json and run records after every
`new`, `lint` and `run` (or `opsquire docs`), so they are current; never edit them. Read them first and open
code only to change it. Give general scripts a module docstring and docstrings on public functions: that text
is what the next agent reads instead of the code.

## Finding things (use `--json` for structured output)

```
opsquire find <words...>             ranked search over ids, areas, targets, purposes, archived history; also
                                     finds offboarded tasks and marks them
opsquire ls [--area x] [--status draft|ready|done|partial|superseded|abandoned|archived
                                 |lint-ok|lint-failing|unchecked]   (the last three are general scripts)
opsquire show <task>                 meta + every run
opsquire offboard <task...> | --due [--days N] [--dry]    move settled tasks out of tasks/ (see Offboarding)
opsquire restore <task...>           bring offboarded tasks back, with the general scripts and steps they need
opsquire runs [<task>] [--status failed] [--area x] [--since 2026-10-30]
opsquire last                        most recent run
opsquire log <run|task> [--events]   output.log (or events.jsonl) of a run, latest run for a task
opsquire wait <task> [--timeout s]   block until the operator's next run of <task> finishes, then print it;
                                     exit 4 when the task was abandoned or superseded instead
opsquire doctor [--json]             is opsquire working here: the console (up, current code, last state build),
                                     the stop hook, the contract, your author, your unread results; exit 1 if not.
                                     Agents cannot open the console's port; this reads its health file instead
opsquire hold <task> --why "..." [--until 15:00] | --release
                                     keep a ready task (and a plan's steps) out of the inbox until it is handed
                                     over or --until passes; `new --hold "why"` creates it held. Use it for work
                                     prepared ahead of its time; never tell the operator to ignore an inbox item
opsquire supersede <task> --by <newer> [--note "..."]   a newer task replaced it: out of the inbox, refused by run
opsquire reopen <task> --note "why"  undo a close or a supersession (an expiry that was wrong, say)
opsquire handover <task> [--apply|--verify] [--why "..."] [--again] [--withdraw] [-- args]
                                     recommend a run of an existing task: in the inbox with your reason until it runs
opsquire threads [--json]            tasks grouped into threads (the stories they belong to), with suggestions
opsquire milestones [--since 2h|HH:MM|date] [--thread x] [--kind done,failed,result] [--json]
                                     what others would want to know: changing tasks closed, applies or verifies that
                                     failed, thread results; never dry/read runs, lint or asks. Read it before any
                                     handover or post; posting itself happens outside opsquire, with the operator's OK.
opsquire judge <task> [--json]       an agent reads the task's record and closes it (or says what is missing)
opsquire locks
opsquire ui [--port N] [--open]      the operator's console in the browser (localhost): the inbox of handed-over
                                     tasks, running them with their prompts, threads, targets, scripts, logs; it keeps the
                                     code it started with, and a banner on the page says to restart it when that is old
```

`<task>` may be the full id or any unique part of it. To answer "did it work", read `run.json`:
`status`, `targets`, `last_error`, `results` (and `usage`: peak memory and CPU time of the script). Read `output.log` only when you need detail.
`last_error` names the cause: the exception line of a traceback your script reported with `sq.error`, joined to the
failed target's message when one follows it. `dry` says which dry run covered an apply, or why it went without one.
`waiting` (`question`, `since`) says a run is stopped at a prompt until the operator answers; `opsquire runs`, `wait`,
`log` and the console show it. A run that ended with it set was stopped at that prompt.
`origin` says where the run started: `console` (a job from `opsquire ui`) or `terminal` (any command line); records older
than the field have none, which means unknown.

## Writing a task

1. `opsquire find <keywords>` first; reuse a task or a general script instead of duplicating it.
   The second time a task is needed, move its logic into `general/` with arguments.
2. Scaffold: `opsquire new <area> <name> --purpose "..." [--changes-prod --rollback "..."]
   [--target a --target b] [--cred ENV=dotted.key] [--uses <general-id>] [--lang py|sh] [--json]`
   (`--general` for reusable scripts). Set `OPSQUIRE_AUTHOR` (e.g. `claude-code <session id>`). Without `--json` it
   prints only the folder. It exits 2 and creates nothing on a bad name, so check its exit code in a script. A host
   named `db_2` has area `db-2` and `--target db_2`: locks match targets exactly. `new` and `handover` report on stderr
   what else waits for the same targets.
   Python tasks start from a working shape, not a stub: a changing task's `main.py` reads every target, decides each
   one's changes in a pure `plan_for` that `test_main.py` tests, reports unchanged targets and leaves them alone, asks
   once listing every change, then changes and reports each target; a read task's `main.py` reads every target into
   `data` events and one finding per target. Both report `progress`; add a test to a read task when it parses anything. `design.md` comes with its sections (Why, Plan,
   Rollback, Verify). Fill them in rather than replacing them with a bare script: keep the decision in a pure function
   with its tests, and replace the starter test with real cases (captured real responses for anything parsed).
   A task that runs in stages gets `--stage build --stage deploy` (in order) at `new`.
3. Edit `main.py` / `main.sh`. Today Python (ruff + `mypy --strict`) and bash (shellcheck) are linted;
   other languages need a linter wired into `opsquire/lint.py` first.
4. `opsquire lint <task>` must pass: ruff, `mypy --strict`, shellcheck, and every `test_*.py` (unittest) in the
   task and in the general scripts it uses. The runner re-checks the exact files at run time and blocks on
   failure. Write offline tests (no ssh, network or credentials) for any logic that decides what gets stopped,
   started or changed; a test file with no tests fails.
5. Hand the operator exactly one command per step: `opsquire run <task-id>` (dry/read), then
   `opsquire run <task-id> --apply`. A ready task also appears in the UI's inbox with its next run, where the
   operator can run it with its prompts; the records are the same. A task you reuse (a read task that already ran,
   a closed task to run again, an existing task with new arguments) owes no run by itself and would never reach the
   inbox: `opsquire handover <task-id> [--apply|--verify] --why "one line" [-- args]` puts exactly that run there,
   with your reason, until it starts. `opsquire ask` records one for every RUN line its agent ends with. An apply that
   already finished ok or is running, with the same scripts and arguments, is refused: read that record first, and only
   when production really needs it again pass `--again --why "..."`. `--withdraw` takes a handover back out. A task
   whose last run failed leaves the inbox until its files change or it is handed over again.
   An apply needs a dry run that finished ok with the same scripts and the same arguments: without one
   `opsquire run <task-id> --apply` is refused (status `blocked`, nothing ran) and `opsquire handover --apply` is not
   recorded, both naming the dry run to do first. So after any edit, and for every argument set (each stage of a
   staged task), the dry run comes again. A plan is checked before its first step: every changing step needs its
   dry run, which the plan's own dry run gives. Only the operator can go without, by typing
   `opsquire run <task-id> --apply --skip-dry "why"`; the reason is recorded in `run.json` (`dry`). Never hand them
   that command to get past a dry run that failed.
6. Every run you hand over, you follow to its end, always: before handing it over read the task's earlier runs
   (`opsquire runs <task-id> --json`, and `opsquire log <run-id>` for any that did not end ok) so you know what
   failed before and do not hand over a run that already answered the question; then run
   `opsquire wait <task-id> --json` yourself, read the record and its output (`opsquire log <task-id>`) and
   continue from them: report what it found, fix and hand over again, or close. Never end your turn on a handed-over
   run without its record, and never ask the operator to paste output. When `wait` times out, wait again.
   opsquire holds you to it, by your `OPSQUIRE_AUTHOR` (set it in every shell): a record counts as read once
   `wait`, `log`, `runs <task>`, `show` or `last` printed it to you. `handover` is refused until you read the task's
   latest run, and `handover` and `new` are refused while a run you handed over has finished unread
   (`opsquire unread` lists them). With the Stop hook `opsquire integrate` installs, a Claude session cannot end its
   turn on such a run either.
7. Closing. An apply run that ends ok closes the task by itself: `outcome` done, a note built from the run record,
   and a `## Result` section in design.md (amend either when the generated text is not the truth). A task with a
   verify check closes only on it: right after an ok apply, `opsquire run` runs the verify check, and the task closes
   when every target is ok; otherwise it stays open. A task that runs in stages declares them (`stages` in
   meta.json): the check runs, and the task closes, only after the last stage's apply, and no handover is needed
   between stages. An unwritten scaffold check (every target skipped) says nothing, and the apply closes the task
   as before. Every other end is the agent's decision, right after `opsquire wait` returns: a failed, partial,
   aborted or interrupted apply means either fix the task and hand it over again, or close it:
   `opsquire close <task> --outcome done|partial|abandoned --note "what actually happened" [--run <run-id>]`
   (done when the host ended up right despite the record, partial when some of it did, abandoned when it will not
   happen). `close` only annotates meta.json and touches no host. A changing task is not settled until it is closed,
   and its thread stays open; the operator never has to close anything. What agents leave behind shows up in the UI
   as "needs a decision", where `opsquire judge` (an agent reading purpose, design, runs, the last output and the
   state of linked PRs on GitHub, under a small budget) decides and closes it, marked as the judge's, or asks for
   the verify check; the operator can amend any close.
8. The verify check. Every changing task is scaffolded with `verify.py` (or `.sh`, `meta.json` `verify`): a
   read-only script that establishes whether the task's intended end state is live, reporting every target `ok`
   or `failed` with what it found. Write it with the task: it is how a task that did not close itself gets settled
   without anyone guessing. `opsquire run <task> --verify` runs it (shared lock, mode `verify`); all targets ok
   closes the task; a failure goes to the judge. After an apply that did not close the task, the inbox asks for the
   verify run next. `--no-verify` is for tasks where no check is possible; say why in design.md.

### Threads: the story a task belongs to

Work rarely ends with one task (a deploy, its check, its rollback, the follow-up). A thread groups them. Give a task
its thread with `opsquire new ... --thread <kebab-name>` (join an existing one with the same name; the name should
read as a story, `gateway-drain-rollout`). Without it, opsquire infers threads from the session that created the
tasks (the session id in `OPSQUIRE_AUTHOR`), the asks that produced them, plans and their steps, supersession,
shared PR or Slack links, and task ids mentioned in purposes and close notes; `opsquire threads` shows them, marked
inferred. A thread is done when every task in it is settled (closed, retired, or a read-only task that ran ok). When
a thread ends, write its one-paragraph result to `threads/<thread>.md` in the home: what changed, where it stands,
what remains. That paragraph, not the run records, is what the handover reads.

`opsquire threads` and the UI also show what a task's record suggests: an apply that ran ok and awaits `close`; a
closed changing task whose design.md has no `## Result`; a task never run for a week, or failed and untouched for
three days; a newer task in the same area using the same scripts (superseded?); a task run many times per success.
They are suggestions with their evidence; acting on them (close, fix, supersede) is the agent's or operator's call.

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
| `superseded_by`, `superseded`, `abandoned_reason`, `reopened` | written by `supersede`, `close` and `reopen`: who decided, when and why |
| `thread` | optional: the thread (story) this task belongs to, kebab-case; inferred when absent |
| `stages` | optional, changing tasks that run in stages: the first argument of each stage, in order (`["build", "deploy"]`; a run with no arguments is the first stage). The inbox offers each stage's dry run and apply with that argument, and the verify check and the automatic close wait for the last stage |
| `verify` | changing tasks: the read-only check script (`verify.py` or `verify.sh`) that says whether the intended state is live |
| `judged` | written by `opsquire judge` when it keeps a task open or asks for the check: decision, note, by, when |
| `steps` | plans only: `[{"task": id, "args": [...], "verify": bool, "on_failure": id}]`; a plan has `entry: ""` and no credentials, uses or interpreter |
| `archived` | `{"note": ..., "history": [{when, by, command, source, outcome}]}` for scripts that ran before opsquire; kept byte-for-byte, never linted or runnable |

### What a script gets

Environment: `OPSQUIRE_MODE` (`read`, `dry`, `apply` or `verify`), `OPSQUIRE_RUN_ID`, `OPSQUIRE_TASK_ID`,
`OPSQUIRE_TARGETS` (space-separated), `OPSQUIRE_GENERAL`, `OPSQUIRE_HELPERS`, `OPSQUIRE_ORIGIN` (`console` when the
run was started from the UI, unset otherwise), plus the declared credentials. Arguments after `--` in
`opsquire run <task> -- ...` are passed through. A dry run must not change anything.

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

Ask once per run. Work out every change first (read the hosts, build the plan, print it), then one
`confirm` before the first change that lists them all ("web1: restart api, web2: restart api and worker"), then
change without asking again. Never confirm per host or per item inside a loop: a run that asks twice gets a warn
event in its record. A changing task whose scripts ask first starts on one click in the console; one that never
asks needs a second click there, and lint says so.

A general script run as a child (`subprocess.run([sys.executable, script])`) can call `sq.confirm` as it is, with
nothing to pass along. Leave its stdout connected: events reach the runner through it, so `capture_output` hides its
prompt from the record and loses its answer.

Report every target with a `target` event. If a script reports none, all targets inherit the run
outcome. If it reports some, the unreported ones become `skipped`.

## Graceful termination (every script, every tool)

Ctrl-C, SIGTERM and SIGHUP must end cleanly: no traceback, exit code 130, scratch work undone
(`sq.run(main, cleanup=...)` / `sq_cleanup`), and a final report of how far it got. Nothing else may
be left behind: no temp files, half-written files, half-created folders, held locks or orphaned
processes. The one thing that stays is the record of the interrupted run.

## Two ways in

- **From a chat**: whenever the operator asks for anything script-shaped (see the local rules),
  follow "Writing a task" (independent pieces in parallel, see "Parallel work"), hand over the command, then
  `opsquire wait`.
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

### Code and config changes: PRs from a copy

Repos agents may change are registered in the home's `repos.json`:
`{"branch_prefix": "name/", "repos": {"<name>": {"path": "~/repo", "github": "owner/repo", "base": "main"}}}`.
Never edit the operator's own checkout of a repo.

```
opsquire repo list                                          registered repos
opsquire repo checkout <repo>                               a copy of origin/<base> under .repos/; prints its path
opsquire repo checkout <repo> --base <branch>               a copy of another branch (e.g. a colleague's PR)
opsquire repo overlap <copy>                                open PRs of others that change the same files
opsquire repo pr <copy> --title "..." --body-file <file>    GitHub-signed commit of the copy's changes + open the PR
```

- The copy is a plain export of the base branch (`git archive`; nothing is written to the operator's repo). Edit it,
  run the repo's own checks there (its CLAUDE.md/AGENTS.md; use the operator checkout's venv with PYTHONPATH at the
  copy), and write the PR description outside the copy.
- `repo pr` compares the copy with its base commit, pushes the differences as one commit through GitHub's
  createCommitOnBranch (signed by GitHub) on `<branch_prefix><title words>` (or `--branch`), and opens the PR.
  Running it again with the same title after more edits adds a commit with only what changed since and keeps the
  same PR. A different title on a copy that already has a branch is refused: start a new PR from a new checkout,
  or pass `--branch <that branch>` to add to the earlier PR on purpose.
- `--base <branch>` copies that branch instead; `repo pr` then opens its PR against it, or with `--branch <that same
  branch>` commits straight onto it (its open PR, if any, gets the commit; with none, `--body-file` becomes the
  commit description). Committing onto it only succeeds while its head is still the commit the copy was made from
  (or this copy's last push); if it moved, check out again. Only push onto someone else's branch when the operator
  asked for it. Never onto the repo's own base.
- Read code from a copy, not the operator's checkout, which may be old: `repo list` shows where each stands against
  origin, and `repo checkout` warns when it could not fetch and its copy is behind.
- `opsquire repo overlap <copy>` lists open PRs that change the files your copy changes; read them before building on
  the same code. `repo pr` warns about the same PRs and never blocks on them.
- Inside an ask, `repo checkout` reuses the ask's one copy per repo. Outside an ask (a chat session) every
  `repo checkout` makes a new copy and prints its path; use that path, never another session's copy.
- PRs open ready for review, config repos included; follow the repo's PR conventions (for umm_config, call out
  risky fields). Opsquire never merges or deploys; a base branch that auto-deploys gets a warning.
- Hand over each PR with a `PR: <url>` line. An ask removes its copies when it ends `ok`; a waiting or interrupted
  ask keeps them for `--resume`. Each ask (and each parallel piece) has its own copies; asks refresh every
  registered repo's origin before the agent starts.

### Parallel work: one request, several agents

Independent pieces of a request are built at the same time, never one after another. Pieces are independent when
none needs another's result and no two would edit the same general script. Each piece is self-contained: its request
carries every detail it needs, and the small decisions (format, defaults, what to skip) are settled in it from the
request and what was looked up; only a decision that truly needs the operator stays open. Whatever a piece decides
goes into its design.md. Research fans out as well: reading records, the catalog, code or a host's state (through
read tasks) can go to read-only helpers without any piece; only work that writes the home needs one.

#### From `opsquire ask`

When a request holds independent pieces, the first agent answers only `SPLIT: <self-contained request>` lines, one
per piece, and builds nothing itself.
`opsquire ask` then starts one agent per piece, each in its own git worktree of the home (`.worktrees/<ask-id>/<n>`,
branch `ask/<ask-id>/<n>`), all at once (`--max-agents N` limits it; `--no-split` keeps one agent).

- A piece agent builds only its piece, exactly as above. Its `opsquire` commands see its worktree as the home and
  the main home's run records (`OPSQUIRE_VAR`).
- Interview questions from every piece reach the operator one at a time, labelled `[n/total name]` (the name
  from `SPLIT: name | request`), pinned at the bottom of the terminal while progress scrolls above them.
- A piece that hands over RUN lines is committed and merged into the home. If another piece changed the same files,
  its agent gets one turn to resolve the conflict markers and lint again; otherwise it is reported as a clash.
- Every merged piece keeps its own RUN lines, and opsquire also bundles all of them into one plan (a piece's plan
  contributes its steps), so one command can run everything in order.
- A piece that ends waiting (a question without a terminal, a NEED, a clash) or is interrupted is parked: its work
  is committed to its branch and the worktree removed. `opsquire ask --resume <ask-id> ["answer"]` recreates the
  worktrees and continues only the unfinished pieces.
- Spend: `--budget` (default 15 USD) covers the whole ask, all agents together. It is a reminder (one notice when
  passed); `--strict-budget` makes it a hard cap on each agent turn.

#### From a chat: your own subagents

An agent in a chat (Claude Code or any tool with a subagent tool) fans out itself, with the same pieces in the same
worktrees. Without a subagent tool, build the pieces one after another in the home as usual. Pieces do not nest.

```
opsquire piece open <name> --request "..." [--group <g>]   worktree + branch piece/<group>/<name>; prints path, env, brief
opsquire piece land <name|group/name|path>                 commit, merge into the home, lint what changed, remove it
opsquire piece drop <name> [--delete]                      park an unfinished piece (open again resumes it) or discard it
opsquire piece ls                                          open and parked pieces
```

- Open one piece per part (`--json` gives the brief as text), then start one subagent per piece with the brief:
  the worktree path, the env it exports before every `opsquire` command (`OPSQUIRE_HOME` is the worktree,
  `OPSQUIRE_VAR` the main home's records), the piece's request, and the whole request for context. The subagent
  builds only its piece, under that path and env, and ends with `RUN:`/`PR:`/`NEED:` lines like any agent.
- Pieces of one request share a group; the default is the session in `OPSQUIRE_AUTHOR`, so set it.
- Subagents do not interview the operator. A piece that hits a truly open decision stops and reports `ASK:` and
  `RECOMMEND:` lines to the agent that started it, who asks the operator one question at a time and resumes that
  subagent with the answer.
- When a subagent reports, land its piece. `land` lints every task and general script the piece changed, in the home,
  and says so per id; a lint failure after the merge goes back to that subagent. A clash (another piece changed the
  same file) is reported with the files, left with conflict markers in the piece's worktree: give that subagent one
  turn to resolve them keeping both intents and lint, then land again; otherwise drop the piece and tell the operator.
- With several landed tasks, bundle them: `opsquire new <area> <name> --plan --step <task> ...`, and hand over every
  piece's RUN lines plus the plan's. A piece that cannot finish (a NEED, a crash) is dropped, which parks its work on
  its branch; say so when handing over.
- `piece` writes only the home (never `var/`), so an agent sandbox that can write the home can fan out.

### The home is a local git repository

`opsquire integrate` installs the `opsquire` skill into Claude Code's user skills, so agents in any folder or
worktree load this contract when they need tooling, and makes the home a local-only git repository (it refuses one
with a remote). Opsquire commits in it by itself: when `opsquire lint` passes (the ready version), before every
`opsquire run`, at the end of an ask, as a snapshot before a parallel ask or a chat piece, and when a piece lands. These commits
use the operator's name and email (a placeholder when git has none) but are never signed (the repository cannot leave the machine, so a signature
proves nothing and would ask for a passphrase before every run); a commit that cannot be made is skipped with a note
and picked up by the next one.

### Adding an agent tool (the retinue)

Agents are configured in `retinue.json`, not code. An entry has `enabled`, `argv` (placeholders
`{prompt}`, `{system}`, `{home}`, `{session}`, and `{allowed_tools*}` which expands the
`allowed_tools` list), optional `resume_argv`, optional `budget_argv` (placeholder `{budget}`, used by
`--strict-budget`), and `output`: `claude-stream-json` (live tool
progress, session id, cost) or `text` (whole stdout is the answer). New output formats are one parser
function in `opsquire/ask.py`. Only enable an agent whose own sandbox keeps it away from what the
local rules protect; otherwise set `"enabled": false` with a `reason`.

## Statuses

Task (derived, never stored): `draft` (lint not passing) → `ready` (lint passes on current files)
→ `done` (an `apply` run, or a `read` run for read-only tasks, finished `ok`); `superseded` /
`abandoned` / `archived` from meta. `opsquire close` records how a task really ended and overrides the run-derived
status with `done`, `partial` or `abandoned`.

General script (derived from its last lint): `lint-ok` (passed on the current files), `lint-failing`, `unchecked`
(never linted, or edited since). A general script is never run on its own, so it has no task status. `opsquire lint
<task>` also lints, each on its own, the general scripts the task uses whose files changed, and
`opsquire lint <general-id>` lints one directly: lint a general script after every edit, so the catalog never shows
`unchecked` for a script other tasks rely on.

## Waiting in the inbox

An inbox item that waited 3 days (since its handover, its last run, or its creation) is marked stale in the inbox
and the console and asks for a decision: run it, `opsquire supersede <task> --by <newer task>`, or
`opsquire close <task> --outcome abandoned --note "why"`. Once a day `new`, `lint` and `run` say on stderr how many
items wait for one. A task or plan that never started a run in 14 days is closed abandoned by itself (`closed_by`
`opsquire (expiry)`); a task that ran once, a failed or partial apply above all, never expires and waits for a
person. `opsquire reopen` undoes any of these.

The author hears what became of its tasks: `opsquire new` and `opsquire handover` print on stderr the decisions
someone else made this week on tasks with the same author, and the author's items that are stale; `opsquire wait`
on a task abandoned or superseded while it waited exits 4 with the decision. Read them before starting again: a
superseded task is not handed over a second time.

## Offboarding

`tasks/`, `ls`, the catalog, the console and the inbox show the work that is still alive. A settled task (closed,
superseded, or a read-only task whose run finished `ok`) with no run waiting in the inbox leaves for
`offboarded/YYYY-MM/<id>/` once it sat idle for the retention: 7 days after its last activity (created, last run, or
last change to meta.json such as a close or a handover) unless the home says otherwise
(`opsquire init --offboard-days N`, 0 = never automatically). The sweep rides `new`, `lint` and `run` at most once a
day, says on stderr what it moved, and commits the move. Nothing is deleted: the folder is moved whole, its run
records stay in `var/`, `find` still finds it (marked offboarded) and `show`, `runs` and `log` still read it.

- A plan and its steps leave together, or stay together while any of them is alive.
- General scripts never leave on their own. `opsquire offboard <general-id>` is refused while an active task uses it.
- An unsettled task never leaves: run it, supersede it, or close it abandoned. Records archived from before opsquire
  leave only by hand.
- To run, lint, close or hand over an offboarded task, restore it first: `opsquire restore <task>` (the error says
  so). Restoring starts its idle clock again. A read task you mean to reuse after a quiet week is found by `find`
  and comes back the same way.
- Never move folders between `tasks/` and `offboarded/` by hand; `opsquire offboard <task...>` does it now,
  whatever the age, and refuses with the reason for each task that is not settled.

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
