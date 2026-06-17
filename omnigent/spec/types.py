"""Typed dataclasses representing a parsed agent image spec."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec

if TYPE_CHECKING:
    # EvaluationContext is a runtime evaluation artifact (see
    # omnigent.policies.types); PhaseSelector.matches takes
    # one by attribute access only, so the annotation lives
    # behind TYPE_CHECKING to avoid an import cycle.
    from omnigent.policies.types import EvaluationContext

# Default classifier timeout for PromptPolicy evaluations. 30 s
# balances classifier latency against the cost of blocking the
# agent loop for every evaluated phase. The agent-level LLM
# default (300 s) is tuned for generation; using it for a
# blocking classifier stalls the loop for minutes on each tool
# call — see POLICIES.md §9.2. Overrideable via
# ``policy.llm.request_timeout`` on individual policies.
DEFAULT_POLICY_CLASSIFIER_TIMEOUT = 30

# Default timeout (seconds) for user approval on an ASK policy.
# 30 s matches omnigent parity. Overrideable per-policy via
# ``PolicySpec.ask_timeout`` or spec-wide via
# ``GuardrailsSpec.ask_timeout``. See POLICIES.md §7, §13.
DEFAULT_ASK_TIMEOUT = 30


@dataclass(frozen=True)
class RetryPolicy:
    """
    Unified retry policy applied at two layers in the harness path
    and as the only retry layer in the in-process LLM path and
    tool-retry contexts.

    See ``designs/RETRY_ACROSS_HARNESSES.md`` for the architecture.

    - L0 (SDK-internal): SDK consumes via the relevant per-SDK
      sub-object adapter (``policy.openai``,
      ``policy.anthropic``, ``policy.claude_cli``,
      ``policy.codex_cli``, ``policy.pi``) at client construction.
      Backoff shape is the SDK's own; we don't override it. Pi
      additionally consumes ``backoff_base_s`` and
      ``backoff_max_s`` because Pi exposes the shape declaratively
      via ``.pi/settings.json``.
    - L2 (AP-side workflow): consumed via
      :meth:`compute_backoff_delay` for the workflow's own retry
      loop between executor-turn attempts.

    :param max_retries: Number of retries beyond the first attempt.
        ``max_retries=7`` means up to 8 total tries. Maps directly
        to SDK ``max_retries`` parameters at L0 and bounds L2's
        loop budget.
    :param backoff_base_s: Exponential base in seconds. Delay
        before retry ``i`` (1-indexed) is
        ``min(base * 2 ** (i - 1), backoff_max_s)``. Used at L2;
        passed to Pi's L0 settings; ignored by other SDKs.
    :param backoff_max_s: Per-retry cap in seconds. Used at L2 and
        Pi L0 (as ``maxDelayMs`` for fail-fast on absurd
        ``Retry-After`` hints).
    :param jitter: Multiply each L2 delay by ``Uniform(0.5, 1.5)``
        to spread retries from many concurrent clients. SDKs handle
        their own jitter at L0.
    :param timeout_per_request_s: Per-HTTP-request timeout. ``None``
        lets the SDK use its default. Used at L0 only — bounds an
        individual attempt's wall-clock.
    :param retryable_status_codes: HTTP status codes the in-process
        LLM path's ``classify_llm_error`` treats as retryable.
        Used only by the in-process LLM path and the tool-retry
        classifier — L0 SDKs ignore this and L2 receives
        already-classified errors.
    """

    max_retries: int = 7
    backoff_base_s: float = 2.0
    backoff_max_s: float = 60.0
    jitter: bool = True
    timeout_per_request_s: float | None = 120.0
    retryable_status_codes: tuple[int, ...] = (429, 500, 502, 503, 504)

    def __post_init__(self) -> None:
        """
        Validate bounds — extreme values produce weird behavior
        (overflow, infinite loops, zero-delay hammering). Fail
        loud per ``designs/DESIGN_PRINCIPLES.md``.

        :raises ValueError: If any field is out of bounds.
        """
        if not 0 <= self.max_retries <= 20:
            raise ValueError(f"max_retries must be 0..20, got {self.max_retries}")
        # Tests use very small backoff for fast-path retry coverage;
        # production uses ~2.0s base. The 0.001 floor catches genuine
        # mistakes (negative, 0.0) without rejecting valid test values.
        if self.backoff_base_s < 0.001:
            raise ValueError(f"backoff_base_s must be > 0, got {self.backoff_base_s}")
        if self.backoff_max_s < 0.001:
            raise ValueError(f"backoff_max_s must be > 0, got {self.backoff_max_s}")
        if self.timeout_per_request_s is not None and self.timeout_per_request_s <= 0:
            raise ValueError(
                f"timeout_per_request_s must be > 0 or None, got {self.timeout_per_request_s}"
            )

    def to_json(self) -> str:
        """
        Serialize the policy to a JSON string for cross-process
        env-var transport.

        Used by AP-side ``_serialize_retry_policy`` (Phase 1f of
        ``designs/RETRY_ACROSS_HARNESSES.md``) to thread the
        spec's retry budget into CLI-harness subprocesses via
        ``HARNESS_*_RETRY_POLICY`` env vars. Round-trips with
        :meth:`from_json` (which filters unknown keys for
        forwards compatibility).

        ``retryable_status_codes`` is a tuple in the dataclass;
        :func:`dataclasses.asdict` converts it to a list for JSON
        (which has no tuple type). :meth:`from_json` reconstructs
        the tuple on the read side.

        :returns: JSON string encoding all policy fields, e.g.
            ``'{"max_retries": 7, "backoff_base_s": 2.0, ...}'``.
        """
        import dataclasses
        import json

        return json.dumps(dataclasses.asdict(self))

    @classmethod
    def from_json(cls, payload: str) -> RetryPolicy:
        """
        Deserialize a policy from the JSON wire format produced
        by :meth:`to_json`.

        Unknown keys are filtered out so older harness wraps stay
        compatible with newer specs that add fields. Malformed
        JSON, non-dict payloads, or values that fail
        :meth:`__post_init__` validation all raise — the caller
        decides whether to fall back to ``RetryPolicy()`` or
        propagate.

        :param payload: JSON string from :meth:`to_json`, e.g.
            ``'{"max_retries": 10, "backoff_base_s": 1.0}'``.
        :returns: A :class:`RetryPolicy` reconstructed from the
            payload, with defaults applied for any unspecified
            fields.
        :raises ValueError: If ``payload`` is not valid JSON,
            does not decode to a dict, or fails the
            :class:`RetryPolicy` field-bound validators.
        """
        import json

        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"RetryPolicy.from_json: invalid JSON: {exc}") from exc
        if not isinstance(decoded, dict):
            raise ValueError(f"RetryPolicy.from_json: expected dict, got {type(decoded).__name__}")
        # Filter to known fields. Forwards compatibility:
        # newer specs may carry fields older wraps don't recognize;
        # those are silently dropped. Backwards compatibility:
        # missing fields fall back to dataclass defaults.
        kwargs: dict[str, Any] = {}  # type: ignore[explicit-any]
        for field_name in (
            "max_retries",
            "backoff_base_s",
            "backoff_max_s",
            "jitter",
            "timeout_per_request_s",
        ):
            if field_name in decoded:
                kwargs[field_name] = decoded[field_name]
        # ``retryable_status_codes`` arrives as a JSON list; the
        # dataclass declares it as ``tuple[int, ...]``. Convert
        # before construction so equality checks (e.g. against
        # the default) and downstream consumers see the
        # canonical type.
        if "retryable_status_codes" in decoded:
            codes = decoded["retryable_status_codes"]
            if not isinstance(codes, list):
                raise ValueError(
                    f"RetryPolicy.from_json: "
                    f"retryable_status_codes must be a list, got "
                    f"{type(codes).__name__}"
                )
            kwargs["retryable_status_codes"] = tuple(codes)
        return cls(**kwargs)

    @property
    def openai(self) -> _OpenAIRetryAdapter:
        """Adapter for ``AsyncOpenAI`` / ``OpenAI`` clients."""
        return _OpenAIRetryAdapter(self)

    @property
    def anthropic(self) -> _AnthropicRetryAdapter:
        """Adapter for ``Anthropic`` / ``AsyncAnthropic`` clients."""
        return _AnthropicRetryAdapter(self)

    @property
    def claude_cli(self) -> _ClaudeCliRetryAdapter:
        """Adapter for the Claude CLI subprocess."""
        return _ClaudeCliRetryAdapter(self)

    @property
    def codex_cli(self) -> _CodexCliRetryAdapter:
        """Adapter for the Codex CLI subprocess."""
        return _CodexCliRetryAdapter(self)

    @property
    def pi(self) -> _PiRetryAdapter:
        """Adapter for the Pi CLI subprocess."""
        return _PiRetryAdapter(self)

    def compute_backoff_delay(
        self,
        retry_index: int,
        retry_after_s: float | None = None,
    ) -> float:
        """
        Delay before retry attempt at L2 or in the in-process LLM
        path's retry loop.

        :param retry_index: 1-indexed retry number — ``1`` is the
            delay before the first retry.
        :param retry_after_s: Server-requested retry hint. When
            provided, the returned delay is at least
            ``retry_after_s``, capped by ``backoff_max_s``. ``None``
            means no server hint — pure exponential backoff applies.
        :returns: Delay in seconds.
        """
        import random

        delay: float = self.backoff_base_s * float(2 ** (retry_index - 1))
        if retry_after_s is not None:
            delay = max(delay, retry_after_s)
        delay = min(delay, self.backoff_max_s)
        if self.jitter:
            # ``random.uniform`` is typed as ``Any``-returning by
            # the stdlib stub; cast to float so the function's
            # declared return type holds without leaking ``Any``.
            delay = float(delay * random.uniform(0.5, 1.5))
        return delay


@dataclass(frozen=True)
class _OpenAIRetryAdapter:
    """Sub-object: produces L0 args for OpenAI SDK clients."""

    policy: RetryPolicy

    def kwargs(self) -> dict[str, Any]:  # type: ignore[explicit-any]
        """
        Args for ``AsyncOpenAI(...)`` / ``OpenAI(...)``
        constructors. Spread with ``**``.

        :returns: Dict with ``max_retries`` always, ``timeout``
            when configured.
        """
        kwargs: dict[str, Any] = {"max_retries": self.policy.max_retries}  # type: ignore[explicit-any]
        if self.policy.timeout_per_request_s is not None:
            kwargs["timeout"] = self.policy.timeout_per_request_s
        return kwargs


@dataclass(frozen=True)
class _AnthropicRetryAdapter:
    """Sub-object: produces L0 args for Anthropic SDK clients."""

    policy: RetryPolicy

    def kwargs(self) -> dict[str, Any]:  # type: ignore[explicit-any]
        """
        Args for ``Anthropic(...)`` / ``AsyncAnthropic(...)``
        constructors. Spread with ``**``.

        :returns: Dict with ``max_retries`` always, ``timeout``
            when configured.
        """
        kwargs: dict[str, Any] = {"max_retries": self.policy.max_retries}  # type: ignore[explicit-any]
        if self.policy.timeout_per_request_s is not None:
            kwargs["timeout"] = self.policy.timeout_per_request_s
        return kwargs


@dataclass(frozen=True)
class _ClaudeCliRetryAdapter:
    """Sub-object: produces env vars for the Claude CLI subprocess."""

    policy: RetryPolicy

    def env(self) -> dict[str, str]:
        """
        Env vars to merge into ``ClaudeAgentOptions.env``.

        The Claude CLI's retry budget isn't publicly documented as
        env-tunable; we set Anthropic SDK conventions
        speculatively.

        :returns: Dict mapping env var names to string values.
        """
        env: dict[str, str] = {
            "ANTHROPIC_MAX_RETRIES": str(self.policy.max_retries),
        }
        if self.policy.timeout_per_request_s is not None:
            env["ANTHROPIC_REQUEST_TIMEOUT_SECONDS"] = str(int(self.policy.timeout_per_request_s))
        return env


@dataclass(frozen=True)
class _CodexCliRetryAdapter:
    """Sub-object: produces env vars for the Codex CLI subprocess."""

    policy: RetryPolicy

    def env(self) -> dict[str, str]:
        """
        Env vars to merge into the Codex CLI subprocess env.
        Codex uses the OpenAI SDK internally; presumed to honor
        standard OpenAI env vars.

        :returns: Dict mapping env var names to string values.
        """
        env: dict[str, str] = {
            "OPENAI_MAX_RETRIES": str(self.policy.max_retries),
        }
        if self.policy.timeout_per_request_s is not None:
            env["OPENAI_TIMEOUT"] = str(int(self.policy.timeout_per_request_s))
        return env


@dataclass(frozen=True)
class _PiRetryAdapter:
    """Sub-object: produces a settings.json patch for Pi."""

    policy: RetryPolicy

    def settings(self) -> dict[str, Any]:  # type: ignore[explicit-any]
        """
        ``retry`` block to merge into Pi's ``.pi/settings.json``
        before subprocess spawn.

        Schema audited from
        ``@earendil-works/pi-coding-agent/docs/settings.md``
        (github.com/earendil-works/pi; formerly published as
        ``@mariozechner/pi-coding-agent@0.68.1``, same settings
        schema). Pi natively implements exponential backoff with
        jitter and ``Retry-After`` honoring; we configure the budget
        and shape.

        :returns: A dict matching Pi's settings shape.
        """
        return {
            "retry": {
                "enabled": self.policy.max_retries > 0,
                "maxRetries": self.policy.max_retries,
                "baseDelayMs": int(self.policy.backoff_base_s * 1000),
                "maxDelayMs": int(self.policy.backoff_max_s * 1000),
            },
        }


@dataclass(frozen=True)
class ApiKeyAuth:
    """
    Executor authentication via a direct OpenAI-compatible API key.

    Use this when the LLM endpoint is reached with a bearer token
    (OpenAI, Azure OpenAI, or any OpenAI-compatible provider).

    Preferred over raw env-var fallback because the key is explicit
    in the spec: the agent's behaviour is self-contained and does not
    depend on ambient ``OPENAI_API_KEY`` in the caller's shell.

    Example YAML (OpenAI)::

        executor:
          auth:
            type: api_key
            api_key: $OPENAI_API_KEY

    Example YAML (custom OpenAI-compatible endpoint)::

        executor:
          auth:
            type: api_key
            api_key: $MY_KEY
            base_url: https://my-gateway.example.com/v1

    :param api_key: The resolved API key value. Env-var references
        (e.g. ``$OPENAI_API_KEY``) are expanded at parse time.
    :param base_url: Optional OpenAI-compatible endpoint URL,
        e.g. ``"https://my-gateway.example.com/v1"``. When omitted,
        the default ``https://api.openai.com/v1`` is used.
    """

    # Required: the resolved API key value (env-var refs expanded at parse time).
    api_key: str
    # Optional: custom endpoint; None means use the default OpenAI endpoint.
    base_url: str | None = None
    type: Literal["api_key"] = "api_key"


@dataclass(frozen=True)
class DatabricksAuth:
    """
    Executor authentication via a Databricks profile from
    ``~/.databrickscfg``.

    Use this to route LLM calls through Databricks model serving
    (Unity AI Gateway or another Databricks-hosted endpoint) using
    a named credential profile.

    Example YAML::

        executor:
          auth:
            type: databricks
            profile: oss

    :param profile: Databricks profile name from ``~/.databrickscfg``,
        e.g. ``"oss"``. The executor resolves workspace host and
        OAuth token from this profile at runtime.
    """

    # Required: Databricks profile name from ~/.databrickscfg (e.g. "oss").
    profile: str
    type: Literal["databricks"] = "databricks"


@dataclass(frozen=True)
class ProviderAuth:
    """
    Executor authentication via a named generic model provider.

    References a provider declared in the ``providers:`` block of
    ``~/.omnigent/config.yaml`` (see
    ``designs/oss-cuj/04-model-selection-implementation.md``). The
    provider entry carries a ``kind`` (``key`` / ``subscription`` /
    ``gateway`` / ``local`` / ``databricks``) and, for the inline
    kinds, per-harness families (``anthropic`` for Claude-style
    harnesses, ``openai`` for Codex-style) supplying base URLs, secret
    references, default models, and wire protocols. This is the
    open-source counterpart to :class:`DatabricksAuth`: a single named
    provider (e.g. a LiteLLM proxy or OpenRouter) can route every
    harness family.

    Example YAML::

        executor:
          auth:
            type: provider
            name: litellm

    :param name: Provider name keyed under ``providers:`` in the global
        config, e.g. ``"litellm"`` or ``"openrouter"``. The harness
        spawn-env builder resolves the appropriate family (``anthropic``
        for Claude-style harnesses, ``openai`` for Codex-style) at
        runtime.
    :param type: Discriminator literal, always ``"provider"``. Lets the
        :data:`ExecutorAuth` union distinguish this from
        :class:`ApiKeyAuth` / :class:`DatabricksAuth`.
    """

    # Required: provider name from ~/.omnigent/config.yaml providers:.
    name: str
    type: Literal["provider"] = "provider"


# Discriminated union of all supported executor auth types.
# Discriminator field is ``type``.
ExecutorAuth = ApiKeyAuth | DatabricksAuth | ProviderAuth


@dataclass
class ExecutorSpec:  # type: ignore[explicit-any]  # config: dict[str, Any] field (see below)
    """
    Top-level executor configuration.

    ``type`` is the discriminator for the entire spec's validity —
    it determines which other top-level sections and fields are
    valid. Invalid fields are rejected by the validator.

    :param type: Executor type. ``"omnigent"`` (default),
        ``"claude_sdk"``, or ``"agents_sdk"``.
    :param timeout: Task deadline in seconds (wall-clock limit for
        the entire agent loop), e.g. ``3600``.
    :param max_iterations: Maximum ``run_turn()`` calls before the
        loop terminates as incomplete, e.g. ``1000``.
    :param profile: The Databricks workspace profile name from
        ``~/.databrickscfg``, e.g. ``"dev"``. Used by the
        ``databricks_supervisor`` harness and (during the
        omnigent-compat sunset) lifted from raw YAML's
        ``executor.profile`` in the omnigent path too. ``None``
        means resolve via env vars / DEFAULT section.

        .. deprecated::
            Set ``executor.auth: {type: databricks, profile: <name>}``
            instead. Direct ``executor.profile`` / ``executor.config.profile``
            will be removed once all callers migrate.
    :param config: Executor-type-specific configuration. For
        ``type == "omnigent"`` this carries ``"harness"`` (e.g.
        ``"claude-sdk"`` or ``"codex"``), optional ``"profile"``
        (e.g. ``"<your-profile>"``), and optional ``"os_env"`` — the
        latter is a nested mapping mirroring the omnigent
        ``OSEnvSpec`` shape (``{type, cwd, sandbox: {...}}``) or
        the literal string ``"inherit"`` on inline-AgentTool
        sub-specs. Empty dict for other executor types.

        🚨 **TECH DEBT — REMOVE WHEN OMNIGENT COMPAT ENDS.**
        This field exists *solely* to carry harness / profile /
        os_env data for the Omnigent integration (see
        ``designs/OMNIGENT_INTEGRATION.md``). A free-form
        ``dict[str, Any]`` on a spec dataclass is the kind of
        bag-of-values escape hatch we'd otherwise reject in
        review — acceptable here only because the Omnigent
        executor is a temporary bridge with explicit sunset
        criteria. Once Omnigent is consolidated (phase 6 of the
        integration design), this field and every reader of it
        must go away. Do NOT use ``config`` as a general-purpose
        extension point for new executor types — add concrete
        fields instead.

        The value type widened from ``dict[str, str]`` to
        ``dict[str, Any]`` when OSEnvSpec support landed, since
        the nested ``sandbox`` mapping can't be flattened to
        string values without losing fidelity.
    :param model: The provider-prefixed model identifier, e.g.
        ``"databricks-gpt-5-5"`` or ``"openai/gpt-5.4"``. Primary
        source of truth for the model across all executor types —
        populated by the parser from either the ``executor.model``
        YAML key or (for backward compatibility) the ``llm.model``
        key. Used by harness spawn-env builders, context-window
        auto-detection, telemetry, and tool-provider inference.
        ``None`` only when no model is declared anywhere in the spec.
    :param connection: Per-provider connection overrides (credentials,
        endpoint URLs), e.g.
        ``{"api_key": "sk-...", "base_url": "https://..."}``.
        Primary source of truth for connection configuration —
        populated by the parser from either the ``executor.connection``
        YAML key or (for backward compatibility) the ``llm.connection``
        key. Keys are provider-specific: ``api_key`` + ``base_url``
        for OpenAI-compatible providers, ``aws_region`` for Bedrock,
        etc. ``None`` means use environment variable defaults or
        profile-based credential resolution.
    :param context_window: Explicit context window size for the model,
        in tokens (input + output combined). When set, overrides the
        automatic litellm / catalog lookup for both the REPL context
        ring and the compression threshold. Use this for models that
        are not yet in the registry, e.g. ``context_window: 400000``
        in ``config.yaml``. ``None`` means auto-detect (default).
    :param supervisor_tools: Verbatim list of nested tool
        declarations for the ``databricks_supervisor`` harness, e.g.
        ``[{"type": "genie_space",
        "genie_space": {"id": "abc-123",
        "description": "Sales analytics"}},
        {"type": "uc_function",
        "uc_function": {"name": "main.search",
        "description": "Search the catalog"}}]``. Each entry has a
        ``type`` key plus a nested sub-dict keyed on the same type
        carrying the tool's per-type config (post env-var
        expansion) — the shape the real Databricks Supervisor API
        accepts. The list round-trips verbatim — the parser does
        NOT normalize these into a function-tool shape because the
        supervisor executor consumes them as-is. ``None`` when the
        ``databricks_supervisor`` harness is not selected. The
        value type is ``dict[str, Any]`` because the nested
        sub-dict carries heterogeneous fields (booleans, numbers,
        etc.) per the gateway's per-type schema.
    :param auth: Explicit LLM authentication configuration. When set,
        the harness uses this to authenticate instead of falling back
        to ambient environment variables or profile auto-detection.
        Supports three types: :class:`ApiKeyAuth` (inline bearer token),
        :class:`DatabricksAuth` (Databricks profile, ucode-backed), and
        :class:`ProviderAuth` (a named generic provider from
        ``~/.omnigent/config.yaml``). ``None`` means fall back to
        environment variable / profile defaults.
    """

    type: str = "omnigent"
    timeout: int = 3600
    max_iterations: int = 1000
    # Databricks workspace profile name from ~/.databrickscfg.
    # Used by the databricks_supervisor harness and (during the
    # omnigent-compat sunset) lifted from raw YAML's
    # executor.profile in the omnigent path too. None = resolve
    # via env vars / DEFAULT section. See class docstring.
    # DEPRECATED: use executor.auth: {type: databricks, profile: <name>} instead.
    profile: str | None = None
    # TECH DEBT (omnigent compat only — see class docstring).
    # Remove when Omnigent consolidation lands; do NOT extend.
    # Any: opaque per-type executor config passed through to adapters.
    config: dict[str, Any] = field(default_factory=dict)  # type: ignore[explicit-any]
    # Primary model identifier for all executor types. Populated by
    # the parser from executor.model or (backward compat) llm.model.
    model: str | None = None
    # Per-provider connection overrides (api_key, base_url, etc.).
    # Populated from executor.connection or (backward compat) llm.connection.
    # None means rely on environment variable / profile defaults.
    connection: dict[str, str] | None = None
    # Explicit context window override (input + output tokens). None = auto-detect via litellm.
    context_window: int | None = None
    # Verbatim list of nested tool declarations for the
    # databricks_supervisor harness. Populated only when
    # config.harness == "databricks_supervisor". Each entry:
    # {type: X, X: {<config>}} matching the Databricks Supervisor
    # API. Inner config dict values are heterogeneous (the gateway
    # accepts strings, numbers, bools), so the value type is
    # dict[str, Any]. None when the supervisor harness is not used.
    supervisor_tools: list[dict[str, Any]] | None = None  # type: ignore[explicit-any]
    # Explicit executor auth. Populated from executor.auth in the YAML.
    # Takes precedence over ambient env vars and profile auto-detection.
    # None = fall back to env vars / profile defaults.
    auth: ApiKeyAuth | DatabricksAuth | ProviderAuth | None = None

    @property
    def harness_kind(self) -> str:
        """
        The agent's harness/kind for display and discovery.

        For ``type == "omnigent"`` the kind lives in
        ``config["harness"]`` (e.g. ``"codex"``, ``"codex-native"``,
        ``"claude-native"``); for every other executor type the kind
        *is* the executor type (e.g. ``"claude_sdk"``,
        ``"agents_sdk"``). Consumed by the ``GET /v1/agents`` catalog
        and ``GET /v1/sessions/{id}/agent`` so the Web UI can tell
        Codex from Claude agents without matching on the name slug.

        :returns: The harness identifier, e.g. ``"codex"`` or
            ``"claude_sdk"``. Never empty — falls back to
            :attr:`type`.
        """
        return self.config.get("harness") or self.type


@dataclass
class CompactionConfig:
    """
    Context compaction configuration.

    Controls when the agent compacts its conversation history to
    stay within the LLM's context window. Compaction is layered:
    (1) clear tool result bodies, (2) LLM summarization, (3)
    truncation as emergency fallback.

    :param trigger_threshold: Fraction of the model's context window
        at which proactive compaction fires (after the first overflow
        has been observed and the window size is known), e.g. ``0.8``
        means fire at 80% of the window.
    :param recent_window: Number of recent LLM iterations to protect
        from compaction. Items within this window are never cleared or
        summarized — the agent always has verbatim access to its most
        recent work, e.g. ``5``.
    """

    trigger_threshold: float = 0.8
    recent_window: int = 5


@dataclass
class LLMConfig:  # type: ignore[explicit-any]  # extra: dict[str, Any] field (see below)
    """
    LLM configuration block from config.yaml.

    ``model`` is the only required field. ``request_timeout`` and
    ``retry`` control call-level resilience. All other keys from the
    YAML ``llm:`` block are collected into ``extra`` and passed
    through to the OpenAI SDK as-is.

    :param model: The provider-prefixed model identifier, e.g.
        ``"openai/gpt-5.4"`` or ``"anthropic/claude-sonnet-4-20250514"``.
    :param extra: Arbitrary kwargs from the YAML ``llm:`` block
        (everything except ``model``, ``connection``,
        ``request_timeout``, and ``retry``). Values are heterogeneous
        (str, int, dict, etc.) so ``Any`` is the narrowest safe type.
        Example: ``{"temperature": 0.7, "max_tokens": 4096}``.
    :param connection: Per-provider connection overrides from the
        YAML ``connection:`` sub-block. Keys are provider-specific,
        e.g. ``{"api_key": "...", "base_url": "..."}`` for
        OpenAI-compatible providers or
        ``{"aws_region": "us-west-2"}`` for Bedrock.
        ``None`` means use environment variable defaults.
    :param profile: Databricks CLI profile name from
        ``~/.databrickscfg``, e.g. ``"my-workspace"``. When set,
        the profile is resolved to workspace credentials at build
        time and used as the connection. ``None`` means no profile
        — use ``connection`` or environment defaults. ``connection``
        wins when both are present.
    :param request_timeout: Per-LLM-call timeout in seconds (both
        streaming and non-streaming), e.g. ``300``. Named
        ``request_timeout`` to distinguish from the task-level
        ``executor.timeout``.
    :param retry: Retry policy for transient LLM failures.
    """

    model: str
    # Arbitrary kwargs from the YAML llm block (everything except
    # ``model``, ``connection``, ``request_timeout``, ``profile``,
    # and ``retry``).
    # Values are heterogeneous (str, int, dict, etc.) so Any is the
    # narrowest safe type.
    extra: dict[str, Any] = field(default_factory=dict)  # type: ignore[explicit-any]
    # Per-provider connection overrides (api_key, base_url, etc.).
    # None means rely on environment variable defaults.
    connection: dict[str, str] | None = None
    # Databricks CLI profile name for profile-based auth.
    profile: str | None = None
    request_timeout: int = 300
    retry: RetryPolicy = field(default_factory=RetryPolicy)


@dataclass
class ModalityConfig:
    """
    Declared input/output content types.

    :param input: Accepted input modalities. Valid values are
        ``"text"``, ``"image"``, ``"audio"``, ``"video"``, and
        ``"file"``. Defaults to ``["text"]``.
    :param output: Produced output modalities. Valid values are
        ``"text"``, ``"image"``, and ``"audio"``. Defaults to
        ``["text"]``.
    """

    input: list[str] = field(default_factory=lambda: ["text"])
    output: list[str] = field(default_factory=lambda: ["text"])


@dataclass
class InteractionConfig:
    """
    Interaction contract: conversational mode and modalities.

    :param conversational: Whether the agent supports multi-turn
        conversation. Defaults to ``True``.
    :param modalities: Input/output content type declarations.
        Defaults to text-only.
    """

    conversational: bool = True
    modalities: ModalityConfig = field(default_factory=ModalityConfig)


@dataclass
class BuiltinToolConfig:
    """
    Configuration for a single built-in tool declared in
    ``tools.builtins``.

    :param name: The registered tool name, e.g.
        ``"web_search"``.
    :param config: Tool-specific key-value pairs, e.g.
        ``{"api_key": "AIza...", "engine_id": "abc123"}``.
        Empty when the tool needs no configuration.
    """

    name: str
    config: dict[str, str] = field(default_factory=dict)


@dataclass
class SandboxConfig:
    """
    Agent-level sandbox configuration for local tool execution.

    Only contains settings the agent author controls (what
    execution environment their tools need). Whether sandboxing
    is enabled/enforced is a runtime decision — see
    ``RuntimeCaps.sandbox_enabled``.

    :param container_image: When set, tools run inside this
        container instead of a local subprocess, e.g.
        ``"python:3.12-slim"``.
    :param docker_image: Deprecated alias for ``container_image``.
        If both are set, ``container_image`` takes precedence.
    :param container_runtime: The container CLI to use, either
        ``"docker"`` (default) or ``"podman"``.
    """

    container_image: str | None = None
    docker_image: str | None = None
    container_runtime: str = "docker"

    def __post_init__(self) -> None:
        if self.container_image is None and self.docker_image is not None:
            self.container_image = self.docker_image
        self.docker_image = self.container_image


@dataclass
class ToolsConfig:
    """
    Declared tool references from config.yaml.

    :param agents: Names of sub-agents this agent can delegate to,
        e.g. ``["summarizer", "code-reviewer"]``. Each name must
        match a directory under ``agents/``.
    :param builtins: Built-in tools to enable, e.g.
        ``[BuiltinToolConfig(name="web_search")]``. Each
        entry carries the tool name and optional config fields
        (API keys, engine IDs, etc.).
    :param timeout: Default timeout in seconds for all tool calls,
        e.g. ``60``. Individual tools can override this.
    :param retry: Default retry policy for all tool calls.
        Individual tools can override this.
    """

    agents: list[str] = field(default_factory=list)
    builtins: list[BuiltinToolConfig] = field(default_factory=list)
    timeout: int = 60
    retry: RetryPolicy = field(
        default_factory=lambda: RetryPolicy(
            max_retries=1,
            backoff_base_s=1.0,
            backoff_max_s=10.0,
        )
    )
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)


@dataclass
class SkillSpec:
    """
    A parsed skill from ``skills/<name>/SKILL.md``.

    :param name: Lowercase kebab-case skill identifier, e.g.
        ``"code-review"``. Must match ``[a-z0-9-]+``.
    :param description: Human-readable summary of what the skill
        does (max 1024 characters).
    :param content: The body of the SKILL.md file after the YAML
        frontmatter, containing the skill's instructions.
    :param skill_dir: Absolute path to the skill's directory on
        disk, e.g. ``Path("/agents/code-review")``. Used by
        ``read_skill_file`` to resolve resource paths. ``None``
        when the skill was created in-memory (e.g. tests).
    """

    name: str
    description: str
    content: str
    skill_dir: Path | None = None


@dataclass
class MCPServerConfig:
    """
    An MCP server declaration from ``tools/mcp/<name>.yaml``.

    Two transports are supported:

    - ``"http"`` — HTTP (SSE) endpoint, reached via
      :func:`mcp.client.sse.sse_client`. ``url`` (and optionally
      ``headers``) describe the endpoint. Traditional "deployed MCP"
      shape: the server runs elsewhere; this process is just a
      client.
    - ``"stdio"`` — local subprocess, spawned via
      :func:`mcp.client.stdio.stdio_client`. ``command`` and
      ``args`` describe the program to run; ``env`` supplies
      per-process environment variables on top of the parent's
      environment. The subprocess runs unsandboxed — same as the
      legacy inner stack at ``omnigent/inner/mcp_tools.py``
      (which has never sandboxed stdio MCPs). The previous
      AP-only ``sandbox: bool`` field that wrapped the spawn
      with ``srt`` was removed in step 7 of the harness contract
      migration: srt's default policy blocks outbound network
      (which every useful MCP needs to reach its backend), so
      sandboxing was producing silent hangs in practice. Per-MCP
      sandboxing through the ``omnigent/environments/``
      primitive with explicit outbound-host allowlists is the
      eventual replacement; left to a future design.

    Validator enforces that HTTP fields (``url``, ``headers``) and
    stdio fields (``command``, ``args``, ``env``) don't appear on
    the wrong transport.

    :param name: Unique server identifier, e.g. ``"github"``.
    :param transport: ``"http"`` or ``"stdio"``. Default
        ``"http"`` for backwards compatibility with existing
        ``tools/mcp/<name>.yaml`` files that pre-date the stdio
        branch.
    :param url: HTTP (SSE) endpoint URL, e.g.
        ``"https://mcp.example.com/sse"``. Required when
        ``transport == "http"``; invalid for ``"stdio"``.
    :param headers: HTTP headers, e.g.
        ``{"Authorization": "Bearer tok_xyz"}``. Valid only on
        ``"http"``.
    :param databricks_profile: Databricks profile name from
        ``~/.databrickscfg``, e.g. ``"oss"``. When set, the
        connection resolves an OAuth token at runtime and injects
        ``Authorization: Bearer <token>`` into the HTTP headers.
        Valid only on ``"http"``. Avoids hardcoding short-lived
        tokens in the YAML.
    :param command: Executable to spawn, e.g. ``"npx"``. Required
        when ``transport == "stdio"``; invalid for ``"http"``.
    :param args: Arguments to pass to *command*, e.g.
        ``["-y", "@modelcontextprotocol/server-github"]``. Valid
        only on ``"stdio"``.
    :param env: Environment variables to set on the spawned
        subprocess (overlaid on the parent environment). Valid
        only on ``"stdio"``. Use for secrets the subprocess reads
        at startup, e.g. ``{"GITHUB_TOKEN": "${GITHUB_TOKEN}"}``.
    :param description: Optional human-readable summary of the
        server's purpose.
    :param timeout: Per-tool timeout in seconds. ``None`` inherits
        ``tools.timeout``. When ultimately ``None`` at runtime, the
        MCP SDK defaults apply: 5 s for the initial HTTP connection
        handshake and 300 s (5 min) for each SSE event read.
        Stdio transport applies the same value to session
        initialization and tool calls.
    :param retry: Per-tool retry policy. ``None`` inherits
        ``tools.retry``.
    """

    name: str
    transport: Literal["http", "stdio"] = "http"
    # HTTP-only fields.
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    # Databricks profile auth — resolves a bearer token at connection
    # time from ``~/.databrickscfg`` and injects it as the
    # ``Authorization`` header. Mutually usable with ``headers``:
    # explicit headers win if both set ``Authorization``.
    databricks_profile: str | None = None
    # Stdio-only fields.
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict, repr=False)
    description: str | None = None
    # Per-tool timeout/retry overrides. None = inherit from
    # tools.timeout / tools.retry.
    timeout: int | None = None
    retry: RetryPolicy | None = None

    def __repr__(self) -> str:
        """
        String representation that redacts secret-bearing fields.

        Header and env values are replaced with ``"[REDACTED]"`` —
        credentials commonly travel through these fields and we
        don't want them in exception tracebacks / log scrapes.
        Keys are preserved so operators can still tell which
        secrets were attached.
        """
        redacted_headers = dict.fromkeys(self.headers, "[REDACTED]") if self.headers else {}
        redacted_env = dict.fromkeys(self.env, "[REDACTED]") if self.env else {}
        return (
            f"MCPServerConfig(name={self.name!r}, transport={self.transport!r}, "
            f"url={self.url!r}, headers={redacted_headers!r}, "
            f"databricks_profile={self.databricks_profile!r}, "
            f"command={self.command!r}, args={self.args!r}, "
            f"env={redacted_env!r}, "
            f"timeout={self.timeout!r}, retry={self.retry!r})"
        )


class ToolRuntime(str, Enum):
    """Where a function tool's implementation lives.

    - :attr:`SERVER`: server loads a dotted ``callable:`` path.
    - :attr:`CLIENT`: SDK consumer provides the impl at stream
      start. ``callable:`` is forbidden (validation error).
    - :attr:`UC_FUNCTION`: tool backed by a Unity Catalog SQL
      function. Executed via ``WorkspaceClient
      .statement_execution.execute_statement()`` at tool-call
      time. ``path`` is ``None``; ``catalog_path`` carries the
      three-level UC name (``catalog.schema.function``).
    """

    SERVER = "server"
    CLIENT = "client"
    UC_FUNCTION = "uc_function"


@dataclass
class LocalToolInfo:  # type: ignore[explicit-any]  # parameters: dict[str, Any] field (see below)
    """
    A discovered local tool file (Python or TypeScript).

    :param name: Derived tool name from filename stem,
        e.g. ``"arxiv_search"`` (from ``arxiv_search.py``).
    :param path: Dotted callable path or relative file path within
        the agent image, e.g.
        ``"examples._shared.tool_functions.search_web"`` or
        ``"tools/python/arxiv_search.py"``. ``None`` only when
        :attr:`runtime` is :attr:`ToolRuntime.CLIENT` — client-runtime
        tools have no server-side implementation, so they have no
        path. The validator enforces the runtime↔path invariant
        (server requires a path; client forbids one).
    :param language: Source language. Either ``"python"`` or
        ``"typescript"``.
    :param timeout: Per-tool timeout in seconds. ``None`` inherits
        ``tools.timeout``.
    :param retry: Per-tool retry policy. ``None`` inherits
        ``tools.retry``.
    :param has_inline_deps: ``True`` if the tool file contains
        PEP 723 inline script metadata with dependencies.
    :param inline_deps: PEP 508 dependency specifiers extracted
        from the ``# /// script`` block. ``None`` when no
        inline metadata is present.
    :param parameters: JSON-Schema ``parameters`` block. ``None``
        means "use the harness default" (introspection for plain
        callables). Required for :class:`CancellableFunctionTool`
        (no inspectable signature) and for client-runtime tools
        (no server-side callable).
    :param runtime: See :class:`ToolRuntime`. Client tools forbid
        :attr:`path` and require explicit :attr:`parameters`.
    :param catalog_path: Three-level Unity Catalog function name,
        e.g. ``"my_catalog.my_schema.classify_sentiment"``.
        ``None`` for non-UC tools. Mutually exclusive with
        :attr:`path` — UC tools have no server-side callable.
        Set only when :attr:`runtime` is
        :attr:`ToolRuntime.UC_FUNCTION`.
    :param warehouse_id: Databricks SQL warehouse ID for UC
        function execution, e.g. ``"abc123def456"``. Required
        when :attr:`runtime` is :attr:`ToolRuntime.UC_FUNCTION`.
        ``None`` for non-UC tools.
    """

    name: str
    path: str | None
    language: str
    # Per-tool timeout/retry overrides. None = inherit from
    # tools.timeout / tools.retry.
    timeout: int | None = None
    retry: RetryPolicy | None = None
    # PEP 723 inline dependency metadata. Populated at load time
    # by scanning the tool source file.
    has_inline_deps: bool = False
    inline_deps: list[str] | None = None
    parameters: dict[str, Any] | None = None  # type: ignore[explicit-any]
    runtime: ToolRuntime = ToolRuntime.SERVER
    catalog_path: str | None = None
    warehouse_id: str | None = None
    description: str | None = None


# ── Guardrails / Policies (POLICIES.md §3, §4) ──────────────
#
# Spec-level types for the policy system. These are pure data
# containers — no runtime behavior here. Runtime policy classes
# (FunctionPolicy, PromptPolicy) and the
# PolicyEngine live under omnigent.runtime.policies in later
# phases.


class Phase(str, Enum):
    """
    The six points in the agent loop where policies fire.

    ``str`` mix-in keeps YAML parsing trivial
    (``Phase("tool_call")``) and preserves the string form in
    logs / JSON serialization.

    Session-level phases (fire once per turn):

    - ``REQUEST``: after a new user message arrives, before
      the LLM turn.
    - ``RESPONSE``: after the LLM's final assistant message,
      before persistence.

    Tool phases (fire per tool invocation):

    - ``TOOL_CALL``: before dispatching a tool call emitted by
      the LLM.
    - ``TOOL_RESULT``: after a tool returns, before the result
      is surfaced back to the LLM.
    LLM phases (fire per LLM round-trip within a turn):

    - ``LLM_REQUEST``: before each LLM call, with the full
      prompt (system instructions + conversation history +
      tool schemas). A single turn with tool calls fires
      this multiple times — once per round-trip, not once
      per turn. Does NOT fire on retries of the same call.
    - ``LLM_RESPONSE``: after each LLM call returns, with
      the raw model output before tool-call extraction or
      post-processing. Mirrors ``LLM_REQUEST`` in firing
      frequency (once per successful round-trip).
    """

    REQUEST = "request"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    RESPONSE = "response"
    LLM_REQUEST = "llm_request"
    LLM_RESPONSE = "llm_response"


class PolicyAction(str, Enum):
    """
    The three decisions a policy can emit.

    - ``ALLOW``: the phase proceeds normally.
    - ``ASK``: park for user approval; on approve → ALLOW, on
      refuse/timeout → DENY.
    - ``DENY``: short-circuit the phase; replace content with a
      sentinel so downstream steps cannot act on it.
    """

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class StateUpdateAction(str, Enum):
    """
    Operations a policy can request on the session state.

    - ``SET``: overwrite (or create) a key with the given value.
    - ``INCREMENT``: add a numeric delta to an existing numeric
      value (defaults to ``0`` when the key is absent).
    - ``DELETE``: remove a key from session state. ``value`` is
      ignored.
    - ``APPEND``: append ``value`` to a list stored at the key.
      Creates a new single-element list when the key is absent.
    """

    SET = "set"
    INCREMENT = "increment"
    DELETE = "delete"
    APPEND = "append"


@dataclass(frozen=True)
class StateUpdate:  # type: ignore[explicit-any]  # value: Any field (see below)
    """
    A single mutation to apply to the session state dict.

    Returned by policy callables inside
    :attr:`PolicyResult.state_updates` as a list. The engine
    applies them in order after composing results from all
    policies in a pass.

    :param key: The session-state key to mutate,
        e.g. ``"call_count"``.
    :param action: The operation to perform,
        e.g. ``StateUpdateAction.INCREMENT``.
    :param value: The operand. Required for ``SET``,
        ``INCREMENT``, and ``APPEND``; ignored for ``DELETE``.
        For ``INCREMENT`` this must be numeric (int or float).
    """

    key: str
    action: StateUpdateAction
    value: Any = None  # type: ignore[explicit-any]


@dataclass(frozen=True)
class PhaseSelector:
    """
    One entry in a policy's ``on:`` list.

    YAML shapes:

    - ``"tool_call"`` →
      ``PhaseSelector(phase=Phase.TOOL_CALL, tool_name=None)``
      (wildcard — matches every tool call).
    - ``"tool_call:code_sandbox"`` →
      ``PhaseSelector(Phase.TOOL_CALL, "code_sandbox")``
      (narrows to one tool by name).

    Tool-name narrowing is only valid on ``TOOL_CALL`` and
    ``TOOL_RESULT`` phases — the parser rejects it on
    ``REQUEST``, ``RESPONSE``, ``LLM_REQUEST``, and
    ``LLM_RESPONSE``.

    :param phase: Which enforcement point this selector fires
        on.
    :param tool_name: When set, only matches that tool. When
        ``None``, matches any tool in the phase (wildcard).
    """

    phase: Phase
    tool_name: str | None = None

    def matches(self, ctx: EvaluationContext) -> bool:
        """
        Test whether this selector matches an evaluation
        context.

        :param ctx: The current evaluation context
            (phase + resolved tool name).
        :returns: ``True`` if the selector's phase matches and
            (when ``tool_name`` is set) the context's
            ``tool_name`` matches.
        """
        if ctx.phase != self.phase:
            return False
        if self.tool_name is None:
            return True
        return ctx.tool_name == self.tool_name


@dataclass(frozen=True)
class LabelDef:
    """
    Schema for one label key.

    Declared statically in
    ``spec.guardrails.labels[key]``. Controls what values the
    label can take and how it can change over the course of a
    conversation (see POLICIES.md §10).

    :param initial: Seed value written at conversation start.
        ``None`` means the label is unset until a policy
        writes it for the first time, e.g. ``"0"``.
    :param values: Ordered list of allowed values. Position
        defines ranking when ``monotonic`` is set. ``None``
        means schemaless — writes are unconstrained, e.g.
        ``["0", "1"]`` or
        ``["public", "internal", "confidential"]``.
    :param monotonic: Update constraint when ``values`` is
        declared. ``"increasing"`` means new index must be
        ``>=`` current; ``"decreasing"`` means ``<=``.
        ``None`` means free transitions between declared
        values.
    """

    initial: str | None = None
    values: list[str] | None = None
    monotonic: Literal["increasing", "decreasing"] | None = None


@dataclass(frozen=True)
class FunctionRef:  # type: ignore[explicit-any]  # arguments: dict[str, Any] field (see below)
    """
    Reference to a policy callable, with optional factory
    kwargs.

    Two YAML shapes parse into this:

    - Bare string: ``function: myorg.policies.simple_check`` →
      ``FunctionRef(path="myorg.policies.simple_check",
      arguments=None)``. The resolved callable is the
      evaluator itself.
    - Dict: ``function: {path: myorg.policies.rate_limit,
      arguments: {limit: 10}}`` →
      ``FunctionRef(path="...", arguments={"limit": 10})``.
      The resolved callable is a factory called once at
      workflow start; its return value is the evaluator.

    :param path: Dotted import path to the callable or
        factory, e.g.
        ``"myorg.policies.search_rate_limit"``.
    :param arguments: Factory kwargs (present = factory form;
        absent = direct callable form). ``None`` when the
        YAML used the short string form.
    """

    path: str
    # Any: opaque factory kwargs passed to user-defined policy callable.
    arguments: dict[str, Any] | None = None  # type: ignore[explicit-any]


@dataclass
class PolicySpec:
    """
    Base class for all policy specs.

    Concrete subtypes (``FunctionPolicySpec``,
    ``PromptPolicySpec``) carry
    type-specific fields. The class itself *is* the
    discriminator — no separate ``type`` field at runtime.

    ``condition`` is a label-gate: if declared, the engine
    checks current label values against it BEFORE dispatching
    to the policy's ``evaluate()``. Non-matching policies are
    skipped entirely (no action emitted, no LLM call, no
    Python call) — cheap way to gate expensive policies on
    session state (POLICIES.md §4, §10).

    :param name: YAML key this policy was declared under,
        e.g. ``"block_canada_input"``.
    :param on: Phases this policy fires on, e.g.
        ``[PhaseSelector(Phase.TOOL_CALL, "web_search")]``.
        ``None`` for :class:`FunctionPolicySpec` — the callable
        self-selects by returning ``None`` to abstain.
    :param condition: Label-gate. Empty / absent =
        always-match; values coerced to strings at spec load,
        e.g. ``{"integrity": "0"}`` or
        ``{"role": ["admin", "ops"]}``.
    :param ask_timeout: Per-policy approval timeout override
        (seconds). ``None`` falls back to
        ``GuardrailsSpec.ask_timeout``. Useful when some ASKs
        are cheap (yes/no) and some expensive (review a 50 KB
        document).
    """

    name: str
    on: list[PhaseSelector] | None
    condition: dict[str, str | list[str]] | None = None
    ask_timeout: int | None = None


@dataclass
class FunctionPolicySpec(PolicySpec):
    """
    A policy backed by a Python callable (see POLICIES.md §9.1).

    :param function: Where the callable lives + optional
        factory kwargs.
    :param action: Allowed actions the callable may return.
        Returns outside this list → fail-closed DENY (or
        substituted ALLOW when the list contains no DENY, per
        the classifier-only carve-out in §13). ``None`` means
        accept any action.
    :param set_labels: Whitelist of label keys the callable
        may write. Keys outside dropped silently. ``None``
        means no writes declared (any key the callable emits
        that has a ``LabelDef`` will still validate against
        that LabelDef, but keys without a LabelDef are set
        freely per omnigent parity).
    :param config: Runtime configuration key-value pairs passed
        to the callable as the second argument on every
        invocation. Declared at policy attachment time (in the
        YAML spec) and surfaced verbatim at evaluation time.
        ``None`` means no config declared; the callable receives
        an empty dict.
    """

    function: FunctionRef | None = None
    action: list[PolicyAction] | None = None
    set_labels: list[str] | None = None
    config: dict[str, str] | None = None


@dataclass
class GuardrailsSpec:
    """
    Top-level guardrails block from config.yaml.

    Bundles label definitions, policies, and the spec-wide
    ASK timeout default (POLICIES.md §3.2).

    :param labels: Per-key ``LabelDef`` schemas. ``None``
        means no labels declared — all label writes go
        through the schemaless-set-freely path.
    :param policies: Policies in YAML declaration order (the
        engine iterates in this order).
    :param ask_timeout: Spec-wide default approval timeout in
        seconds. Individual policies may override via
        ``PolicySpec.ask_timeout``. Defaults to
        :data:`DEFAULT_ASK_TIMEOUT` (30 s).
    """

    labels: dict[str, LabelDef] | None = None
    policies: list[PolicySpec] | None = None
    ask_timeout: int = DEFAULT_ASK_TIMEOUT


@dataclass
class AgentSpec:  # type: ignore[explicit-any]  # params: dict[str, Any] field (see below)
    """
    A fully parsed agent image.

    Produced by the parser from a directory on disk; validated by
    the validator.

    :param spec_version: Schema version of the agent spec. Currently
        must be ``1``.
    :param name: Human-readable agent name, e.g. ``"code-reviewer"``.
    :param description: Short summary of the agent's purpose.
    :param llm: LLM configuration. ``None`` means the agent does not
        declare an LLM preference.
    :param interaction: Conversational mode and modality settings.
    :param tools: Declared tool references (sub-agent names, etc.).
    :param params: Arbitrary key-value parameters readable by skills
        and tools. Values are heterogeneous (str, int, bool, list,
        dict), so ``Any`` is the narrowest safe type. Example:
        ``{"max_retries": 3, "style": "concise"}``.
    :param instructions: Agent system prompt, typically from
        ``AGENTS.md``. ``None`` if no instructions file is present.
    :param skills: Parsed skills from ``skills/<name>/SKILL.md``.
    :param mcp_servers: MCP server declarations from
        ``tools/mcp/<name>.yaml``.
    :param local_tools: Discovered local tool files from
        ``tools/python/`` and ``tools/typescript/``.
    :param sub_agents: Recursively parsed child agents from
        ``agents/<name>/``.
    :param executor: Executor configuration (type, task timeout,
        max iterations). ``executor.type`` is the
        discriminator for the entire spec's validity.
    :param compaction: Compaction configuration for context management.
        ``None`` means use defaults (trigger at 80%, protect last 5
        iterations).
    :param guardrails: Guardrails configuration (labels + policies
        + ASK timeout). ``None`` means the agent declared no
        ``guardrails:`` block — runtime builds a no-op engine
        with zero policies and an empty label cache. See
        POLICIES.md §3, §4.
    :param async_enabled: Whether the LLM-callable async-dispatch
        builtins (``sys_call_async``, ``sys_read_inbox``,
        ``sys_cancel_async``) are registered. YAML key is
        ``async:``; the dataclass field avoids the Python
        keyword. **Defaults to ``True``** to match the legacy
        inner-stack default at
        ``omnigent/inner/datamodel.py::AgentDef.async_enabled``
        — the same YAML must produce the same tool surface
        whether the user opts into Omnigent mode or runs the legacy
        path. Agents that want to suppress the async surface
        declare ``async: false`` explicitly. See
        ``designs/SERVER_HARNESS_CONTRACT.md`` §Async work +
        inbox + the step-11 sub-step entries.
    :param os_env: The agent's OS environment, e.g.
        ``OSEnvSpec(type="caller_process", cwd=".",
        sandbox=OSEnvSandboxSpec(type="linux_bwrap",
        write_paths=["."], allow_network=False))``. The default
        sandbox on Linux is ``linux_bwrap`` (mount-namespace +
        seccomp hardening; see
        :mod:`omnigent.inner.bwrap_sandbox`), selected when the
        ``bwrap`` binary is on ``PATH``. ``None`` means the agent declares no
        ``os_env:`` block — the runtime skips registering
        ``sys_os_*`` tools and the ``claude-sdk`` harness wrap
        falls back to its enable-natives-by-default rule.
        Native Omnigent YAML accepts this as a top-level ``os_env:``
        mapping; the omnigent-compat translator copies
        ``AgentDef.os_env`` into this field directly. The
        ``"inherit"`` string sentinel from omnigent inline
        AgentTool sub-specs is resolved against the parent at
        translation time and is never carried as a string on
        ``AgentSpec``.
    :param terminals: Declared interactive tmux terminals
        the agent can launch via ``sys_terminal_launch``.
        Map of ``terminal_name`` → :class:`TerminalEnvSpec`
        (the inner-datamodel dataclass: ``command``, ``args``,
        ``env``, per-terminal ``os_env``, ``scrollback``, etc.).
        ``None`` means the agent declares no ``terminals:``
        block; ``sys_terminal_*`` tools are not registered.
        Populated by the omnigent-compat translator from
        ``AgentDef.terminals``. Native Omnigent YAML support is
        deferred — see ``designs/OMNIGENT_TERMINAL_BRIDGE.md`` §3.
    :param timers: Whether the LLM-callable timer builtins
        (``sys_timer_set``, ``sys_timer_cancel``) are
        registered. YAML key is ``timers:``. **Defaults to
        ``False``** to match the legacy inner-stack default at
        ``omnigent/inner/datamodel.py::AgentDef.timers`` —
        agents opt into the timer surface explicitly. Step 10
        of the harness contract migration adds the AP-side
        port; firings durable across server restarts via the
        the legacy background-timer workflow (the inner
        stack uses in-memory asyncio tasks; Omnigent supersedes that
        with a workflow per timer pinned to the timer_id). See
        ``designs/SERVER_HARNESS_CONTRACT.md`` §Timers and
        step 10.
    :param spawn: Whether the agent may spawn child sessions
        OUTSIDE any declared sub-agent list: registers
        ``sys_session_create`` (launch an existing agent by id or
        a custom locally-authored bundle via ``config_path``),
        along with ``sys_session_send`` / ``sys_session_close``
        to drive and tombstone those children. YAML key is
        ``spawn:`` (top-level boolean, like ``timers:``).
        **Defaults to ``False``.** This is a distinct grant from
        ``tools.agents``, which permits spawning ONLY the
        specified sub-agent types (named ``sys_session_send`` +
        ``sys_session_close``, no ``sys_session_create``). The
        session *read* tools (``sys_session_list`` /
        ``sys_session_get_history`` / ``sys_session_get_info``)
        are always registered and are not affected by either
        opt-in.
    """

    spec_version: int
    name: str | None = None
    description: str | None = None
    llm: LLMConfig | None = None
    interaction: InteractionConfig = field(default_factory=InteractionConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    # Arbitrary key-value params readable by skills and tools.
    # Values are heterogeneous (str, int, bool, list, dict), so Any
    # is the narrowest safe type here.
    params: dict[str, Any] = field(default_factory=dict)  # type: ignore[explicit-any]
    instructions: str | None = None  # contents of AGENTS.md
    skills: list[SkillSpec] = field(default_factory=list)
    # Filter for host-scope skill loading (skills the harness picks up
    # from the user's machine — ``~/.claude/skills/`` and any
    # ``.claude/skills/`` along the cwd ancestry — separate from
    # bundled skills declared above). Maps from the top-level YAML
    # ``skills:`` key. Three forms:
    #
    # - ``"all"`` (default, ``None`` is normalized to ``"all"`` at
    #   parse time): every host-scope skill is exposed to the model.
    # - ``"none"``: empty filter, no host-scope skills are exposed
    #   (the agent only sees its own bundled skills if any).
    # - ``list[str]``: only the named skills are exposed.
    #
    # The Claude SDK harness consumes this directly as its ``skills``
    # option (``"all"`` ↔ ``"all"``, ``"none"`` ↔ ``[]``,
    # list-of-names passes through). Other harnesses ignore the
    # field. Bundled skills (the ``skills`` field above) are loaded
    # via a separate plugin-dir mechanism and aren't subject to this
    # filter — agents that want to suppress bundled skills do so by
    # not shipping them, not by setting this filter to ``"none"``.
    skills_filter: str | list[str] = "all"
    mcp_servers: list[MCPServerConfig] = field(default_factory=list)
    local_tools: list[LocalToolInfo] = field(default_factory=list)
    sub_agents: list[AgentSpec] = field(default_factory=list)
    executor: ExecutorSpec = field(default_factory=ExecutorSpec)
    compaction: CompactionConfig | None = None
    guardrails: GuardrailsSpec | None = None
    async_enabled: bool = True
    os_env: OSEnvSpec | None = None
    terminals: dict[str, TerminalEnvSpec] | None = None
    timers: bool = False
    spawn: bool = False
