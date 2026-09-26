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
- `agent-switch-cleanup`: switching the session's agent removes the old agent's
  terminals instead of leaving closed tabs.
- `tmux-outage`: terminals stay usable through temporary tmux health-check
  failures and still notice a real exit afterwards.
- `hidden-terminal`: in chat view, the background terminal never draws over the
  chat or composer.
- `dialog-routing`: when the harness is waiting on a dialog in its terminal, chat
  says so and routes the user to the terminal.

## How to get to it (user POV)

**Agent terminal** (sessions whose harness has a terminal):

- Use the Chat/Terminal switcher in the session header.
- On a phone, use the view mode choice in the header menu.
- Choose Resume when the terminal shows as stopped.

**User shells:**

- Open a new shell from the workspace rail on desktop, or from the header menu
  on a phone.
- Select an open shell from the rail's shell list; close it from its tab.

**After the session moves:** open the host badge, choose "Switch host…", pick
another host, then look at both the terminal strip and the agent terminal.

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
  `tests/e2e_ui/shells/test_new_shell.py::test_empty_terminal_view_remains_selectable_and_resumable`,
  `tests/e2e_ui/shells/test_new_shell.py::test_starting_terminal_view_shows_loading_without_resume`,
  `tests/e2e_ui/shells/test_new_shell.py::test_stopped_terminal_view_keeps_resume`
- **`user-shell`:**
  `tests/e2e_ui/shells/test_new_shell.py::test_new_shell_launches_and_opens`,
  `tests/e2e_ui/shells/test_new_shell.py::test_new_shell_accepts_typed_command`.
  Closing a shell has no e2e coverage: close its tab, confirm, and expect the
  tab to disappear.
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
- **`agent-switch-cleanup`:**
  `tests/e2e_ui/agent_switch/test_switch_agent_terminals.py::test_switch_agent_prunes_dead_terminals`
  (opt-in; read the test for the environment variable it needs).
- **`tmux-outage`:**
  `tests/e2e_ui/shells/test_antigravity_tmux_recovery.py::test_antigravity_survives_probe_outage_then_detects_exit`
  (opt-in; Antigravity only).
- **`hidden-terminal`:**
  `tests/e2e_ui/chat/test_hidden_terminal_scrollbar.py::test_hidden_terminal_scrollbar_never_paints_over_foreground`
- **`dialog-routing`:**
  `tests/e2e_ui/chat/test_blocked_dialog_terminal_routing.py::test_chat_blocked_on_terminal_dialog_routes_user_to_terminal`

## Gotchas

- The terminal strip and the agent terminal behind the Chat/Terminal switcher
  are different surfaces. A reattach or reconnect fix must be checked on both.
- A host switch updates the host badge immediately. The badge changing is not
  proof the terminal reattached; look for the new host's terminal content.
- Key bindings added for scrollback can change how full-screen harness terminals
  behave. After a scrollback change, drive the harness terminal itself, not only
  a plain shell.
- Terminal output is drawn on a canvas, so page text searches do not see it.
  Use the tests' approach (the harness's own transcript or program input)
  instead of reading the page.
- "Starting up" and "stopped" look similar. Starting up offers no Resume action;
  stopped does. Wait past startup before calling a terminal stopped.
- Tests that need a harness-specific or opt-in environment skip quietly without
  it. Check that the test ran, not only that the suite passed.
