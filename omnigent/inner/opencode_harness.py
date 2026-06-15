"""``harness: opencode`` wrap.

Thin module exposing :func:`create_app` — the entrypoint the shared
:mod:`omnigent.runtime.harnesses._runner` invokes after the parent
process resolves ``"opencode"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Internally, instantiates
:class:`omnigent.runtime.harnesses._executor_adapter.ExecutorAdapter`
around an :class:`omnigent.inner.opencode_executor.OpenCodeExecutor`
configured from env vars the parent process sets before spawning.
Mirrors the ``codex`` / ``claude-sdk`` / ``supervisor`` wraps; the
OpenCode wrap is the leanest of the bunch because the underlying CLI
manages its own credentials, sessions, and tools.

Env vars read at startup:

- ``HARNESS_OPENCODE_MODEL``: model identifier in OpenCode's
  ``provider/model`` form, e.g. ``"anthropic/claude-sonnet-4-5"``
  or ``"openai/gpt-5"``. ``None`` lets the CLI use its configured
  default.
- ``HARNESS_OPENCODE_AGENT``: optional agent name passed via
  ``--agent``; ``None`` uses the default agent.
- ``HARNESS_OPENCODE_CWD``: working directory the CLI launches in
  (forwarded as ``--dir``). ``None`` falls back to the harness
  subprocess's inherited cwd.
- ``HARNESS_OPENCODE_PATH``: absolute path to an ``opencode`` CLI
  binary. ``None`` searches ``PATH``.
- ``HARNESS_OPENCODE_VARIANT``: optional reasoning-effort variant
  name forwarded via ``--variant``.
- ``HARNESS_OPENCODE_THINKING``: ``"1"`` / ``"true"`` to enable
  ``--thinking``, surfacing reasoning blocks as
  :class:`ReasoningChunk` events. Default off — matches the CLI.
- ``HARNESS_OPENCODE_DANGEROUSLY_SKIP_PERMISSIONS``: ``"1"`` /
  ``"true"`` to pass ``--dangerously-skip-permissions``.
  **Defaults to ``True``** because a headless meta-harness has
  nowhere to surface interactive permission prompts; set to
  ``"0"`` only when you've arranged for permission UI elsewhere.

Gateway-routing env vars — synthesise an ``OPENCODE_CONFIG_CONTENT``
provider override per ``packages/opencode/src/config/config.ts``.
The override is layered on top of the user's global
``~/.config/opencode/config.json`` (non-destructive); the executor
also sets ``OPENCODE_DISABLE_PROJECT_CONFIG=1`` whenever any of
these are non-empty, so a user-project ``opencode.json`` cannot
silently re-introduce a provider/MCP entry the operator wanted
suppressed.

- ``HARNESS_OPENCODE_GATEWAY_PROVIDER``: provider id whose
  ``options`` get overridden, e.g. ``"anthropic"`` (default) or
  ``"openai"``. Unset → no override.
- ``HARNESS_OPENCODE_GATEWAY_BASE_URL``: ``options.baseURL`` for the
  chosen provider. Typical value: a Databricks AI gateway endpoint
  like ``https://<workspace>.databricks.com/serving-endpoints/<id>``.
- ``HARNESS_OPENCODE_GATEWAY_API_KEY``: ``options.apiKey`` for the
  chosen provider. Required alongside ``BASE_URL`` for the
  gateway-routing override to take effect.

MCP-bridge env var:

- ``HARNESS_OPENCODE_MCP_SERVERS``: JSON object of
  ``{server_name: ConfigMCPV1.Info}`` entries merged into the
  ``OPENCODE_CONFIG_CONTENT`` ``mcp`` map. Used by the workflow to
  point OpenCode at an Omnigent-owned MCP endpoint so spec tools
  round-trip through the standard dispatch path. Include
  ``{"<server>": {"enabled": false}}`` entries to suppress any of
  the user's globally-registered MCP servers that would otherwise
  leak in.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from omnigent.inner.executor import Executor
from omnigent.inner.opencode_executor import OpenCodeExecutor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)


def _build_opencode_executor() -> Executor:
    """Construct an inner :class:`OpenCodeExecutor`.

    The wrapper itself is cheap (no subprocess work) — the CLI is
    spawned lazily on the first :meth:`run_turn`. Binary lookup
    failures therefore surface on the first request, not at FastAPI
    boot, so a missing ``opencode`` install only fails the affected
    session.
    """
    return OpenCodeExecutor()


def create_app() -> FastAPI:
    """Build the OpenCode harness's FastAPI app.

    Required entry point per the harness contract — the runner
    imports this module (resolved from
    :data:`omnigent.runtime.harnesses._HARNESS_MODULES`) and
    invokes ``create_app()`` to get the app it serves.
    """
    adapter = ExecutorAdapter(executor_factory=_build_opencode_executor)
    return adapter.build()
