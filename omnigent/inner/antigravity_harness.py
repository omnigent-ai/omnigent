"""``harness: antigravity`` wrap.

Exposes :func:`create_app`, the entrypoint the shared
:mod:`omnigent.runtime.harnesses._runner` invokes after the parent resolves
``"antigravity"`` via :data:`omnigent.runtime.harnesses._HARNESS_MODULES`. It
wraps a :class:`~omnigent.inner.antigravity_executor.AntigravityExecutor` in an
:class:`~omnigent.runtime.harnesses._executor_adapter.ExecutorAdapter`,
configured from env vars the parent sets before spawning. Mirrors the
openai-agents wrap; see the claude-sdk module for the config-flow rationale.

Like that wrap, this is an in-process SDK harness (no CLI/sandbox subprocess),
though the SDK itself launches a native ``localharness`` binary needing a
recent glibc (see the executor note). Auth is Gemini-native (API key or
Vertex AI), so there are no ``*_GATEWAY_*`` env vars.

Env vars read at startup:

- ``HARNESS_ANTIGRAVITY_MODEL``: model the executor pins for every turn, e.g.
  ``"gemini-3.5-flash"``. Wins over per-turn ``request.model`` (which carries
  the agent NAME, not an LLM id). ``None`` falls back to the built-in default.
- ``HARNESS_ANTIGRAVITY_API_KEY``: direct Antigravity / Gemini API key. Set
  when the spec declares ``executor.auth: {type: api_key, …}`` or a global
  API-key auth resolves. Takes precedence over ambient credential lookup.
- ``HARNESS_ANTIGRAVITY_VERTEX``: ``"1"`` / ``"true"`` / ``"yes"`` to use
  Vertex AI (GCP ADC) instead of an API key.
- ``HARNESS_ANTIGRAVITY_PROJECT``: GCP project id for the Vertex AI path,
  e.g. ``"my-gcp-project"``.
- ``HARNESS_ANTIGRAVITY_LOCATION``: GCP region for the Vertex AI path,
  e.g. ``"us-central1"``.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI

from omnigent.inner.antigravity_executor import AntigravityExecutor
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)

# Env-var keys read at construction time (semantics in the module docstring);
# centralized as constants for a single grep target.
_ENV_MODEL = "HARNESS_ANTIGRAVITY_MODEL"
_ENV_API_KEY = "HARNESS_ANTIGRAVITY_API_KEY"
_ENV_VERTEX = "HARNESS_ANTIGRAVITY_VERTEX"
_ENV_PROJECT = "HARNESS_ANTIGRAVITY_PROJECT"
_ENV_LOCATION = "HARNESS_ANTIGRAVITY_LOCATION"

# Truthy spellings accepted for the boolean Vertex flag.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _build_antigravity_executor() -> Executor:
    """Construct an :class:`AntigravityExecutor` from env-var config.

    Called lazily by the :class:`ExecutorAdapter` on the first turn, so an
    absent ``google-antigravity`` package surfaces as a request-time error
    rather than an app-boot crash.

    :returns: A configured :class:`AntigravityExecutor` instance.
    """
    return AntigravityExecutor(
        model=os.environ.get(_ENV_MODEL) or None,
        api_key=os.environ.get(_ENV_API_KEY) or None,
        vertex=os.environ.get(_ENV_VERTEX, "").strip().lower() in _TRUTHY,
        project=os.environ.get(_ENV_PROJECT) or None,
        location=os.environ.get(_ENV_LOCATION) or None,
    )


def create_app() -> FastAPI:
    """Build the antigravity harness's FastAPI app.

    Required entry point per the harness contract: the runner imports this
    module and calls ``create_app()`` to get the app it serves. The wrapped
    :class:`AntigravityExecutor` is constructed lazily on the first turn.

    :returns: The FastAPI app from :class:`ExecutorAdapter`'s
        :meth:`build` method.
    """
    adapter = ExecutorAdapter(executor_factory=_build_antigravity_executor)
    return adapter.build()
