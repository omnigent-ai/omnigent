# Composer

The composer is where a user writes a message and chooses how the agent runs
it. It appears in two places that look alike but are separate surfaces: the
in-session composer at the bottom of a chat, and the new-session composer on
the landing page. Both show a model/effort pill with a hover tooltip, open a
configuration menu for harness, model, reasoning effort, and permission mode,
and accept attachments and slash commands. The in-session composer also queues
and steers messages while the agent is busy.

## Sub-features

- `pill-tooltip`: hovering the pill shows one tooltip with Harness, Model, and
  Connection rows (plus effort when the harness has it).
- `pill-tooltip-suppressed`: the tooltip stays hidden while the model picker is
  open and does not reappear when the picker closes.
- `pill-opens-config`: clicking anywhere on the highlighted pill label opens the
  configuration menu.
- `model-picker`: lists the host's live model catalog, highlights the current
  model, and keeps the selection. States: catalog loading, delayed catalog,
  alias vs. full model ID.
- `effort-picker`: reasoning effort for harnesses that support it (Codex, Claude,
  Pi). States: available, unavailable placeholder, max/ultra levels.
- `effort-terminal-mirror`: an effort change typed in the Codex terminal shows
  in the composer, and a composer pick is not reverted by later terminal turns.
- `permission-mode`: the harness's native approval modes; the current mode is
  marked and the choice persists.
- `slash-menu`: typing `/` opens commands and skills with keyboard navigation.
- `attachments`: attach button, paste, and drop onto the transcript; chips can
  be removed. State: unsupported file type rejected without losing the message.
- `send-shortcut`: Enter or Mod+Enter, chosen in settings; only one gesture sends.
- `queue-and-steer`: messages sent while the agent is busy wait in a queue and
  can be steered into the running turn.
- `draft-persistence`: unsent text survives arriving messages and prompts.
- `mobile-labels`: on narrow screens labels collapse to icons without
  overlapping the stop button.

## How to get to it (user POV)

**In-session composer** (open any session):

- Hover the model/effort pill to see the tooltip; click it to open configuration.
- In configuration, open the model picker, the effort picker, or the permission
  mode menu.
- For a Codex session, open the Terminal view and change effort there, then
  return to Chat.
- Type `/` in the message box; attach files with the button, by paste, or by
  dropping them on the transcript.
- Send while the agent is working to queue a message, then steer it.

**New-session composer** (landing page, or New session):

- Hover the model/effort pill to see the tooltip.
- Pick a harness, then open its configuration for model, effort (Codex, Claude,
  Pi), and permission mode before the session exists.
- Attach files or type `/` before the first send.

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

- **`pill-tooltip`, `pill-tooltip-suppressed`, in-session:**
  `tests/browser_ui/chat/test_composer_tooltips.py::test_pill_uses_one_tooltip_and_suppresses_it_while_picker_is_open`
  (runs with zero and one turn).
- **`pill-tooltip`, new-session composer:** no browser-level coverage; the web
  unit tests for the new-session dialog cover it. Manually: open the landing
  page, hover the pill, and expect the same styled rows as in a session, not a
  plain browser title tooltip.
- **`pill-opens-config`:**
  `tests/e2e_ui/chat/test_composer_pill_hover_matches_hit_area.py::test_composer_pill_highlighted_label_is_clickable`
- **`model-picker`, in-session:**
  `tests/e2e_ui/chat/test_claude_model_picker.py::test_claude_native_picker_lists_only_live_databricks_models`,
  `tests/e2e_ui/chat/test_claude_model_picker.py::test_claude_native_picker_updates_after_delayed_catalog`,
  `tests/e2e_ui/chat/test_claude_model_picker.py::test_claude_native_picker_highlights_the_reported_model`,
  `tests/e2e_ui/chat/test_claude_model_picker.py::test_claude_native_alias_selection_persists`
- **`model-picker`, new-session composer:**
  `tests/e2e_ui/start_session/test_model_flows_prelaunch.py::test_claude_default_entry_names_the_true_default`,
  `tests/e2e_ui/start_session/test_composer_transition.py::test_selected_model_survives_delayed_create`
- **`effort-picker`, new-session composer:**
  `tests/e2e_ui/start_session/test_codex_effort_prelaunch.py::test_new_codex_session_gear_offers_reasoning_effort`
- **`effort-terminal-mirror`:**
  `tests/e2e_ui/chat/test_codex_effort_terminal_composer_mirror.py::test_codex_terminal_effort_change_reaches_composer`,
  `tests/e2e_ui/chat/test_codex_effort_terminal_composer_mirror.py::test_composer_effort_pick_survives_terminal_turns`
- **`permission-mode`:**
  `tests/e2e_ui/chat/test_claude_model_picker.py::test_claude_native_permission_mode_switch_persists`
- **`slash-menu`:**
  `tests/browser_ui/chat/test_slash_menu.py::test_slash_menu_tracks_real_focus_and_wrapping_keyboard_navigation`
- **`attachments`:**
  `tests/e2e_ui/chat/test_composer_attachments.py::test_attach_then_remove_file`,
  `tests/e2e_ui/chat/test_composer_attachments.py::test_file_dropped_on_the_transcript_attaches`,
  `tests/e2e_ui/chat/test_composer_attachments.py::test_landing_rejects_unsupported_type_and_keeps_message`
- **`send-shortcut`:**
  `tests/e2e_ui/chat/test_composer_submit_shortcut.py::test_submit_with_mod_enter_persists_and_is_the_only_send_gesture`
- **`queue-and-steer`:**
  `tests/e2e_ui/chat/test_queue_steer.py::test_steer_sends_queued_message_while_busy`,
  `tests/e2e_ui/chat/test_composer_bulk_steer.py::test_bulk_steer_retries_the_whole_queue`
- **`draft-persistence`:**
  `tests/e2e_ui/chat/test_draft_survives_incoming_messages.py::test_mid_typing_answer_survives_arriving_prompt`
- **`mobile-labels`, new-session composer:**
  `tests/e2e_ui/mobile/test_composer_model_label_stop_overlap.py::test_new_session_composer_collapses_labels_to_icons_on_mobile`

## Gotchas

- The in-session and new-session composers are different surfaces. A change to
  the pill, tooltip, configuration menu, or effort picker in one does not reach
  the other. Verify both, plus the mobile layout.
- "New session" in a test name can mean a session with no turns yet, not the
  landing page. Check which surface the test opens.
- Tooltip changes need a hover after the picker closes, not only on first load.
  A tooltip that returns after clicking away is a regression.
- Effort exists only for some harnesses. Check at least one harness with effort
  and one without, since the pill and tooltip rows change.
- Codex effort can change from the terminal as well as the composer. Confirm the
  terminal command actually landed before blaming the mirror.
- The mock environment configures Claude and Codex only. Other harnesses'
  catalogs need real CLIs or credentials.
