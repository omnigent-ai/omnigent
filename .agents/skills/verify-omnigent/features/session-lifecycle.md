# Start and manage sessions

## Sub-features

- New-chat agent, host, directory, model, permission, and worktree choices
- Sidebar selection, rename, pin, archive, search, stop, and reconnect
- Fork, clone, agent switch, projects, and deep links
- Local, connected-host, and managed-sandbox session types

## How to get to it

Open `/`, configure the landing composer, and send the first prompt. Manage the
created session from the sidebar or chat header.

## Driving it with Playwright

Run `scripts/verify.sh core` for the broad path, then select focused tests under
`tests/e2e_ui/start_session`, `sessions`, `fork_session`, and `agent_switch`.
Prefer existing `data-testid` controls such as
`new-chat-landing-agent-select`; verify the create request parameters and final
`/c/{id}` route. For real creation changes, do not stop at a route-stubbed test.
Include a real server and runner-bound journey.

The observable end state is the intended session selected with the correct
server-side metadata and a usable composer or explicit read-only/offline state.

## Gotchas

- The standard fixture runner is not a registered host; directory-picker tests
  may stub host filesystem calls.
- Managed Databricks sessions add launcher and Lakebox client timelines.
- Do not use hot reload while turn-testing a managed session; it drops the
  runner tunnel.
- New launch fields must be optional across old clients, servers, and runners.
