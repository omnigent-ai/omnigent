"""
``harness: openai-agents`` wrap.

Thin module exposing :func:`create_app` — the entrypoint the
shared :mod:`omnigent.runtime.harnesses._runner` invokes after
the parent process resolves ``"openai-agents"`` to this module
via :data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

The registry key is ``"openai-agents"`` (matching the Omnigent
YAML ``executor.harness`` spelling and ``OmnigentExecutor``'s
existing harness allowlist); the Python module retains the ``_sdk``
suffix because the underlying SDK package is ``openai-agents`` and
the executor class is :class:`OpenAIAgentsSDKExecutor`.

Internally, instantiates :class:`omnigent.runtime.harnesses._executor_adapter.ExecutorAdapter`
around a :class:`omnigent.inner.openai_agents_sdk_executor.OpenAIAgentsSDKExecutor`
configured from env vars the parent process sets before spawning.
Mirrors the claude-sdk wrap (``claude_sdk_harness.py``), codex
wrap (``codex_harness.py``), and pi wrap (``pi_harness.py``); see
the claude-sdk module's docstring for the v1 config-flow rationale
(env vars vs per-request).

OpenAI Agents SDK is the **simplest** of the four wrapped
harnesses because:

- No CLI binary — pure-Python ``openai-agents`` package, so no
  PATH check / no ``cli_binary`` field on the harness probe.
- No sandbox — the Python SDK runs in-process; there's no
  CLI subprocess to wrap with bwrap.
- No ``os_env`` field — the SDK doesn't host file/shell tools
  the way claude-sdk / codex / pi do; ``sys_os_*`` builtins
  travel through AP's tool surface as usual.
- Model is still a simple constructor override from the spawn env,
  not a CLI/runtime concern. The wrap also uses this model name to
  select the correct OpenAI Agents SDK endpoint default for models
  that need it.

Env vars read at startup:

- ``HARNESS_OPENAI_AGENTS_MODEL``: model identifier the
  inner executor pins for every turn, e.g.
  ``"databricks-gpt-5-4-mini"``. Constructor-level override —
  wins over the per-turn ``request.model`` (which under the
  harness contract carries the agent NAME, not an LLM
  identifier). ``None`` falls back to ``cfg.model`` then the
  executor's built-in default.
- ``HARNESS_OPENAI_AGENTS_DATABRICKS_PROFILE``: Databricks-specific
  profile from ``~/.databrickscfg`` to use, e.g. ``"<your-profile>"``.
  Single canonical spelling — same as the AP-side spawn-env
  builder
  :func:`omnigent.runtime.workflow._build_openai_agents_sdk_spawn_env`
  and the parametrized test fixture
  :data:`tests.e2e._harness_probes.HarnessProbe.env_prefix`.
  Unlike the claude-sdk / codex / pi wraps there's no
  ``GATEWAY=true`` truthy gate — those wraps construct their
  inner executor with ``gateway=...`` boolean kwargs that
  flip credential resolution; ``OpenAIAgentsSDKExecutor``
  takes the profile name directly and resolves credentials
  itself, so a separate gate would be dead surface.
- ``HARNESS_OPENAI_AGENTS_GATEWAY_HOST``: gateway workspace host
  origin, e.g. ``"https://example.databricks.com"``. When set, the
  inner executor skips profile host lookup and refreshes auth
  through the gateway auth command.
- ``HARNESS_OPENAI_AGENTS_GATEWAY_BASE_URL``: OpenAI-compatible gateway
  base URL, e.g.
  ``"https://example.databricks.com/ai-gateway/codex/v1"``.
- ``HARNESS_OPENAI_AGENTS_API_KEY``: direct OpenAI-compatible API
  key, written when the agent spec declares
  ``executor.auth: {type: api_key, api_key: …}``. Takes precedence
  over the ambient ``OPENAI_API_KEY`` env var so the spec is
  self-contained. Mutually exclusive with
  ``HARNESS_OPENAI_AGENTS_DATABRICKS_PROFILE``.
- ``HARNESS_OPENAI_AGENTS_USE_RESPONSES``: ``"1"`` / ``"true"``
  to use the OpenAI ``/responses`` endpoint (default); any other
  truthy-parsing-rejected value falls back to
  ``/chat/completions``. Databricks-hosted NON-GPT models
  (any ``databricks-*`` id without the ``gpt`` token, e.g.
  ``databricks-claude-*``, ``databricks-kimi-*``,
  ``databricks-meta-llama-*``) default to ``/chat/completions``
  because the Databricks gateway only serves GPT over the Responses
  wire — every other model it hosts speaks chat/completions, so
  ``/responses`` 404s. Databricks GPT models keep the Responses
  default. An explicit env-var value still wins as the
  highest-priority switch, so bad specs fail loudly at the gateway
  instead of being silently rewritten.
- ``HARNESS_OPENAI_AGENTS_REASONING_ITEM_ID_POLICY``: optional Responses
  replay policy, either ``"preserve"`` or ``"omit"``. Unset uses the
  OpenAI Agents SDK default.
"""

from __future__ import annotations

import logging
import os
from typing import cast

from fastapi import FastAPI

from omnigent.inner.executor import Executor
from omnigent.inner.openai_agents_sdk_executor import (
    OpenAIAgentsSDKExecutor,
    ReasoningItemIdPolicy,
)
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_logger = logging.getLogger(__name__)

# Env-var keys the wrap reads at executor construction time. See
# the module docstring for semantics. Centralizing as constants
# so misconfigurations surface as a single grep target.
_ENV_MODEL = "HARNESS_OPENAI_AGENTS_MODEL"
_ENV_DATABRICKS_PROFILE = "HARNESS_OPENAI_AGENTS_DATABRICKS_PROFILE"
_ENV_GATEWAY_HOST = "HARNESS_OPENAI_AGENTS_GATEWAY_HOST"
_ENV_USE_RESPONSES = "HARNESS_OPENAI_AGENTS_USE_RESPONSES"
_ENV_GATEWAY_BASE_URL = "HARNESS_OPENAI_AGENTS_GATEWAY_BASE_URL"
_ENV_GATEWAY_AUTH_COMMAND = "HARNESS_OPENAI_AGENTS_GATEWAY_AUTH_COMMAND"
_ENV_REASONING_ITEM_ID_POLICY = "HARNESS_OPENAI_AGENTS_REASONING_ITEM_ID_POLICY"
# Direct OpenAI-compatible API key set when the agent spec declares
# executor.auth: {type: api_key, api_key: …}. Takes precedence over
# ambient OPENAI_API_KEY in the caller's environment.
_ENV_API_KEY = "HARNESS_OPENAI_AGENTS_API_KEY"

# Truthy strings the wrap accepts for boolean env vars. Must
# match the claude-sdk / codex / pi wraps' parsers for
# consistency — operators learn one set of conventions, not five.
_TRUTHY_STRINGS = ("1", "true", "yes")


def _is_databricks_non_gpt_model(model: str | None) -> bool:
    """
    Return whether *model* is a Databricks-hosted NON-GPT model.

    Only the Databricks gateway's GPT serving path speaks the OpenAI
    Responses wire; every other model it hosts (Kimi, Claude,
    Llama, …) is served over chat/completions, and pointing the
    Agents SDK at ``/responses`` for them 404s or mishandles the
    surface. So the harness defaults those to ``use_responses=False``
    instead of requiring every YAML to carry ``use_responses: false``.
    GPT models keep the Responses-API default. Databricks Kimi —
    the model this carve-out was originally written for — is a strict
    subset of this rule.

    :param model: Model identifier from
        ``HARNESS_OPENAI_AGENTS_MODEL``, e.g.
        ``"databricks-claude-sonnet-4-6"`` or
        ``"databricks/databricks-kimi-k2-6"``. ``None`` means no
        model was configured at harness construction.
    :returns: ``True`` when *model* is a ``databricks-`` prefixed id
        (case-insensitive, after stripping an optional leading
        ``"databricks/"`` provider prefix) that does NOT contain the
        ``"gpt"`` vendor token; ``False`` otherwise (including for
        non-Databricks models, whose endpoint default is unchanged).
    """
    if model is None:
        return False
    normalized = model.lower()
    if normalized.startswith("databricks/"):
        normalized = normalized.removeprefix("databricks/")
    if not normalized.startswith("databricks-"):
        return False
    return "gpt" not in normalized


def _parse_truthy(env_var: str, default: bool) -> bool:
    """
    Parse a boolean-style env var the same way the claude-sdk /
    codex / pi wraps do.

    :param env_var: The env-var name (e.g.
        ``HARNESS_OPENAI_AGENTS_USE_RESPONSES``).
    :param default: The fallback when the env var is unset or
        empty.
    :returns: ``True`` if the value is in :data:`_TRUTHY_STRINGS`
        (case-insensitive); ``False`` for any other non-empty
        value; *default* when unset or empty.
    """
    raw = os.environ.get(env_var, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY_STRINGS


def _build_openai_agents_sdk_executor() -> Executor:
    """
    Construct an :class:`OpenAIAgentsSDKExecutor` from env-var
    config.

    Called lazily by the :class:`ExecutorAdapter` on the first
    turn. Heavyweight init (eager Databricks credential
    resolution if a profile is set) happens at this point —
    operators see the failure surface as a startup error on the
    first request, not at FastAPI app boot.

    :returns: A configured :class:`OpenAIAgentsSDKExecutor`
        instance.
    :raises ImportError: If the ``openai-agents`` package isn't
        installed — the inner executor's ``_ensure_agents_sdk``
        surfaces this as a clear ImportError on first
        :meth:`run_turn` call.
    :raises OSError: If ``HARNESS_OPENAI_AGENTS_DATABRICKS_PROFILE``
        is set but credentials are missing AND the env-var
        fallback also fails — the inner executor's
        :func:`_get_openai_async_client` fails loud with a
        message naming the profile.
    """
    # Single canonical spelling for the profile env var:
    # ``DATABRICKS_PROFILE`` (Databricks-specific). The AP-side
    # spawn-env builder always emits this name; the parametrized
    # harness wrap e2e fixture sets the same name. There is no
    # ``GATEWAY=true`` gate here — if the cfg file is unreachable
    # :func:`_get_openai_async_client` falls through to the
    # env-var path on its own.
    profile = os.environ.get(_ENV_DATABRICKS_PROFILE) or None
    api_key = os.environ.get(_ENV_API_KEY) or None
    model = os.environ.get(_ENV_MODEL) or None
    default_use_responses = not _is_databricks_non_gpt_model(model)
    use_responses = _parse_truthy(
        _ENV_USE_RESPONSES,
        default=default_use_responses,
    )
    reasoning_item_id_policy = cast(
        ReasoningItemIdPolicy | None,
        os.environ.get(_ENV_REASONING_ITEM_ID_POLICY) or None,
    )
    return OpenAIAgentsSDKExecutor(
        profile=profile,
        api_key=api_key,
        use_responses=use_responses,
        model=model,
        base_url_override=os.environ.get(_ENV_GATEWAY_BASE_URL) or None,
        gateway_host=os.environ.get(_ENV_GATEWAY_HOST) or None,
        gateway_auth_command=os.environ.get(_ENV_GATEWAY_AUTH_COMMAND) or None,
        reasoning_item_id_policy=reasoning_item_id_policy,
    )


def create_app() -> FastAPI:
    """
    Build the openai-agents-sdk harness's FastAPI app.

    Required entry point per the harness contract — the runner
    imports this module (resolved from
    :data:`omnigent.runtime.harnesses._HARNESS_MODULES`) and
    invokes ``create_app()`` to get the app it serves.

    :returns: The FastAPI app from :class:`ExecutorAdapter`'s
        :meth:`build` method, with all routes from the harness
        API subset wired up. The wrapped
        :class:`OpenAIAgentsSDKExecutor` is constructed lazily
        on the first turn (so an absent ``openai-agents``
        package surfaces as a request-time error, not a FastAPI
        app-boot crash).
    """
    adapter = ExecutorAdapter(executor_factory=_build_openai_agents_sdk_executor)
    return adapter.build()
