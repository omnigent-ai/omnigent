"""
``harness: opencode`` wrap.

Thin module exposing :func:`create_app` — the entrypoint the
shared :mod:`omnigent.runtime.harnesses._runner` invokes after
the parent process resolves ``"opencode"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Internally, instantiates
:class:`omnigent.runtime.harnesses._executor_adapter.ExecutorAdapter`
around a :class:`omnigent.inner.opencode_executor.OpenCodeExecutor`.
Mirrors the claude-sdk, codex, pi, and openai-agents-sdk wraps; see
the openai-agents-sdk module's docstring for the v1 config-flow
rationale (env vars vs per-request).

The transport drives OpenCode via a persistent ``opencode serve``
process + the ``opencode-ai`` Python SDK (``AsyncOpencode``), not a
per-turn ``opencode run`` invocation.  This unlocks mid-turn interrupt,
live message queue, and in-process MCP tool-bridge — all unavailable in
the single-shot CLI path.

Env vars read at startup (all optional):

- ``HARNESS_OPENCODE_MODEL``: model identifier in
  ``"provider/model"`` form (e.g. ``"anthropic/claude-sonnet-4-5"``)
  or bare model id (e.g. ``"gpt-5"``).  Constructor-level override —
  wins over the per-turn ``request.model_override`` which carries the
  agent name under the harness contract.  ``None`` falls back to the
  server's configured default provider/model.
- ``HARNESS_OPENCODE_CWD``: working directory the ``opencode serve``
  process (and the agents it spawns) will use.  Defaults to the
  harness process CWD.
- ``HARNESS_OPENCODE_PATH``: absolute path to the ``opencode`` binary.
  Falls back to ``shutil.which("opencode")`` when unset.
- ``HARNESS_OPENCODE_THINKING``: ``"1"``/``"true"``/``"yes"``/``"on"``
  to surface reasoning parts as :class:`~omnigent.inner.executor.ReasoningChunk`
  events.  Silently dropped otherwise.
- ``HARNESS_OPENCODE_DANGEROUSLY_SKIP_PERMISSIONS``: when set to a
  falsy value (``"0"``/``"false"``/``"no"``/``"off"``), the default
  skip-permissions behaviour is disabled.  Defaults to ``True`` when
  unset (i.e. permissions are skipped by default).
- ``HARNESS_OPENCODE_GATEWAY_PROVIDER``: OpenCode provider id for the
  gateway override, e.g. ``"anthropic"``.  Used together with
  ``HARNESS_OPENCODE_GATEWAY_BASE_URL`` / ``HARNESS_OPENCODE_GATEWAY_API_KEY``
  to synthesise the ``OPENCODE_CONFIG_CONTENT`` env var that OpenCode
  reads at startup.
- ``HARNESS_OPENCODE_GATEWAY_BASE_URL``: provider base URL for the
  gateway, e.g. ``"https://example.databricks.com/ai-gateway/anthropic/v1"``.
- ``HARNESS_OPENCODE_GATEWAY_API_KEY``: API key for the gateway.  Set
  when the agent spec declares ``executor.auth: {type: api_key, …}``.
- ``HARNESS_OPENCODE_API_KEY``: an OpenCode Zen / Go account key,
  exported to the ``opencode serve`` process as ``OPENCODE_API_KEY``
  (OpenCode's own auth resolves it for both the ``opencode`` and
  ``opencode-go`` provider ids).  Set when the agent's provider entry
  declares an ``opencode:`` family.
- ``HARNESS_OPENCODE_MCP_SERVERS``: JSON object whose keys are MCP
  server names and whose values are the server-info dicts OpenCode
  accepts (``{"type": "remote", "url": "…"}``).  Merged with the
  in-process Omnigent tool-bridge entry before OpenCode starts.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from omnigent.inner.executor import Executor
from omnigent.inner.opencode_executor import OpenCodeExecutor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)


def _build_opencode_executor() -> Executor:
    """Construct the inner :class:`OpenCodeExecutor` (server spawned lazily)."""
    return OpenCodeExecutor()


def create_app() -> FastAPI:
    """Build the OpenCode harness's FastAPI app (per the harness contract).

    Required entry point per the harness contract — the runner imports
    this module (resolved from
    :data:`omnigent.runtime.harnesses._HARNESS_MODULES`) and invokes
    ``create_app()`` to get the app it serves.

    :returns: The FastAPI app from :class:`ExecutorAdapter`'s
        :meth:`build` method, with all routes from the harness API
        subset wired up.  The wrapped :class:`OpenCodeExecutor` is
        constructed lazily on the first turn (so a missing
        ``opencode`` binary surfaces as a request-time error, not a
        FastAPI app-boot crash).
    """
    adapter = ExecutorAdapter(executor_factory=_build_opencode_executor)
    return adapter.build()
