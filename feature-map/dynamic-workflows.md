# Experimental dynamic workflows

Run a DAG of agent tasks through an opt-in CLI or explicit Python integration.
Review revisioned candidates, reuse owners across repairs and pass accepted outputs
to downstream tasks with checkpoints for restart and reconciliation.

## Sub-features

- `opt-in`: module CLI requires `OMNIGENT_FEATURES=dynamic_workflows`.
- `validate`: reject cyclic or invalid graphs and bounded expansions.
- `execute`: isolated member sessions with parallel or owner-led execution.
- `review`: accept exact candidate revisions, or send feedback for repair.
- `outputs`: pass accepted JSON outputs and artifact references to dependants.
- `recover`: reuse checkpoints and park uncertain remote actions for reconciliation.

## How to get to it (user POV)

- Enable `OMNIGENT_FEATURES=dynamic_workflows` in the CLI shell.
- Validate: `python -m omnigent.workflows FILE --validate`.
- Simulate: `python -m omnigent.workflows FILE --simulate --state DIRECTORY`.
- Execute: `python -m omnigent.workflows FILE --server URL --state DIRECTORY`.
- Choose a connected host or a managed sandbox provider in each member's placement.
- Inspect/resume: `python -m omnigent.workflows --inspect|--resume --state DIRECTORY`.
- Review: `python -m omnigent.workflows --event FILE --state DIRECTORY`, then resume.
- Python: import `WorkflowRunner` and `RunStore` from `omnigent.workflows`; supply an executor.

## Driving it with the repro environment

Preconditions: read [Verify Omnigent](skills/verify-omnigent/SKILL.md). Component tests
need no environment; the process scenario owns its isolated server and host. Read
`examples/dynamic-workflows/README.md` for manual inputs and expected exit codes.

- Graphs, scheduling and expansion: `tests/workflows/test_workflows.py`.
- Candidate review, outputs, consultation and repair: `tests/workflows/test_ownership.py`.
- Checkpoints and reconciliation: `tests/workflows/test_recovery.py`.
- Local checks: `tests/workflows/test_gates.py`.
- Opt-in and module CLI: `tests/workflows/test_cli.py`.
- Session transport, native output and isolation: `tests/workflows/test_sessions.py`.
- Runtime selection and shared login: `tests/workflows/test_execution.py`.
- Real local processes with scripted models: `tests/e2e/test_remote_url_chat_fresh_session_2437.py`,
  `test_run_launches_a_runner_on_this_machines_host`, variants `workflow`,
  `workflow-parallel` and `workflow-consult`. They verify concurrency, owner reuse,
  repair after restart and downstream release using existing process primitives.
- Explicit live provisioning check: `tests/e2e_live/test_workflow_sandboxes_live.py`.
  It needs an authenticated server, registered agent and configured sandbox provider.
  It verifies three distinct managed hosts, synthesis, output handoff and host removal.
  It does not independently inspect cloud resource termination.

## Gotchas

- The CLI is disabled by default, even for simulation. Python integration is explicit.
- Simulation does not allocate agents/compute or run local commands.
- Owner strategy starts specialists only on request. Parallel starts every member.
- Review events bind to a candidate; failed/unreviewed output never releases dependants.
- Artifacts reference published revisions; they do not copy files between sandboxes.
- Unknown remote outcomes require explicit reconciliation. Completed tasks stay terminal.
- A controller crash or cancellation can leave remote work running. Inspect its sessions.
- Credentials remain outside definitions; checkpoints still contain prompts and outputs.
- This is a single-controller experiment. MAR and a distributed scheduler are deferred.
