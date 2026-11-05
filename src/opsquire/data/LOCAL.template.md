The operator is <your name>. These are the rules of this home; `opsquire contract --write` appends them to the
agent contract in `AGENTS.md`. Keep every rule a decided statement, not a wish, and delete what does not apply.

### What is script-shaped here

Access rules always win: if a sandbox or a company policy blocks something, it is a task, whatever the rest of
this list says. Never work around a block.

A task the operator runs, if any of these hold:
1. It needs what agents cannot reach: ssh to hosts, production networks or databases, the credentials file.
2. It changes a live system: service restarts, deployments, config changes on hosts, database writes.
3. It acts under the operator's identity where a person, not an agent, should act.
4. It is an operational procedure likely to happen again, where the record matters.

Done directly by the agent (sanctioned): local code, tests, lint and analysis of local files; version control
through the operator's usual path; documentation and research.

Grey zone: read-only queries against production are read-only tasks (the record helps, and the credentials stay
with the operator); the second time, make them a general script. When unsure, ask in one line with a
recommendation.

### Areas and credentials

Areas (the first part of every task id) are hosts, services or systems: name the ones used here, for example
`web1`, `db`, `k8s`. Credentials live in the operator's credentials file (`~/.creds` unless `OPSQUIRE_CREDS` says
otherwise): a JSON object, nested as you like, read only by the runner. A task declares what it needs as
`--cred ENV=dotted.key` (for example `--cred PG_PASSWORD=postgres.prod.password`) and gets it as an environment
variable at run time; the runner redacts every known value from the output. Agents ask for key names, never
values.

### Tools

Name the tools agents may use directly, and any they must not (for example an agent framework whose approval
rules this home has not reviewed).
