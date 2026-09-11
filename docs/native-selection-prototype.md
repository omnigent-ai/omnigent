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

## Prototype limitations

- Every input/output byte takes the existing WebSocket/server route, even when
  the runner is local. This can increase latency and depends on server/tunnel
  availability. Claude, Codex, and Antigravity retain their existing reconnect
  recovery and session-lifecycle handling. The other launchers use a single
  WebSocket attachment; if the connection drops, rerun your resume command.
- Tmux's status line, conversation link, copy-mode, and popups are absent.
  Keep the web UI available for approvals normally shown as tmux popups.
- Terminal clipboard shortcuts are the intended copy path. The native
  WebSocket client does not consume the browser's tmux clipboard notifications.
- Simultaneous attachments, restored scrollback, terminal modes, and application
  mouse behavior still need real-terminal comparison before a default rollout.

This is a transport experiment, not a replacement local control-mode client.
