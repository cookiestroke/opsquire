# Opsquire

Agents prepare the scripts, you run them, and every run is recorded for the next agent.

A squire prepares the arms; the knight acts. Opsquire lets AI coding agents do the preparation
for operational work (finding or writing the right script, linting it, planning the order)
while you stay the one who runs anything that touches production, with one command per step.
Every run leaves a structured, agent-readable record that agents cannot forge.

## Install

```
pip install -e .            # from a checkout; needs ruff, mypy, shellcheck on PATH for the lint gate
opsquire home ~/ops-home    # where tasks and run records live
```

## Daily use

```
opsquire ask "<what you want>"     an agent finds or writes the task and prints what to run
opsquire find <words>              is there already a script for this?
opsquire run <plan>                several tasks in order, one command (dry first, then --apply)
opsquire run <task>                dry run (or read-only run)
opsquire run <task> --apply        the real thing; asks before each change
opsquire last                      what just happened
opsquire runs --status failed      what went wrong, across everything
opsquire log <task>                full output of the latest run
opsquire wait <task>               (for agents) block until your next run of a task finishes
opsquire locks / unlock <target>
opsquire integrate                 install the opsquire skill for Claude agents; make the home a local git repo
opsquire ask --no-split "<...>"    one agent only (asks split into parallel pieces by default)
opsquire ask --max-agents 2 ...    at most 2 agents at once; --budget 20 [--strict-budget] for the spend
opsquire repo checkout|pr ...      (for agents) change a registered repo through a GitHub-signed PR
```

Ctrl-C is always safe: the run is recorded as `interrupted`, scratch work is undone, locks are
released, and nothing else is left behind.

The full contract agents follow is in [src/opsquire/data/AGENTS.md](src/opsquire/data/AGENTS.md);
`opsquire contract --write` installs it, plus the home's `LOCAL.md` rules, as the home's `AGENTS.md`.

## Development

```
python3 tests/test_opsquire.py -v
ruff check . && ruff format --check .
mypy src tests
(cd src/opsquire/data/helpers && mypy --config-file ../mypy.ini opsquire_helpers.py)
(cd src/opsquire/data/templates && MYPYPATH=../helpers mypy --config-file ../mypy.ini main.py)
shellcheck -x -P src/opsquire/data/helpers src/opsquire/data/helpers/opsquire.sh src/opsquire/data/templates/main.sh
```

Standard library only, Python 3.10+.
