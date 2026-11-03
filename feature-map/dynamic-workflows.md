# Experimental dynamic workflows

Design and test a DAG of agent tasks in an opt-in UI, or execute it through the CLI
and explicit Python integration.
Review revisioned candidates, reuse owners across repairs and pass accepted outputs
to downstream tasks with checkpoints for restart and reconciliation.

## Sub-features

- `opt-in`: CLI, API/service, sidebar and direct route require `OMNIGENT_FEATURES=dynamic_workflows`.
- `design`: add task dialogs, per-task agents, branches/joins and removable arrows.
- `export`: validated YAML usable by the CLI and API.
- `validate`: reject cyclic or invalid graphs and bounded expansions.
- `execute`: isolated member sessions with parallel or owner-led execution.
- `review`: accept exact candidate revisions, or send feedback for repair.
- `outputs`: pass accepted JSON outputs and artifact references to dependants.
- `recover`: reuse checkpoints and park uncertain remote actions for reconciliation.

## How to get to it (user POV)

- Enable `OMNIGENT_FEATURES=dynamic_workflows` in the CLI shell or server process.
- Desktop: choose **Workflows** in the sidebar.
- Browser/mobile: open `/workflows` directly; embedded apps prepend their configured basename.
- Designer: add tasks, configure their agents, connect prerequisites, and export YAML.
- Test view: choose Simulation or Task agents, then **Test workflow**.
- API: `/v1/workflows` exposes options, definition compilation, create/inspect, events, stop and resume.
- Validate: `python -m omnigent.workflows FILE --validate`.
- Simulate: `python -m omnigent.workflows FILE --simulate --state DIRECTORY`.
- Execute: `python -m omnigent.workflows FILE --backend arclet --server URL --state DIRECTORY`.
- Connected hosts or other managed providers: use `--backend sessions`.
- Inspect/resume: `python -m omnigent.workflows --inspect|--resume --state DIRECTORY`.
- Review: `python -m omnigent.workflows --event FILE --state DIRECTORY`, then resume.
- Python: import `WorkflowRunner` and `RunStore` from `omnigent.workflows`; supply an executor.

## Driving it with the repro environment

Preconditions: read [Verify Omnigent](skills/verify-omnigent/SKILL.md). Component tests
need no environment; the process scenario owns its isolated server and host. Read
`examples/dynamic-workflows/README.md` for manual inputs and expected exit codes.

- Designer entry points (own environment): `tests/e2e_ui/scheduled/test_dynamic_workflows.py`
  builds the graph, edits arrows, exports and downloads YAML, exercises review/repair
  and checks downstream outcomes. Desktop sidebar and direct mobile runs cover
  enabled and disabled servers. Run with `--ui-skip-build --video=on` after building.
- Server-prefixed routing and loading/disabled gates: `web/src/App.test.tsx`.
  This alias is component-tested; it has no separate live browser proof.
- Form state and execution defaults: `web/src/pages/WorkflowsPage.test.tsx` and
  `web/src/pages/workflows/workflowDesign.test.ts`.
- API validation, owner isolation and stop/resume: `tests/workflows/test_service.py`.
- No service or API when disabled: `tests/server/integration/test_utility_endpoints.py`.
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
- Explicit live provisioning check: `tests/e2e_live/test_workflow_arclets_live.py`.
  It needs an authenticated server, registered agent and Arclet CLI. It proves
  three distinct cloud environments, multi-agent synthesis, output handoff and cleanup.

## Gotchas

- All user-facing entry points are disabled by default, even for simulation.
  Python integration is explicit. Restart the server after changing flags.
- Export YAML to save a design; unsaved drafts do not survive reload.
- Web live execution uses one configured operator connection and an explicit owner;
  other users can still simulate. Local single-user mode permits the local user.
- Simulation does not allocate agents/compute or run local commands.
- Owner strategy starts specialists only on request. Parallel starts every member.
- Review events bind to a candidate; failed/unreviewed output never releases dependants.
- Artifacts reference published revisions; they do not copy files between sandboxes.
- Unknown remote outcomes require explicit reconciliation. Completed tasks stay terminal.
- A controller crash or cancellation can leave remote work running. Inspect its sessions.
- Credentials remain outside definitions; checkpoints still contain prompts and outputs.
- This is a single-controller experiment. MAR and a distributed scheduler are deferred.
