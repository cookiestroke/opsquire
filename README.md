# Opsquire

[![CI](https://github.com/cookiestroke/opsquire/actions/workflows/ci.yml/badge.svg)](https://github.com/cookiestroke/opsquire/actions/workflows/ci.yml)

Agents prepare the scripts, you run them, and every run is recorded for the next agent.

A squire prepares the arms; the knight acts. Opsquire lets AI coding agents do the preparation
for operational work (finding or writing the right script, linting it, planning the order)
while you stay the one who runs anything that touches production, with one command per step.
Every run leaves a structured, agent-readable record that agents cannot forge.

## Install

From the latest release (a wheel attached to the GitHub release; the repo is private, so `gh` fetches it). Opsquire
needs Python 3.13 or newer; `uv` brings its own, so this works on any Linux (or WSL), whatever Python it ships:

```
curl -LsSf https://astral.sh/uv/install.sh | sh             # uv, if you do not have it; then open a new terminal
sudo apt install -y gh git shellcheck                        # Debian, Ubuntu
gh auth login
gh release download --repo cookiestroke/opsquire --pattern '*.whl' --dir /tmp/opsquire
uv tool install --python 3.13 --with ruff --with mypy /tmp/opsquire/*.whl
opsquire init ~/ops-home --budget 15     # one command: home, local rules, config, credentials, skill, first task
opsquire run <the first task it printed>
```

With pipx instead, on a system whose Python is already 3.13 or newer:
`pipx install --python python3.13 /tmp/opsquire/*.whl && pipx inject opsquire ruff mypy`. Upgrading later is the same
`uv tool install` with `--force`.

`ruff` and `mypy` are the lint gate every Python task must pass before it runs (`shellcheck` for shell tasks), so a
home without them can run nothing. `opsquire init` is safe to run again: it never overwrites what exists, and it
reports every step, including any tool that is missing. It writes
`LOCAL.md` in the home from a template: edit it, it is where this home's rules live (who the operator is, what
counts as a task here, areas, credentials, tools). Credentials go in `~/.creds` (or `$OPSQUIRE_CREDS`) as JSON;
tasks declare what they need by key and get it as environment variables, and the runner redacts the values from
every record. Agents never read the file.

From a checkout, for development:

```
uv tool install --python 3.13 --editable . --with ruff --with mypy
```

## Daily use

```
opsquire ask "<what you want>"     an agent finds or writes the task and prints what to run
opsquire find <words>              is there already a script for this?
opsquire run <plan>                several tasks in order, one command (dry first, then --apply)
opsquire run <task>                dry run (or read-only run)
opsquire run <task> --apply        the real thing; asks before each change; refused until a dry run of the
                                   same scripts and arguments finished ok (--skip-dry "why" overrides, on record)
opsquire last                      what just happened
opsquire runs --status failed      what went wrong, across everything
opsquire log <task>                full output of the latest run
opsquire wait <task>               (for agents) block until your next run of a task finishes
opsquire handover <task> [--apply] --why "..."   (for agents) put a run of an existing task in the inbox, with the reason
opsquire locks / unlock <target>
opsquire hold <task> --why "..." [--until 15:00]   prepared for later: out of the inbox until handed over
opsquire supersede <task> --by <newer>   a waiting task that will not run as it is; `reopen` undoes it
                                   (inbox items waiting 3+ days ask for this; tasks that never ran expire in 14)
opsquire offboard --due            settled tasks idle for 7 days leave tasks/ for offboarded/ (also daily, on its own)
opsquire restore <task>            bring one back; `opsquire init --offboard-days N` sets the retention
opsquire milestones --since 2h     what others would want to know: tasks closed, applies that failed, thread results
opsquire doctor                    console up and current, stop hook, contract, author: what needs fixing
opsquire ui --open                 the console: the inbox of runs waiting on you, threads, runs, locks, and an
                                   Ask box: your request goes to an agent as a chat you can follow and answer
opsquire integrate                 install the opsquire skill for Claude agents; make the home a local git repo
opsquire ask --no-split "<...>"    one agent only (asks split into parallel pieces by default)
opsquire ask --max-agents 2 ...    at most 2 agents at once; --budget 20 [--strict-budget] for the spend
opsquire ask --model quick ...      a tier the agent's retinue entry defines (quick, default, deep) or a model id;
                                   --agent picks another agent from retinue.json. Spend is what the agent reports
                                   at API list prices; its retinue entry says whether that is a plan or an API key
opsquire repo checkout|pr ...      (for agents) change a registered repo through a GitHub-signed PR
```

Ctrl-C is always safe: the run is recorded as `interrupted`, scratch work is undone, locks are
released, and nothing else is left behind.

The full contract agents follow is in [src/opsquire/data/AGENTS.md](src/opsquire/data/AGENTS.md);
`opsquire contract --write` installs it, plus the home's `LOCAL.md` rules, as the home's `AGENTS.md`, and opsquire
keeps that file current afterwards; it never overwrites an edit made in it by hand (it says to move it into `LOCAL.md`).

## Development

```
python3 tests/test_opsquire.py -v
ruff check . && ruff format --check .
mypy src tests scripts
(cd src/opsquire/data/helpers && mypy --config-file ../mypy.ini opsquire_helpers.py)
(cd src/opsquire/data/templates && MYPYPATH=../helpers mypy --config-file ../mypy.ini main.py verify.py read.py)
shellcheck -x -P src/opsquire/data/helpers src/opsquire/data/helpers/opsquire.sh src/opsquire/data/templates/*.sh
python scripts/full_power.py   # every feature end to end, through the real CLI, in a scratch home
```

Standard library only, Python 3.13+. [CI](.github/workflows/ci.yml) runs the same checks on every push and
pull request, plus the test suite on the oldest and newest supported Python, and builds the wheel, installs it
into an empty venv and checks that every data file ships.

Commits reach `main` as commits GitHub signs itself, never through `git push`:

```
git add <files>
python3 scripts/signed_commit.py -m "area: what changed" [-m "why, in a paragraph"]
```

It sends the staged files with `createCommitOnBranch`, refuses when the checkout is behind `origin/main`, and
moves the local branch onto the new commit. Subjects are `area: summary` (`ui:`, `repo:`, `close:` ...); the
body says why. Release notes are built from them.

## Releases

One command, from anywhere:

```
gh workflow run release.yml --repo cookiestroke/opsquire -f bump=patch     # or minor, major
```

The [release workflow](.github/workflows/release.yml) runs the full CI on `main`, bumps the version in
`pyproject.toml` with a signed `release: vX.Y.Z` commit, tags it, builds the sdist and wheel from the tag, and
publishes a GitHub release whose notes list the commits since the previous release grouped by area, with the
wheel attached. `opsquire --version` shows the installed version. Nothing is published outside GitHub.

## License

[Apache-2.0](LICENSE).
