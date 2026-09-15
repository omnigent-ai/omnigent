# Optional capabilities

## Sub-features

- Projects and project-grouped sessions
- Automations / Scheduled Tasks
- Managed sandboxes and host choices
- Smart routing, dictation, auth/admin, and workspace-wide sharing

## How to get to it

Capabilities appear as routes, navigation items, composer controls, or settings
only when the server advertises and supports them.

## Driving it with Playwright

For enabled behavior, run the focused journey, such as
`scripts/verify.sh automations`, and read back the REST side effect. For disabled
behavior, start a server with the capability absent or false and assert the
navigation or control is absent or disabled and the unsupported endpoint is not
called. Test an older `/v1/info` payload with the field omitted.

The observable end state is a three-way invariant:

1. `/v1/info` advertises the capability accurately.
2. The UI safely defaults when the field is absent and exposes no dead affordance.
3. The server endpoint enforces the same state.

## Gotchas

- Managed Databricks deliberately disables Automations because no scheduled-task
  store, scheduler, or router is wired.
- Projects downstream are structural: advertise them only when the router or
  store exists.
- SAFE and preview flags are per request; avoid process-global assumptions.
- Adding an optional feature must not add eager startup requests, large initial
  chunks, or blocking render work when the feature is disabled.
