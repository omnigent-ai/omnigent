# Composer

The composer is where a user writes a message and chooses how the agent runs
it. It appears in two places that look alike but are separate surfaces: the
in-session composer at the bottom of a chat, and the new-session composer on
the landing page. Both show a model/effort pill with a hover tooltip, open a
configuration menu for harness, model, reasoning effort, and permission mode,
and accept attachments and slash commands. The in-session composer also queues
and steers messages while the agent is busy.

## Sub-features

- `offline-hosts`: the new-session host picker keeps other offline hosts collapsed,
  revealing five at a time in server recency order in a scrollable list. The selected
  offline host and desktop reconnect action stay visible. Closing the menu resets it.
- `pill-tooltip`: hovering the pill shows one tooltip with Harness, Model, and
  Connection rows (plus effort when the harness has it).
- `pill-tooltip-suppressed`: the tooltip stays hidden while the model picker is
  open and does not reappear when the picker closes.
- `pill-opens-config`: clicking anywhere on the highlighted pill label opens the
  configuration menu.
- `host-tooltip`: hovering the host chip shows a tooltip styled like the pill's
  with Host and Status rows, hidden while the host menu is open. States:
  connected, disconnected, asleep (resumable managed host), unknown, no host,
  and a launching or failed sandbox with its stage; before launch also a
  selected sandbox and connecting.
- `model-picker`: lists the host's live model catalog, highlights the current
  model, and keeps the selection. Ctrl+Shift+M opens it in both composers.
  States: catalog loading, delayed catalog,
  alias vs. full model ID, refused switch (the pill keeps the applied model).
- `effort-picker`: reasoning effort for harnesses that support it (Codex, Claude,
  Pi, Devin). States: available, unavailable placeholder, max/ultra levels, refused
  change (the pill keeps the applied level).
- `effort-terminal-mirror`: an effort change typed in the Codex terminal shows
  in the composer, and a composer pick is not reverted by later terminal turns.
- `permission-mode`: the harness's native approval modes; the current mode is
  marked and the choice persists.
- `managed-mcp-picker`: choose administrator-configured services before launch
  on a host or sandbox. States: loading, empty, error/retry, selected, unavailable,
  remembered selection, explicitly none, restored connection needing sign-in.
- `managed-mcp-sign-in`: selecting an unconnected service opens inline sign-in;
  selection and launch wait for success. States: connected, pending, cancelled,
  failed, popup blocked, Databricks workspace prompt, bearer token needed.
- `slash-menu`: typing `/` opens commands and skills with keyboard navigation.
  The main composer lists its full built-in set and the session's skills; a live
  side chat offers a narrower menu — `/help` plus the session's skills. A built-in
  such as `/help` is handled locally; a recognised skill routes through
  `slash_command` so it runs on an in-process harness. A native terminal side
  chat shows no menu and sends slash text to the vendor TUI.
- `new-command`: `/new` opens a blank new-session composer in the same project;
  working-directory selection follows the project's defaults, not the old session.
- `attachments`: attach button, paste, and drop onto the transcript; chips can
  be removed. State: unsupported file type rejected without losing the message.
- `send-shortcut`: Enter or Mod+Enter, chosen in settings; only one gesture sends.
  Shift+Enter and Alt/Option+Enter insert a newline in both composers.
- `list-formatting`: typing `-`, `*`, `1.` or `1)` then Space at the start of a
  line indents it as a list item; the newline gesture continues the list
  (numbers increment) and ends it after an empty item.
- `focus-hotkey`: Ctrl+Shift+L focuses the in-session message box from anywhere
  in the session view (not on mobile).
- `new-session-hotkey`: ⌘/Ctrl+Alt+N opens and focuses the new-session composer.
- `queue-and-steer`: messages sent while the agent is busy wait in a queue and
  can be steered into the running turn.
- `browser-pointer-queue`: desktop Design-mode instructions follow the same queue
  and always-steer preference, keeping their element screenshots.
- `pi-compact`: `/compact` follows the normal queue/Always steer preference.
  Send now interrupts the turn and compacts. During compaction, queued messages
  wait in the web queue; immediate sends are retained by the Pi integration.
- `draft-persistence`: unsent text survives arriving messages and prompts.
- `delivered-send-recovery`: a sent message stays out of the composer when its
  HTTP acknowledgement is lost. Delivery during draft restoration clears the
  untouched draft; user edits remain.
- `import-bundle`: Create custom agent → Import bundle installs a `.tar.gz` agent
  bundle and selects it. States: import in flight (Cancel and Create locked),
  rejected bundle, imported agent missing from the refreshed list, and hidden on
  a server without agent install.
- `mobile-labels`: on narrow screens labels collapse to icons without
  overlapping the stop button.

## How to get to it (user POV)

**In-session composer** (open any session):

- Hover the model/effort pill to see the tooltip; click it to open configuration.
- Hover the host chip (status dot and machine icon) to see the host and whether
  it is connected; click it to open the host menu.
- In configuration, open the model picker, the effort picker, or the permission
  mode menu.
- For a custom `claude-sdk` or `codex` agent session, open configuration and
  pick an effort rung; the ladder is the harness's declared family (anthropic
  or openai) and the pick persists on the session. Changing effort after a
  completed turn applies on the next conversational turn and keeps the history.
  Claude SDK `/compact` still operates on the existing client's context.
  `/effort default` restores the harness's default. This is the in-session
  dial, distinct from the new-session composer's effort choice.
- Press Ctrl+Shift+M (Ctrl on every platform) to open the model picker without
  the gear; press Ctrl+Shift+L to focus the message box.
- For a Codex session, open the Terminal view and change effort there, then
  return to Chat.
- Type `/` in the message box; attach files with the button, by paste, or by
  dropping them on the transcript.
- Send while the agent is working to queue a message, then steer it.
- Type `/n`, press Tab to complete `/new`, then Enter to open the new-session
  page in the same project. Selecting **/new** in Commands also opens it.

**Desktop browser pointer** (open a session in the desktop app):

- Choose **+ → Browser** to open a Browser soft tab, navigate to a page, enable
  Design mode, and click an element. Type an instruction in the floating popup
  and use Send or Enter.
- Change Settings → Always steer to choose whether busy-session follow-ups
  queue or send immediately. Existing queued messages retain FIFO ordering.

**New-session composer** (landing page, or New session):

- Open the host chip, expand **Offline hosts (N)**, then **Show more**. Check the
  same menu on mobile and with a managed sandbox available. Offline rows remain
  disabled; a remembered offline selection still requires an explicit switch.
- From any page, press ⌘/Ctrl+Alt+N to open and focus a clean new-session
  composer.
- Hover the model/effort pill to see the tooltip.
- Hover the host chip to see the selected host or sandbox and its connection.
- Pick a harness, then open its configuration for model, effort (Codex, Claude,
  Pi, Devin), and permission mode before the session exists. Ctrl+Shift+M also
  opens the agent/model picker here.
- With a managed sandbox selected and no connected host, the model picker lists
  the sandbox's default catalog when `/v1/info` advertises `default_models` (an
  unbound gateway with `sandbox.model_discovery`, or a provider hook): rows under
  the provider's label plus a "Default" row, for native and SDK agents.
  A failed listing shows its error and keeps Default launchable; a
  harness the catalog cannot serve keeps its ordinary list. This is distinct
  from a connected host's own catalog and from a bound (`inference_models`)
  target. Covered by
  `tests/e2e_ui/start_session/test_sandbox_gateway_models_without_binding.py`
  and `tests/e2e_ui/start_session/test_sandbox_model_selection.py::test_optional_gateway_preview_without_a_host`.
- Attach files or type `/` before the first send.
- Open **MCPs** in the workspace bar above the composer (the plug icon when space is tight), then turn
  services on or off in **Tools for this session** before launching on a host or
  sandbox. Turning a service off keeps its account connected. Explicit selections
  are remembered for new sessions in this browser; an unsent draft takes precedence.
  Reload to verify both remembered selections and choosing none.
  If the catalog fails, retry or turn off saved services by ID to launch without
  MCPs. The error itself leaves preferences intact.
- Turn on an unconnected OAuth service to open its sign-in popup. Complete sign-in
  to select it; for Databricks, first enter the workspace URL and choose
  **Connect workspace**. Bearer-token services require **Manage MCP accounts**
  before selection. Use **Reconnect** if a selected service loses its connection.
- Open the agent picker's custom agents, then Create custom agent → Import
  bundle: pick a `.tar.gz` agent bundle. It installs, closes the dialog, and
  selects the agent, which stays listed after a reload. A rejected bundle (for
  example a server agent's name) keeps the dialog open with the server's reason.

**Fork dialog** (Fork in the session header menu, or Fork from here on a
message, with a native target): the same model, effort, and permission mode
pickers, seeded from the source session when its harness family is kept.

**Mobile** (either composer on a phone-sized viewport): the same controls with
collapsed labels.

## Driving it with the repro environment

Preconditions: a running instance (`verify-env start`, then `verify-env
doctor`), the built web UI, and mock replies configured for any journey that
sends a message. Run e2e tests through the instance:

```sh
verify-env run -- python -m pytest <test> --ui-skip-build --video=on \
  --output="$VERIFY_EVIDENCE/composer"
```

Tests under `tests/browser_ui/` stub every backend call and need no instance:
`uv run pytest <test> --browser-ui-skip-build --video=on --output="$VERIFY_EVIDENCE/composer"`.

For managed MCP journeys, the server must have an administrator-configured
registry enabled and a service visible to the current account; otherwise the
picker is hidden or the catalog is empty. Host and sandbox runners must support
managed MCP services. Use a fictional local OAuth provider for live sign-in
checks and allow popups; the browser tests below stub the catalog and callbacks.
Other connection providers alone do not enable the managed MCP picker.

- **`offline-hosts`:**
  `tests/e2e_ui/start_session/test_start_session.py::test_start_session_keeps_offline_host_selected`
  covers the connected-host and managed-sandbox variants with stubbed host responses.
  Repeat at a phone viewport; desktop reconnect exceptions also have component
  coverage in `web/src/shell/NewChatDialog.test.tsx`.
- **`pill-tooltip`, `pill-tooltip-suppressed`, in-session:**
  `tests/browser_ui/chat/test_composer_tooltips.py::test_pill_uses_one_tooltip_and_suppresses_it_while_picker_is_open`
  (runs with zero and one turn).
- **`pill-tooltip`, new-session composer:** no browser-level coverage; the web
  unit tests for the new-session dialog cover it. Manually: open the landing
  page, hover the pill, and expect the same styled rows as in a session, not a
  plain browser title tooltip.
- **`host-tooltip`, in-session:**
  `tests/browser_ui/chat/test_composer_tooltips.py::test_host_chip_tooltip_names_the_host_and_matches_the_pill`
  (connected, menu suppression, same surface as the pill); disconnected and
  asleep in `tests/e2e_ui/sessions/test_host_badge.py`; a provisioning sandbox
  handing over to its connected host in
  `tests/e2e_ui/start_session/test_managed_first_message_routing.py::test_managed_first_message_rebinds_live_stream_across_replicas`.
- **`host-tooltip`, new-session composer:** no browser-level coverage; the web
  unit tests for the new-session dialog cover it. Manually: open the landing
  page, hover the host chip, and expect the same card as the pill's tooltip
  with the host and its status, not a plain browser title tooltip.
- **`pill-opens-config`:**
  `tests/e2e_ui/chat/test_composer_pill_hover_matches_hit_area.py::test_composer_pill_highlighted_label_is_clickable`
- **`model-picker`, in-session:**
  `tests/e2e_ui/chat/test_claude_model_picker.py::test_claude_native_picker_lists_only_live_databricks_models`,
  `tests/e2e_ui/chat/test_claude_model_picker.py::test_claude_native_picker_updates_after_delayed_catalog`,
  `tests/e2e_ui/chat/test_claude_model_picker.py::test_claude_native_picker_highlights_the_reported_model`,
  `tests/e2e_ui/chat/test_claude_model_picker.py::test_claude_native_alias_selection_persists`
- **`model-picker`, new-session composer:**
  `tests/e2e_ui/start_session/test_model_flows_prelaunch.py::test_claude_default_entry_names_the_true_default`,
  `tests/e2e_ui/start_session/test_native_picker_cli_parity.py::test_codex_picker_offers_the_clis_catalog_and_default`
  (real host and Codex CLI; custom, bundled, and hidden defaults on desktop and mobile),
  `tests/e2e_ui/start_session/test_codex_catalog_availability.py::test_codex_prelaunch_label_reflects_catalog_availability`
  (desktop and mobile; populated catalogs without a default, empty catalogs, and probe failures),
  `tests/e2e_ui/start_session/test_composer_transition.py::test_selected_model_survives_delayed_create`
- **`effort-picker`, new-session composer:**
  `tests/e2e_ui/start_session/test_codex_effort_prelaunch.py::test_new_codex_session_gear_offers_reasoning_effort`
- **`effort-picker`, SDK in-session composer:**
  `tests/e2e_ui/chat/test_sdk_effort_dial.py::test_claude_sdk_session_offers_the_effort_dial`
  covers selection, persistence and reload. The stale-catalog cases in
  `web/src/store/chatStore.test.ts` cover saving while catalog refresh stalls.
  The effort-transition cases in `tests/inner/test_claude_sdk_executor.py` and
  `tests/inner/test_codex_executor.py` cover subsequent turns and default reset.
  Manually, complete a turn, change effort, then send a follow-up; inspect the
  mock provider request to confirm the new effort and prior conversation.
- **`model-picker`, `effort-picker`, refused change:** the rollback cases in
  `web/src/store/chatStore.test.ts` and the composer case in
  `web/src/pages/ChatPage.composer.test.tsx` (`pnpm --dir web test`). A pick
  during Codex startup waits for the terminal, then applies. If startup does
  not become ready within the recovery budget, the pill keeps the previous
  selection; server/runner coverage is in
  `tests/server/integration/test_codex_model_change_after_idle_reap.py`.
- **`effort-terminal-mirror`:**
  `tests/e2e_ui/chat/test_codex_effort_terminal_composer_mirror.py::test_codex_terminal_effort_change_reaches_composer`,
  `tests/e2e_ui/chat/test_codex_effort_terminal_composer_mirror.py::test_composer_effort_pick_survives_terminal_turns`
- **`permission-mode`:**
  `tests/e2e_ui/chat/test_claude_model_picker.py::test_claude_native_permission_mode_switch_persists`
- **`model-picker`, `effort-picker`, `permission-mode`, Fork dialog:** the
  permission-mode picker has component coverage in
  `web/src/shell/ForkSessionDialog.test.tsx`; drive the model and effort
  pickers by hand: fork a Codex session, pick another effort in the dialog, and
  expect the new session's pill to show it. See [sessions](./sessions.md).
- **`managed-mcp-picker`, new-session composer:**
  `tests/e2e_ui/sessions/test_mcp_registry_connections.py::test_managed_mcp_selection_is_sent_with_launch`
  covers desktop sandbox and mobile local-host launches, including a long catalog
  in a short viewport with a reachable last row and visible heading and footer.
  After launch, the same cases use desktop agent info and mobile **Agent info** to select managed
  services on a builtin agent and remove unavailable references after registry
  disablement, while custom MCP controls remain read-only. The module has four
  browser cases: these two launches, settings sign-in routing, and the
  Databricks sign-in launch below. Selection persistence,
  explicit opt-out during a persistent catalog failure, launch gating during
  sign-in, and an absent picker without registry enablement
  are covered in `web/src/shell/NewChatDialog.flow.test.tsx`.
- **`managed-mcp-sign-in`, new-session composer:**
  `tests/e2e_ui/sessions/test_mcp_registry_connections.py::test_databricks_connects_from_launch_picker`
  covers the workspace prompt and successful sign-in before a desktop host
  launch. `web/src/components/McpRegistry.test.tsx` and
  `web/src/lib/mcpRegistry.test.ts` cover selection after sign-in, cancellation,
  retry, reconnect, and closed or blocked popups (`pnpm --dir web test`).
  `tests/server/integration/test_mcp_gateway_http.py::test_real_http_oauth_refresh_through_gateway`
  covers real OAuth, refresh, account reuse, and disconnect without a browser
  or external credentials (`uv run --no-sync pytest tests/server/integration/test_mcp_gateway_http.py`).
  Manually on desktop and mobile: select an unconnected OAuth service, confirm
  launch is disabled while sign-in is pending, then approve in the popup and
  expect it to close with the service checked and the selection count updated.
  Repeat with popup cancellation: expect an error, an unchecked service, and a
  usable retry. Launch on a host and a sandbox with the selected service and
  confirm its tools are available on the first turn.
- **`new-command`:**
  `tests/browser_ui/chat/test_slash_menu.py::test_new_command_opens_landing_in_current_project`
  covers Enter, Tab then Enter, and clicking the command, with first-class,
  legacy-label, off-sidebar, partial-sidebar, and no-project sessions.
  `tests/browser_ui/chat/test_slash_menu.py::test_new_command_respects_unfiled_sidebar_row`
  covers explicit removal from a project;
  `tests/browser_ui/chat/test_slash_menu.py::test_new_command_project_lookup_error_is_visible_and_retryable`
  covers failed lookup and retry without using a stale project name.
  `tests/browser_ui/chat/test_slash_menu.py::test_new_command_deleted_project_opens_unfiled`
  covers deleted projects with dangling session membership.
  No running agent is needed.
- **`slash-menu`:**
  `tests/browser_ui/chat/test_slash_menu.py::test_slash_menu_tracks_real_focus_and_wrapping_keyboard_navigation`,
  `tests/e2e_ui/chat/test_side_chat_slash_command.py::test_side_chat_slash_menu_and_skill_invocation`
- **`attachments`:**
  `tests/browser_ui/chat/test_composer_attachments.py::test_attach_supported_files_and_remove`,
  `tests/browser_ui/chat/test_composer_attachments.py::test_file_dropped_on_the_transcript_attaches`,
  `tests/browser_ui/chat/test_composer_attachments.py::test_landing_rejects_unsupported_type_and_keeps_message`
- **`send-shortcut`:**
  `tests/e2e_ui/chat/test_composer_submit_shortcut.py::test_submit_with_mod_enter_persists_and_is_the_only_send_gesture`,
  `tests/e2e_ui/chat/test_composer_submit_shortcut.py::test_alt_enter_inserts_newline_in_both_composers`
- **`list-formatting`, both composers:** no browser-level coverage; the list
  cases in `web/src/components/composer/ChatComposer.test.tsx`
  (`pnpm --dir web test`). Manually: type `1.` then Space and expect ` 1.`;
  type an item, press Shift+Enter and expect ` 2.` on the next line; press
  Shift+Enter on the empty item and expect the marker removed. With Enter as
  the send gesture, plain Enter still sends a list; with Mod+Enter chosen,
  plain Enter continues it.
- **`model-picker` and `focus-hotkey`, keyboard:** no browser-level coverage;
  component tests `web/src/hooks/useModelPickerHotkey.test.tsx` and
  `web/src/hooks/useFocusComposerHotkey.test.tsx`. Manually: in a session and on
  the landing page press Ctrl+Shift+M and expect the model picker open; in a
  session click the transcript, press Ctrl+Shift+L and expect the message box
  focused. In a session whose gear is disabled (read-only or unreachable),
  Ctrl+Shift+M does nothing.
- **`queue-and-steer`:**
  `tests/e2e_ui/chat/test_queue_steer.py::test_steer_sends_queued_message_while_busy`,
  `tests/e2e_ui/chat/test_composer_bulk_steer.py::test_bulk_steer_retries_the_whole_queue`
- **`browser-pointer-queue`, desktop:** manually hold an agent turn open, then
  send a normal composer follow-up and two pointer instructions (Send and Enter).
  With Always steer off, all three stay queued and the popup says "Queued for
  agent." After the turn ends, verify FIFO delivery and each pointer screenshot.
  Repeat in a fresh session with Always steer on: both entry points send while
  busy and the pointer popup says "Sent to agent." If older messages are already
  queued, new pointer instructions must join them even with Always steer on.
- **`pi-compact`:** in a Pi session, start a long turn, type `/comp`, press Tab,
  then Enter. With Always steer off, confirm `/compact` queues; use Send now to
  interrupt and compact. With Always steer on and an empty queue, Enter should
  compact immediately. During compaction, send a follow-up in each mode: normal
  queue mode keeps its queue row; Always steer hands it to Pi. Both should run
  once after compaction. Repeat the queued flow while viewing another session.
- **`draft-persistence`:**
  `tests/e2e_ui/chat/test_draft_survives_incoming_messages.py::test_mid_typing_answer_survives_arriving_prompt`
- **`delivered-send-recovery`, in-session composer (desktop and mobile):**
  `tests/e2e_ui/mobile/test_composer_after_unacked_send.py::test_composer_stays_empty_after_delivered_but_unacked_send`
  runs with a phone viewport. Run the same lost-ack journey at a desktop viewport
  for the desktop entry point. The restore/delivery ordering and preservation of
  user edits are covered in `web/src/pages/ChatPage.composer.test.tsx`.
- **`new-session-hotkey`:**
  `tests/e2e_ui/sessions/test_new_session_hotkey.py::test_new_session_hotkey_from_focused_composer`
- **`import-bundle`:** no browser-level coverage; the web unit tests for the
  Create custom agent and new-session dialogs cover it. Manually: open the agent
  picker's custom agents, then Create custom agent → Import bundle, and pick a
  `.tar.gz` of an agent directory. Expect the dialog to close with the agent
  selected, and the agent still listed after a reload. A bundle named like a
  server agent keeps the dialog open with the server's reason.
- **`mobile-labels`, new-session composer:**
  `tests/e2e_ui/mobile/test_composer_model_label_stop_overlap.py::test_new_session_composer_collapses_labels_to_icons_on_mobile`

## Gotchas

- Chat `/new` is local navigation, not a message to the agent. Native TUI
  `/new` is unchanged.

- The in-session and new-session composers are different surfaces. A change to
  the pill, tooltip, configuration menu, or effort picker in one does not reach
  the other. Verify both, plus the mobile layout. The Fork dialog repeats the
  model, effort, and permission-mode pickers for native targets; check it too.
- "New session" in a test name can mean a session with no turns yet, not the
  landing page. Check which surface the test opens.
- Tooltip changes need a hover after the picker closes, not only on first load.
  A tooltip that returns after clicking away is a regression.
- Effort exists only for some harnesses. Check at least one harness with effort
  and one without, since the pill and tooltip rows change.
- Ctrl+Shift+M and Ctrl+Shift+L use Ctrl even on macOS, unlike the ⌘/Ctrl+Alt
  chords. All of them yield to a focused terminal or code editor, so press them
  with the transcript or composer focused.
- A refused model or effort change rolls the pill back to the applied setting.
  There is no separate error control to inspect.
- Codex effort can change from the terminal as well as the composer. Confirm the
  terminal command actually landed before blaming the mirror.
- The mock environment configures Claude and Codex only. Other harnesses'
  catalogs need real CLIs or credentials.
- The managed MCP picker uses only the administrator's registry; it does not
  add arbitrary MCP URLs or enable itself from existing GitHub/Databricks
  connections. Bearer-token accounts must be connected in settings first.
- Managed MCP browser tests stub launch and authentication. They prove composer
  wiring; the HTTP integration test proves the real provider boundary.
