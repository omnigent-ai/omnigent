# Native terminal colors

Omnigent's terminal appearance and a CLI's syntax highlighting are separate.
The browser palette supplies the terminal's default foreground/background;
Codex's `/theme` controls code highlighting, not its input and menu surfaces.

## Startup handshake

Runner-created Codex terminals wait up to two seconds for a browser palette.
The app-server starts first; only the TUI waits. Without a browser (including
headless sub-agents), the timer releases startup automatically.

```text
Browser xterm                  tmux                         Codex TUI
apply light/dark palette                                    wait-for
resize ----------------------> set dimensions
init {foreground, background}-> set window-style
                               release wait-for ----------> start
                               <--------------------------- OSC 10/11 queries
                               reply with configured RGB -> cache palette
```

Tmux answers OSC 10/11 itself. Merely attaching xterm before starting Codex is
not sufficient: tmux must know the renderer's colors. Xterm consumes the copied
queries without replying again, avoiding duplicate or late replies being sent
as keyboard input. Palette validation accepts only six-digit RGB hex values,
and read-only viewers cannot set the palette or release startup.

Repeated initialization updates the palette for subsequent probes, but releases
the startup waiter only once. Existing resize-only clients remain compatible:
they use the bounded startup fallback.

## Limits and verification

Codex caches its terminal palette. Changing Omnigent's appearance does not
invalidate that cache, and this handshake cannot repair a TUI that already
started with the wrong colors. Select the intended terminal appearance before
starting a fresh Codex session. Changing `/theme` does not reset the cached
terminal background either. Explicit syntax-theme choices are preserved.

If the browser arrives after the two-second deadline, Codex uses whatever
terminal colors were available at startup. This is a deliberate bounded wait,
not a guarantee that every TUI sees a browser palette. No automatic TUI restart
or transcript/output rewriting is performed.

To verify:

1. In Settings, select Light for the terminal appearance.
2. Start a new Codex session and open its Terminal view promptly.
3. Open `/theme` or `/model`. The input/menu surface should be light; the syntax
   preview can remain dark if an explicit dark syntax theme is selected.
4. Repeat with Dark selected and a new session. The surface should be dark.
5. Start a Codex session without opening Terminal. Startup must still complete.

Automated protocol checks:

```sh
uv run --no-sync pytest tests/terminals/test_browser_ready.py tests/terminals/test_control_bridge.py
cd web && pnpm test src/components/blocks/TerminalSession.test.ts
```
