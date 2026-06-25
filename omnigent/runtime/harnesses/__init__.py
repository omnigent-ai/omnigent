"""
Harness package — per-conversation subprocesses that implement a
subset of the Omnigent REST API.

See ``designs/SERVER_HARNESS_CONTRACT.md`` for the full contract.
The harness IS an HTTP service speaking the same Pydantic models AP
serves to external clients (re-use ``omnigent.server.schemas`` —
there is no separate protocol module).

This package contains:

- ``_HARNESS_MODULES``: registry mapping harness name (the value of
  ``spec.executor.harness`` in an agent spec) to the fully-qualified
  Python module path that exports a zero-argument ``create_app() ->
  FastAPI``. Populated as per-harness wraps land (Phase 1 step 4).
- ``process_manager``: ``HarnessProcessManager`` — owns
  per-conversation subprocess lifecycle.
- ``_runner``: shared ``python -m`` entrypoint that any registered
  harness's ``create_app()`` is served through.

The package directory is intentionally small. Behavior lives in the
sibling modules; this ``__init__.py`` is just the registry.
"""

from __future__ import annotations

# Harness-name → fully-qualified module path. Each module must
# export ``create_app() -> FastAPI``; the runner imports the module,
# calls the factory, and serves the result over a Unix socket.
#
# Populated as per-harness wraps land in Phase 1 step 4. The test
# suite injects fixture entries at test time (via direct dict
# mutation in conftest fixtures).
_HARNESS_MODULES: dict[str, str] = {
    # Step 4b: claude-sdk harness wrap. See
    # omnigent/inner/claude_sdk_harness.py.
    "claude-sdk": "omnigent.inner.claude_sdk_harness",
    # User-facing alias accepted in specs / Omnigent harness dispatch.
    "claude": "omnigent.inner.claude_sdk_harness",
    # Native Claude Code terminal bridge used by ``omnigent claude``.
    "claude-native": "omnigent.inner.claude_native_harness",
    # Native Codex TUI terminal bridge used by ``omnigent codex``.
    "codex-native": "omnigent.inner.codex_native_harness",
    # Step 4c: codex harness wrap. See
    # omnigent/inner/codex_harness.py.
    "codex": "omnigent.inner.codex_harness",
    # Step 4d: pi harness wrap. See
    # omnigent/inner/pi_harness.py.
    "pi": "omnigent.inner.pi_harness",
    # Native Pi TUI bridge used by ``omnigent pi``.
    "pi-native": "omnigent.inner.pi_native_harness",
    # Native Antigravity (agy) TUI terminal bridge used by
    # ``omnigent antigravity``. The in-process SDK counterpart is the
    # canonical ``antigravity`` harness registered below.
    "antigravity-native": "omnigent.inner.antigravity_native_harness",
    # Step 4e: openai-agents harness wrap. See
    # omnigent/inner/openai_agents_sdk_harness.py. Registry
    # key is the Omnigent-side spelling (``openai-agents``,
    # no ``-sdk`` suffix) to match
    # ``OmnigentExecutor``'s harness allowlist and the
    # ``executor.harness`` field used in Omnigent YAML; the
    # backing Python module retains the ``_sdk`` suffix because
    # the underlying SDK package is ``openai-agents`` and the
    # executor class is :class:`OpenAIAgentsSDKExecutor`.
    "openai-agents": "omnigent.inner.openai_agents_sdk_harness",
    # cursor harness wrap (Cursor's ``cursor-agent`` CLI, headless). See
    # omnigent/inner/cursor_harness.py.
    "cursor": "omnigent.inner.cursor_harness",
    # cursor-native harness wrap. Drives the resident ``cursor-agent`` TUI by
    # injecting each web-UI turn into its tmux pane and mirroring the transcript
    # back — a native-CLI harness like claude/codex/pi-native, so it IS in
    # ``NATIVE_HARNESSES``. See omnigent/inner/cursor_native_harness.py.
    "cursor-native": "omnigent.inner.cursor_native_harness",
    # Native Kiro TUI bridge used by ``omnigent kiro``.
    "kiro-native": "omnigent.inner.kiro_native_harness",
    # goose-native harness wrap. Drives the resident ``goose session`` TUI by
    # injecting each web-UI turn into its tmux pane and mirroring the transcript
    # back from Goose's SQLite session store — a native-CLI harness like
    # cursor-native, so it IS in ``NATIVE_HARNESSES``. See
    # omnigent/inner/goose_native_harness.py.
    "goose-native": "omnigent.inner.goose_native_harness",
    # qwen-native harness wrap. Drives the resident ``qwen`` TUI by appending
    # JSONL ``submit`` commands to its ``--input-file`` and mirroring the
    # transcript back from its ``--json-file`` event stream — a native-CLI
    # harness like goose-native, so it IS in ``NATIVE_HARNESSES``. The bare
    # ``qwen`` name stays the ACP-piped harness. See
    # omnigent/inner/qwen_native_harness.py.
    "qwen-native": "omnigent.inner.qwen_native_harness",
    # Google Antigravity SDK harness wrap. See
    # omnigent/inner/antigravity_harness.py. In-process SDK harness
    # (``google-antigravity``), like openai-agents — Omnigent spawns no CLI
    # binary or sandbox subprocess (the SDK itself launches a native
    # localharness binary; needs glibc >=~2.36). Drives Gemini 3.5 Flash by
    # default (also Claude / GPT-OSS), with Gemini API-key or Vertex AI auth.
    "antigravity": "omnigent.inner.antigravity_harness",
    # Qwen Code harness wrap. See omnigent/inner/qwen_harness.py.
    # Drives the ``qwen`` CLI in ACP mode (``qwen --acp``) for agent execution.
    "qwen": "omnigent.inner.qwen_harness",
    # Headless Goose harness wrap. See omnigent/inner/goose_harness.py.
    # Drives Block's ``goose`` CLI in ACP mode (``goose acp``) — the chat-first
    # counterpart to the terminal-first ``goose-native`` TUI harness. Tool
    # approvals surface as web elicitation cards via session/request_permission.
    "goose": "omnigent.inner.goose_harness",
    # Native OpenCode server bridge used by ``omnigent opencode``. The runner
    # owns ``opencode serve`` + an SSE forwarder and this harness injects each
    # web-UI turn over loopback HTTP — a native-server harness like
    # codex-native, so both ``opencode-native`` and its ``native-opencode``
    # alias are in ``NATIVE_HARNESSES``. See
    # omnigent/inner/opencode_native_harness.py.
    "opencode-native": "omnigent.inner.opencode_native_harness",
    # ``opencode`` is accepted as a friendly alias for the canonical
    # ``opencode-native`` (there is no separate SDK ``opencode`` harness).
    "opencode": "omnigent.inner.opencode_native_harness",
    # GitHub Copilot SDK harness wrap. See omnigent/inner/copilot_harness.py.
    # In-process SDK harness (``github-copilot-sdk``), like cursor / antigravity:
    # the SDK bundles the Copilot CLI binary it drives as a backing server, so
    # Omnigent spawns no separately-installed CLI. Authenticates against GitHub's
    # Copilot backend with a GitHub token (no Databricks gateway).
    "copilot": "omnigent.inner.copilot_harness",
    # Hermes Agent harness wrap. Runs the ``hermes`` CLI as a subprocess
    # for each turn, managing its own session state via Hermes' SQLite
    # session store. See omnigent/inner/hermes_harness.py and
    # omnigent/inner/hermes_executor.py. The ``hermes`` binary must be
    # on PATH (or set by HARNESS_HERMES_PATH).
    "hermes": "omnigent.inner.hermes_harness",
}

__all__ = ["_HARNESS_MODULES"]
