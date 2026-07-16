"""Agent execution workflow — the core agent loop.

Load agent → build prompt → call LLM → execute tools → repeat.
All durably checkpointed for crash recovery.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml

if TYPE_CHECKING:
    # Lazy: PolicyEngine + PolicyResult are pulled in at runtime via
    # the function-local imports inside ``_handle_policy_ask`` and
    # ``_enforce_policy``-using helpers (see uses below). The
    # TYPE_CHECKING import is only here so static type-checkers + ruff
    # can resolve the names in deferred annotations (``from __future__
    # import annotations`` is in effect).
    from omnigent.inner.datamodel import OSEnvSpec

from omnigent.entities import (
    NON_CONTENT_ITEM_TYPES,
    CompactionData,
    ConversationItem,
    NewConversationItem,
)
from omnigent.env_credentials import expand_envvars_with_omnigent_prefix
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.llms import Client as LLMClient
from omnigent.onboarding.databricks_config import (
    DATABRICKS_CLAUDE_DEFAULT_MODEL,
    get_workspace_url_for_profile,
)
from omnigent.onboarding.detected import (
    codex_config_provider_dismissed,
    effective_config_with_detected,
)
from omnigent.onboarding.provider_config import (
    ANTHROPIC_FAMILY,
    BEDROCK_KIND,
    CLI_CONFIG_KIND,
    DATABRICKS_KIND,
    OPENAI_FAMILY,
    RESPONSES_WIRE_API,
    SUBSCRIPTION_KIND,
    FamilyConfig,
    ProviderEntry,
    default_provider_for_harness,
    first_available_provider,
    harness_family,
    load_config,
    load_providers,
)
from omnigent.onboarding.ucode_state import UcodeAgentState, read_ucode_state
from omnigent.runtime import (
    get_artifact_store,
    get_conversation_store,
    get_file_store,
)
from omnigent.runtime.compaction import (
    CompactionResult,
    SummaryMetadata,
    _CompactionState,
    compact,
    compaction_to_history_items,
    count_tokens,
)
from omnigent.runtime.content_resolver import resolve_content_references
from omnigent.runtime.prompt import build_instructions, history_to_input_items
from omnigent.spec import AgentSpec
from omnigent.spec.parser import check_unresolved_env_vars
from omnigent.spec.types import (
    ApiKeyAuth,
    DatabricksAuth,
    LLMConfig,
    ProviderAuth,
    RetryPolicy,
)
from omnigent.stores import ConversationStore

# ── Module-level constants ────────────────────────────────────

_logger = logging.getLogger(__name__)

# Task kind for background `@tool(synchronous=False)` work items —
# the unit the parent loop separates from the polling-based
# sub-agent path so each kind uses the right collection mechanism.
# Kinds whose completion arrives via the async_work_complete drain
# and that can block the parent turn from finalizing. Tools,
# sub-agents, and client tools belong here: they represent jobs the
# parent expects to finish, and blocking the turn so the LLM sees
# the result in-line is a real UX win.

# Per-payload character cap for sub-agent output piggy-backed on
# the async_work_complete signal (matches the @tool path's
# ``truncate_for_llm`` budget — keeps the LLM-facing system
# message under control regardless of which kind produced it).

# G20: cadence for `response.heartbeat` SSE events emitted while
# the parent loop is blocked on the async-tool drain. 15 s keeps
# proxies that close idle connections at 30 s safely under their
# threshold without flooding the channel with pings.

# How often the blocking drain wakes to poll the conversation
# store for steering messages. Decoupled from
# ``_HEARTBEAT_INTERVAL_S`` because they answer different
# questions: heartbeat is for SSE-proxy keepalive (ceiling on
# how long a connection can stay idle), steering poll is for
# user-perceived latency (floor on how quickly the agent
# reacts to a mid-flight "hello"). 1 s is fast enough that
# steering feels instant while keeping conv_store load to
# 1 QPS per blocked workflow.

# Generic type variable used by ``_to_thread`` (pure helper).

# Hard upper bound on LLM turns per execution. Prevents runaway
# loops. See designs/AGENTLOOP.md "Not Yet" for making this
# configurable.

# SSE event types emitted for reasoning content (set by the
# streaming accumulator and consumed by the terminal frontend).

# Executor storage layout — each (conversation, agent) gets a
# stable subdir under ``_EXECUTOR_STORAGE_BASE`` that persists
# across tasks. The artifact-store key prefix mirrors the disk
# layout so snapshots round-trip cleanly.

# Client-side tool result polling — used by
# ``_build_await_tool_output`` while waiting for a PATCH'd
# function_call_output to arrive. 50 ms keeps interactive REPL
# turns snappy (a 5-tool turn loses at most 250 ms to polling
# instead of 2.5 s) while keeping the DB query cost negligible
# (~20 short SELECTs/sec while waiting). See
# designs/REPL_STREAMING_THROUGHPUT.md bottleneck #2.
# Translates Omnigent spec executor types (underscore) to harness subprocess names
# (hyphen), e.g. ``"claude_sdk"`` → ``"claude-sdk"`` used by ``_HARNESS_MODULES``.


AgentHarnessType = Literal[
    "claude-sdk", "codex", "pi", "openai-agents-sdk", "antigravity", "kimi", "qwen", "goose"
]


@dataclass(frozen=True)
class UcodeHarnessConfig:
    """Env-var mapping for one harness's ucode agent state.

    :param agent_name: ucode agent key, e.g. ``"claude"`` or ``"codex"``.
    :param model_key: Harness model env var.
    :param base_url_key: Harness gateway base URL env var.
    :param base_url_family: Optional provider key to use when the ucode agent
        entry only has provider-specific base URLs, e.g. ``"claude"``.
    :param base_urls_key: Optional harness gateway base URLs env var for
        agents with multiple provider URLs.
    :param host_key: Harness gateway workspace host env var.
    :param auth_key: Optional harness gateway auth command env var.
    :param refresh_key: Optional harness gateway auth refresh interval env var.
    :param databricks_default_model: Fallback model id to use on the
        Databricks gateway path when neither the spec nor the ucode
        state names a model, e.g. ``"databricks-claude-opus-4-8"``.
        ``None`` for harnesses with no confirmed Databricks default.
        Required because the Databricks AI gateway only routes
        ``databricks-*`` endpoint names, so the CLI's own host-config
        default (an Anthropic-direct id) is not a usable fallback there.
    """

    agent_name: str
    model_key: str
    base_url_key: str
    base_url_family: str | None
    base_urls_key: str | None
    host_key: str
    auth_key: str | None
    refresh_key: str | None
    databricks_default_model: str | None = None


_UCODE_HARNESS_CONFIGS: dict[AgentHarnessType, UcodeHarnessConfig] = {
    "claude-sdk": UcodeHarnessConfig(
        agent_name="claude",
        model_key="HARNESS_CLAUDE_SDK_MODEL",
        base_url_key="HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL",
        base_url_family="claude",
        base_urls_key=None,
        host_key="HARNESS_CLAUDE_SDK_GATEWAY_HOST",
        auth_key="HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND",
        refresh_key="HARNESS_CLAUDE_SDK_GATEWAY_AUTH_REFRESH_INTERVAL_MS",
        # The executor only applies this on the profile-derived gateway path,
        # so the producer must supply it on the ucode-cached path.
        databricks_default_model=DATABRICKS_CLAUDE_DEFAULT_MODEL,
    ),
    "codex": UcodeHarnessConfig(
        agent_name="codex",
        model_key="HARNESS_CODEX_MODEL",
        base_url_key="HARNESS_CODEX_GATEWAY_BASE_URL",
        base_url_family="codex",
        base_urls_key=None,
        host_key="HARNESS_CODEX_GATEWAY_HOST",
        auth_key="HARNESS_CODEX_GATEWAY_AUTH_COMMAND",
        refresh_key="HARNESS_CODEX_GATEWAY_AUTH_REFRESH_INTERVAL_MS",
    ),
    "pi": UcodeHarnessConfig(
        agent_name="pi",
        model_key="HARNESS_PI_MODEL",
        base_url_key="HARNESS_PI_GATEWAY_BASE_URL",
        base_url_family="claude",
        base_urls_key="HARNESS_PI_GATEWAY_BASE_URLS",
        host_key="HARNESS_PI_GATEWAY_HOST",
        auth_key="HARNESS_PI_GATEWAY_AUTH_COMMAND",
        refresh_key="HARNESS_PI_GATEWAY_AUTH_REFRESH_INTERVAL_MS",
        # Same parity as claude-sdk: the executor only defaults on the
        # profile-derived gateway path, so the producer must supply it here.
        databricks_default_model=DATABRICKS_CLAUDE_DEFAULT_MODEL,
    ),
    "openai-agents-sdk": UcodeHarnessConfig(
        agent_name="codex",
        model_key="HARNESS_OPENAI_AGENTS_MODEL",
        base_url_key="HARNESS_OPENAI_AGENTS_GATEWAY_BASE_URL",
        base_url_family="codex",
        base_urls_key=None,
        host_key="HARNESS_OPENAI_AGENTS_GATEWAY_HOST",
        auth_key="HARNESS_OPENAI_AGENTS_GATEWAY_AUTH_COMMAND",
        refresh_key=None,
    ),
    "qwen": UcodeHarnessConfig(
        agent_name="qwen",
        model_key="HARNESS_QWEN_MODEL",
        base_url_key="HARNESS_QWEN_GATEWAY_BASE_URL",
        base_url_family="openai",
        base_urls_key=None,
        host_key="HARNESS_QWEN_GATEWAY_HOST",
        auth_key="HARNESS_QWEN_GATEWAY_AUTH_COMMAND",
        refresh_key=None,
    ),
    # NB: ``antigravity`` is intentionally absent. Unlike the gateway
    # harnesses above, the Antigravity SDK authenticates Gemini-natively
    # (API key or Vertex AI) and has no OpenAI-compatible ``base_url``, so it
    # has no ucode gateway entry — ``_build_antigravity_spawn_env`` threads
    # ``HARNESS_ANTIGRAVITY_API_KEY`` / ``_VERTEX`` directly.
}


# Lazy singleton — created on first LLM call so import doesn't
# fail when provider API keys are not yet set.
_llm_client: LLMClient | None = None


def _get_llm_client() -> LLMClient:
    """Return the shared LLM client, creating it on first use."""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client


def _get_runner_client_for_compaction(
    conversation_id: str | None,
) -> Any | None:
    """
    Return the httpx client for the runner handling *conversation_id*.

    Used by compaction call sites so Layer 2 summarization is routed
    through the runner's own credentials (e.g. the user's Databricks
    profile) instead of the Omnigent server's.

    Returns ``None`` when:
    - ``conversation_id`` is ``None`` (in-process / no-server mode).
    - No ``RunnerRouter`` is wired up (unit tests, direct execution).
    - The conversation is not yet pinned to a runner.

    Propagates :class:`OmnigentError` when the pinned runner is
    offline so the caller can surface the error cleanly.

    :param conversation_id: Conversation id, e.g.
        ``"conv_0123456789abcdef"``, or ``None``.
    :returns: An :class:`httpx.AsyncClient` tunneled to the runner,
        or ``None`` when no runner is available.
    """
    if conversation_id is None:
        return None
    from omnigent.runtime import get_runner_router

    router = get_runner_router()
    if router is None:
        return None
    routed = router.client_for_existing_conversation(conversation_id)
    return routed.client if routed else None


def configure_agent_harness_with_ucode(
    env: dict[str, str],
    profile: str | None,
    *,
    harness_type: AgentHarnessType,
) -> None:
    """Inject per-harness model, URL, and auth values from ucode state.

    The harness-specific constants live here so callers only declare which
    agent harness they are configuring. ucode's per-agent ``agents`` entries
    are the source of truth for gateway URLs and auth commands.

    :param env: Mutable spawn-env dict, modified in place.
    :param profile: The ``executor.profile`` / provider-config value,
        e.g. ``"oss"``.  ``None`` short-circuits the entire lookup.
    :param harness_type: Canonical harness type, e.g. ``"claude-sdk"``.
    """
    if not profile:
        return
    workspace_url = get_workspace_url_for_profile(profile)
    if workspace_url is None:
        return
    state = read_ucode_state(workspace_url)
    if state is None:
        return
    config = _UCODE_HARNESS_CONFIGS[harness_type]
    agent_state = state.agent(config.agent_name)
    if agent_state is None:
        return
    _inject_ucode_agent_state(
        env,
        agent_state,
        model_key=config.model_key,
        base_url_key=config.base_url_key,
        base_url_family=config.base_url_family,
        base_urls_key=config.base_urls_key,
        host_key=config.host_key,
        auth_key=config.auth_key,
        refresh_key=config.refresh_key,
        workspace_url=state.workspace_host,
    )
    # When ucode caches no model, default it so the CLI doesn't fall back to
    # its host-config model (an Anthropic-direct id the gateway rejects).
    if config.model_key not in env and config.databricks_default_model:
        env[config.model_key] = config.databricks_default_model


def _inject_ucode_agent_state(
    env: dict[str, str],
    state: UcodeAgentState,
    *,
    model_key: str,
    base_url_key: str,
    base_url_family: str | None,
    base_urls_key: str | None,
    host_key: str,
    auth_key: str | None,
    refresh_key: str | None,
    workspace_url: str,
) -> None:
    """Copy one ucode agent entry into harness env vars.

    :param env: Mutable spawn-env dict, modified in place.
    :param state: Parsed ucode per-agent state.
    :param model_key: Harness model env var, e.g. ``"HARNESS_CODEX_MODEL"``.
    :param base_url_key: Harness gateway base URL env var.
    :param base_url_family: Optional provider key to use when ``state`` has
        provider-specific base URLs instead of a single base URL.
    :param base_urls_key: Optional harness gateway base URLs env var for
        agents with multiple provider URLs.
    :param host_key: Harness gateway workspace host env var.
    :param auth_key: Optional harness gateway auth command env var.
    :param refresh_key: Optional harness gateway auth refresh interval env var.
    :param workspace_url: Workspace URL for token refresh commands.
    """
    if model_key not in env and state.model:
        env[model_key] = state.model
    base_url = state.base_url
    if base_url is None and base_url_family is not None:
        base_url = state.base_urls.get(base_url_family)
    if base_url:
        env[base_url_key] = base_url
    if base_urls_key and state.base_urls:
        env[base_urls_key] = json.dumps(state.base_urls, sort_keys=True)
    env[host_key] = workspace_url
    if auth_key and state.auth_command:
        env[auth_key] = state.auth_command
    if refresh_key and state.auth_refresh_interval_ms is not None:
        env[refresh_key] = str(state.auth_refresh_interval_ms)


# Maps single-family harnesses to the generic-provider family they consume.
# (``pi`` is handled separately — it consumes both families.) The keys are
# the canonical harness names used by the Chunk-1a provider-config layer
# (``omnigent/onboarding/provider_config.py`` ``_HARNESS_FAMILY``);
# ``openai-agents`` (no ``-sdk``) is that layer's name for the
# openai-agents-sdk harness, so :func:`_provider_harness_name` translates.
_PROVIDER_HARNESS_FAMILY: dict[AgentHarnessType, str] = {
    "claude-sdk": ANTHROPIC_FAMILY,
    "codex": OPENAI_FAMILY,
    "openai-agents-sdk": OPENAI_FAMILY,
    # Antigravity is Gemini-native but routes generic-provider traffic over
    # the OpenAI-compatible wire (OpenRouter / LiteLLM / Databricks gateway),
    # so it consumes the ``openai`` family like openai-agents-sdk.
    "antigravity": OPENAI_FAMILY,
    # Qwen Code routes through OpenAI-compatible providers (like Kimi v1).
    "qwen": OPENAI_FAMILY,
}

# Maps harnesses that gate the vendor-neutral gateway transport on a
# ``HARNESS_*_GATEWAY`` truthy flag to that env var name. The flag enables
# the executor's gateway path (base URL + token command + model) regardless
# of which producer fed it — generic providers or the Databricks AI gateway.
# ``openai-agents-sdk`` is absent: its executor takes the API key / base URL
# directly with no such gate (see :func:`_apply_provider_to_openai_agents`).
_HARNESS_GATEWAY_FLAG: dict[AgentHarnessType, str] = {
    "claude-sdk": "HARNESS_CLAUDE_SDK_GATEWAY",
    "codex": "HARNESS_CODEX_GATEWAY",
    "pi": "HARNESS_PI_GATEWAY",
    "qwen": "HARNESS_QWEN_GATEWAY",
}

# Maps a generic-provider family to the key pi uses in its
# ``HARNESS_PI_GATEWAY_BASE_URLS`` JSON object (pi's own family naming).
_PI_FAMILY_KEY: dict[str, str] = {
    ANTHROPIC_FAMILY: "claude",
    OPENAI_FAMILY: "openai",
}

# Per-harness ``HARNESS_*_DATABRICKS_PROFILE`` env var name, used by the
# databricks-kind provider branch (which delegates to the existing ucode
# path). This stays Databricks-named: it is a ``~/.databrickscfg`` profile
# the executor uses for Databricks-specific credential resolution / token
# refresh, not part of the vendor-neutral gateway transport.
# ``openai-agents-sdk`` uses the same var name but has no enable flag.
_HARNESS_DATABRICKS_PROFILE: dict[AgentHarnessType, str] = {
    "claude-sdk": "HARNESS_CLAUDE_SDK_DATABRICKS_PROFILE",
    "codex": "HARNESS_CODEX_DATABRICKS_PROFILE",
    "pi": "HARNESS_PI_DATABRICKS_PROFILE",
    "openai-agents-sdk": "HARNESS_OPENAI_AGENTS_DATABRICKS_PROFILE",
    "qwen": "HARNESS_QWEN_DATABRICKS_PROFILE",
    # NB: no ``antigravity`` — it has no Databricks/gateway path (Gemini-native).
    # NB: no ``kimi`` — upstream kimi has no per-spawn provider override flag,
    # so Omnigent cannot thread a Databricks gateway through. Users configure
    # providers via ``kimi provider add`` in ``~/.kimi/config.toml``
    # (Omnigent-side provider injection is a deferred follow-up).
}


def _provider_harness_name(harness_type: AgentHarnessType) -> str:
    """Translate a workflow harness type to the provider-config harness name.

    The Chunk-1a provider-config layer keys harness→family with
    ``"openai-agents"`` (no ``-sdk`` suffix), whereas this module's
    :data:`AgentHarnessType` uses ``"openai-agents-sdk"``. Every other
    harness name matches verbatim, so only that one differs.

    :param harness_type: Canonical workflow harness type, e.g.
        ``"openai-agents-sdk"`` or ``"claude-sdk"``.
    :returns: The provider-config harness name, e.g. ``"openai-agents"`` or
        ``"claude-sdk"``.
    """
    return "openai-agents" if harness_type == "openai-agents-sdk" else harness_type


def _provider_auth_command(family: FamilyConfig) -> str:
    """Return a bearer-token shell command for *family*, failing loud if absent.

    Mirrors the executors' transport contract: the executors' gateway path
    invokes a shell command that prints the bearer token. A static
    ``api_key`` (already resolved to plaintext by
    :meth:`ProviderEntry.family`) becomes ``printf %s <shlex-quoted-key>``;
    a user-supplied dynamic ``auth_command`` passes through verbatim.

    :param family: The resolved provider family (``base_url`` + secret
        expanded by :meth:`ProviderEntry.family`).
    :returns: A shell command that prints the bearer token to stdout, e.g.
        ``"printf %s sk-or-abc"`` or the literal ``auth_command``.
    :raises OmnigentError: If the family carries neither a static
        ``api_key`` nor an ``auth_command`` (should not happen post-parse).
    """
    if family.api_key is not None:
        # printf %s avoids the trailing newline ``echo`` would add and is
        # shell-safe for keys with special characters via shlex.quote.
        return f"printf %s {shlex.quote(family.api_key)}"
    if family.auth_command is not None:
        return family.auth_command
    raise OmnigentError(
        "provider family has no credential (neither 'api_key' nor 'auth_command') "
        "to build a bearer-token command from.",
        code=ErrorCode.INVALID_INPUT,
    )


def _origin_of(base_url: str) -> str:
    """Return the scheme://host[:port] origin of *base_url*.

    The gateway executors expect a ``HARNESS_*_GATEWAY_HOST`` workspace
    origin separate from the full base URL (which carries the API path).

    :param base_url: The endpoint base URL, e.g.
        ``"https://openrouter.ai/api/v1"`` or ``"http://localhost:4000/v1"``.
    :returns: The origin, e.g. ``"https://openrouter.ai"`` or
        ``"http://localhost:4000"``.
    """
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def configure_agent_harness_with_provider(
    env: dict[str, str],
    entry: ProviderEntry,
    *,
    harness_type: AgentHarnessType,
) -> None:
    """Inject per-harness model, URL, and auth from a generic provider.

    The open-source counterpart to :func:`configure_agent_harness_with_ucode`:
    it takes a resolved :class:`ProviderEntry` (from the ``providers:`` block
    of ``~/.omnigent/config.yaml``) and emits the **same** vendor-neutral
    ``HARNESS_*_GATEWAY_*`` env vars the Databricks producer emits (base URL,
    host, a bearer-token command, model default), so the executors' existing
    gateway path handles a
    LiteLLM / OpenRouter / local endpoint with no executor changes. Dispatch
    is on :attr:`ProviderEntry.kind`:

    - ``key`` / ``gateway`` / ``local`` — resolve the harness's family and
      emit the ``HARNESS_*_GATEWAY_*`` env vars (see
      :func:`_apply_provider_family`).
    - ``subscription`` — the native/CLI harness carries its own login; no
      gateway vars. For codex, pin the built-in ``openai`` provider
      (``HARNESS_CODEX_MODEL_PROVIDER``) so a custom default in the user's
      ``~/.codex/config.toml`` cannot shadow the subscription.
    - ``cli-config`` — pin the entry's ``model_provider``
      (``HARNESS_CODEX_MODEL_PROVIDER``); the provider table + credential
      come from the user's ``~/.codex/config.toml``, which the executor
      bridges into the per-session ``CODEX_HOME``. Codex harness only.
    - ``databricks`` — delegate to the existing ucode path keyed on the
      provider's profile, reusing :func:`configure_agent_harness_with_ucode`
      so the ``polly`` / Databricks coding-agent flow is unchanged.
    - ``bedrock`` — rejected (raises): AWS Bedrock mode is wired only into
      the native ``omnigent claude`` launch, not the in-process / gateway
      harnesses.

    :param env: Mutable spawn-env dict, modified in place.
    :param entry: The resolved provider entry to apply.
    :param harness_type: Canonical harness type, e.g. ``"claude-sdk"``.
    :raises OmnigentError: If an inline-family provider lacks the family
        the harness requires, no model can be resolved for it, or the harness
        is ``antigravity`` (which is Gemini-native and has no gateway path).
    """
    if harness_type == "antigravity":
        # The Antigravity SDK authenticates Gemini-natively (a direct API key
        # or Vertex AI) and has no OpenAI-compatible base_url, so it cannot
        # consume a generic provider / gateway. ``_build_antigravity_spawn_env``
        # threads HARNESS_ANTIGRAVITY_API_KEY / _VERTEX directly instead, so
        # this path must never run for it — fail loud rather than emit inert
        # gateway env vars the executor no longer reads.
        raise OmnigentError(
            "The 'antigravity' harness authenticates Gemini-natively (API key "
            "or Vertex AI) and does not support generic providers or gateway "
            "routing. Set executor.auth to an api_key, or executor.config "
            "vertex/project/location, instead of a 'providers:' entry.",
            code=ErrorCode.INVALID_INPUT,
        )
    if entry.kind == BEDROCK_KIND:
        # Bedrock mode is wired only into the native ``omnigent claude`` launch
        # (:func:`omnigent.claude_native._bedrock_config_for_native_claude`),
        # which sets CLAUDE_CODE_USE_BEDROCK + AWS_BEARER_TOKEN_BEDROCK directly.
        # The in-process / gateway harnesses have no Bedrock path, so emitting
        # the generic ``HARNESS_*_GATEWAY_*`` vars would silently point the
        # harness at the Bedrock endpoint as if it spoke the Anthropic Messages
        # API and fail at request time. Fail loud instead.
        raise OmnigentError(
            f"provider {entry.name!r} (kind 'bedrock') is only supported by the "
            f"native 'omnigent claude' terminal, not the {harness_type!r} harness. "
            "For agents / 'omnigent run', use a 'gateway' provider "
            "(OpenAI/Anthropic-compatible endpoint), or a 'databricks' / 'key' "
            "provider.",
            code=ErrorCode.INVALID_INPUT,
        )
    if entry.kind == SUBSCRIPTION_KIND:
        # A logged-in CLI (claude / codex) carries its own auth; the
        # native/CLI harness reads its own login. Emitting inline-family
        # gateway vars here would point the harness at a non-existent
        # endpoint. (Chunk 1b routes only the inline-family + databricks
        # kinds; subscription routing — toggling the CLI's logged-in model
        # — is a later chunk.)
        if harness_type == "codex":
            # The codex executor symlinks the user's ~/.codex/config.toml
            # into the per-session CODEX_HOME, so a custom default
            # ``model_provider`` there (e.g. isaac's Databricks AI Gateway)
            # would silently hijack a Subscription selection. Pin codex's
            # built-in ``openai`` provider so "Subscription" always means
            # the ChatGPT login — a no-op when the user's config sets no
            # custom default.
            env["HARNESS_CODEX_MODEL_PROVIDER"] = "openai"
        return

    if entry.kind == CLI_CONFIG_KIND:
        # The pi harness consumes both families and can route a cli-config
        # Databricks AI Gateway (the gateway's Anthropic Messages surface is one
        # Pi speaks natively) — the same provider pi-native routes via
        # ``_cli_config_pi_provider``. Translate it into the pi gateway
        # transport rather than failing loud; a non-Databricks cli-config is
        # never selected for pi (see ``default_provider_for_harness``), so it
        # won't reach here.
        if harness_type == "pi":
            _apply_cli_config_databricks_to_pi(env, entry)
            return
        # A custom model provider defined (and authenticated) by the codex
        # CLI's own config.toml: pin it by name; the executor's bridged
        # config.toml carries the provider table + credential. Only the
        # codex harness reads that file — openai-agents-sdk / claude-sdk
        # cannot consume a codex provider table, so fail loud rather than
        # launch them credential-less.
        if harness_type != "codex":
            raise OmnigentError(
                f"provider {entry.name!r} (kind 'cli-config') pins a provider in "
                f"~/.codex/config.toml and can only drive the 'codex' harness, "
                f"not {harness_type!r}. Configure a key/gateway provider for this "
                "harness in ~/.omnigent/config.yaml.",
                code=ErrorCode.INVALID_INPUT,
            )
        # entry.model_provider is required by the cli-config parse branch.
        env["HARNESS_CODEX_MODEL_PROVIDER"] = str(entry.model_provider)
        return

    if entry.kind == DATABRICKS_KIND:
        # A Databricks profile: reuse the existing ucode path so the
        # Databricks coding agent / polly keep working unchanged. The
        # profile name drives model + base URL + auth-command lookup from
        # ~/.databrickscfg + ucode state. This mirrors the legacy
        # DatabricksAuth branch: enable the neutral gateway transport (the
        # Databricks AI gateway is one producer of that transport), record
        # the Databricks profile (Databricks-specific, used by the executor
        # for token refresh), then delegate gateway enrichment to ucode.
        profile = entry.profile
        flag = _HARNESS_GATEWAY_FLAG.get(harness_type)
        if flag is not None:
            env[flag] = "true"
        if profile:
            env[_HARNESS_DATABRICKS_PROFILE[harness_type]] = profile
        configure_agent_harness_with_ucode(env, profile, harness_type=harness_type)
        return

    # Inline-family kinds: key / gateway / local.
    if harness_type == "pi":
        _apply_provider_to_pi(env, entry)
        return
    family_name = _PROVIDER_HARNESS_FAMILY[harness_type]
    family = entry.family(family_name)
    if family is None:
        raise OmnigentError(
            f"provider {entry.name!r} has no {family_name!r} family, required by the "
            f"{harness_type!r} harness. Add a '{family_name}:' block to that provider in "
            f"~/.omnigent/config.yaml.",
            code=ErrorCode.INVALID_INPUT,
        )
    if harness_type == "openai-agents-sdk":
        _apply_provider_to_openai_agents(env, family)
    else:
        _apply_provider_family(env, harness_type, family)


# Maps an omnigent provider family to the bundled catalog provider name
# whose default model serves it. The two happen to share names today
# (``anthropic`` family ⇄ ``anthropic`` catalog, ``openai`` family ⇄
# ``openai`` catalog), but routing through this map keeps the family→catalog
# coupling explicit and one place to change if a family ever fans out to a
# differently-named catalog (e.g. an openai-compatible vendor).
_FAMILY_CATALOG_PROVIDER: dict[str, str] = {
    ANTHROPIC_FAMILY: "anthropic",
    OPENAI_FAMILY: "openai",
}


def _catalog_default_model(family_name: str) -> str | None:
    """Return the bundled catalog's default model for a provider family.

    Used as the model-resolution fallback for a ``key`` / ``gateway`` /
    ``local`` provider on a KNOWN family (anthropic / openai) when neither
    the spec nor the provider's ``models.default`` names a model: rather than
    fail loud, resolve a sensible vendor model from the catalog. The neutral
    gateway path never falls back to a ``databricks-*`` model. This is a real,
    designed default — see
    :func:`omnigent.onboarding.providers.default_chat_model` for the rule
    (newest general-purpose chat model for that vendor) — not an invented
    one masking missing data: it only applies on a family the catalog knows.

    :param family_name: The omnigent family, ``"anthropic"`` or
        ``"openai"``.
    :returns: The catalog default model id, e.g. ``"claude-opus-4-6-20260205"``
        or ``"gpt-5.4-2026-03-05"``, or ``None`` when the family has no
        catalog mapping or the catalog has no chat model for it (genuinely
        unknown — the caller then fails loud).
    """
    from omnigent.onboarding.providers import default_chat_model

    catalog_provider = _FAMILY_CATALOG_PROVIDER.get(family_name)
    if catalog_provider is None:
        return None
    return default_chat_model(catalog_provider)


def _apply_provider_family(
    env: dict[str, str],
    harness_type: AgentHarnessType,
    family: FamilyConfig,
) -> None:
    """Apply a provider family to a gateway-style harness (claude-sdk / codex).

    Emits the same vendor-neutral ``HARNESS_*_GATEWAY_*`` env vars the
    Databricks producer emits so the executor's existing gateway path is
    reused unchanged. The model default is only applied when the spec did
    not already set the model env var.

    :param env: Mutable spawn-env dict, modified in place.
    :param harness_type: ``"claude-sdk"`` or ``"codex"``.
    :param family: The resolved provider family for this harness.
    :raises OmnigentError: If no model is resolvable (neither the spec nor
        the family declares one).
    """
    cfg = _UCODE_HARNESS_CONFIGS[harness_type]
    env[_HARNESS_GATEWAY_FLAG[harness_type]] = "true"
    env[cfg.base_url_key] = family.base_url
    env[cfg.host_key] = _origin_of(family.base_url)
    if cfg.auth_key is not None:
        env[cfg.auth_key] = _provider_auth_command(family)
    # Model precedence: spec model (already in env via _resolve_spec_model) >
    # provider ``models.default`` > catalog family default > fail loud.
    if cfg.model_key not in env and family.default_model:
        env[cfg.model_key] = family.default_model
    if cfg.model_key not in env:
        # Neither the spec nor the provider names a model. On a KNOWN family
        # (anthropic / openai) fall back to the bundled catalog's default for
        # that vendor — a real designed default — rather than failing loud.
        # The neutral gateway path never selects a ``databricks-*`` model;
        # the executor's old flag-triggered ``databricks-*`` fallback is gone.
        catalog_default = _catalog_default_model(_PROVIDER_HARNESS_FAMILY[harness_type])
        if catalog_default is not None:
            env[cfg.model_key] = catalog_default
    if cfg.model_key not in env:
        # Fail loud only when the catalog also has nothing for this family
        # (a genuinely unknown family — should not happen for the two known
        # ones, but keeps the resolution total).
        raise OmnigentError(
            f"No model resolved for the {harness_type!r} harness on a generic provider: "
            "the agent spec sets no model, the provider's family has no "
            "'models.default', and the bundled catalog has no default for that family. "
            "Set 'executor.model' in the agent YAML, or add a "
            "'models: {default: ...}' to that provider family in ~/.omnigent/config.yaml.",
            code=ErrorCode.INVALID_INPUT,
        )
    if harness_type == "codex":
        # Codex defaults to the Responses wire API; OpenRouter-style
        # chat-only gateways set wire_api: chat. See codex_harness.py.
        env["HARNESS_CODEX_WIRE_API"] = family.wire_api or RESPONSES_WIRE_API


def _apply_provider_to_openai_agents(env: dict[str, str], family: FamilyConfig) -> None:
    """Apply a provider family to the openai-agents-sdk harness.

    Unlike the gateway harnesses, the OpenAI-Agents executor takes the API
    key and base URL directly (no ``GATEWAY`` enable flag). A static key uses
    ``HARNESS_OPENAI_AGENTS_API_KEY``; a dynamic token command uses the
    auth-command + host pair. ``wire_api`` maps to ``USE_RESPONSES``.

    :param env: Mutable spawn-env dict, modified in place.
    :param family: The resolved ``openai`` provider family.
    :raises OmnigentError: If no model is resolvable (neither the spec nor
        the family declares one).
    """
    env["HARNESS_OPENAI_AGENTS_GATEWAY_BASE_URL"] = family.base_url
    if family.api_key:
        env["HARNESS_OPENAI_AGENTS_API_KEY"] = family.api_key
    else:
        # Dynamic token command: the executor wraps httpx with a shell-command
        # bearer auth, refreshing the token from this command + host.
        env["HARNESS_OPENAI_AGENTS_GATEWAY_AUTH_COMMAND"] = _provider_auth_command(family)
        env["HARNESS_OPENAI_AGENTS_GATEWAY_HOST"] = _origin_of(family.base_url)
    # Model precedence: spec model > provider ``models.default`` > catalog
    # family default > fail loud. The openai-agents harness always consumes
    # the ``openai`` family.
    if "HARNESS_OPENAI_AGENTS_MODEL" not in env and family.default_model:
        env["HARNESS_OPENAI_AGENTS_MODEL"] = family.default_model
    if "HARNESS_OPENAI_AGENTS_MODEL" not in env:
        catalog_default = _catalog_default_model(OPENAI_FAMILY)
        if catalog_default is not None:
            env["HARNESS_OPENAI_AGENTS_MODEL"] = catalog_default
    if "HARNESS_OPENAI_AGENTS_MODEL" not in env:
        # Fail loud only when the catalog has no default for the openai family
        # (genuinely unknown — should not happen for a known family).
        raise OmnigentError(
            "No model resolved for the 'openai-agents-sdk' harness on a generic provider: "
            "the agent spec sets no model, the provider's 'openai' family has no "
            "'models.default', and the bundled catalog has no default for it. "
            "Set 'executor.model' in the agent YAML, or add a "
            "'models: {default: ...}' to that provider family in ~/.omnigent/config.yaml.",
            code=ErrorCode.INVALID_INPUT,
        )
    if family.wire_api is not None:
        env["HARNESS_OPENAI_AGENTS_USE_RESPONSES"] = (
            "true" if family.wire_api == RESPONSES_WIRE_API else "false"
        )


def _optional_provider_family(entry: ProviderEntry, family_name: str) -> FamilyConfig | None:
    """Return a provider family, or ``None`` if absent *or* its key env var is unset.

    For the ``pi`` harness, which carries a single credential but probes
    both families: a family whose ``$VAR`` is unresolved is treated as
    unavailable rather than fatal, so e.g. a user who only exported
    ``ANTHROPIC_API_KEY`` can still run pi on the anthropic family.

    The only :class:`OmnigentError` raised at family-access time comes from
    the deferred ``$VAR`` expansion (structural validation already happened
    at parse time), so catching it here narrowly means "this family's
    credential is not configured". A ``keychain:`` ref raises ``ValueError``
    (deferred — see :func:`resolve_secret`); that propagates so the user
    sees the clear "not yet supported" message rather than a silent skip.

    :param entry: The resolved provider entry.
    :param family_name: Family key, e.g. ``"openai"`` or ``"anthropic"``.
    :returns: The :class:`FamilyConfig`, or ``None`` when the family is
        absent or its credential env var is unset.
    """
    try:
        return entry.family(family_name)
    except OmnigentError:
        return None


def _apply_provider_to_pi(env: dict[str, str], entry: ProviderEntry) -> None:
    """Apply a provider to the pi harness, which consumes both families.

    pi reads ``HARNESS_PI_GATEWAY_BASE_URLS`` (a JSON object keyed by pi's
    own family names) and a single auth command. When both families are
    configured with different credentials pi can only carry one — it uses the
    ``anthropic`` family's auth when present, else the ``openai`` family's.
    For a single-key gateway (e.g. a LiteLLM proxy) this is exact.

    A family whose credential env var is unset is skipped (not fatal) so a
    user who exported only one vendor's key can still run pi on that family.
    If neither family resolves, this fails loud.

    :param env: Mutable spawn-env dict, modified in place.
    :param entry: The resolved provider entry (at least one inline family).
    :raises OmnigentError: If no configured family's credentials resolve,
        or no model can be resolved for the chosen family.
    """
    anthropic = _optional_provider_family(entry, ANTHROPIC_FAMILY)
    openai = _optional_provider_family(entry, OPENAI_FAMILY)
    base_urls: dict[str, str] = {}
    if anthropic is not None:
        base_urls[_PI_FAMILY_KEY[ANTHROPIC_FAMILY]] = anthropic.base_url
    if openai is not None:
        base_urls[_PI_FAMILY_KEY[OPENAI_FAMILY]] = openai.base_url
    if not base_urls:
        raise OmnigentError(
            f"pi harness: provider {entry.name!r} configures no family whose "
            "credentials resolve — set the api_key env var for its 'anthropic' or "
            "'openai' family in your shell, then retry.",
            code=ErrorCode.INVALID_INPUT,
        )
    # pi carries a single credential: anthropic's when present, else openai's.
    # The model fallback must match the family that supplied that credential.
    auth_source: FamilyConfig | None
    if anthropic is not None:
        auth_source = anthropic
        auth_family = ANTHROPIC_FAMILY
    else:
        auth_source = openai
        auth_family = OPENAI_FAMILY
    assert auth_source is not None  # base_urls non-empty ⇒ one family resolved
    env[_HARNESS_GATEWAY_FLAG["pi"]] = "true"
    env["HARNESS_PI_GATEWAY_BASE_URLS"] = json.dumps(base_urls, sort_keys=True)
    env["HARNESS_PI_GATEWAY_HOST"] = _origin_of(next(iter(base_urls.values())))
    env["HARNESS_PI_GATEWAY_AUTH_COMMAND"] = _provider_auth_command(auth_source)
    # Model precedence: spec model > provider ``models.default`` > catalog
    # family default (of the auth-source family) > fail loud.
    if "HARNESS_PI_MODEL" not in env and auth_source.default_model:
        env["HARNESS_PI_MODEL"] = auth_source.default_model
    if "HARNESS_PI_MODEL" not in env:
        catalog_default = _catalog_default_model(auth_family)
        if catalog_default is not None:
            env["HARNESS_PI_MODEL"] = catalog_default
    if "HARNESS_PI_MODEL" not in env:
        # Fail loud only when the catalog has no default for the chosen family
        # (genuinely unknown — should not happen for a known family).
        raise OmnigentError(
            "No model resolved for the 'pi' harness on a generic provider: the agent "
            "spec sets no model, the provider family has no 'models.default', and the "
            "bundled catalog has no default for it. Set 'executor.model' in the agent "
            "YAML, or add a 'models: {default: ...}' to that provider family in "
            "~/.omnigent/config.yaml.",
            code=ErrorCode.INVALID_INPUT,
        )


def _apply_cli_config_databricks_to_pi(env: dict[str, str], entry: ProviderEntry) -> None:
    """Apply a cli-config Databricks AI Gateway to the pi (gateway-harness) path.

    The gateway-harness pi launch (``omnigent run`` / agents) and pi-native
    (the terminal) both resolve the same default provider
    (:func:`default_provider_for_harness`), so when that default is a
    ``cli-config`` Databricks AI Gateway, this path must route it rather than
    fail loud. We reuse the pi-native translation
    (:func:`omnigent.pi_native_credentials._cli_config_pi_provider`) — which
    reads the codex ``[model_providers.X]`` transport, rewrites the base URL to
    the gateway's Anthropic Messages surface (``/anthropic``) Pi speaks
    natively, and builds the per-request bearer-token ``!command`` apiKey — then
    maps its fields onto the ``HARNESS_PI_GATEWAY_*`` env vars the pi harness
    wrap reads (the same vars :func:`_apply_provider_to_pi` emits).

    :param env: Mutable spawn-env dict, modified in place.
    :param entry: The resolved ``cli-config`` provider entry (a Databricks
        gateway — selection guarantees a non-Databricks cli-config never
        reaches here).
    :raises OmnigentError: If the cli-config entry cannot be translated into a
        Pi gateway provider (its codex table can't be resolved or it is not a
        recognized Databricks AI Gateway) — selection should prevent this, so a
        failure here is a real misconfiguration worth surfacing.
    """
    # Imported lazily: pi_native_credentials is on the runner's session-create
    # hot path and pulls onboarding-only deps; keep this off workflow import.
    from omnigent.pi_native_credentials import _cli_config_pi_provider

    # The spec model (if any) is already in HARNESS_PI_MODEL; thread it so the
    # gateway translation honors an explicit override, else its default.
    model_override = env.get("HARNESS_PI_MODEL")
    provider = _cli_config_pi_provider(entry, model=model_override)
    if provider is None:
        raise OmnigentError(
            f"provider {entry.name!r} (kind 'cli-config') was selected for the 'pi' "
            "harness but its codex [model_providers] table could not be resolved as a "
            "Databricks AI Gateway. Check the [model_providers] base_url + auth in "
            "~/.codex/config.toml, or configure a key/gateway provider for pi in "
            "~/.omnigent/config.yaml.",
            code=ErrorCode.INVALID_INPUT,
        )
    # Pi speaks the gateway's Anthropic Messages surface — register it under
    # pi's "claude" family key (mirrors _apply_provider_to_pi's anthropic path).
    base_urls = {_PI_FAMILY_KEY[ANTHROPIC_FAMILY]: provider.base_url}
    env[_HARNESS_GATEWAY_FLAG["pi"]] = "true"
    env["HARNESS_PI_GATEWAY_BASE_URLS"] = json.dumps(base_urls, sort_keys=True)
    env["HARNESS_PI_GATEWAY_HOST"] = _origin_of(provider.base_url)
    # provider.api_key is a "!command" form (Pi's models.json convention); the
    # gateway transport env var wants the bare shell command, so strip the "!".
    env["HARNESS_PI_GATEWAY_AUTH_COMMAND"] = provider.api_key.lstrip("!")
    env["HARNESS_PI_MODEL"] = provider.model


def _synthesize_databricks_provider(profile: str | None) -> ProviderEntry:
    """
    Build an in-memory ``databricks``-kind provider for a legacy credential.

    Legacy Databricks credentials — a spec ``DatabricksAuth`` /
    ``executor.profile``, the global ``auth: {type: databricks}`` block, or a
    ``databricks-`` model name — are folded into the generic provider path by
    wrapping them in a synthesized :class:`ProviderEntry`, so the single
    :func:`configure_agent_harness_with_provider` databricks branch wires the
    gateway transport instead of a per-builder ``else``. Never persisted;
    ``profile`` ``None`` enables the gateway with no pinned profile (the
    executor resolves the default ``~/.databrickscfg``).

    :param profile: The ``~/.databrickscfg`` profile, or ``None``.
    :returns: A ``databricks``-kind :class:`ProviderEntry`.
    """
    return ProviderEntry(name="databricks", kind=DATABRICKS_KIND, profile=profile)


def _legacy_databricks_provider(
    profile: str | None,
    *,
    harness_type: AgentHarnessType,
    for_launch: bool,
) -> ProviderEntry | None:
    """
    Synthesize a databricks provider for a legacy credential, when applicable.

    Returns a synthesized ``databricks`` :class:`ProviderEntry` only for a
    launch (*for_launch*) of a gateway-flag harness (claude-sdk / codex / pi /
    qwen) — the harnesses whose databricks apply branch reproduces the legacy
    ``else`` env exactly. Returns ``None`` otherwise (the ``/model`` readout /
    cost / native paths and the openai-agents harness), so those keep their own
    handling byte-for-byte.

    :param profile: The legacy ``~/.databrickscfg`` profile, or ``None``.
    :param harness_type: Canonical harness type, e.g. ``"codex"``.
    :param for_launch: Whether this resolution feeds an actual spawn.
    :returns: A synthesized databricks provider, or ``None``.
    """
    if for_launch and _HARNESS_GATEWAY_FLAG.get(harness_type) is not None:
        return _synthesize_databricks_provider(profile)
    return None


def _resolve_provider_for_build(
    spec: AgentSpec,
    *,
    harness_type: AgentHarnessType,
    for_launch: bool = False,
) -> ProviderEntry | None:
    """Resolve the provider that should route *harness_type*, if any.

    The single credential resolver, shared by the spawn-env builders (with
    *for_launch*) and the readout / cost / native paths (without). Precedence,
    most explicit first:

    1. ``spec.executor.auth`` is a :class:`ProviderAuth` → that named provider
       (fails loud when undeclared).
    2. A legacy Databricks credential — ``executor.auth: {type: databricks}``,
       a legacy ``executor.profile``, the global ``auth: {type: databricks}``
       block, or a ``databricks-`` model name — resolves to a *synthesized*
       ``databricks`` provider so the one
       :func:`configure_agent_harness_with_provider` databricks branch wires it
       (no per-builder ``else``). Folded only ``for_launch`` of a gateway-flag
       harness; elsewhere it returns ``None`` so the readout / native /
       openai-agents paths keep their own handling.
    3. An :class:`ApiKeyAuth` (spec or global) → ``None`` (the claude-sdk /
       openai-agents builders thread the key themselves).
    4. The per-family global default (``providers: … default: true``), then an
       ambient-detected default.
    5. (``for_launch`` only) the first credential that can serve the family even
       though it is not marked default — so a launch credentials the head (e.g.
       Debby's codex head with only a never-defaulted Databricks workspace)
       rather than failing with "Invalid API key". Off for the readout / cost
       paths so they never show a provider the user did not choose.

    :param spec: The agent spec.
    :param harness_type: Canonical workflow harness type, e.g. ``"codex"``.
    :param for_launch: ``True`` for the spawn-env builders (permissive: fold
        legacy Databricks credentials into the provider path and fall back to
        the first available credential). ``False`` (readout / cost / native)
        keeps strict, config-only resolution with no synthesis or fallback.
    :returns: The :class:`ProviderEntry` to route through, or ``None``.
    :raises OmnigentError: If a named :class:`ProviderAuth` references a
        provider absent from the ``providers:`` block.
    """
    explicit_config = load_config()
    harness = _provider_harness_name(harness_type)
    auth = spec.executor.auth
    if isinstance(auth, ProviderAuth):
        # A named provider is resolved against the explicit config merged with
        # ambient detections, so a spec may name a detected provider too.
        providers = load_providers(effective_config_with_detected(explicit_config))
        entry = providers.get(auth.name)
        if entry is None:
            raise OmnigentError(
                f"executor.auth references provider {auth.name!r}, but no such provider is "
                "configured under 'providers:' in ~/.omnigent/config.yaml. "
                "Run `omnigent setup --no-internal-beta` to configure one.",
                code=ErrorCode.INVALID_INPUT,
            )
        return entry
    if isinstance(auth, DatabricksAuth):
        # Spec databricks auth → synthesized provider for a gateway-harness
        # launch, else None so the builder's own DatabricksAuth branch runs.
        return _legacy_databricks_provider(
            auth.profile or None, harness_type=harness_type, for_launch=for_launch
        )
    if auth is not None:
        # ApiKeyAuth — threaded by the claude-sdk / openai-agents builders.
        return None
    legacy_profile = spec.executor.profile or spec.executor.config.get("profile")
    if legacy_profile:
        # A legacy profile is a Databricks credential and wins over a configured
        # default, exactly as before — folded into the synthesized provider for
        # a gateway-harness launch, else None so the legacy ``else`` runs.
        return _legacy_databricks_provider(
            str(legacy_profile), harness_type=harness_type, for_launch=for_launch
        )

    # No spec auth. Most explicit wins, ambient last, then a launch-only fallback.
    explicit_default = default_provider_for_harness(explicit_config, harness)
    if explicit_default is not None:
        return explicit_default
    global_auth = _load_global_auth()
    if isinstance(global_auth, DatabricksAuth):
        # None (readout / non-gateway) → defer to the builder's global-auth path.
        return _legacy_databricks_provider(
            global_auth.profile or None, harness_type=harness_type, for_launch=for_launch
        )
    if global_auth is not None:
        # Global ApiKeyAuth — threaded by the builder's global-auth branch.
        return None
    model = _resolve_spec_model(spec)
    if model is not None and model.startswith(("databricks-", "databricks/")):
        # The model name itself signals Databricks intent (no pinned profile).
        return _legacy_databricks_provider(None, harness_type=harness_type, for_launch=for_launch)
    effective = effective_config_with_detected(explicit_config)
    ambient_default = default_provider_for_harness(effective, harness)
    if ambient_default is not None:
        return ambient_default
    # Launch-only last resort: no default anywhere, but a credential that serves
    # this family is configured (e.g. a Databricks workspace the user added but
    # never set as the default). The runner is the one chokepoint every head
    # (CLI, web UI, or a remote host) funnels through, so this credentials the
    # head on every surface, for any agent. Resolved per spawn — nothing is
    # persisted; the startup creds line names the same provider via
    # :func:`first_available_provider`, so the readout cannot disagree.
    if for_launch:
        family = harness_family(harness)
        if family is not None:
            return first_available_provider(effective, family)
    return None


def _resolve_spec_model(spec: AgentSpec) -> str | None:
    """
    Return the model identifier from the spec's executor block.

    :param spec: The agent spec.
    :returns: The model identifier, or ``None`` when the spec
        declares no model.
    """
    return spec.executor.model


def _add_claude_sdk_skills_env(
    env: dict[str, str],
    spec: AgentSpec,
    workdir: Path | None,
) -> None:
    """
    Populate the skills-related ``HARNESS_CLAUDE_SDK_*`` env vars.

    Threads ``spec.skills_filter`` (always — the harness wrap
    falls back to ``"all"`` on a missing var, which would
    silently override an explicit ``skills: none``), ``spec.name``,
    and the bundle's extracted on-disk path so the harness wrap
    can wire ``ClaudeAgentOptions.plugins`` for agent-bundled
    skills.

    :param env: Dict mutated in-place with the new keys.
    :param spec: The agent spec.
    :param workdir: The bundle's on-disk path (extracted by the
        agent cache). ``None`` skips the plugin-dir wiring.
    """
    # ``skills_filter`` is JSON-encoded because the value can be
    # ``"all"`` / ``"none"`` / a list of skill names. Always set:
    # the harness wrap's ``_resolve_skills_filter`` falls back to
    # ``"all"`` on a missing var, which would silently override
    # an explicit ``skills: none`` from the spec. (The original
    # regression that motivated this whole bridge.)
    env["HARNESS_CLAUDE_SDK_SKILLS_FILTER"] = json.dumps(spec.skills_filter)
    if spec.name:
        env["HARNESS_CLAUDE_SDK_AGENT_NAME"] = spec.name
    if workdir is not None:
        env["HARNESS_CLAUDE_SDK_BUNDLE_DIR"] = str(workdir)


def _build_claude_sdk_spawn_env(
    spec: AgentSpec,
    *,
    workdir: Path | None = None,
) -> dict[str, str]:
    """
    Build the env-var dict the claude-sdk harness wrap reads.

    Maps spec.executor fields → the ``HARNESS_CLAUDE_SDK_*`` env
    vars defined in ``omnigent/inner/claude_sdk_harness.py``.
    Per the v1 spec-config-flow design (see §Step 5b in the
    design doc), per-spawn env overrides are how Omnigent threads
    per-spec config into the subprocess without polluting
    ``os.environ``.

    :param spec: The agent spec.
    :param workdir: The bundle's on-disk path (extracted by the
        agent cache). Threaded through as
        ``HARNESS_CLAUDE_SDK_BUNDLE_DIR`` so the harness wrap can
        wire the SDK ``--plugin-dir`` for agent-bundled skills.
    :returns: A dict of env-var overrides for
        :meth:`HarnessProcessManager.get_client(env=...)`.
    """
    env: dict[str, str] = {}
    model = _resolve_spec_model(spec)
    if model is not None:
        env["HARNESS_CLAUDE_SDK_MODEL"] = model

    # ── Auth resolution ────────────────────────────────────────────────
    # Priority (highest first):
    # 0. Generic provider — spec.executor.auth: {type: provider, name: X},
    #    OR (no spec auth) the per-family global default from the
    #    ``providers:`` config block (:func:`_resolve_provider_for_build`).
    #    Routes a LiteLLM / OpenRouter / local / Databricks-profile provider.
    # 1. spec.executor.auth — explicit typed auth in the agent YAML.
    # 2. Legacy spec.executor.profile / executor.config["profile"] (deprecated).
    # 3. Global config ~/.omnigent/config.yaml auth: — only when spec has
    #    no auth at all (same guard as openai-agents to prevent global defaults
    #    from silently overriding YAML-declared legacy profiles).
    # 4. Auto-Databricks: databricks-* model prefix triggers Databricks routing.
    provider = _resolve_provider_for_build(spec, harness_type="claude-sdk", for_launch=True)
    if provider is not None:
        configure_agent_harness_with_provider(env, provider, harness_type="claude-sdk")
    else:
        # No provider resolved → the only remaining credential is an ApiKeyAuth
        # (spec ``executor.auth`` or the global ``auth:`` block). The databricks /
        # legacy-profile / databricks-model cases were folded into the
        # synthesized-provider path above, so no profile or ucode wiring remains
        # here. The executor strips ANTHROPIC_API_KEY to force subscription auth
        # inside Claude Code, so the key is threaded via the CLI's apiKeyHelper
        # (a shell command the CLI invokes; shlex.quote keeps it shell-safe).
        auth_from_spec = spec.executor.auth
        if auth_from_spec is None:
            auth_from_spec = _load_global_auth()
        if isinstance(auth_from_spec, ApiKeyAuth) and auth_from_spec.api_key:
            _key_cmd = f"printf %s {shlex.quote(auth_from_spec.api_key)}"
            env["HARNESS_CLAUDE_SDK_API_KEY_HELPER"] = _key_cmd
            if auth_from_spec.base_url:
                env["HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL"] = auth_from_spec.base_url
                env["HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND"] = _key_cmd
        # Enable the gateway for an ApiKeyAuth ``base_url`` (a custom endpoint) or
        # a ``databricks-`` model; without the flag the executor ignores the base
        # URL and falls through to api.anthropic.com.
        if (isinstance(auth_from_spec, ApiKeyAuth) and bool(auth_from_spec.base_url)) or (
            model is not None and model.startswith(("databricks-", "databricks/"))
        ):
            env["HARNESS_CLAUDE_SDK_GATEWAY"] = "true"
    _add_claude_sdk_skills_env(env, spec, workdir)
    # OS env: enabling this in the inner ClaudeSDKExecutor is
    # what gates the SDK-native ``Bash/Read/Edit/Write/Glob/Grep``
    # tools. The legacy non-AP path enables them by default
    # for omnigent-style specs (the inner CLI auto-creates a
    # ``caller_process`` os_env when ``--os`` is set, and the
    # SDK's bundled CLI exposes the natives unconditionally in
    # some configurations); routing through Omnigent mode without a
    # similar default leaves Omnigent mode users staring at a
    # tool list that's ~80% smaller. Forward the spec's
    # OSEnvSpec verbatim when present; default to
    # ``caller_process + sandbox=none`` otherwise so the parity
    # holds.
    os_env_payload = _serialize_os_env(spec.os_env)
    if os_env_payload is not None:
        env["HARNESS_CLAUDE_SDK_OS_ENV"] = os_env_payload
    # Phase 1f: thread the spec's RetryPolicy through to the
    # claude-sdk subprocess so the inner ``ClaudeSDKExecutor``
    # picks up custom retry budgets (``ANTHROPIC_MAX_RETRIES``,
    # ``ANTHROPIC_REQUEST_TIMEOUT_SECONDS``) instead of falling
    # back to the executor's hard-coded ``RetryPolicy()`` default.
    # Omitted when the policy matches defaults — see
    # :func:`_serialize_retry_policy`.
    retry_payload = _serialize_retry_policy(_resolve_retry_policy(spec))
    if retry_payload is not None:
        env["HARNESS_CLAUDE_SDK_RETRY_POLICY"] = retry_payload
    # Permission mode: controls whether Claude asks for approval before
    # calling native tools. When set to anything other than the default
    # ``"bypassPermissions"``, the SDK's ``can_use_tool`` callback is
    # active and approval requests surface via Omnigent elicitation. Read from
    # Omitted when not set — harness falls back to ``"bypassPermissions"``.
    permission_mode = spec.executor.config.get("permission_mode")
    if permission_mode is not None:
        env["HARNESS_CLAUDE_SDK_PERMISSION_MODE"] = str(permission_mode)
    return env


def _build_codex_spawn_env(
    spec: AgentSpec,
    *,
    workdir: Path | None = None,
) -> dict[str, str]:
    """
    Build the env-var dict the codex harness wrap reads.

    Maps spec.executor fields → the ``HARNESS_CODEX_*`` env vars
    defined in ``omnigent/inner/codex_harness.py``. Mirrors
    :func:`_build_claude_sdk_spawn_env` — same per-spawn env-var
    pattern from §Step 5a. The codex-specific env vars
    (``HARNESS_CODEX_PATH``, ``HARNESS_CODEX_ENABLE_WEB_SEARCH``,
    ``HARNESS_CODEX_DISABLE_NATIVE_TOOLS``) are not threaded
    through here in v1: the legacy
    :func:`omnigent.inner.executor_factory.create_executor`
    path doesn't surface them either, so AP-side parity is
    preserved by leaving them at the inner executor's defaults.
    Operators who want non-default values set those env vars
    on the Omnigent server directly (they propagate to the subprocess
    through normal env inheritance — the wrap's per-spawn
    overrides only override, they don't filter).

    :param spec: The agent spec.
    :param workdir: The bundle's on-disk path (extracted by the
        agent cache). Threaded through as
        ``HARNESS_CODEX_BUNDLE_DIR`` so the harness wrap's executor
        can also source bundled skills from
        ``<bundle>/skills/<name>/``.
    :returns: A dict of env-var overrides for
        :meth:`HarnessProcessManager.get_client(env=...)`.
    """
    env: dict[str, str] = {}
    model = _resolve_spec_model(spec)
    if model is not None:
        env["HARNESS_CODEX_MODEL"] = model

    # Generic-provider branch (slotted ahead of the legacy-profile /
    # databricks-prefix path): a ProviderAuth on the spec, or — when the spec
    # declares no auth — the per-family global default. See
    # :func:`_resolve_provider_for_build`. Otherwise the existing path is
    # unchanged.
    provider = _resolve_provider_for_build(spec, harness_type="codex", for_launch=True)
    if provider is not None:
        configure_agent_harness_with_provider(env, provider, harness_type="codex")
    elif codex_config_provider_dismissed(load_config()):
        # No credential resolved. If the user Removed codex's custom
        # ~/.codex/config.toml provider (dismissed), pin the built-in ``openai``
        # provider so the dismissal holds at run time — the executor's bridged
        # config.toml would otherwise still route this launch through that removed
        # default model_provider. (Gateway mode never reaches here: it resolves a
        # provider above, and the executor rejects a double pin.)
        env["HARNESS_CODEX_MODEL_PROVIDER"] = "openai"
    # Skills bridge — same shape as the claude-sdk variant. Always
    # set so the harness wrap doesn't fall back to its ``"all"``
    # default and override an explicit ``skills: none`` spec.
    env["HARNESS_CODEX_SKILLS_FILTER"] = json.dumps(spec.skills_filter)
    if spec.name:
        env["HARNESS_CODEX_AGENT_NAME"] = spec.name
    if workdir is not None:
        env["HARNESS_CODEX_BUNDLE_DIR"] = str(workdir)
    os_env_payload = _serialize_os_env(spec.os_env)
    if os_env_payload is not None:
        env["HARNESS_CODEX_OS_ENV"] = os_env_payload
    # Phase 1f: thread the spec's RetryPolicy through to the
    # codex subprocess. See :func:`_build_claude_sdk_spawn_env`
    # for the rationale.
    retry_payload = _serialize_retry_policy(_resolve_retry_policy(spec))
    if retry_payload is not None:
        env["HARNESS_CODEX_RETRY_POLICY"] = retry_payload
    return env


def _build_pi_spawn_env(
    spec: AgentSpec,
    *,
    cwd: Path | None = None,
    workdir: Path | None = None,
) -> dict[str, str]:
    """
    Build the env-var dict the pi harness wrap reads.

    Maps spec.executor fields → the ``HARNESS_PI_*`` env vars
    defined in ``omnigent/inner/pi_harness.py``. Mirrors
    :func:`_build_claude_sdk_spawn_env` /
    :func:`_build_codex_spawn_env` — same per-spawn env-var
    pattern from §Step 5a.

    :param spec: The agent spec.
    :param cwd: Runtime working directory for the Pi CLI. This is the
        session workspace, not the agent bundle workdir.
    :param workdir: The bundle's on-disk path (extracted by the
        agent cache). Threaded through as ``HARNESS_PI_BUNDLE_DIR``
        so the harness wrap's executor can source bundled skills
        from ``<bundle>/skills/<name>/``.
    :returns: A dict of env-var overrides for
        :meth:`HarnessProcessManager.get_client(env=...)`.
    """
    env: dict[str, str] = {}
    model = _resolve_spec_model(spec)
    if model is not None:
        env["HARNESS_PI_MODEL"] = model

    # Generic-provider branch (slotted ahead of the legacy-profile /
    # databricks-prefix path): a ProviderAuth on the spec, or — when the spec
    # declares no auth — the per-family global default. pi consumes both
    # families (see :func:`_apply_provider_to_pi`). Otherwise the existing
    # path is unchanged.
    provider = _resolve_provider_for_build(spec, harness_type="pi", for_launch=True)
    if provider is not None:
        configure_agent_harness_with_provider(env, provider, harness_type="pi")
    # Skills bridge — same shape as the claude-sdk + codex variants.
    # Always set so the harness wrap doesn't fall back to ``"all"``
    # and override an explicit ``skills: none`` from the spec.
    env["HARNESS_PI_SKILLS_FILTER"] = json.dumps(spec.skills_filter)
    if spec.name:
        env["HARNESS_PI_AGENT_NAME"] = spec.name
    if cwd is not None:
        env["HARNESS_PI_CWD"] = str(cwd)
    if workdir is not None:
        env["HARNESS_PI_BUNDLE_DIR"] = str(workdir)
    os_env_payload = _serialize_os_env(spec.os_env)
    if os_env_payload is not None:
        env["HARNESS_PI_OS_ENV"] = os_env_payload
    return env


def _build_qwen_spawn_env(
    spec: AgentSpec,
    *,
    workdir: Path | None = None,
) -> dict[str, str]:
    """
    Build the env-var dict the qwen harness wrap reads.

    Maps spec.executor fields → the ``HARNESS_QWEN_*`` env vars
    defined in ``omnigent/inner/qwen_harness.py``. Mirrors
    :func:`_build_claude_sdk_spawn_env` /
    :func:`_build_codex_spawn_env`.

    :param spec: The agent spec.
    :param workdir: The bundle's on-disk path (extracted by the agent
        cache). Accepted for signature parity with the other
        ``_build_*_spawn_env`` builders; the qwen wrap does not yet
        consume a bundle dir (no skills bridge — see docs/QWEN_FOLLOWUPS.md).
    :returns: A dict of env-var overrides for
        :meth:`HarnessProcessManager.get_client(env=...)`.
    """
    env: dict[str, str] = {}
    model = _resolve_spec_model(spec)
    if model is not None:
        env["HARNESS_QWEN_MODEL"] = model

    # Generic-provider branch (slotted ahead of the legacy-profile /
    # databricks-prefix path): a ProviderAuth on the spec, or — when the spec
    # declares no auth — the per-family global default. qwen routes through
    # OpenAI-compatible providers.
    provider = _resolve_provider_for_build(spec, harness_type="qwen", for_launch=True)
    if provider is not None:
        configure_agent_harness_with_provider(env, provider, harness_type="qwen")
    # NB: no skills bridge for qwen yet. Unlike the claude-sdk / codex
    # variants, the qwen wrap (omnigent/inner/qwen_harness.py) and
    # QwenExecutor have no skills concept, so emitting
    # HARNESS_QWEN_SKILLS_FILTER / _AGENT_NAME / _BUNDLE_DIR would set env
    # nothing reads. Wire those through when skills land — see
    # docs/QWEN_FOLLOWUPS.md.
    os_env_payload = _serialize_os_env(spec.os_env)
    if os_env_payload is not None:
        env["HARNESS_QWEN_OS_ENV"] = os_env_payload
    return env


def _build_goose_spawn_env(
    spec: AgentSpec,
    *,
    workdir: Path | None = None,
) -> dict[str, str]:
    """
    Build the env-var dict the headless goose harness wrap reads.

    Maps spec.executor fields → the ``HARNESS_GOOSE_*`` env vars defined in
    ``omnigent/inner/goose_harness.py``. Unlike the SDK harnesses, Goose owns its
    own auth via ``goose configure`` (keyring / ``~/.config/goose/config.yaml``),
    so this builder wires **no** provider/gateway credential — it forwards only an
    optional model override and the os_env/sandbox spec. A ``databricks-*`` model
    is dropped (not a valid Goose model id; the provider/model then come from the
    user's Goose config), mirroring how the native CLIs handle gateway ids.

    :param spec: The agent spec.
    :param workdir: The bundle's on-disk path. Accepted for signature parity with
        the other ``_build_*_spawn_env`` builders; the goose wrap consumes no
        bundle dir yet (no skills bridge).
    :returns: A dict of env-var overrides for the harness process spawn.
    """
    env: dict[str, str] = {}
    model = _resolve_spec_model(spec)
    if model is not None and not model.startswith(("databricks-", "databricks/")):
        env["HARNESS_GOOSE_MODEL"] = model
    os_env_payload = _serialize_os_env(spec.os_env)
    if os_env_payload is not None:
        env["HARNESS_GOOSE_OS_ENV"] = os_env_payload
    return env


def _build_acp_spawn_env(
    spec: AgentSpec,
    *,
    workdir: Path | None = None,
) -> dict[str, str]:
    """Build the env-var dict the generic ACP harness wrap reads.

    Resolves the picked ``acp:<slug>`` (carried in ``spec.executor.config`` — the
    slug is the addressable half of the harness id) to a user-configured agent in
    the ``acp:`` config block, and forwards its command + protocol knobs as the
    ``HARNESS_ACP_*`` env vars defined in ``omnigent/inner/acp_harness.py``.

    Like Goose, a generic ACP agent owns its own auth, so this wires **no**
    provider/gateway credential. A ``databricks-*`` model is dropped (not a valid
    third-party model id); the agent's own configured model (or a flag in its
    command) then applies. When the slug is missing/unknown, falls back to the
    first configured agent so a bare ``acp`` id still launches something.

    :param spec: The agent spec.
    :param workdir: Accepted for signature parity with the other builders; the
        ACP wrap consumes no bundle dir.
    :returns: A dict of ``HARNESS_ACP_*`` env-var overrides for the spawn.
    """
    env: dict[str, str] = {}
    raw_harness = ""
    cfg = getattr(spec.executor, "config", None)
    if isinstance(cfg, dict):
        raw_harness = str(cfg.get("harness") or "")
    slug = raw_harness.split(":", 1)[1] if raw_harness.startswith("acp:") else ""

    # Lazily import the config reader — the hot spawn-env path shouldn't pull in
    # the onboarding/config stack eagerly (mirrors the cursor builder).
    from omnigent.onboarding.acp_auth import acp_agents, resolve_acp_agent

    agent = resolve_acp_agent(slug) if slug else None
    if agent is None:
        agents = acp_agents()
        agent = agents[0] if agents else None

    if agent is not None:
        env["HARNESS_ACP_COMMAND"] = agent.command
        env["HARNESS_ACP_NAME"] = agent.name
        env["HARNESS_ACP_SESSION_ID_MODE"] = agent.session_id_mode
        if agent.send_model:
            env["HARNESS_ACP_SEND_MODEL"] = "1"

        model = _resolve_spec_model(spec)
        if model is not None and not model.startswith(("databricks-", "databricks/")):
            env["HARNESS_ACP_MODEL"] = model
        elif agent.model:
            env["HARNESS_ACP_MODEL"] = agent.model
    # else: no agent configured — leave HARNESS_ACP_COMMAND unset so the wrap
    # raises a clear request-time error pointing the user at `omnigent setup`.

    os_env_payload = _serialize_os_env(spec.os_env)
    if os_env_payload is not None:
        env["HARNESS_ACP_OS_ENV"] = os_env_payload
    return env


def _load_global_auth() -> ApiKeyAuth | DatabricksAuth | None:
    """
    Load the ``auth:`` block from ``~/.omnigent/config.yaml``.

    Reads the user-level global config file (respecting
    ``$OMNIGENT_CONFIG_HOME`` for test isolation) and parses the
    optional ``auth:`` mapping into a typed auth dataclass.  Returns
    ``None`` when the file does not exist, the ``auth:`` key is absent,
    or the block is not a recognized shape.

    This provides a user-level auth default: agents that do not declare
    ``executor.auth`` in their own spec inherit credentials from here,
    so the user only configures auth once during ``omnigent setup``
    rather than in every agent YAML.

    :returns: A :class:`ApiKeyAuth` or :class:`DatabricksAuth`, or
        ``None`` when the global config has no ``auth:`` block or the
        file is missing.
    """
    config_home = os.environ.get("OMNIGENT_CONFIG_HOME")
    path = (
        Path(config_home) / "config.yaml"
        if config_home
        else Path.home() / ".omnigent" / "config.yaml"
    )
    if not path.exists():
        return None
    try:
        with open(path) as f:
            raw: dict[str, Any] = yaml.safe_load(f) or {}
    except Exception:
        return None
    raw_auth = raw.get("auth")
    if not isinstance(raw_auth, dict):
        return None
    auth_type = str(raw_auth.get("type", ""))
    if auth_type == "api_key":
        api_key = str(raw_auth.get("api_key") or "")
        if not api_key:
            return None
        # Expand $VAR references (the config file may store the literal
        # env-var reference; expand at use-time so the secret never
        # needs to live in the YAML file itself).
        api_key = expand_envvars_with_omnigent_prefix(api_key)
        check_unresolved_env_vars("auth.api_key", api_key)
        raw_base_url = raw_auth.get("base_url")
        base_url: str | None = None
        if raw_base_url:
            base_url = expand_envvars_with_omnigent_prefix(str(raw_base_url))
            check_unresolved_env_vars("auth.base_url", base_url)
        return ApiKeyAuth(api_key=api_key, base_url=base_url)
    if auth_type == "databricks":
        profile_val = str(raw_auth.get("profile") or "")
        return DatabricksAuth(profile=profile_val) if profile_val else None
    return None


def _config_flag_is_true(value: object) -> bool:
    """Interpret a free-form executor config value as a boolean flag."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes"}


def _build_openai_agents_sdk_spawn_env(spec: AgentSpec) -> dict[str, str]:
    """
    Build the env-var dict the openai-agents harness wrap reads.

    Maps spec.executor fields → the ``HARNESS_OPENAI_AGENTS_*``
    env vars defined in
    ``omnigent/inner/openai_agents_sdk_harness.py``. Threads
    model + auth + use_responses.

    Auth resolution order (highest priority first):

    1. ``spec.executor.auth`` — explicit typed auth in the agent YAML.
    2. Legacy ``spec.executor.profile`` / ``spec.executor.config["profile"]``
       (**deprecated** — use ``executor.auth: {type: databricks, …}``).
       Both 1 and 2 are spec-level declarations; the spec always wins.
    3. Global config ``~/.omnigent/config.yaml`` ``auth:`` block —
       **only consulted when the spec declares no auth at all** (neither
       new nor legacy style). This prevents the user's global default
       from silently overriding a YAML that uses the old profile field.
    4. Auto-Databricks: ``databricks-`` / ``databricks/`` model prefix
       with no auth → fall back to the SDK ``"DEFAULT"`` profile so
       ambient OPENAI_API_KEY doesn't short-circuit Databricks routing.

    :param spec: The agent spec.
    :returns: A dict of env-var overrides for
        :meth:`HarnessProcessManager.get_client(env=...)`. May
        be empty when no auth is configured and the model name does not
        start with ``databricks-``; in that case the wrap falls back to
        ``OPENAI_BASE_URL`` / ``OPENAI_API_KEY`` env vars and the
        ``use_responses=True`` default.
    """
    env: dict[str, str] = {}
    model = _resolve_spec_model(spec)
    if model is not None:
        env["HARNESS_OPENAI_AGENTS_MODEL"] = model

    # ── Auth resolution ────────────────────────────────────────────────
    # Priority: generic provider → spec.executor.auth → global config auth →
    # legacy profile in config dict → auto-Databricks for databricks-* models.
    #
    # 0. Generic provider — spec.executor.auth: {type: provider, name: X},
    #    OR (no spec auth) the per-family global default. Sets API key /
    #    base URL / model and maps the openai family's wire_api to
    #    USE_RESPONSES. No ucode enrichment (no Databricks profile to look
    #    up), so it returns early. A spec's explicit ``use_responses`` still
    #    wins over the provider's wire_api.
    provider = _resolve_provider_for_build(spec, harness_type="openai-agents-sdk", for_launch=True)
    if provider is not None:
        configure_agent_harness_with_provider(env, provider, harness_type="openai-agents-sdk")
        use_responses = spec.executor.config.get("use_responses")
        if use_responses is not None:
            env["HARNESS_OPENAI_AGENTS_USE_RESPONSES"] = (
                "true" if _config_flag_is_true(use_responses) else "false"
            )
        return env

    # Global config auth is only consulted when the spec declares NO
    # auth at all — neither the new executor.auth block nor the legacy
    # executor.profile / executor.config["profile"].  A spec that uses
    # the old profile style is still an explicit spec-level auth
    # declaration and must not be silently overridden by the user's
    # global default (which may be a different auth type entirely).
    #
    # ProviderAuth is fully handled by the early-return block above (it
    # resolves a provider or fails loud), so spec.executor.auth is narrowed
    # to ApiKeyAuth / DatabricksAuth / None here.
    spec_auth = spec.executor.auth
    auth: ApiKeyAuth | DatabricksAuth | None = (
        spec_auth if isinstance(spec_auth, (ApiKeyAuth, DatabricksAuth)) else None
    )
    _spec_has_legacy_profile = bool(spec.executor.profile or spec.executor.config.get("profile"))
    if auth is None and not _spec_has_legacy_profile:
        auth = _load_global_auth()

    if isinstance(auth, ApiKeyAuth):
        env["HARNESS_OPENAI_AGENTS_API_KEY"] = auth.api_key
        if auth.base_url:
            env["HARNESS_OPENAI_AGENTS_GATEWAY_BASE_URL"] = auth.base_url
    elif isinstance(auth, DatabricksAuth):
        env["HARNESS_OPENAI_AGENTS_DATABRICKS_PROFILE"] = auth.profile
    else:
        # Legacy path: executor.config["profile"] (deprecated — use executor.auth instead).
        # DEPRECATED: config["profile"] will be removed once all specs migrate to auth:.
        profile = spec.executor.config.get("profile")
        if not profile and model and model.startswith(("databricks-", "databricks/")):
            # databricks- / databricks/ prefix: route to Databricks (avoiding the
            # OPENAI_API_KEY short-circuit) via the SDK's DEFAULT profile. The
            # ambient DATABRICKS_CONFIG_PROFILE env var is deliberately NOT
            # consulted — credentials are controlled by the spec or by
            # `omnigent setup` provider config, never by shell environment.
            profile = "DEFAULT"
        if profile:
            # Single canonical env var: ``DATABRICKS_PROFILE``. No
            # ``GATEWAY=true`` gate (unlike claude-sdk / codex /
            # pi) because :class:`OpenAIAgentsSDKExecutor` takes the
            # profile name directly and resolves credentials itself —
            # a separate truthy gate would be dead surface.
            env["HARNESS_OPENAI_AGENTS_DATABRICKS_PROFILE"] = str(profile)

    # Resolve the effective profile for ucode state lookup (model/base-URL enrichment).
    # For api_key auth there is no profile to look up, so ucode enrichment is skipped.
    ucode_profile: str | None = None
    if isinstance(auth, DatabricksAuth):
        ucode_profile = auth.profile
    elif "HARNESS_OPENAI_AGENTS_DATABRICKS_PROFILE" in env:
        ucode_profile = env["HARNESS_OPENAI_AGENTS_DATABRICKS_PROFILE"]

    use_responses = spec.executor.config.get("use_responses")
    if use_responses is not None:
        env["HARNESS_OPENAI_AGENTS_USE_RESPONSES"] = (
            "true" if _config_flag_is_true(use_responses) else "false"
        )
    configure_agent_harness_with_ucode(
        env,
        ucode_profile,
        harness_type="openai-agents-sdk",
    )
    return env


def _build_cursor_spawn_env(
    spec: AgentSpec,
    *,
    workdir: Path | None = None,
) -> dict[str, str]:
    """
    Build the ``HARNESS_CURSOR_*`` env-var dict the cursor harness wrap reads.

    Maps spec.executor fields → the ``HARNESS_CURSOR_*`` env vars defined
    in ``omnigent/inner/cursor_harness.py``. Unlike the gateway-backed
    builders (claude-sdk / codex / pi / openai-agents), there is NO gateway or
    Databricks-profile resolution: the Cursor SDK talks only to Cursor's own
    backend (``CURSOR_API_KEY``) and has no custom API base-URL override, so it
    never routes through the Databricks AI gateway. That is also why cursor is
    intentionally absent from :data:`AgentHarnessType` and the gateway/ucode
    dicts above.

    Auth: an explicit ``executor.auth: {type: api_key, api_key: ...}`` is
    forwarded as ``HARNESS_CURSOR_API_KEY`` (the cursor harness passes it to the
    Cursor SDK as its ``api_key``). When the spec declares no auth at all, a
    ``CURSOR_API_KEY`` registered once via ``omnigent setup`` (the dedicated
    ``cursor:`` config block — see :mod:`omnigent.onboarding.cursor_auth`) is
    used instead, so a user need not export it in every shell. With neither, the
    harness falls back to an inherited ``CURSOR_API_KEY`` — a ``DatabricksAuth``
    profile does not apply to cursor and is ignored.

    :param spec: The agent spec.
    :param workdir: The bundle's on-disk path, threaded as
        ``HARNESS_CURSOR_BUNDLE_DIR``.
    :returns: A dict of env-var overrides for
        :meth:`HarnessProcessManager.get_client(env=...)`.
    """
    env: dict[str, str] = {}
    model = _resolve_spec_model(spec)
    if model is not None:
        env["HARNESS_CURSOR_MODEL"] = model
    # Auth precedence: an explicit api-key auth on the spec wins; with NO spec
    # auth at all, fall back to a CURSOR_API_KEY registered once via
    # ``omnigent setup`` (the dedicated ``cursor:`` config block), else an
    # ambient CURSOR_API_KEY (an exported key / a host launched with one). A
    # Databricks / provider auth has no cursor equivalent and never silently
    # adopts a stored or ambient cursor key.
    if isinstance(spec.executor.auth, ApiKeyAuth):
        env["HARNESS_CURSOR_API_KEY"] = spec.executor.auth.api_key
    elif spec.executor.auth is None:
        # Imported lazily — the onboarding layer pulls in the secret store /
        # keyring, which the hot spawn-env path shouldn't import eagerly.
        from omnigent.onboarding.cursor_auth import resolve_cursor_api_key

        stored_key = resolve_cursor_api_key()
        if stored_key:
            env["HARNESS_CURSOR_API_KEY"] = stored_key
        else:
            ambient_key = os.environ.get("CURSOR_API_KEY")
            if ambient_key and ambient_key.strip():
                env["HARNESS_CURSOR_API_KEY"] = ambient_key.strip()
    # Always set so the wrap doesn't fall back to ``"all"`` and override an
    # explicit ``skills: none`` from the spec (parity with the peer builders).
    env["HARNESS_CURSOR_SKILLS_FILTER"] = json.dumps(spec.skills_filter)
    if spec.name:
        env["HARNESS_CURSOR_AGENT_NAME"] = spec.name
    if workdir is not None:
        env["HARNESS_CURSOR_BUNDLE_DIR"] = str(workdir)
    os_env_payload = _serialize_os_env(spec.os_env)
    if os_env_payload is not None:
        env["HARNESS_CURSOR_OS_ENV"] = os_env_payload
    # Permission stance for native-tool elicitation. Default ``auto`` (set by
    # the harness wrap when unset) skips ApprovalCards so headless / Polly
    # Cursor SDK workers don't stall; an explicit config value overrides.
    permission_mode = spec.executor.config.get("permission_mode")
    if permission_mode is not None:
        env["HARNESS_CURSOR_PERMISSION_MODE"] = str(permission_mode)
    return env


def _build_kimi_spawn_env(
    spec: AgentSpec,
    *,
    cwd: Path | None = None,
) -> dict[str, str]:
    """Build the env-var dict the kimi harness wrap reads.

    Maps ``spec.executor`` fields → the ``HARNESS_KIMI_*`` env vars
    defined in :mod:`omnigent.inner.kimi_harness`.

    The upstream Kimi Code CLI has no per-spawn provider override flag
    (no ``--config-file`` / ``--mcp-config-file``), so this builder
    only threads the model, working directory, and ``os_env`` sandbox
    spec. Provider routing for kimi lives in ``~/.kimi/config.toml``
    and is managed out-of-band via ``kimi provider add``. Unlike the
    sibling builders, ``_build_kimi_spawn_env`` never calls
    :func:`configure_agent_harness_with_provider` (there is no env-var
    surface to translate a provider into), so the rejection of declared
    auth has to live here: a spec that declares an explicit
    provider / Databricks / api_key auth raises directly so the user
    understands why their auth didn't take effect rather than silently
    routing through whatever default kimi already had.

    :param spec: The agent spec.
    :param cwd: Runtime working directory for the kimi subprocess — the
        session workspace (the folder the user launched in), NOT the agent
        bundle dir. Threaded as ``HARNESS_KIMI_CWD`` so kimi's tools operate on
        the user's project rather than the /tmp bundle (upstream kimi has no
        ``--work-dir`` flag, so the subprocess ``cwd=`` is the only lever).
        When unset, the harness wrap falls back to ``OMNIGENT_RUNNER_WORKSPACE``.
        Mirrors :func:`_build_pi_spawn_env`'s ``cwd`` handling.
    :returns: A dict of env-var overrides.
    :raises OmnigentError: If the spec declares ``executor.auth`` —
        upstream kimi has no per-spawn provider override, so the
        declared auth cannot be honored and we fail loud rather than
        launch against an unrelated ambient provider.
    """
    if spec.executor.auth is not None:
        raise OmnigentError(
            "The 'kimi' harness does not support per-invocation provider / "
            "auth injection: upstream kimi has no per-spawn config override "
            "(no ``--config-file`` / ``--mcp-config-file``). Remove "
            "``executor.auth`` from the spec and configure the provider once "
            "via `kimi provider add` in ~/.kimi/config.toml, then pin the "
            "resulting model id in the agent spec. Omnigent-side provider "
            "injection is a deferred follow-up.",
            code=ErrorCode.INVALID_INPUT,
        )
    env: dict[str, str] = {}
    model = _resolve_spec_model(spec)
    if model is not None:
        env["HARNESS_KIMI_MODEL"] = model
    if cwd is not None:
        env["HARNESS_KIMI_CWD"] = str(cwd)
    os_env_payload = _serialize_os_env(spec.os_env)
    if os_env_payload is not None:
        env["HARNESS_KIMI_OS_ENV"] = os_env_payload
    return env


def _build_antigravity_spawn_env(spec: AgentSpec) -> dict[str, str]:
    """
    Map ``spec.executor`` fields → the ``HARNESS_ANTIGRAVITY_*`` env vars the
    antigravity harness wrap reads.

    Antigravity is Gemini-native with no OpenAI-compatible ``base_url``, so there
    is no gateway / ucode / Databricks path — only a direct API key or Vertex AI.
    API-key resolution (first wins): (1) spec ``executor.auth`` api_key; (2) the
    dedicated ``antigravity:`` config block from ``omnigent setup``; (3) an
    ambient ``GEMINI_API_KEY`` / ``ANTIGRAVITY_API_KEY``. The legacy global
    ``auth:`` block is deliberately NOT consulted: it carries the OpenAI/gateway
    key the other SDK harnesses inherit (an ``sk-…`` key), which the Gemini-native
    SDK can't use — adopting it would guarantee an auth failure / mis-billing and
    shadow the user's ambient ``GEMINI_API_KEY``. Any ``base_url`` is dropped (the
    SDK has no such field). Vertex AI is opt-in via ``executor.config``
    vertex/project/location, independent of the key path. A ``DatabricksAuth`` is
    unsupported — warned and ignored.

    :param spec: The agent spec.
    :returns: Env-var overrides; may be empty (the wrap then uses the SDK's
        ambient creds and default model).
    """
    env: dict[str, str] = {}
    model = _resolve_spec_model(spec)
    if model is not None:
        env["HARNESS_ANTIGRAVITY_MODEL"] = model

    spec_auth = spec.executor.auth
    if spec_auth is not None and not isinstance(spec_auth, ApiKeyAuth):
        # Non-api_key auth implies gateway/base_url routing the SDK can't do.
        # Warn (don't drop silently — that looks like "my auth didn't take").
        _logger.warning(
            "antigravity harness: spec executor.auth is %s, but the Antigravity "
            "SDK only supports a direct API key or Vertex AI. Ignoring it and "
            "falling back to ambient Gemini credentials — configure an api_key, "
            "or executor.config vertex/project/location, instead.",
            type(spec_auth).__name__,
        )

    # Spec api-key wins; with no spec auth, fall back to the dedicated
    # ``antigravity:`` block, then an ambient Gemini key (see docstring). The
    # global ``auth:`` block is intentionally NOT consulted — it holds the
    # OpenAI/gateway key the SDK can't use. A non-api-key auth never adopts a key.
    if isinstance(spec_auth, ApiKeyAuth):
        # base_url intentionally dropped — the SDK has no such field.
        env["HARNESS_ANTIGRAVITY_API_KEY"] = spec_auth.api_key
    elif spec_auth is None:
        # Lazy import — the onboarding layer pulls in the secret store / keyring.
        from omnigent.onboarding.antigravity_auth import (
            ANTIGRAVITY_ENV_VARS,
            resolve_antigravity_api_key,
        )

        stored_key = resolve_antigravity_api_key()
        if stored_key is not None:
            env["HARNESS_ANTIGRAVITY_API_KEY"] = stored_key
        else:
            for _env_var in ANTIGRAVITY_ENV_VARS:
                if os.environ.get(_env_var):
                    env["HARNESS_ANTIGRAVITY_API_KEY"] = os.environ[_env_var]
                    break

    # Vertex AI: opt-in via executor.config (authenticated by GCP ADC).
    config = spec.executor.config
    if config.get("vertex"):
        env["HARNESS_ANTIGRAVITY_VERTEX"] = "1"
        project = config.get("project")
        if project:
            env["HARNESS_ANTIGRAVITY_PROJECT"] = str(project)
        location = config.get("location")
        if location:
            env["HARNESS_ANTIGRAVITY_LOCATION"] = str(location)

    return env


def _build_copilot_spawn_env(
    spec: AgentSpec,
    *,
    workdir: Path | None = None,
) -> dict[str, str]:
    """
    Build the ``HARNESS_COPILOT_*`` env-var dict the copilot harness wrap reads.

    Maps spec.executor fields → the ``HARNESS_COPILOT_*`` env vars defined in
    ``omnigent/inner/copilot_harness.py``. Like the cursor / antigravity
    builders there is NO gateway or Databricks-profile resolution: the GitHub
    Copilot SDK talks only to GitHub's Copilot backend (a GitHub token) and has
    no custom API base-URL override, so it never routes through the Databricks
    AI gateway. That is also why copilot is intentionally absent from
    :data:`AgentHarnessType` and the gateway/ucode dicts above.

    Auth: an explicit ``executor.auth: {type: api_key, api_key: ...}`` carries
    the GitHub token, forwarded as ``HARNESS_COPILOT_GITHUB_TOKEN`` (the copilot
    harness passes it to the SDK as its ``github_token``). When the spec declares
    no auth at all, a GitHub token registered once via ``omnigent setup`` (the
    dedicated ``copilot:`` config block — see
    :mod:`omnigent.onboarding.copilot_auth`) is used instead, so a user need not
    export it in every shell. With neither, the harness falls back to an
    inherited ``COPILOT_GITHUB_TOKEN`` / ``GH_TOKEN`` / ``GITHUB_TOKEN`` — a
    ``DatabricksAuth`` profile does not apply to copilot and is ignored.

    :param spec: The agent spec.
    :param workdir: The bundle's on-disk path, threaded as
        ``HARNESS_COPILOT_BUNDLE_DIR``.
    :returns: A dict of env-var overrides for
        :meth:`HarnessProcessManager.get_client(env=...)`.
    """
    env: dict[str, str] = {}
    model = _resolve_spec_model(spec)
    if model is not None:
        env["HARNESS_COPILOT_MODEL"] = model
    # Auth precedence: an explicit api-key auth on the spec wins (its ``api_key``
    # is the GitHub token); with NO spec auth at all, fall back to a token
    # registered once via ``omnigent setup`` (the dedicated ``copilot:`` config
    # block), else an ambient ``COPILOT_GITHUB_TOKEN`` / ``GH_TOKEN`` /
    # ``GITHUB_TOKEN``. A Databricks / provider auth has no copilot equivalent
    # and never silently adopts a stored or ambient copilot token.
    if isinstance(spec.executor.auth, ApiKeyAuth):
        env["HARNESS_COPILOT_GITHUB_TOKEN"] = spec.executor.auth.api_key
    elif spec.executor.auth is None:
        # Imported lazily — the onboarding layer pulls in the secret store /
        # keyring, which the hot spawn-env path shouldn't import eagerly.
        from omnigent.onboarding.copilot_auth import (
            COPILOT_TOKEN_ENV_VARS,
            resolve_copilot_github_token,
        )

        stored_token = resolve_copilot_github_token()
        if stored_token is not None:
            env["HARNESS_COPILOT_GITHUB_TOKEN"] = stored_token
        else:
            for _env_var in COPILOT_TOKEN_ENV_VARS:
                if os.environ.get(_env_var):
                    env["HARNESS_COPILOT_GITHUB_TOKEN"] = os.environ[_env_var]
                    break
    # Always set so the wrap doesn't fall back to ``"all"`` and override an
    # explicit ``skills: none`` from the spec (parity with the peer builders).
    env["HARNESS_COPILOT_SKILLS_FILTER"] = json.dumps(spec.skills_filter)
    if spec.name:
        env["HARNESS_COPILOT_AGENT_NAME"] = spec.name
    if workdir is not None:
        env["HARNESS_COPILOT_BUNDLE_DIR"] = str(workdir)
    os_env_payload = _serialize_os_env(spec.os_env)
    if os_env_payload is not None:
        env["HARNESS_COPILOT_OS_ENV"] = os_env_payload
    return env


def _serialize_os_env(value: OSEnvSpec | None) -> str | None:
    """
    Encode an :class:`OSEnvSpec` for the wrap's env-var input.

    JSON-encodes :func:`dataclasses.asdict` of the OSEnvSpec so
    the wrap can :func:`json.loads` it back on the harness side
    (per the per-spawn env-var pattern from §Step 5a). When
    *value* is ``None`` (no os_env declared on the spec), this
    returns ``None`` and ``_build_claude_sdk_spawn_env`` omits
    the env var entirely — the wrap then falls back to its
    enable-natives-by-default rule.

    :param value: ``spec.os_env`` — an :class:`OSEnvSpec`
        instance or ``None``.
    :returns: JSON string encoding the OSEnvSpec, or ``None``
        when *value* is ``None``.
    """
    import dataclasses

    if value is None:
        return None
    return json.dumps(dataclasses.asdict(value))


def _serialize_retry_policy(value: RetryPolicy | None) -> str | None:
    """
    Encode a :class:`RetryPolicy` for the wrap's env-var input.

    Phase 1f of ``designs/RETRY_ACROSS_HARNESSES.md``: the spec's
    ``LLMConfig.retry`` must take effect inside the CLI-harness
    subprocess. Serializing the whole policy as one JSON env var
    keeps the wire format compact (~150 bytes) and lets the
    harness round-trip via :func:`json.loads` →
    ``RetryPolicy(**dict)``. JSON over a flat fan-out of
    ``HARNESS_*_RETRY_MAX_RETRIES`` / ``..._BACKOFF_BASE_S`` /
    etc. because (a) ``RetryPolicy`` has 6 fields including a
    tuple, and a flat fan-out would multiply boilerplate at
    every wrap; (b) future field additions stay
    backwards-compatible — older wraps just ignore unknown
    keys via the ``__init__`` filter below.

    Returns ``None`` when the policy matches
    ``RetryPolicy()`` defaults so the env var is omitted —
    saves the harness wrap an unnecessary parse step on the
    common path.

    :param value: ``llm_config.retry`` — a :class:`RetryPolicy`
        or ``None``. ``None`` and ``RetryPolicy()`` (defaults)
        both produce ``None`` so the wrap falls back to the
        same baked-in default.
    :returns: JSON string encoding the policy's fields, or
        ``None`` when *value* is ``None`` or matches the
        defaults.
    """
    import dataclasses
    import json

    if value is None or value == RetryPolicy():
        return None
    payload = dataclasses.asdict(value)
    # ``retryable_status_codes`` is a tuple in the dataclass;
    # ``asdict`` converts it to a list. JSON has no tuple type —
    # the harness side reconstructs the tuple in
    # ``_deserialize_retry_policy``.
    return json.dumps(payload)


def _resolve_retry_policy(spec: AgentSpec) -> RetryPolicy | None:
    """
    Read the retry policy off a spec.

    Used by the per-harness ``_build_*_spawn_env`` builders to
    decide whether to thread a ``HARNESS_*_RETRY_POLICY`` env
    var through to the subprocess. Returns ``None`` when the
    spec has no ``llm`` block (e.g. a CLI-harness spec where
    the retry policy is implicit) so the harness wrap falls
    back to its baked-in :class:`RetryPolicy()` default.

    :param spec: The agent spec.
    :returns: ``spec.llm.retry`` if set; ``None`` otherwise.
    """
    if spec.llm is None:
        return None
    return spec.llm.retry


# ── Responses API helpers ─────────────────────────────────


def _apply_request_model_override(
    llm_config: LLMConfig,
    model_override: str | None,
) -> LLMConfig:
    """
    Substitute the per-request LLM model into the agent's LLM config.

    The override is also stashed in ``extra["model_override"]`` so the
    harness path can forward it to the subprocess — the harness body's
    ``model`` field carries the agent name, distinct from the LLM
    model. Returns a new :class:`LLMConfig`; original is not mutated.

    :param llm_config: The agent spec's LLM config (already merged
        with any per-request reasoning override).
    :param model_override: Per-request LLM model identifier, e.g.
        ``"databricks-claude-sonnet-4-6"``, or ``None`` to pass
        ``llm_config`` through unchanged.
    :returns: A (possibly new) :class:`LLMConfig`.
    """
    if model_override is None:
        return llm_config
    merged_extra = {**llm_config.extra, "model_override": model_override}
    return LLMConfig(
        model=model_override,
        extra=merged_extra,
        connection=llm_config.connection,
        request_timeout=llm_config.request_timeout,
        retry=llm_config.retry,
    )


# ── Checkpointed steps ───────────────────────────────────


# ── Executor @step wrapper ────────────────────────────────


# Maps executor reasoning event_type to SSE event type string.


# ── Executor turn → response dict bridge ──────────────────


def _prepare_messages(
    spec: AgentSpec,
    llm_config: LLMConfig,
    history: list[ConversationItem],
    instructions: str | None,
    tool_schemas: list[dict[str, Any]],
    compaction_state: _CompactionState,
    content_cache: dict[str, str] | None,
    *,
    conversation_id: str | None = None,
) -> tuple[str, list[dict[str, Any]], int]:
    """
    Build system instructions and Responses API input items.

    Resolves content references and counts system token budget.
    Extracted from ``_call_llm_maybe_compact`` for reuse by the
    executor path.

    :param spec: The parsed AgentSpec.
    :param llm_config: LLM configuration.
    :param history: Conversation history as persisted items.
    :param instructions: Optional per-request instructions.
    :param tool_schemas: OpenAI-format tool schemas.
    :param compaction_state: Per-execution compaction state.
    :param content_cache: Per-task content reference cache.
    :param conversation_id: Optional owning conversation/session id
        used to verify session-scoped file ownership.
    :returns: Tuple of (system_instructions, messages, sys_tokens).
    """
    sys_instructions = build_instructions(spec, instructions, tool_schemas)
    file_store = get_file_store()
    artifact_store = get_artifact_store()
    resolved = history
    if file_store is not None and artifact_store is not None:
        resolved = resolve_content_references(
            history,
            file_store,
            artifact_store,
            content_cache,
            session_id=conversation_id,
        )
    messages = history_to_input_items(resolved)
    sys_tokens = count_tokens(
        [{"role": "system", "content": sys_instructions}],
        compaction_state.model,
    )
    return sys_instructions, messages, sys_tokens


# ── Output helpers ────────────────────────────────────────


# ── Pagination helper ─────────────────────────────────────


def fetch_all_items(
    conv_store: ConversationStore,
    conversation_id: str,
    after: str | None = None,
) -> list[ConversationItem]:
    """
    Fetch all conversation items starting after the given
    cursor, paginating through every page until ``has_more``
    is ``False``.

    :param conv_store: The ConversationStore to query.
    :param conversation_id: The conversation to fetch items
        from, e.g. ``"conv_abc123"``.
    :param after: Cursor item ID to start after, or ``None``
        to fetch from the beginning.
    :returns: All items in chronological order after the
        cursor.
    """
    all_items: list[ConversationItem] = []
    cursor = after
    while True:
        page = conv_store.list_items(conversation_id, after=cursor)
        all_items.extend(page.data)
        if not page.has_more:
            break
        # Advance cursor to the last item of this page
        cursor = page.last_id
    return all_items


# ── Extracted helpers ─────────────────────────────────────


def _strip_mcp_tool_prefix(name: str) -> str:
    """Strip ``mcp__<server>__`` prefix from *name*; preserve bare ``__``."""
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return parts[2]
    return name


# ─── Async tool dispatch ───────────────────────────────────


@dataclass
class _AsyncToolHandle:
    """
    Handle returned to the LLM when an async tool is dispatched.

    Replaces the inline tool result string from a sync invocation.
    The LLM gets back a structured task handle and waits for
    auto-delivery (D7/G12); explicit polling was dropped per
    design step 11.

    :param task_id: The newly created task's ID, e.g.
        ``"tsk_async_xyz"``. Identical to the
        the dispatching task's id (G56).
    :param tool_name: The dispatched tool's name, included in the
        handle so the LLM can correlate the handle to its own
        tool_calls field.
    :param status: Always ``"in_progress"`` at handle-creation
        time — terminal status arrives via the
        ``async_work_complete`` signal.
    :param message: A self-explanatory instruction for the LLM
        (G12). Names the task_id explicitly so the LLM can
        copy-paste it into ``sys_cancel_task`` if it wants
        to abort.
    """

    task_id: str
    tool_name: str
    status: str
    message: str

    def to_handle_json(self) -> str:
        """
        Serialize the handle as JSON for the tool-call return path.

        The runner contract returns strings, so the handle ships
        as a JSON-encoded dict. The LLM treats the result like
        any other tool output.

        :returns: JSON string with ``task_id``, ``tool_name``,
            ``status``, and ``message`` keys.
        """
        return json.dumps(
            {
                "task_id": self.task_id,
                "tool_name": self.tool_name,
                "status": self.status,
                "message": self.message,
            }
        )


def _async_handle_message(task_id: str, tool_name: str) -> str:
    """
    Build the LLM-facing instruction text on a fresh async handle.

    Every word here is load-bearing — the message is the LLM's
    only signal that the result is NOT in this string. Without
    "asynchronous" + "auto-deliver" + the literal task_id, the
    LLM tends to either treat the handle as the result and
    hallucinate completion, or repeatedly poll for status.

    :param task_id: The async task's ID, included verbatim so
        the LLM can pass it to ``sys_cancel_task`` if it wants
        to abort.
    :param tool_name: The dispatched tool's name.
    :returns: A compact instruction string.
    """
    return (
        f"Tool {tool_name!r} dispatched asynchronously. "
        f"The result will be auto-delivered as a system message "
        f"when ready. To abort, call sys_cancel_task with "
        f"task_id={task_id!r}."
    )


# ── Phase 6: TOOL_CALL / TOOL_RESULT / OUTPUT enforcement ───


# Builtin tools that dispatch a sub-agent by its declared
# ``tools.agents`` name. Listed here so the TOOL_CALL enforcement
# site can narrow ``tool_name`` to the sub-agent's declared name
# for ``match_tools`` matching — YAML authors think of a sub-agent
# as a tool named after its ``tools.agents`` entry, not as a call
# to the generic spawn / send builtin. Names come from the tool
# classes' ``.name()`` classmethods so a rename in one of them
# can't silently desync this constant.


def _find_latest_compaction_item(
    conv_store: ConversationStore,
    conversation_id: str,
) -> ConversationItem | None:
    """
    Return the most recently appended compaction item for a
    conversation, or ``None`` if none exists.

    Uses a descending ``limit=1`` query so only one row is read
    regardless of total conversation length.

    :param conv_store: The ConversationStore to query.
    :param conversation_id: The conversation to search,
        e.g. ``"conv_abc123"``.
    :returns: The latest compaction item, or ``None``.
    """
    page = conv_store.list_items(
        conversation_id,
        type="compaction",
        order="desc",
        limit=1,
    )
    return page.data[0] if page.data else None


@dataclass(frozen=True)
class _LoadedHistory:
    """
    Result of :func:`_load_initial_history`.

    Bundles the conversation items with metadata about the latest
    compaction so callers can make decisions (e.g. seed invalidation)
    without re-querying the store.

    :param items: History items ready for prompt construction.
        May begin with a synthetic summary pair from
        :func:`compaction_to_history_items`.
    :param last_compaction_created_at: ``created_at`` of the latest
        compaction item used as a history cursor, or ``None`` when
        no valid compaction item exists.
    """

    items: list[ConversationItem]
    last_compaction_created_at: int | None = None


def _load_initial_history(
    conv_store: ConversationStore,
    conversation_id: str,
) -> _LoadedHistory:
    """
    Load the conversation history for the start of an execution.

    When a compaction item exists, only the items AFTER the
    summary's coverage boundary are loaded — the synthetic
    summary pair replaces the older items the LLM does not need
    to see verbatim. This bounds the load to O(items since last
    compaction), not O(total conversation length).

    When no compaction item exists, the full conversation is
    loaded (existing behaviour).

    :param conv_store: The ConversationStore to query.
    :param conversation_id: The conversation to load,
        e.g. ``"conv_abc123"``.
    :returns: A :class:`_LoadedHistory` with items ready for
        prompt construction and the latest compaction timestamp.
    """
    compaction_item = _find_latest_compaction_item(conv_store, conversation_id)
    if compaction_item is None:
        return _LoadedHistory(
            items=[
                item
                for item in fetch_all_items(conv_store, conversation_id)
                if item.type not in NON_CONTENT_ITEM_TYPES
            ]
        )
    assert isinstance(compaction_item.data, CompactionData)
    # Validate the compaction item before trusting it as a cursor.
    # A broken item (empty summary, missing/bogus last_item_id) would
    # make the after= query return nothing, leaving history as just
    # the empty synthetic pair — the Omnigent thinks context is ~0 tokens
    # and never triggers compaction while the executor's real context
    # keeps growing.
    last_id = compaction_item.data.last_item_id
    if not compaction_item.data.summary or not last_id or last_id.startswith("synthetic_"):
        _logger.warning(
            "Ignoring broken compaction item %s for %s: empty summary or bogus last_item_id=%r",
            compaction_item.id,
            conversation_id,
            last_id,
        )
        return _LoadedHistory(
            items=[
                item
                for item in fetch_all_items(conv_store, conversation_id)
                if item.type not in NON_CONTENT_ITEM_TYPES
            ]
        )
    # Load after last_item_id, NOT after the compaction item itself.
    # The compaction item may be appended after additional output
    # items that the summary does not cover — using last_item_id
    # ensures those post-summary items are included.
    recent_items = fetch_all_items(
        conv_store,
        conversation_id,
        after=compaction_item.data.last_item_id,
    )
    # Filter metadata items — they are not conversation content the
    # LLM should receive verbatim.
    content_items = [i for i in recent_items if i.type not in NON_CONTENT_ITEM_TYPES]
    return _LoadedHistory(
        items=compaction_to_history_items(compaction_item) + content_items,
        last_compaction_created_at=compaction_item.created_at,
    )


async def compact_conversation_now(
    *,
    task_id: str,
    conversation_id: str,
    spec: AgentSpec,
    llm_config: LLMConfig,
    instructions: str | None = None,
    tool_schemas: list[dict[str, Any]] | None = None,
    model_override: str | None = None,
    preserve_recent_window: int | None = None,
) -> CompactionResult:
    """
    Force a compaction pass for an existing conversation.

    This is the runtime entry point used by the sessions ``/compact``
    control event. It builds the same prompt input that a normal agent
    iteration would use, runs compaction with ``force=True`` so Layer 2
    summarisation is attempted even below the automatic threshold, and
    persists a compaction item when a summary is produced.

    :param task_id: Synthetic response id for this compaction operation.
    :param conversation_id: Conversation/session id to compact.
    :param spec: Agent spec bound to the conversation.
    :param llm_config: Effective LLM config used for summarisation.
    :param instructions: Optional per-request instructions.
    :param tool_schemas: Tool schemas to include in system-token budget.
    :param model_override: Optional model override. Applied to
        ``llm_config`` before counting and summarising.
    :param preserve_recent_window: Override for how many recent LLM
        response groups to keep verbatim. ``/compact`` passes ``1``;
        this is mapped to the compaction helper's inclusive boundary
        semantics below so the explicit command reduces context immediately.
        ``None`` uses the agent's configured compaction policy.
    :returns: The :class:`CompactionResult` returned by
        :func:`omnigent.runtime.compaction.compact`.
    """
    conv_store = get_conversation_store()
    loaded = _load_initial_history(conv_store, conversation_id)
    history = loaded.items
    if not history:
        return CompactionResult(messages=[], summary_metadata=None)

    effective_llm_config = _apply_request_model_override(llm_config, model_override)
    effective_llm_config = _route_bare_model_for_compaction(effective_llm_config)
    compaction_config = spec.compaction
    if preserve_recent_window is not None:
        # The compaction helper's boundary is inclusive: recent_window=1
        # protects the latest assistant/function-call boundary itself, which
        # often means preserving the entire final turn. For explicit /compact,
        # use 0 to summarize through the latest completed item while still
        # keeping the command opt-in/user-driven.
        from omnigent.spec.types import CompactionConfig

        trigger_threshold = compaction_config.trigger_threshold if compaction_config else 0.8
        compaction_config = CompactionConfig(
            trigger_threshold=trigger_threshold,
            recent_window=max(preserve_recent_window - 1, 0),
        )
    compaction_state = _CompactionState(
        context_window=None,
        last_summary=None,
        config=compaction_config,
        model=effective_llm_config.model,
        connection=effective_llm_config.connection,
        conversation_id=conversation_id,
    )
    _sys_instructions, messages, sys_tokens = _prepare_messages(
        spec,
        effective_llm_config,
        history,
        instructions,
        tool_schemas or [],
        compaction_state,
        content_cache={},
        conversation_id=conversation_id,
    )
    from omnigent.llms.context_window import get_model_context_window

    context_window = get_model_context_window(effective_llm_config.model)
    result = await compact(
        messages,
        history,
        config=compaction_state.config,
        context_window=context_window,
        system_token_budget=sys_tokens,
        model=compaction_state.model,
        task_id=task_id,
        llm_client=_get_llm_client(),
        connection=compaction_state.connection,
        runner_client=_get_runner_client_for_compaction(conversation_id),
        force=True,
        fail_on_summary_error=True,
        conversation_id=conversation_id,
    )
    if result.summary_metadata is None:
        raise OmnigentError(
            "Compaction did not produce a persisted summary. The conversation was "
            "left unchanged; check server logs for the summarization failure.",
            code=ErrorCode.INTERNAL_ERROR,
        )
    _maybe_persist_compaction_item(
        result.summary_metadata,
        task_id,
        conversation_id,
        conv_store,
    )
    return result


def _route_bare_model_for_compaction(llm_config: LLMConfig) -> LLMConfig:
    """
    Prefix bare model ids so compaction's generic client picks the right provider.

    Normal harness execution infers the provider from the harness (e.g.
    ``claude-sdk`` → Anthropic, ``openai-agents`` → Databricks/OpenAI).
    Explicit ``/compact`` instead uses the generic runtime LLM client,
    whose :func:`~omnigent.llms.routing.parse_model_string` defaults any
    prefix-less id to OpenAI — so a bare ``databricks-*`` or Anthropic
    ``claude-*`` id gets sent to ``api.openai.com`` and the summarization
    call fails with a 500 (issue #1950). Already-prefixed ids
    (``anthropic/…``, ``openai/…``) and bare ``gpt-*`` (correctly OpenAI)
    are left untouched.

    :param llm_config: Effective LLM config for the session.
    :returns: ``llm_config`` unchanged, or a copy with a provider-prefixed model.
    """
    model = llm_config.model
    if "/" in model:
        # Already provider-prefixed (e.g. "anthropic/claude-…") — trust it.
        return llm_config
    if model.startswith("databricks-"):
        return replace(llm_config, model=f"databricks/{model}")
    if model.startswith("claude-"):
        # Bare Anthropic id (e.g. "claude-haiku-4-5-20251001") — route to
        # Anthropic instead of the prefix-less OpenAI default.
        return replace(llm_config, model=f"anthropic/{model}")
    # ponytail: only databricks + anthropic here — the /compact failures seen
    # in the wild. Other bare non-OpenAI prefixes (deepseek-, moonshot-, …)
    # would need the same nudge if they ever surface.
    return llm_config


def _maybe_persist_compaction_item(
    summary: SummaryMetadata,
    task_id: str,
    conversation_id: str,
    conv_store: ConversationStore,
) -> None:
    """
    Persist a compaction item for the current execution, unless one
    already exists (idempotent append for crash-recovery safety).

    The ``response_id`` on the item is the task ID, which is unique
    per execution. On replay the runner re-emits any pending tail —
    the check-before-write prevents a duplicate compaction item from
    being appended.

    :param summary: The :class:`SummaryMetadata` from Layer 2.
    :param task_id: The task identifier used as the item's
        ``response_id``, e.g. ``"task_abc123"``.
    :param conversation_id: The conversation to append to,
        e.g. ``"conv_abc123"``.
    :param conv_store: The ConversationStore to append to.
    """
    # Guard: never persist a broken compaction item. An empty summary
    # or a missing last_item_id would poison the history cursor so
    # _load_initial_history returns an empty history on every
    # subsequent turn — the executor keeps running on its internal
    # session while the Omnigent thinks context is near-zero and never
    # triggers compaction again.
    if (
        not summary.text
        or not summary.last_item_id
        or summary.last_item_id.startswith("synthetic_")
    ):
        _logger.warning(
            "Skipping compaction persist for task %s: empty summary "
            "or bogus last_item_id (text=%r, last_item_id=%r)",
            task_id,
            summary.text[:80] if summary.text else None,
            summary.last_item_id,
        )
        return
    existing = conv_store.list_items(
        conversation_id,
        type="compaction",
        order="desc",
        limit=1,
    )
    if existing.data and existing.data[0].response_id == task_id:
        # Already persisted — idempotent on crash recovery replay.
        return
    conv_store.append(
        conversation_id,
        [
            NewConversationItem(
                type="compaction",
                response_id=task_id,
                data=CompactionData(
                    summary=summary.text,
                    last_item_id=summary.last_item_id,
                    model=summary.model,
                    token_count=summary.token_count,
                ),
            )
        ],
    )


# ── await_tool_output implementation ───────────────────────


# ── The agent loop ────────────────────────────────────────


def _find_spec_by_name(
    spec: AgentSpec,
    name: str,
) -> AgentSpec | None:
    """
    Resolve a sub-agent spec by name within the parent spec tree.

    Recursively searches ``spec.sub_agents`` (names are validated unique
    across the tree, so at most one matches).

    The one exception is the built-in ``__web_researcher`` sub-agent that
    backs the ``web_fetch`` tool: ``WebFetchTool`` synthesizes its spec in
    memory and appends it to the parent's live ``sub_agents`` list, but
    that spec is never serialized into the parent's persisted bundle. A
    child ``__web_researcher`` session boots by re-parsing the bundle
    fresh (``runner/_entry.py`` spec resolver), so the researcher is
    absent from the re-parsed tree and a plain search returns ``None``.
    Every caller swaps to the resolved sub-spec only ``if ... is not
    None`` and otherwise keeps the parent spec, which boots the child as
    a full clone of the parent (runaway recursion via ``sys_session_send``
    when the parent is a coordinator). To keep that fallback safe, the
    researcher is reconstructed deterministically from the parent (the
    same pure builder ``WebFetchTool`` uses) instead of returning ``None``,
    but only when some node in the tree actually declares the ``web_fetch``
    builtin. That builtin is the sole reason the researcher ever exists, so a
    tree without it anywhere has no such child and the name falls through to
    normal resolution (``None``). Reconstructing unconditionally would let a
    caller-controlled ``sub_agent_name`` coerce any parent into a
    shell-capable researcher (``build_researcher_spec`` synthesizes an
    ``OSEnvSpec``), widening the parent's tool boundary.

    The owning node need not be the root: a nested sub-agent may own
    ``web_fetch`` while the handed-in root does not. The gate locates the
    ``web_fetch`` owner via a root-first pre-order walk
    (:func:`_find_web_fetch_owner`) and reconstructs from THAT owner, not the
    root, so the researcher inherits the owner's LLM and sandbox/egress
    boundary (``build_researcher_spec`` derives both from its argument). When
    several nodes own ``web_fetch`` the first pre-order owner wins; this is a
    deliberate limitation tied to the ``__web_researcher`` name not being
    unique per owner (plumbing an "effective parent" through every call site
    is out of scope). The root-owner case is unchanged: the owner is the
    root, so the output is identical to before.

    :param spec: The root agent spec to search.
    :param name: The sub-agent name to find,
        e.g. ``"researcher"``.
    :returns: The matching sub-agent spec, the reconstructed
        ``__web_researcher`` spec when the parent declares ``web_fetch``
        and the name matches, or ``None`` otherwise.
    """
    found = _search_sub_agent_tree(spec, name)
    if found is not None:
        return found
    # Built-in web_fetch researcher: derived from its owner, never persisted
    # in the bundle, so reconstruct it on a resolve-miss rather than letting
    # callers fall back to the parent spec. Gated on some node in the tree
    # declaring the ``web_fetch`` builtin, the only reason the researcher
    # exists (``WebFetchTool.__init__`` appends it). The owner may be nested,
    # so reconstruct from the owner (root-first pre-order) — never the root —
    # to inherit the owner's LLM and sandbox/egress boundary. Imported lazily
    # to keep the tools layer off this module's import path.
    from omnigent.tools.builtins.web_fetch import RESEARCHER_NAME

    if name == RESEARCHER_NAME:
        owner = _find_web_fetch_owner(spec)
        if owner is not None:
            from omnigent.tools.builtins.web_fetch import build_researcher_spec

            return build_researcher_spec(owner)
    return None


def _find_web_fetch_owner(spec: AgentSpec) -> AgentSpec | None:
    """
    Find the first node owning the ``web_fetch`` builtin, root-first.

    Pre-order DFS (root, then children left-to-right), mirroring
    :func:`_search_sub_agent_tree`. The ``web_fetch`` owner is the node whose
    ``tools.builtins`` carries an entry named ``web_fetch``; that node's spec
    is the correct parent for ``build_researcher_spec`` (its LLM + sandbox).

    :param spec: The agent spec whose sub-tree to search.
    :returns: The first node (root-first) owning ``web_fetch``, or ``None``.
    """
    if any(entry.name == "web_fetch" for entry in spec.tools.builtins):
        return spec
    for sa in spec.sub_agents:
        owner = _find_web_fetch_owner(sa)
        if owner is not None:
            return owner
    return None


def _search_sub_agent_tree(
    spec: AgentSpec,
    name: str,
) -> AgentSpec | None:
    """
    Recursively search ``spec.sub_agents`` for a sub-agent named ``name``.

    The pure tree search backing :func:`_find_spec_by_name`; kept separate
    so the ``__web_researcher`` reconstruction in that function fires once
    at the root rather than on every recursive frame.

    :param spec: The agent spec whose sub-tree to search.
    :param name: The sub-agent name to find.
    :returns: The matching sub-agent spec, or ``None`` if not found.
    """
    for sa in spec.sub_agents:
        if sa.name == name:
            return sa
        found = _search_sub_agent_tree(sa, name)
        if found is not None:
            return found
    return None
