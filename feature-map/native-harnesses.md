# Native harnesses

Omnigent runs twelve vendor coding CLIs as native harnesses. A user can start
each one from the web new-session picker or from the command line with
`omnigent <name>`, then chat with it in Omnigent while its own terminal runs
alongside. The harnesses share one set of user journeys (launch, sign-in state,
model and effort choice, approvals, resume, terminal, and cleanup) but each
implements them separately, so a fix for one harness does not reach the others.

## Sub-features

- `launch-web`: pick the harness in the new-session composer and start a session.
- `launch-cli`: `omnigent <name>` starts the harness in the user's terminal with
  an Omnigent session behind it.
- `needs-auth`: a harness without usable credentials is shown as needing
  sign-in, with a repair hint, instead of failing after launch. A harness the
  host has not configured can be hidden from the picker.
- `model-and-effort`: the harness's own model catalog, and reasoning effort where
  the harness declares it. The web picker should offer what the CLI offers.
- `approvals`: tool calls the harness gates show an approval card in chat, and
  the answer reaches the harness.
- `resume`: resume a previous conversation from the CLI (`--resume`, or a bare
  `--resume` picker that lists only this host's sessions) or by reopening it.
- `steer`: sending while the harness is mid-turn steers the active turn.
- `chat-render`: the harness's output renders in chat like other harnesses.
- `side-chat`: Codex forks an ephemeral native thread through `/side`; follow-ups
  stay in that thread, and closing it leaves the parent usable.
- `cleanup`: stopping, cancelling, or idling a session reaps the harness's
  helper processes and per-session files.
- `disconnect`: startup waits and active operations settle when their native
  connection ends; reconnect can receive fresh events. Distinguish a native
  CLI disconnect, a runner going offline, and a browser stream reconnect.

- `launch-settings`: Settings → Harnesses → a configured Claude or Codex →
  Settings (or its card's gear). One Startup configuration block shows Command,
  Environment, and Arguments, unmasked and read-only, with the selected host's source.
  Env wrappers are split into these fields, without a duplicate raw invocation.
  Session and workspace config can add to or override these host defaults.
  Other harnesses keep their credential card only.
- `skill-contents`: open plain or plugin skills to read their SKILL.md markdown,
  with loading, truncation, unavailable-host, and older-server states.

- `mcp-tools`: expand a configured or plugin MCP server to probe its tools,
  with connected/auth/timeout/unreachable/unsupported and mixed-version states.

- `plugin-inventory`: installed Claude plugins, including disabled and hook/command-only plugins, report metadata and bundled skills/MCPs in Settings → Harnesses.
- `harness-settings-navigation`: Harnesses is always available in Settings and
  through direct links. The import review modal's See more opens Harnesses and
  dismisses the modal. Import sessions contains only session imports.

## How to get to it (user POV)

**Web:** start a new session, choose the harness in the harness picker, open its
configuration for model and effort, and send. Approval cards and the Terminal
view appear in the session.

**CLI:** run `omnigent <name>` from the matrix below; add `--resume` with or
without a session ID to resume.

**Skill contents:** Settings → Harnesses → configured harness card (or gear),
then Skills → a skill, or Plugins → a plugin → a skill. Back returns to the
list or plugin.

**MCP tools:** Settings → Harnesses → configured harness card (or gear),
then MCP servers → expand a server, or Plugins → plugin → MCPs → expand.
Probes run only on expansion.

**Harness settings navigation:** Settings sidebar → Harnesses, a direct link to
`/settings/harnesses` or its harness detail pages, or the import review modal
shown for a newly connected or requested host → See more.

**Interrupted session:** observe startup before the first message, a running
turn, and Stop separately. For an offline host use the reconnect paths in
[sessions](./sessions.md); a detached terminal has its own paths in
[terminals](./terminals.md).

**Codex side chat:** type `/side <question>` in the parent composer, send a
follow-up in the side pane, then close its tab. The parent conversation stays
separate. See [sessions](./sessions.md) for all side-chat entry points.

**Matrix.** "Mock" means the verification instance can drive the harness with
the mock model; the others need their real CLI and vendor credentials. Test
columns name one journey test per harness; "—" means none exists yet.

| Harness | CLI | Mock | Chat render test | Other journey test | Dev skill |
|---|---|---|---|---|---|
| `antigravity-native` | `omnigent antigravity` or `omnigent agy` | no | — | tests/e2e/test_antigravity_native_isolated_hooks_e2e.py::test_dispatched_agy_session_loads_user_hooks | [antigravity-native-e2e-dev](../.claude/skills/antigravity-native-e2e-dev/SKILL.md) |
| `claude-native` | `omnigent claude` | yes | tests/e2e_ui/messages/test_native_claude_render_parity.py::test_native_claude_message_render_parity | tests/e2e/test_claude_native_cli_resume_e2e.py::test_claude_native_cli_resume_restores_history | — |
| `codex-native` | `omnigent codex` | yes | tests/e2e_ui/messages/test_native_codex_render_parity.py::test_native_codex_message_render_parity | tests/e2e/test_codex_native_cli_resume_e2e.py::test_codex_native_cli_resume_restores_history | — |
| `cursor-native` | `omnigent cursor` | no | tests/e2e_ui/messages/test_native_cursor_render_parity.py::test_native_cursor_message_render_parity | tests/e2e/test_cursor_native_cli_e2e.py::test_cursor_native_cli_smoke | — |
| `devin-native` | `omnigent devin` | no | — | tests/e2e_ui/chat/test_devin_native_picker.py::test_devin_picker_offers_its_own_models_and_effort | — |
| `goose-native` | `omnigent goose` | no | tests/e2e_ui/messages/test_native_goose_render_parity.py::test_native_goose_message_render_parity | tests/e2e/test_goose_native_cli_e2e.py::test_goose_native_cli_smoke | — |
| `hermes-native` | `omnigent hermes` | no | tests/e2e_ui/messages/test_native_hermes_render_parity.py::test_native_hermes_message_render_parity | tests/e2e/test_hermes_native_policy_hook_path_e2e.py::test_hermes_native_policy_hook_path_lets_the_tool_run | — |
| `kimi-native` | `omnigent kimi` | no | — | tests/e2e/test_kimi_native_steering_e2e.py::test_midturn_steer_is_applied_not_queued | — |
| `kiro-native` | `omnigent kiro` | no | tests/e2e_ui/messages/test_native_kiro_render_parity.py::test_native_kiro_message_render_parity | tests/e2e/test_kiro_native_cli_e2e.py::test_kiro_native_cli_smoke | — |
| `opencode-native` | `omnigent opencode` | no | — | tests/e2e/test_opencode_native_startup_cancel_leak_e2e.py::test_opencode_native_startup_cancel_reaps_serve | — |
| `pi-native` | `omnigent pi` | no | — | tests/e2e/test_pi_native_send_now_steer_e2e.py::test_pi_native_send_now_steers_into_active_turn | [pi-native-e2e-dev](../.claude/skills/pi-native-e2e-dev/SKILL.md) |
| `qwen-native` | `omnigent qwen` | no | — | tests/e2e/test_qwen_native_subagent_wake_e2e.py::test_qwen_native_subagent_completion_wakes_parent | — |

## Driving it with the repro environment

Preconditions: a running instance (`verify-env start`, then `verify-env
doctor`) and the built web UI. Only Claude and Codex are configured with the
mock model there. For any other harness, install its CLI, sign in with a test
account, and run its tests with plain `uv run pytest` instead, or follow its dev
skill when one is linked above.

```sh
verify-env run -- python -m pytest <test> --ui-skip-build --video=on \
  --output="$VERIFY_EVIDENCE/native-harnesses"
```

**Launch settings (own environment):** connect a host with Claude/Codex
configured, and put a command and two args under
`harness.claude-native` / `harness.codex-native` in its `~/.omnigent/config.yaml`.
Open each harness through both its gear and card → Settings. Check one Startup
configuration block with Command, Environment, and Arguments, plus source and
credential. Repeat with `command: /usr/bin/env`
and args containing environment assignments before the wrapped command. Check
full override values, empty values, inheritance and `-i`/`-u` behavior. Values
must appear only once; no Configured invocation panel. Unknown env options must
keep the raw Command and Arguments and show an interpretation warning.
These are host defaults, not a running session's full command or environment.
Select a second host on the
grid and repeat. An older host shows an update message; an older server hides
the extra fields. Resolver and raw-tunnel checks:
`tests/host/test_harness_startup.py`,
`tests/server/integration/test_host_tunnel_route.py::test_startup_http_through_real_tunnel`.

Cross-harness journeys:

- **`side-chat`, Codex:**
  `tests/e2e_ui/chat/test_native_codex_side_chat.py::test_native_codex_side_chat_inherits_context_and_closes_independently`
  uses a real Codex CLI/app-server and the local mock model. It verifies inherited
  model context, distinct native threads on one runner, transcript isolation,
  follow-ups, and a working parent after side-chat closure.
- **`harness-settings-navigation`:** run
  `web/src/components/onboarding/ImportContextModal.test.tsx` and
  `web/src/pages/SettingsPage.test.tsx`, plus
  `web/src/shell/settingsNav.test.tsx` and
  `tests/e2e_ui/onboarding/test_import_review_modal.py::test_import_modal_opens_once_and_links_to_harnesses`.
  Start the isolated instance without release flags. Check the Harnesses sidebar
  link and direct links to the catalog and a harness detail page. Connect a new
  host, then click See more beside Confirm. Check that the modal closes and
  Harnesses opens. Open Import sessions and check that only session imports
  remain. Older clients connected to an upgraded server keep these entry points
  without deployment changes. The server response contract is covered by
  `tests/server/integration/test_utility_endpoints.py::test_info_returns_expected_fields`.

- **`needs-auth`:**
  `tests/e2e_ui/start_session/test_harness_credential.py::test_needs_auth_harness_is_disabled_with_repair_tooltip`,
  `tests/e2e_ui/chat/test_hide_unconfigured_harnesses.py::test_hide_unconfigured_harnesses_filters_the_picker`,
  `tests/e2e_ui/chat/test_hide_unconfigured_harnesses.py::test_hide_unconfigured_hides_a_harness_missing_from_the_host_map`
- **`model-and-effort`:**
  `tests/e2e_ui/start_session/test_native_picker_cli_parity.py::test_claude_picker_omits_aliases_the_cli_picker_does_not_offer`,
  `tests/e2e_ui/start_session/test_native_picker_cli_parity.py::test_codex_picker_offers_the_clis_catalog_and_default`;
  see also [composer](./composer.md) for effort.
- **`model-and-effort`, Codex runtime settings:**
  `tests/e2e/test_codex_native_supported_efforts_e2e.py::test_codex_clamps_unsupported_effort`,
  `tests/e2e/test_codex_native_supported_efforts_e2e.py::test_codex_preserves_supported_effort`,
  `tests/e2e/test_codex_native_supported_efforts_e2e.py::test_codex_model_switch_clamps_inherited_effort`,
  `tests/e2e/test_codex_native_supported_efforts_e2e.py::test_codex_combined_model_and_reset_uses_target_default`,
  `tests/e2e/test_codex_native_supported_efforts_e2e.py::test_codex_effort_reset_survives_next_turn`.
  These own their environment: run with plain `uv run pytest`. They drive a
  real Codex TUI and REST session settings, checking the outgoing Responses
  effort, native settings, private config, and session state. Only the model
  replies are mocked; models absent from the installed CLI are skipped.
  Existing-session cases also require the picker to show the applied effort
  when an unsupported request clamps back to the already active native value.
  Concurrent runner controls are covered by
  `tests/runner/test_app_sessions_native_events_lifecycle.py::test_codex_native_concurrent_settings_use_the_applied_model`
  (component test): an overlapping effort pick uses the newly applied model.
  `tests/runner/test_app_sessions_native_events_lifecycle.py::test_codex_native_effort_uses_the_applied_model_after_a_failed_mirror`
  and
  `tests/runner/test_app_sessions_native_events_lifecycle.py::test_codex_native_model_switch_inherits_an_effort_whose_config_write_failed`
  cover a model or effort whose private-config write failed: later updates
  still use it and retry the write until a terminal switch rewrites the config.
  Routed switches and turns read the same record
  (`tests/harnesses/codex_native/test_codex_native_hook.py::test_routed_model_switch_keeps_an_effort_whose_config_write_failed`).
- **`model-and-effort`, rejected Codex reset (server/runner integration):**
  `tests/server/integration/test_codex_effort_forward_failure.py::test_rejected_reset_returns_error_and_preserves_applied_settings`,
  `tests/server/integration/test_codex_effort_forward_failure.py::test_rejected_change_preserves_concurrent_selection_and_sibling_settings`,
  `tests/server/integration/test_codex_effort_forward_failure.py::test_rejected_change_keeps_an_effort_the_terminal_reported_meanwhile`,
  `tests/server/integration/test_codex_effort_forward_failure.py::test_rejected_change_restores_an_effort_the_terminal_reported_before_saving`,
  `tests/server/integration/test_codex_effort_forward_failure.py::test_refusal_after_the_runner_re_tunnelled_keeps_the_new_replicas_selection`,
  `tests/runner/test_app_sessions_native_events_lifecycle.py::test_codex_native_reset_without_a_current_model_is_rejected_before_connecting`,
  `tests/server/integration/test_codex_effort_forward_failure.py::test_successful_update_mirrors_unchanged_native_effort_without_notification`,
  `tests/server/integration/test_codex_effort_forward_failure.py::test_combined_model_and_effort_uses_target_model_capabilities`,
  `tests/server/integration/test_codex_effort_forward_failure.py::test_legacy_server_split_reset_uses_the_previous_model_default`,
  `tests/server/integration/test_codex_effort_forward_failure.py::test_legacy_combined_reset_failure_preserves_the_applied_model`,
  `tests/server/integration/test_codex_effort_forward_failure.py::test_forwarder_recovers_a_failed_immediate_effort_mirror`,
  `tests/server/integration/test_codex_effort_forward_failure.py::test_offline_or_silent_effort_change_is_saved_for_resume`,
  `tests/server/integration/test_codex_effort_forward_failure.py::test_overlapping_refused_changes_restore_the_applied_effort`,
  `tests/runner/test_app_sessions_native_workflow_messages.py::test_refused_codex_startup_effort_follows_the_server_rollback_contract`,
  `tests/runner/test_app_sessions_native_events_lifecycle.py::test_codex_native_settings_update_times_out_and_releases_the_lock`,
  `tests/server/integration/test_codex_effort_forward_failure.py::test_legacy_runner_refusal_keeps_the_effort_it_cached`,
  `tests/server/integration/test_codex_effort_forward_failure.py::test_unconfirmed_effort_update_is_kept_for_the_next_turn`,
  `tests/server/integration/test_codex_effort_forward_failure.py::test_lost_combined_change_restores_the_model_and_effort`.
  Run with plain `uv run pytest`. Both HTTP apps and persistence are real;
  Codex RPC failures inject missing defaults and discovery timeouts. A refused
  reset returns an error and preserves applied settings and concurrent edits:
  a save or terminal report after the change stays, and one before it is restored;
  a replica the runner has re-tunnelled away from re-addresses instead of rolling back;
  overlapping refused changes end on the last applied setting. Offline and
  silent explicit efforts are applied on resume; a saved Default is stored, but
  resume keeps the private config's effort.
  A combined model/effort PATCH uses the target model's capabilities. Older
  runners apply the model first and then the effort; if that second step
  fails, the error preserves the model already applied. A failed immediate
  mirror is retried when the forwarder next reads the private config.
  Target-model Default requires both the updated server and the updated runner.
  An older server resets the previous model first, even with an updated runner;
  its default is then inherited if the target supports it. On older servers,
  switch models first and select Default as a separate action afterward.
  While a connected runner is still starting Codex and has no loaded bridge,
  live settings return a retryable 503 and retain the previous selection.
  Retry once the terminal is ready; fully offline and silent saves remain deferred.
  An older server keeps such a refused change, so the updated runner applies it
  on the next turn; an older runner keeps it itself, so the updated server keeps
  it too. A hung connect fails after five seconds; a hung update is unconfirmed,
  so an effort or combined model/effort change is kept for the next turn instead
  of rolled back. A combined change that never reaches the runner restores both.
  An older server gets no early timeout; it waits for Codex as before
  (`tests/runner/test_app_sessions_native_events_lifecycle.py::test_codex_native_late_settings_ack_still_reaches_an_older_server`).
- **`approvals`:**
  `tests/e2e_ui/approvals/test_native_edit_tools_approval_card.py::test_native_file_edit_tools_require_approval_card`
- **`resume`, bare picker scoped to this host:**
  `tests/e2e/test_native_resume_picker_cross_host_e2e.py::test_bare_resume_picker_excludes_other_hosts_sessions`
- **`resume`, Codex persisted effort after a runner restart:**
  `tests/e2e/test_codex_native_supported_efforts_e2e.py::test_codex_resume_clamps_persisted_effort`
  (own environment, real Codex with mock model replies).
  `tests/harnesses/codex_native/app_server/test_reasoning_effort.py::test_resume_effort_update_times_out_and_closes_client`
  checks that a stalled settings connection, write, or close cannot block resume;
  `tests/harnesses/codex_native/app_server/test_reasoning_effort.py::test_resume_records_an_effort_its_config_write_lost`
  keeps a resumed effort whose config write failed for later updates.
- **`chat-render`, `steer`, per harness:** use the matrix.
- **`skill-contents`:** run `tests/host/test_skill_content.py`,
  `tests/server/routes/test_skill_content.py`, and the real-host test
  `tests/e2e/test_host_skill_content_e2e.py::test_host_skill_content` with plain
  pytest. Web coverage is in `web/src/pages/settings/SettingsHarnessesSection.test.tsx`.
  Open installed plugin skills while the plugin is disabled and when the skill
  is not user-invocable. With matching names in two marketplaces or a plain skill,
  verify each plugin page shows its own instructions.
  For both card and gear entry points, open a plain skill and a plugin skill;
  verify markdown, Back, and the truncation note for a body over 256 KiB.
  Remote markdown images must not load. A 501 shows an update hint; inject a
  404 from the contents route and verify the list remains with nonclickable
  skill rows. A 502/504 shows a generic failure. Bodies must be absent from
  the skills listing and host/server logs, with no other files or paths returned.

- **`mcp-tools`:** run `tests/host/test_mcp_tools.py`,
  `tests/server/routes/test_mcp_tools.py`, and the real-host test
  `tests/e2e/test_host_mcp_tools_e2e.py::test_host_mcp_tools` with plain pytest.
  Web coverage is in `web/src/pages/settings/SettingsHarnessesSection.test.tsx`.
  Disabled plugin MCP rows stay visible without expansion or probes. Check
  same-name plugins from different marketplaces return their respective tools.
  HTTP probes honor the host's proxy and certificate environment settings.
  From both card and gear entry points, expand a standalone and a plugin server;
  verify the left chevron, immediate expansion, names, count and status dot.
  Collapsed rows must not send probes or start processes. Reopen within five
  minutes to reuse results. Test an HTTP 401, a hanging stdio process and a
  missing executable; expect auth, timeout and unreachable states.
  If both probe slots are occupied, an expired queued request reports that the
  host is busy; collapse and reopen after capacity frees to retry immediately.
  A 501 shows an update hint; inject a 404 from the tools route to retain plain
  rows without expansion. Confirm no raw config, schemas or synthetic secrets
  appear in responses/logs and no probe processes survive cancellation/timeout.
- **`cleanup`:** no single cross-harness test. For each harness in scope, start
  a session, stop it (and separately cancel one during startup), then confirm
  no helper process from that session is still running.
- **`disconnect`, Codex transport (plain `uv run pytest`, no vendor CLI):**
  `tests/e2e/test_codex_native_event_stream_disconnect_e2e.py` covers waiting
  consumers, buffered events, explicit close, and startup discovery without a
  deadline. `tests/e2e/test_codex_native_app_server_disconnect_e2e.py` covers
  pending requests, cancellation, and a reply arriving before disconnect.
  Both use real loopback WebSockets with a controlled peer.
  `tests/harnesses/codex_native/test_codex_native_app_server_event_stream.py` adds multiple waiting
  consumers and reconnecting the same client to receive fresh events.
- **`disconnect`, Codex startup consumers (component tests):**
  `tests/harnesses/codex_native/session/test_subscription.py::test_wait_for_thread_started_fails_when_stream_ends`
  checks the CLI error with a fake client;
  `tests/runner/test_codex_startup_telemetry.py::test_startup_failure_is_visible_at_error_and_belongs_to_child`
  checks host-started failure reporting with stubbed discovery. Run these with
  plain `uv run pytest`. The ongoing chat forwarder also consumes native events;
  these startup tests do not prove it stops or recovers after a live disconnect.
- **`disconnect`, browser stream (own environment):**
  `tests/e2e_ui/chat/test_stream_disconnect_stage_matrix.py::test_numbered_output_recovers_across_stream_stages`
  checks output recovery across a real server restart and injected stream-open
  failures. It supplies native-style events; it does not run a vendor CLI.
  Run with plain `uv run pytest` and the browser prerequisites in the skill.

- **`plugin-inventory` (component and host tests):**
  `tests/e2e/test_host_plugins_e2e.py::test_host_plugin_inventory` starts a real
  host against the test server and checks metadata and secret exclusion.
  `tests/host/test_plugins.py`, `tests/server/routes/test_plugins.py`, and
  `tests/server/integration/test_host_tunnel_route.py::test_host_tunnel_routes_plugins_result_to_future`.
  Run `pnpm --dir web test src/hooks/useHarnessInventory.test.tsx src/pages/settings/SettingsHarnessesSection.test.tsx`.
  Open Settings → Harnesses, select the test
  host and Claude Code, then Plugins. Verify name, version, marketplace, enabled
  state, and hook/command labels from the host. Open a plugin, inspect its
  description and Skills/MCPs tabs, then return with Plugins. Repeat via the
  harness card's Settings gear and switch to Plugins. Installed disabled plugins
  remain visible. For an older host (501) or server (404), verify that the
  derived skill/MCP plugin listing still works; a 502 shows an inventory error.
  Codex and Cursor keep their existing derived listings.

## Gotchas

- A change to shared native-harness behavior (cleanup, idle handling,
  approvals, sign-in state, resume) must be checked on every harness it claims
  to cover. List the harnesses you actually drove; "all harnesses" means all
  twelve rows above.
- Approval and permission callbacks come from several harnesses, not only
  Claude. Restricting a gate to one harness breaks the others' approvals.
- Needing sign-in and not being installed are different states with different
  prompts. Reproduce on a host with the same credential situation as the reporter.
- Some harnesses have a sign-in flow of their own when launched outside
  Omnigent's managed setup; the managed and unmanaged paths behave differently.
- The mock instance proves Omnigent's integration with Claude and Codex, not a
  live vendor model. A passing mock run is not evidence for another harness.
- Codex's background title requests can echo the user's prompt on another model.
  Identify the user thread when checking its outgoing model and effort, and
  script repeatable replies so title generation cannot exhaust the turn's reply.
- A transport check is not a full reconnect journey. To verify that claim,
  use an isolated configured harness, interrupt only its test connection, then
  resume and send another turn; check both terminal and chat for missing or
  duplicate output. Record an unavailable live check explicitly. Codex checks
  above do not cover other harnesses' transports or runner-tunnel recovery.
- The harness registry declares which harness supports effort, approvals, and
  resume. Check it before assuming a column applies.
