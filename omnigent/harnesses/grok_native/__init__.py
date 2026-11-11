"""Native Grok Build (``grok agent serve``) harness for Omnigent.

Second ``native-server`` harness after opencode-native: the vendor agent runs
as its own WebSocket server (ACP over WS plus the ``x.ai/hooks`` client
registration extension) instead of a tmux TUI. See docs/GROK_NATIVE_DESIGN.md.
"""
