"""Single source of truth for Omnigent wrapper-session labels.

Wrapper-style sessions (``omnigent claude`` today; future
``codex`` / ``pi`` wrappers tomorrow) stamp an ``omnigent.wrapper``
label on the conversation row at creation time. The server reads it
to gate behavior (claude-native message bypass at
``omnigent/server/routes/sessions.py:182-183``); the chat redirect
and resume dispatcher read it to route a resume to the right
runtime.

The values are tiny string constants that need to match across at
least four call sites. Centralizing them here lets us:

* keep ``omnigent.repl._resume_picker`` decoupled from the
  ``omnigent.claude_native`` import graph (which pulls in tmux /
  websocket code); the picker just imports this module instead;
* fail fast in CI if a refactor diverges any of the call sites
  (see ``tests/test_wrapper_labels.py``);
* expose one symbol per concept so a future ``codex`` wrapper adds
  another constant here rather than another stringly-typed literal.
"""

from __future__ import annotations

# Label key stamped on every wrapper-owned conversation. Reserved
# for the ``omnigent.*`` namespace; never reused for guardrails /
# policy labels.
WRAPPER_LABEL_KEY = "omnigent.wrapper"

# Label key + value that put the Web UI in terminal-first mode (the inline
# native-CLI terminal renders as the main view; the Web UI gates on
# ``labels["omnigent.ui"] == "terminal"``). Stamped at creation for the
# native-CLI wrapper agents alongside WRAPPER_LABEL_KEY. Centralized here so
# the fork route can re-derive it for a switched agent rather than copying
# the source's (which would put an SDK clone wrongly in terminal mode).
UI_MODE_LABEL_KEY = "omnigent.ui"
UI_MODE_TERMINAL_VALUE = "terminal"

# Value the ``omnigent claude`` wrapper writes into
# ``conversations.labels[WRAPPER_LABEL_KEY]``. Treated as a string
# literal on the wire (see API.md "Bind Session Runner") so changes
# here are a server-side contract break.
CLAUDE_NATIVE_WRAPPER_VALUE = "claude-code-native-ui"

# Value the ``omnigent codex`` wrapper writes into
# ``conversations.labels[WRAPPER_LABEL_KEY]``.
CODEX_NATIVE_WRAPPER_VALUE = "codex-native-ui"

# Value the ``omnigent pi`` wrapper writes into
# ``conversations.labels[WRAPPER_LABEL_KEY]``.
PI_NATIVE_WRAPPER_VALUE = "pi-native-ui"

# Value the ``omnigent cursor`` wrapper writes into
# ``conversations.labels[WRAPPER_LABEL_KEY]``.
CURSOR_NATIVE_WRAPPER_VALUE = "cursor-native-ui"
