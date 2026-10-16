# Custom agents

Custom agents settings lists server and owned agents, saves reusable agents
without starting a session, shows their summaries, edits owned configurations,
and deletes owned agents.

## Sub-features

- `list`: server cards, owned rows, search of loaded results, and pagination.
- `create`: name, description, harness, model, instructions, and MCP servers.
- `review`: configuration preview, including version, dates, MCP servers, and skills.
- `edit`: partial configuration updates, explicit credential replacement/removal,
  dirty-draft confirmation, error retention, and stale-revision reload.
- `delete`: confirmation, in-use warning, explicit forced removal, and errors.
- `gating`: hidden navigation without the feature/install gates; an update notice
  and disabled management when single-agent retrieval is unsupported.
- `states`: loading, empty, failed requests with retry, and missing agents.

## How to get to it (user POV)

- Desktop: open Settings, then Customize → Custom agents in the sidebar.
- Mobile: open Settings, then Customize → Custom agents in the settings navigation.
- From the list: Create opens the form; an owned row opens its configuration form,
  and a server card opens its read-only summary.
- Direct links: `/settings/custom-agents`, `/settings/custom-agents/new`, and
  `/settings/custom-agents/<agent-id>` open those same pages.
- Creation and review pages: Custom agents returns to the list; Cancel also
  returns from creation and configuration review. Owned agents open directly in
  an editable form with Save changes and Delete when the server supports them.

## Driving it with the repro environment

Preconditions: follow [Verify Omnigent](skills/verify-omnigent/SKILL.md). Enable
`OMNIGENT_FEATURES=custom_agents_settings_ui` before starting the isolated server.
The server must advertise `agent_install: true` and `agent_detail: true`.
Deletion on a detail page also needs the response to include `user_owned`. Editing
requires `user_owned: true` and `agent_config_edit: true`.

- Desktop and mobile: open Settings through navigation and select Custom agents.
  Confirm Customize contains Harnesses and Custom agents, and General no longer
  contains Harnesses. Confirm server cards and the empty owned state; capture
  both viewport sizes.
- Create: enter a unique name, a configured model, instructions, and an MCP
  server. Save, verify the summary, reload, and verify persistence through
  `GET /v1/agents/<id>`. Session count must remain unchanged. Return to
  the list and find the agent using search. Cancel a second draft.
- Review and direct links: follow a server card and an owned row; open their
  URLs afresh. Server summaries, including operator-added agents, have no Delete
  action. Each cold bookmark requests one agent directly without fetching list
  pages. An unknown ID shows a missing-agent message; a failed request offers
  Retry. Test Create and Back from direct links too.
- Edit: open an owned agent, change Description in the page form, and Save changes.
  Confirm the same form shows the saved description, unchanged instructions, and
  configured credential names. Changing a URL must preserve hidden credentials; replacing or
  removing a credential requires an explicit action. Existing server names and
  transports cannot be changed; managed servers are read-only.
  Cancel a dirty draft and choose Keep editing, then cancel/discard it. Confirm
  Custom agents also asks before leaving a dirty draft, and no edit modal opens.
  Change the same agent in another tab before saving: the first draft remains
  visible with Reload latest. Confirm reload, then verify the newer value.
  `tests/e2e_ui/sessions/test_custom_agent_edit.py` covers desktop and mobile
  persistence and conflict recovery against the real API; it enables only the UI
  release flag in the info response. Cold desktop bookmarks and mobile list entry
  are covered. Manually check desktop list entry and normal Settings navigation.
- Delete: cancel once and verify the agent remains, then delete and verify the
  list and API omit it. For an agent used by a session, verify the initial
  deletion reports the count and waits for Remove anyway. Only that action
  should force removal.
- Gating: remove the feature flag, restart the isolated server, and reload.
  Customize must disappear, Harnesses must return to General, and Custom agents
  URLs must show General.
  Repeat against a server without the installation capability. With the feature
  and installation enabled but `agent_detail` absent or false, every Custom
  agents URL must show an update notice with no agent requests or management
  controls. An older detail response missing `user_owned` must hide Delete and
  explain that a server update is required. With `agent_config_edit` absent/false,
  retain create/review/delete but show a read-only summary and an editing update
  notice.
- Component checks for duplicate names, failed save/delete, direct lookup,
  request errors, and capability gating:
  `pnpm --filter web test src/pages/settings/SettingsCustomAgentsSection.test.tsx src/shell/settingsNav.test.tsx src/lib/capabilities.test.ts`.
- The shared new-session creation dialog regression is covered by
  `tests/e2e_ui/start_session/test_create_custom_agent.py`; run it through the
  isolated environment with browser recording as described in the skill.

## Gotchas

- On mobile, Custom agents links open the content directly. The header Back
  button opens the settings navigation; the form's Custom agents button returns
  to the agent list and confirms unsaved changes.
- Saving an existing owned name replaces its entire configuration and affects
  sessions using it. Use a unique name when creating a separate agent.
- Owned previews load the configuration endpoint and ETag; summaries never prefill it.
  Missing revision headers prevent saving. Unchanged fields are omitted from PATCH.
- Global browser history and settings navigation can discard a draft; Cancel and
  the form's Custom agents button confirm first, and refresh/close warns.
  Model/harness incompatibilities are reported on save.
- Search covers loaded results. Load more to include older agents.
- A detail bookmark uses the single-agent endpoint, independently of list
  pagination. Duplicate names are separate rows identified by ID.
- Built-in harness wrappers belong in Harnesses settings. The older Create
  custom agent dialog in new-session composition still starts a session.
