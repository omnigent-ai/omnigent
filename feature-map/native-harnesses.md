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
- `cleanup`: stopping, cancelling, or idling a session reaps the harness's
  helper processes and per-session files.
- `disconnect`: startup waits and active operations settle when their native
  connection ends; reconnect can receive fresh events. Distinguish a native
  CLI disconnect, a runner going offline, and a browser stream reconnect.

- `launch-settings`: Settings → Harnesses → a configured Claude or Codex →
  Settings (or its card's gear). One Startup configuration block shows Command,
  Environment, and Arguments, unmasked and read-only, with the selected host's source.
  Env wrappers are split into these fields, without a duplicate raw invocation.
  Session and workspace config can add to or override these host defaults. Behind
  `harness_settings_ui`; other harnesses keep their credential card only.
- `skill-contents`: open plain or plugin skills to read their SKILL.md markdown,
  with loading, truncation, unavailable-host, and older-server states.

- `mcp-tools`: expand a configured or plugin MCP server to probe its tools,
  with connected/auth/timeout/unreachable/unsupported and mixed-version states.

- `plugin-inventory`: installed Claude plugins, including disabled and hook/command-only plugins, report metadata and bundled skills/MCPs in Settings → Harnesses.
- `harness-settings-navigation`: with `harness_settings_ui` enabled, the import
  review modal's See more opens Harnesses and dismisses the modal. Settings →
  Import sessions keeps session imports but hides Harness imports. With the flag
  off, the modal has no See more and Harness imports remains available.

## How to get to it (user POV)

**Web:** start a new session, choose the harness in the harness picker, open its
configuration for model and effort, and send. Approval cards and the Terminal
view appear in the session.

**CLI:** run `omnigent <name>` from the matrix below; add `--resume` with or
without a session ID to resume.

**Skill contents:** Settings → Harnesses → configured harness card (or gear),
then Skills → a skill, or Plugins → a plugin → a skill. Back returns to the
list or plugin. Requires `harness_settings_ui`.

**MCP tools:** Settings → Harnesses → configured harness card (or gear),
then MCP servers → expand a server, or Plugins → plugin → MCPs → expand.
Probes run only on expansion; requires `harness_settings_ui`.

**Harness settings navigation:** the import review modal shown for a newly
connected or requested host → See more opens Settings → Harnesses. With the
flag off, Settings → Import sessions → Harness imports → Review imports reopens
the review modal instead.

**Interrupted session:** observe startup before the first message, a running
turn, and Stop separately. For an offline host use the reconnect paths in
[sessions](./sessions.md); a detached terminal has its own paths in
[terminals](./terminals.md).

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

**Launch settings (own environment):** enable `harness_settings_ui`, connect a
host with Claude/Codex configured, and put a command and two args under
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

- **`harness-settings-navigation`:** run
  `web/src/components/onboarding/ImportContextModal.test.tsx` and
  `web/src/pages/SettingsPage.test.tsx`. In an isolated instance, connect a new
  host, then click See more beside Confirm. Check that the modal closes and
  Harnesses opens. Open Import sessions and check that only session imports
  remain. Repeat with `harness_settings_ui` off: no See more, and Harness
  imports can still reopen the modal.

- **`needs-auth`:**
  `tests/e2e_ui/start_session/test_harness_credential.py::test_needs_auth_harness_is_disabled_with_repair_tooltip`,
  `tests/e2e_ui/chat/test_hide_unconfigured_harnesses.py::test_hide_unconfigured_harnesses_filters_the_picker`,
  `tests/e2e_ui/chat/test_hide_unconfigured_harnesses.py::test_hide_unconfigured_hides_a_harness_missing_from_the_host_map`
- **`model-and-effort`:**
  `tests/e2e_ui/start_session/test_native_picker_cli_parity.py::test_claude_picker_omits_aliases_the_cli_picker_does_not_offer`,
  `tests/e2e_ui/start_session/test_native_picker_cli_parity.py::test_codex_picker_offers_the_clis_catalog_and_default`;
  see also [composer](./composer.md) for effort.
- **`approvals`:**
  `tests/e2e_ui/approvals/test_native_edit_tools_approval_card.py::test_native_file_edit_tools_require_approval_card`
- **`resume`, bare picker scoped to this host:**
  `tests/e2e/test_native_resume_picker_cross_host_e2e.py::test_bare_resume_picker_excludes_other_hosts_sessions`
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
  With `harness_settings_ui` enabled, open Settings → Harnesses, select the test
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
- A transport check is not a full reconnect journey. To verify that claim,
  use an isolated configured harness, interrupt only its test connection, then
  resume and send another turn; check both terminal and chat for missing or
  duplicate output. Record an unavailable live check explicitly. Codex checks
  above do not cover other harnesses' transports or runner-tunnel recovery.
- The harness registry declares which harness supports effort, approvals, and
  resume. Check it before assuming a column applies.
