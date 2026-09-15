# Native terminal selection prototype

This opt-in experiment selects the attachment transport independently of the
agent. It supports the native Claude, Codex, Cursor, Antigravity, Pi, OpenCode,
Goose, Kimi, Hermes, Qwen, and Kiro launchers, including deployments that wrap
these commands. It does not change SDK harnesses or the web UI.

## Try it

Run the CLI from this checkout after installing its development dependencies
with `uv sync --frozen --extra all --group dev`. Choose the agent you normally use:

```bash
OMNIGENT_EXPERIMENTAL_CONTROL_MODE_ATTACH=1 uv run --no-sync omnigent claude
OMNIGENT_EXPERIMENTAL_CONTROL_MODE_ATTACH=1 uv run --no-sync omnigent codex
OMNIGENT_EXPERIMENTAL_CONTROL_MODE_ATTACH=1 uv run --no-sync omnigent cursor
```

Keep your usual authentication, model, server, and resume arguments. If you use
a wrapper, it must launch this checkout's Omnigent code and inherit the variable.
The terminal prints `Experimental control-mode attach` before attaching, so you
can tell whether the experiment is active.

For comparison, run the same command without the variable:

```bash
env -u OMNIGENT_EXPERIMENTAL_CONTROL_MODE_ATTACH uv run --no-sync omnigent claude
```

Only the exact value `1` enables the experiment. No persistent configuration or
tmux mouse bindings are changed. This replaces the Claude-only toggle from the
initial draft of the prototype.

## What changes

The normal same-machine path runs interactive `tmux attach`. The experimental
path uses the existing native WebSocket attachment and the runner's tmux
control-mode bridge, just like the web terminal:

```text
Normal:       terminal <-> interactive tmux client <-> agent
Experimental: terminal <-> WebSocket relay <-> tmux control mode <-> agent
```

The host terminal receives application output, not tmux's mouse capture or
copy-mode UI. It can therefore own ordinary selection and scrollback. An
application that enables its own mouse handling can still require your
terminal's normal selection override; this experiment does not strip application
escape sequences or promise selection survives every application redraw.

## Manual comparison

In Cursor's integrated terminal, and then in a standalone terminal:

1. Ask your agent to print 100 numbered lines of sample text without running tools.
2. Drag over completed text **without Shift or Option**. Release the mouse,
   copy using the terminal's normal shortcut, and paste into an editor.
3. Repeat while more output is arriving. Record separately whether selection
   disappears during the drag, on release, or when the application redraws.
4. Scroll up and down with the wheel. Return to the bottom and type a follow-up;
   you should not need to exit tmux copy-mode first.
5. Resize the terminal and verify the prompt remains usable. Open the same
   session in the web UI and verify input/output still reaches both clients.
6. Exit the agent normally and verify your shell still echoes input correctly.
   Reattach using your usual resume command with the variable set and check
   the restored screen and scrollback.
7. For a launcher without automatic reconnect (for example, Cursor), interrupt
   the remote connection while attached. An abnormal disconnect should exit
   with `Error: Terminal WebSocket connection failed`, the session ID, and a
   reminder to resume, not a Python traceback. A clean server close may exit
   silently. Restore connectivity and resume the same session.

## Prototype limitations

- Every input/output byte takes the existing WebSocket/server route, even when
  the runner is local. This can increase latency and depends on server/tunnel
  availability. Claude, Codex, and Antigravity retain their existing reconnect
  recovery and session-lifecycle handling. The other launchers use a single
  WebSocket attachment; if the connection drops, rerun your resume command.
  Connection, handshake, and abnormal-close failures report a concise CLI error
  with the session ID and resume guidance; they do not trigger a tmux fallback.
- Tmux's status line, conversation link, copy-mode, and popups are absent.
  Keep the web UI available for approvals normally shown as tmux popups.
- Terminal clipboard shortcuts are the intended copy path. The native
  WebSocket client does not consume the browser's tmux clipboard notifications.
- Simultaneous attachments, restored scrollback, terminal modes, and application
  mouse behavior still need real-terminal comparison before a default rollout.

This is a transport experiment, not a replacement local control-mode client.

## Regression gate and rollback

Start with a disposable conversation, not an important running task. Use this
checkout's CLI without replacing your installed CLI, and set the flag on each
command rather than exporting it in your shell profile. Keep the web UI open
for approvals. The terminal emulator and agent are separate choices: testing
inside Cursor does not require using the Cursor agent.

Test the default path first with the variable removed, then repeat with it set
to `1`. The default path must not print the experimental startup notice. The
opt-in path must print it; otherwise you may be testing a different installation
or a wrapper that did not inherit the variable.

| Check | Default path | Experimental path |
| --- | --- | --- |
| Fresh launch and resume | Existing prompt, history, and session identity | Same session and usable prompt after resume |
| Plain-text selection | Existing terminal/tmux behavior | Plain drag, release, copy, and paste without losing selection on release |
| Input and interrupt | Existing behavior | Normal typing, multiline paste, Unicode, and interrupt still work |
| Scroll and resize | Existing behavior | Scrollback remains usable; resizing does not garble the prompt |
| Simultaneous web attachment | Existing behavior | Both clients show output; input is not duplicated |
| Session lifecycle | Existing detach/exit behavior | No unexpected runner shutdown or hang; shell input/echo restored after exit |
| Approval | Existing approval flow | Complete an approval in the web UI; tmux popups are intentionally unavailable |
| Connection failure | Existing recovery behavior | Existing recovery retained for Claude/Codex/Antigravity; other launchers exit cleanly and can be resumed manually |

For a practical manual pass, prioritize your usual agent in Cursor's integrated
terminal, then the same agent in Ghostty or iTerm. Before widening the experiment,
also exercise Claude, Codex, and Antigravity if available: they have distinct
reconnect and cleanup paths. The automated routing tests cover all 11 launchers,
but do not replace hands-on testing of the installed agent TUIs. Exercise network
failure only with a disposable connection; do not restart a shared server.

Rollback requires no configuration or session migration. Stop the experimental
client and rerun the same launcher/resume command with the variable removed:

```bash
env -u OMNIGENT_EXPERIMENTAL_CONTROL_MODE_ATTACH uv run --no-sync omnigent claude --resume SESSION_ID
```

Replace `claude` and `SESSION_ID`, and retain your usual server/auth arguments.
Keep this opt-in until the default-path regression checks pass and the missing
tmux UI, selection limitations, and reconnect differences are acceptable.
