# Experimental dynamic workflows

Run a DAG of agent tasks with prerequisites, accepted outcomes, review gates and
bounded follow-up tasks. Each task member gets its own session; an owner keeps
its session across consultation, validation failures and repairs. Independent
branches run concurrently within a configurable limit.

The module CLI is disabled by default. Enable it explicitly in the controller shell:

```sh
export OMNIGENT_FEATURES=dynamic_workflows
```

Add the name to the comma-separated list if other features are enabled. An explicit
`-dynamic_workflows` disables it. The Python library is available for explicit
integrations. This package does not start a background scheduler in ordinary servers.

## Try without credentials

From the repository root:

```sh
uv run --no-sync python -m omnigent.workflows \
  examples/dynamic-workflows/workflow.yaml --validate
uv run --no-sync python -m omnigent.workflows \
  examples/dynamic-workflows/workflow.yaml --simulate
```

The simulation returns success with four tasks, including an inserted follow-up.
It creates no remote agents or compute and does not execute local command gates.

Try review and repair with a fresh state directory:

```sh
WORKFLOW_STATE=$(mktemp -d /tmp/workflow.XXXXXX)
uv run --no-sync python -m omnigent.workflows \
  examples/dynamic-workflows/owner-workflow.yaml --simulate --state "$WORKFLOW_STATE"
uv run --no-sync python -m omnigent.workflows --state "$WORKFLOW_STATE" \
  --event examples/dynamic-workflows/failed-check.json
uv run --no-sync python -m omnigent.workflows --resume --state "$WORKFLOW_STATE"
uv run --no-sync python -m omnigent.workflows --state "$WORKFLOW_STATE" \
  --event examples/dynamic-workflows/passed-check.json
uv run --no-sync python -m omnigent.workflows --resume --state "$WORKFLOW_STATE"
```

The first two runs return exit code 3 (waiting). The final run returns 0. The
`implement` owner keeps its session, repairs candidate one into candidate two,
and releases `summarize` only after review passes. Inspect with `--inspect --state`.
Exit code 1 means execution failed; 2 means invalid CLI usage.

## Real agent execution

Copy `arclet-workflow.yaml`, replace the registered agent ID and set the server URL:

```sh
uv run --no-sync python -m omnigent.workflows /path/to/workflow.yaml \
  --backend arclet --server https://your-server.example --state /tmp/new-workflow-run
```

The Arclet adapter uses the server's existing managed sandbox provider and creates
one sandbox per task member. `OMNIGENT_WORKFLOW_ARCLET_PROVIDER` can override the
provider name advertised by a deployment. Generic `--backend sessions` also supports
other managed providers and connected hosts with an explicit `host_id` and absolute
workspace path. Managed workspaces are repository URLs cloned separately for each member.

Authentication reuses the existing login for that URL or `OMNIGENT_REMOTE_AUTH_TOKEN`.
For a Databricks deployment, explicitly set `OMNIGENT_WORKFLOW_DATABRICKS_PROFILE`
to a logged-in CLI profile. All members share its token cache and refresh; an invalid
explicit profile fails instead of switching identities. Credentials do not go into
workflow definitions or checkpoints. Runtime-side model/tool credentials remain the
managed provider's responsibility; certificate renewal during execution is not covered.

`strategy: parallel` starts every member and then continues the lead with their
contributions. `strategy: owner` starts the lead and only launches specialists
when it requests them. Owner work, consultation and repair each consume a round;
`max_rounds` bounds the total. The unused specialists consume no sessions.

## Outcomes and outputs

Owners return JSON with a nonempty `summary`, `status` (`ready`, `waiting` or
`consult`), optional `outputs`, revisioned `artifacts`, and optional `add_nodes`.
A consultation names members in `consult_members`; its summary gives their assignment.
A waiting result describes the input needed. Only a ready candidate can expand the DAG.

Named outputs contain JSON values up to 64 KiB per candidate. After all review gates
pass, dependants receive `dependency_results.<task_id>` with `status`, `summary`,
`outputs` and `artifacts`. Rejected outputs remain withheld. Accepted outputs survive
restart. Files/code must be published where other agents can fetch them: a local
Arclet path is not shared. Git artifacts use a repository URL and full published
commit SHA; consumers must verify the exact revision. References do not transfer
files or merge branches, and the controller does not verify remote content.

External review events name the exact node, candidate and gate. For the Arclet example:

```json
{"id":"review-1","kind":"gate_result","node_id":"propose","candidate_id":"propose-1","gate_id":"review","outcome":{"passed":true,"detail":"Inspected the stated revision"}}
```

Save it as a file, submit with `--event FILE --state DIRECTORY`, then `--resume`.
Stale or conflicting events are rejected. Command gates run on the controller host
in an explicit absolute directory, receive candidate JSON on stdin, and pass on
exit 0. Only user-authored definitions can introduce command gates. A crash can
repeat a command check, so checks must tolerate reruns.

## Recovery and limits

`--state` writes atomic checkpoints and a durable event inbox. One controller may
own a directory. Completed turns and sessions are reused; unknown remote outcomes
are parked for explicit `resolve_turn` reconciliation rather than replayed. Inspect
the session and submit its actual output before resuming. Missing saved sessions
fail rather than launching replacements. A create timeout can leave a remote session
whose ID was never returned; locate it before reconciliation.

Cancellation attempts to interrupt known sessions and records failures. It does not
delete their sandboxes. Provider idle lifecycle remains in effect. Stopping a
controller or disabling the feature does not guarantee that remote agents stopped.
Checkpoints contain prompts and outputs; keep them private.

This is a single-controller prototype with bounded graph expansion, not a distributed
scheduler. Completed tasks cannot be reopened. `parent` records containment without
adding aggregate completion semantics. MAR execution is deferred.

## Verification

```sh
uv run --no-sync pytest tests/workflows --reruns 0 -q
uv run --no-sync pytest tests/e2e/test_remote_url_chat_fresh_session_2437.py \
  -k 'test_run_launches_a_runner_on_this_machines_host and workflow' --reruns 0 -q
```

The process scenarios use real local server/host/runner processes with scripted
model replies, covering owner-only, parallel and specialist-on-request execution,
repair in the same session across CLI restarts and downstream release.

The opt-in live test checks two contributing agents and a downstream consumer on
three distinct Arclets, verifies outputs and cleans up only its own compute:

```sh
OMNIGENT_WORKFLOW_ARCLET_URL=https://your-server.example \
OMNIGENT_WORKFLOW_LIVE_AGENT_ID=your-registered-agent-id \
uv run --no-sync pytest tests/e2e_live/test_workflow_arclets_live.py --reruns 0 -q -s
```

It requires a valid server login and an authenticated `arclet list --json` for the
same user. It allocates compute and calls models, so it is excluded from normal CI.
A passing mocked test does not establish compatibility with a live deployment.
