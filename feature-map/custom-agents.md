# Custom agents

Custom agents settings lists server and owned agents, saves reusable agents
without starting a session, shows their summaries, and deletes owned agents.

## Sub-features

- `list`: server cards, owned rows, search of loaded results, and pagination.
- `create`: name, description, harness, model, instructions, and MCP servers.
- `review`: read-only summary, including version, dates, MCP servers, and skills.
- `delete`: confirmation, in-use warning, explicit forced removal, and errors.
- `gating`: hidden navigation and General fallback without both capabilities.
- `states`: loading, empty, failed requests with retry, and missing agents.

## How to get to it (user POV)

- Desktop: open Settings, then Customize → Custom agents in the sidebar.
- Mobile: open Settings, then Customize → Custom agents in the settings navigation.
- From the list: Create opens the form; an agent name opens its summary.
- Direct links: `/settings/custom-agents`, `/settings/custom-agents/new`, and
  `/settings/custom-agents/<agent-id>` open those same pages.
- Creation and review pages: Custom agents returns to the list; Cancel also
  returns from creation. Delete is available in an owned agent's summary.

## Driving it with the repro environment

Preconditions: follow [Verify Omnigent](skills/verify-omnigent/SKILL.md). Enable
`OMNIGENT_FEATURES=custom_agents_settings_ui` before starting the isolated server.
The server must implement agent installation (PR #8677) and advertise
`agent_install: true`. Main without that dependency only exercises gating.

- Desktop and mobile: open Settings through navigation and select Custom agents.
  Confirm Customize contains Harnesses and Custom agents, and General no longer
  contains Harnesses. Confirm server cards and the empty owned state; capture
  both viewport sizes.
- Create: enter a unique name, a configured model, instructions, and an MCP
  server. Save, verify the summary, reload, and verify persistence through
  `GET /v1/agents?scope=user`. Session count must remain unchanged. Return to
  the list and find the agent using search. Cancel a second draft.
- Review and direct links: follow a server card and an owned row; open their
  URLs afresh. Server summaries have no Delete action. An unknown ID eventually
  shows a missing-agent message. Test Create and Back from direct links too.
- Delete: cancel once and verify the agent remains, then delete and verify the
  list and API omit it. For an agent used by a session, verify the initial
  deletion reports the count and waits for Remove anyway. Only that action
  should force removal.
- Gating: remove the feature flag, restart the isolated server, and reload.
  Customize must disappear, Harnesses must return to General, and Custom agents
  URLs must show General.
  Repeat against a server without the installation capability.
- Component checks for duplicate names, failed save/delete, empty continuation
  pages, and capability gating:
  `pnpm --filter web test src/pages/settings/SettingsCustomAgentsSection.test.tsx src/shell/settingsNav.test.tsx src/lib/capabilities.test.ts`.
- The shared new-session creation dialog regression is covered by
  `tests/e2e_ui/start_session/test_create_custom_agent.py`; run it through the
  isolated environment with browser recording as described in the skill.

## Gotchas

- On mobile, opening a Settings URL afresh shows the existing full-screen
  settings navigation first. Select Custom agents, then Create or an agent row;
  links within the section keep the content visible.
- Saving an existing owned name replaces its entire configuration and affects
  sessions using it. Use a unique name when creating a separate agent.
- Summary data cannot safely populate an editor; editing is deferred.
- Search covers loaded results. Load more to include older agents.
- A detail bookmark may need several list requests, including empty pages with
  continuation cursors. Duplicate names are separate rows identified by ID.
- Built-in harness wrappers belong in Harnesses settings. The older Create
  custom agent dialog in new-session composition still starts a session.
