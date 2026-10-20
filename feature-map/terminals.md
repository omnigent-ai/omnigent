# Terminals

A session can show a live terminal next to or instead of the chat. There are two
kinds that look alike but are separate surfaces: the agent terminal, which runs
the native harness behind the Chat/Terminal switcher, and user shells, which a
user opens from the workspace rail. Both attach to a terminal on the session's
host, survive view switches, reconnect after a dropped connection, and must
reattach when the session moves to another host.

## Sub-features

- `agent-terminal-view`: the Chat/Terminal switcher shows the harness terminal
  and returns to chat with the terminal's content kept. States: starting up
  (no Resume action), stopped (Resume offered), connected.
- `user-shell`: open a shell, type into it, and close it.
- `scrollback`: scroll back with the mouse wheel (including programs that track
  the mouse), by touch on phones, and by keyboard.
- `reconnect`: after the terminal connection drops, the terminal shows that it
  is reconnecting and then reattaches without user action.
- `host-switch-reattach`: after "Switch host…", both the terminal strip and the
  agent terminal attach to the new host's terminal, not the old one.
- `direct-attach`: when the runner is on the same machine the terminal connects
  directly, and falls back to the relay when the direct route is unreachable.
- `tmux-outage`: terminals stay usable through temporary tmux health-check
  failures and still notice a real exit afterwards.
- `hidden-terminal`: in chat view, the background terminal never draws over the
  chat or composer.
- `dialog-routing`: when the harness is waiting on a dialog in its terminal, chat
  says so and routes the user to the terminal.

## How to get to it (user POV)

**Agent terminal** (sessions whose harness has a terminal):

Native subagents that share a parent's terminal show their work in chat and have
no Chat/Terminal switcher. Open the parent session to use its terminal. Children
with their own terminal retain the switcher.

- Use the Chat/Terminal switcher in the session header.
- Press ⌘/Ctrl+Alt+\ to toggle between Chat and Terminal view. The chord is
  off while a rail-opened shell owns the main view.
- On a phone, use the view mode choice in the header menu.
- Choose Resume when the terminal shows as stopped.

**User shells:**

- Open a new shell from the workspace rail on desktop, or from the header menu
  on a phone.
- Press ⌘/Ctrl+Alt+T to open a shell of the remembered default type. The chord
  is inert while the host is offline, the session is stranded, or a shell is
  still being created, and it yields to a focused terminal or editor.
- Select an open shell from the rail's shell list; close it from its tab.

**After the session moves:** open the host badge, choose "Switch host…", pick
another host, then look at both the terminal strip and the agent terminal.
Sandbox-backed sessions, including Databricks Sandbox and Arclet, cannot switch
hosts. Their "Switch host…" item is disabled; hover or focus it for the
unsupported explanation.

**Local terminal:** a native harness launched from the CLI runs in the user's
own terminal; see [native harnesses](./native-harnesses.md).

## Driving it with the repro environment

Preconditions: a running instance (`verify-env start`, then `verify-env
doctor`) and the built web UI. The mock instance has Claude and Codex terminals;
other harnesses need their real CLIs. Run each test through the instance:

```sh
verify-env run -- python -m pytest <test> --ui-skip-build --video=on \
  --output="$VERIFY_EVIDENCE/terminals"
```

- **`agent-terminal-view`:**
  `tests/e2e_ui/shells/test_new_shell.py::test_starting_terminal_view_shows_loading_without_resume`,
  `tests/e2e_ui/shells/test_new_shell.py::test_stopped_terminal_view_keeps_resume`.
  The stopped state with a selectable empty terminal (own environment):
  `tests/e2e_ui/shells/test_new_shell.py::test_empty_terminal_view_remains_selectable_and_resumable`
  kills the session's runner, so it fails under `verify-env run`; run it with
  plain `uv run pytest`.
  Native child ownership and live output (opt-in, own environment):
  `tests/e2e/test_codex_native_child_visibility_e2e.py`.
  This uses real Codex credentials and holds a child command open while checking
  the parent terminal and child chat; see the prerequisites in
  `tests/harnesses/codex_native/forwarder/README.md`.
- **`user-shell`:**
  `tests/e2e_ui/shells/test_new_shell.py::test_new_shell_launches_and_opens`,
  `tests/e2e_ui/shells/test_new_shell.py::test_new_shell_accepts_typed_command`.
  Closing a shell has no e2e coverage: close its tab, confirm, and expect the
  tab to disappear.
- **`agent-terminal-view`, `user-shell`, keyboard:** no e2e coverage; component
  tests `web/src/hooks/useViewModeToggleHotkey.test.tsx` and
  `web/src/hooks/useNewShellHotkey.test.tsx` (`pnpm --dir web test`). Manually,
  in a Claude or Codex session with the composer focused: press ⌘/Ctrl+Alt+\
  twice and expect Terminal view, then Chat view; press ⌘/Ctrl+Alt+T and
  expect a new shell tab to open and take focus. Repeat ⌘/Ctrl+Alt+T with the
  shell focused and expect no second shell.
- **`scrollback`, wheel:**
  `tests/e2e_ui/shells/test_new_shell.py::test_shell_wheel_scroll_reaches_mouse_tracking_program`
- **`scrollback`, touch:**
  `tests/e2e_ui/mobile/test_terminal_touch_scroll.py::test_terminal_touch_swipe_scrolls_back`
- **`scrollback`, keyboard:** no e2e coverage. In a Codex terminal with more
  output than fits, press Page Up and expect older output; then check that the
  Codex terminal's other keys still behave as before.
- **`reconnect`:**
  `tests/e2e_ui/shells/test_terminal_bridge_reconnect.py::test_embedded_terminal_reconnects_after_transport_close`
- **`host-switch-reattach`:**
  `tests/e2e_ui/sessions/test_host_switch_reattaches_terminal.py::test_switching_host_reattaches_the_web_terminal`
  covers the terminal strip. The agent terminal has only web unit coverage:
  after the switch, open Terminal view and expect the new host's terminal.
- **`direct-attach`:**
  `tests/e2e_ui/shells/test_terminal_direct_attach.py::test_terminal_upgrades_to_loopback_attach_when_runner_is_local`,
  `tests/e2e_ui/shells/test_terminal_direct_attach.py::test_terminal_falls_back_to_relay_when_loopback_is_unreachable`
- **`tmux-outage`:**
  `tests/e2e_ui/shells/test_antigravity_tmux_recovery.py::test_antigravity_survives_probe_outage_then_detects_exit`
  (opt-in; Antigravity only).
- **Launch failure and cancellation (plain `uv run pytest`, own tmux):**
  `tests/terminals/test_launch_failure_cleanup.py` checks a missing tmux
  executable at spawn and cancellation after a real pane starts, including
  during the initial liveness check. Each must release its private directory
  and tmux server before registry ownership; a caller cancelled during that
  cleanup still gets a completed close first. This registry is shared by native
  agent terminals, user shells, and `sys_terminal_launch`; no vendor model or
  browser is used in these process checks.
  `tests/e2e/test_terminal_launch_retry_cleanup_e2e.py` also drives the runner's
  resource facade through three failed launches and a successful retry for
  Claude/Codex terminal roles and an auxiliary shell. The successful real pane
  must accept input, and final cleanup must release every allocated instance.
- **`hidden-terminal`:**
  `tests/e2e_ui/chat/test_hidden_terminal_scrollbar.py::test_hidden_terminal_scrollbar_never_paints_over_foreground`
- **`dialog-routing`:**
  `tests/e2e_ui/chat/test_blocked_dialog_terminal_routing.py::test_chat_blocked_on_terminal_dialog_routes_user_to_terminal`
  injects the blocked event; it does not run a vendor CLI. For Claude's OSC
  working/blocked status with an attached or closed viewer and the flag-off
  fallback, use the [native working-status recipe](./native-harnesses.md#driving-it-with-the-repro-environment).

## Gotchas

- The terminal strip and the agent terminal behind the Chat/Terminal switcher
  are different surfaces. A reattach or reconnect fix must be checked on both.
- A host switch updates the host badge immediately. The badge changing is not
  proof the terminal reattached; look for the new host's terminal content.
- The ⌘/Ctrl+Alt chords use ⌘ on macOS and Ctrl elsewhere, and they match the
  physical key, so AltGr+T on an international layout must not open a shell.
- Key bindings added for scrollback can change how full-screen harness terminals
  behave. After a scrollback change, drive the harness terminal itself, not only
  a plain shell.
- Terminal output is drawn on a canvas, so page text searches do not see it.
  Use the tests' approach (the harness's own transcript or program input)
  instead of reading the page.
- "Starting up" and "stopped" look similar. Starting up offers no Resume action;
  stopped does. Wait past startup before calling a terminal stopped. A
  cloud-sandbox session stays in Starting up until its sandbox is ready, which
  can take minutes.
- Tests that need a harness-specific or opt-in environment skip quietly without
  it. Check that the test ran, not only that the suite passed.
