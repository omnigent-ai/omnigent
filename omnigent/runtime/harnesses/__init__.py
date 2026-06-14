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
    # Supervisor harness wrap. See
    # omnigent/inner/databricks_supervisor_harness.py. Drives the Databricks
    # Agent Bricks Supervisor API at
    # ``{workspace}/ai-gateway/mlflow/v1/responses``. Differs from
    # the SDK-wrapping harnesses above in that the inner executor
    # has no third-party SDK dependency — it talks HTTP / SSE
    # directly to the Databricks gateway.
    "databricks_supervisor": "omnigent.inner.databricks_supervisor_harness",
}

__all__ = ["_HARNESS_MODULES"]
