"""
Provider-neutral prompt-cache policy, capability, and observation types.

Prompt caching here is a semantics-preserving optimization: Omnigent never
stores prompts or cache handles itself. It only decides whether a request may
carry provider-supported cache hints and then normalizes what the provider
reports about cache reads and writes.

Two boundaries exist:

* **Controllable** — direct-provider calls made by :mod:`omnigent.llms`.
  The Anthropic adapter adds explicit ``cache_control`` breakpoints to the
  stable prefix (tools, system). The OpenAI Responses adapter keeps automatic
  common-prefix caching and adds a ``prompt_cache_key`` routing hint.
* **Observable only** — vendor CLIs (Claude Code, Codex) own their request
  payloads. Omnigent keeps its own inputs deterministic and only normalizes
  the cache token counts those harnesses already report.

Caching is off unless enabled per call (``prompt_cache=``) or process-wide via
``OMNIGENT_PROMPT_CACHE=opportunistic``. When off, provider payloads are
byte-identical to the uncached request. Observations never carry prompt text,
cache keys, or digests, so they are safe to log and export as telemetry.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from omnigent.llms.types import Usage

_logger = logging.getLogger(__name__)

PROMPT_CACHE_ENV_VAR = "OMNIGENT_PROMPT_CACHE"

# Anthropic's default (5 minute) ephemeral cache breakpoint.
ANTHROPIC_CACHE_CONTROL: dict[str, str] = {"type": "ephemeral"}

_OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_OPENAI_CACHE_KEY_PREFIX = "omnigent-"
# Leaves the key well under OpenAI's prompt_cache_key length limit.
_OPENAI_CACHE_KEY_HEX_CHARS = 32


class PromptCacheMode(str, Enum):
    """Whether a request may carry provider cache hints."""

    DISABLED = "disabled"
    OPPORTUNISTIC = "opportunistic"


class PromptCacheMechanism(str, Enum):
    """How the provider caches the prompt prefix for a request."""

    NONE = "none"
    EXPLICIT_BREAKPOINTS = "explicit_breakpoints"  # Anthropic cache_control
    AUTOMATIC_PREFIX = "automatic_prefix"  # OpenAI automatic prefix caching
    VENDOR_MANAGED = "vendor_managed"  # hidden inside a vendor CLI


class PromptCacheOutcome(str, Enum):
    """What the provider's usage report says happened to the prefix cache."""

    HIT = "hit"
    WRITE = "write"
    MISS = "miss"
    BYPASS = "bypass"
    UNKNOWN = "unknown"


class PromptCacheReason(str, Enum):
    """Closed set of non-sensitive explanations attached to an observation."""

    APPLIED = "applied"
    DISABLED = "disabled"
    UNSUPPORTED_PROVIDER = "unsupported_provider"
    CUSTOM_ENDPOINT = "custom_endpoint"
    PROVIDER_REJECTED = "provider_rejected"
    VENDOR_MANAGED = "vendor_managed"
    NO_USAGE = "no_usage"


@dataclass(frozen=True)
class PromptCachePolicy:
    """
    Caller intent for prompt caching.

    :param mode: ``DISABLED`` sends the uncached payload unchanged;
        ``OPPORTUNISTIC`` adds provider-supported hints and never fails the
        request when the provider does not accept them.
    """

    mode: PromptCacheMode = PromptCacheMode.DISABLED

    @property
    def enabled(self) -> bool:
        """Whether cache hints may be added to requests."""
        return self.mode is not PromptCacheMode.DISABLED


@dataclass(frozen=True)
class PromptCacheCapability:
    """
    What Omnigent can do about caching on one request path.

    :param mechanism: The provider-side caching mechanism.
    :param observable: Whether cache read/write token counts are reported.
    :param controllable: Whether Omnigent can add cache hints to the payload.
    """

    mechanism: PromptCacheMechanism
    observable: bool
    controllable: bool


EXPLICIT_BREAKPOINT_CAPABILITY = PromptCacheCapability(
    mechanism=PromptCacheMechanism.EXPLICIT_BREAKPOINTS, observable=True, controllable=True
)
AUTOMATIC_PREFIX_CAPABILITY = PromptCacheCapability(
    mechanism=PromptCacheMechanism.AUTOMATIC_PREFIX, observable=True, controllable=True
)
AUTOMATIC_PREFIX_OBSERVE_ONLY_CAPABILITY = PromptCacheCapability(
    mechanism=PromptCacheMechanism.AUTOMATIC_PREFIX, observable=True, controllable=False
)
NATIVE_HARNESS_CAPABILITY = PromptCacheCapability(
    mechanism=PromptCacheMechanism.VENDOR_MANAGED, observable=True, controllable=False
)
UNSUPPORTED_CAPABILITY = PromptCacheCapability(
    mechanism=PromptCacheMechanism.NONE, observable=False, controllable=False
)


@dataclass
class PromptCachePlan:
    """
    Per-request cache decision shared by the client and one adapter.

    Mutable only so an adapter can record that the provider rejected the
    hints and the request was retried without them.

    :param capability: What the request path supports.
    :param policy: The resolved caller policy.
    :param reason: Why hints are or are not applied.
    :param rejected: Set when the provider refused the hints.
    """

    capability: PromptCacheCapability
    policy: PromptCachePolicy
    reason: PromptCacheReason
    rejected: bool = False

    @property
    def apply(self) -> bool:
        """Whether the adapter should add cache hints to the payload."""
        return self.policy.enabled and self.capability.controllable and not self.rejected

    def mark_rejected(self) -> None:
        """Record that the provider rejected the hints (fail-open retry)."""
        self.rejected = True
        self.reason = PromptCacheReason.PROVIDER_REJECTED


@dataclass(frozen=True)
class PromptCacheObservation:
    """
    Normalized, non-sensitive cache telemetry for one model request or turn.

    :param mechanism: Effective provider caching mechanism.
    :param outcome: Hit / write / miss / bypass, or unknown without usage.
    :param controllable: Whether Omnigent could steer caching on this path.
    :param reason: Closed-set explanation; never prompt-derived.
    :param cache_read_input_tokens: Prompt tokens served from cache.
    :param cache_creation_input_tokens: Prompt tokens written to cache.
    """

    mechanism: PromptCacheMechanism
    outcome: PromptCacheOutcome
    controllable: bool
    reason: PromptCacheReason
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None

    def to_attributes(self) -> dict[str, str | int | bool]:
        """Flatten into telemetry attributes (``omnigent.prompt_cache.*``)."""
        attrs: dict[str, str | int | bool] = {
            "omnigent.prompt_cache.mechanism": self.mechanism.value,
            "omnigent.prompt_cache.outcome": self.outcome.value,
            "omnigent.prompt_cache.controllable": self.controllable,
            "omnigent.prompt_cache.reason": self.reason.value,
        }
        if self.cache_read_input_tokens is not None:
            attrs["omnigent.prompt_cache.read_tokens"] = self.cache_read_input_tokens
        if self.cache_creation_input_tokens is not None:
            attrs["omnigent.prompt_cache.write_tokens"] = self.cache_creation_input_tokens
        return attrs


def resolve_prompt_cache_policy(
    value: PromptCachePolicy | PromptCacheMode | str | None,
) -> PromptCachePolicy:
    """
    Resolve a caller-supplied policy, falling back to the environment.

    Unknown values resolve to ``DISABLED`` with a warning so a typo can never
    fail a request.

    :param value: A policy, mode, mode string, or ``None`` to read
        ``OMNIGENT_PROMPT_CACHE``.
    :returns: The resolved policy.
    """
    if isinstance(value, PromptCachePolicy):
        return value
    if isinstance(value, PromptCacheMode):
        return PromptCachePolicy(mode=value)
    raw = value if value is not None else os.environ.get(PROMPT_CACHE_ENV_VAR)
    if raw is None or not raw.strip():
        return PromptCachePolicy()
    try:
        return PromptCachePolicy(mode=PromptCacheMode(raw.strip().lower()))
    except ValueError:
        _logger.warning("Ignoring unknown prompt cache mode; prompt caching stays disabled")
        return PromptCachePolicy()


def plan_prompt_cache(
    provider: str,
    policy: PromptCachePolicy,
    *,
    base_url_override: str | None = None,
) -> PromptCachePlan:
    """
    Decide how one direct-provider request may use prompt caching.

    :param provider: Routed provider name, e.g. ``"anthropic"``.
    :param policy: The resolved caller policy.
    :param base_url_override: Per-call ``connection_params["base_url"]``.
    :returns: The plan the client passes to the adapter.
    """
    if provider == "anthropic":
        capability = EXPLICIT_BREAKPOINT_CAPABILITY
        reason = PromptCacheReason.APPLIED
    elif provider == "openai":
        custom = base_url_override is not None and (
            base_url_override.rstrip("/") != _OPENAI_DEFAULT_BASE_URL
        )
        if custom:
            # Gateways that proxy the Responses API may reject unknown fields.
            capability = AUTOMATIC_PREFIX_OBSERVE_ONLY_CAPABILITY
            reason = PromptCacheReason.CUSTOM_ENDPOINT
        else:
            capability = AUTOMATIC_PREFIX_CAPABILITY
            reason = PromptCacheReason.APPLIED
    else:
        capability = UNSUPPORTED_CAPABILITY
        reason = PromptCacheReason.UNSUPPORTED_PROVIDER
    if not policy.enabled and capability.controllable:
        reason = PromptCacheReason.DISABLED
    return PromptCachePlan(capability=capability, policy=policy, reason=reason)


def stable_prefix_digest(
    instructions: str | Sequence[Any] | None,
    tools: Sequence[Mapping[str, Any]] | None,
) -> str:
    """
    Digest the stable request prefix: tool schemas then instructions.

    Conversation input is deliberately excluded so only stable content moves
    the digest. The value is derived from prompt content and must never be
    logged or exported.

    :param instructions: System instructions (string or content blocks).
    :param tools: Tool schemas in request order.
    :returns: A hex SHA-256 digest.
    """
    canonical = json.dumps(
        {"tools": list(tools or []), "instructions": instructions},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def openai_prompt_cache_key(
    instructions: str | None,
    tools: Sequence[Mapping[str, Any]] | None,
) -> str:
    """
    Build an OpenAI ``prompt_cache_key`` routing hint for a stable prefix.

    :param instructions: Responses API ``instructions``.
    :param tools: Responses API ``tools`` in request order.
    :returns: An opaque key; never log it.
    """
    digest = stable_prefix_digest(instructions, tools)
    return f"{_OPENAI_CACHE_KEY_PREFIX}{digest[:_OPENAI_CACHE_KEY_HEX_CHARS]}"


def apply_anthropic_cache_control(payload: Mapping[str, Any]) -> dict[str, Any]:
    """
    Return a copy of an Anthropic Messages payload with stable-prefix breakpoints.

    Anthropic caches in ``tools -> system -> messages`` order, so a breakpoint
    on the last tool caches the tool schemas and one on the system prompt
    caches tools plus system. Messages (the dynamic suffix) are left untouched
    and reuse that prefix. The input payload is not mutated.

    :param payload: The uncached Anthropic request payload.
    :returns: A new payload with ``cache_control`` on eligible stable blocks.
    """
    cached = dict(payload)
    tools = payload.get("tools")
    if isinstance(tools, list) and tools and isinstance(tools[-1], dict):
        cached["tools"] = [*tools[:-1], {**tools[-1], "cache_control": ANTHROPIC_CACHE_CONTROL}]
    system = payload.get("system")
    if isinstance(system, str) and system:
        cached["system"] = [
            {"type": "text", "text": system, "cache_control": ANTHROPIC_CACHE_CONTROL}
        ]
    return cached


def is_cache_hint_rejection(status_code: int, body: str) -> bool:
    """
    Whether a provider error is a refusal of the cache hints themselves.

    :param status_code: HTTP status of the failed request.
    :param body: Provider error body (inspected only, never logged here).
    :returns: ``True`` for a 400 that names a cache field.
    """
    return status_code == 400 and ("cache_control" in body or "prompt_cache" in body)


def _token_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(int(value), 0)


def _outcome(
    read: int | None,
    write: int | None,
    *,
    caching_active: bool,
) -> PromptCacheOutcome:
    if read is None and write is None:
        return PromptCacheOutcome.UNKNOWN
    if read:
        return PromptCacheOutcome.HIT
    if write:
        return PromptCacheOutcome.WRITE
    return PromptCacheOutcome.MISS if caching_active else PromptCacheOutcome.BYPASS


def observe_response_usage(plan: PromptCachePlan, usage: Usage | None) -> PromptCacheObservation:
    """
    Normalize a direct-provider response's cache usage against its plan.

    :param plan: The plan used for the request.
    :param usage: The response usage, or ``None``.
    :returns: A non-sensitive observation.
    """
    capability = plan.capability
    if not capability.observable:
        return PromptCacheObservation(
            mechanism=capability.mechanism,
            outcome=PromptCacheOutcome.BYPASS,
            controllable=False,
            reason=plan.reason,
        )
    if usage is None:
        return PromptCacheObservation(
            mechanism=capability.mechanism,
            outcome=PromptCacheOutcome.UNKNOWN,
            controllable=capability.controllable,
            reason=PromptCacheReason.NO_USAGE,
        )
    read = _token_count(usage.cache_read_input_tokens)
    write = _token_count(usage.cache_creation_input_tokens)
    if read is None and write is None:
        # Usage without cache counters means nothing was read from cache.
        read = 0
    # OpenAI caches common prefixes automatically even without hints.
    caching_active = not plan.rejected and (
        plan.apply or capability.mechanism is PromptCacheMechanism.AUTOMATIC_PREFIX
    )
    return PromptCacheObservation(
        mechanism=capability.mechanism,
        outcome=_outcome(read, write, caching_active=caching_active),
        controllable=capability.controllable,
        reason=plan.reason,
        cache_read_input_tokens=read,
        cache_creation_input_tokens=write,
    )


def observe_harness_usage(
    usage: Mapping[str, object] | None,
    capability: PromptCacheCapability = NATIVE_HARNESS_CAPABILITY,
) -> PromptCacheObservation:
    """
    Normalize a vendor harness's reported usage into a cache observation.

    Accepts the executor/native usage shape (``cache_read_input_tokens``,
    ``cache_creation_input_tokens``) and the native session-usage cumulative
    spelling (``cumulative_cache_read_input_tokens``).

    :param usage: Usage dict reported by the harness, or ``None``.
    :param capability: The harness's cache capability.
    :returns: A non-sensitive observation.
    """
    if usage is None:
        return PromptCacheObservation(
            mechanism=capability.mechanism,
            outcome=PromptCacheOutcome.UNKNOWN,
            controllable=capability.controllable,
            reason=PromptCacheReason.NO_USAGE,
        )
    read = _token_count(
        usage.get("cache_read_input_tokens", usage.get("cumulative_cache_read_input_tokens"))
    )
    write = _token_count(
        usage.get(
            "cache_creation_input_tokens", usage.get("cumulative_cache_creation_input_tokens")
        )
    )
    # Vendors omit cache counters when nothing was cached.
    if (
        read is None
        and write is None
        and any(key in usage for key in ("input_tokens", "cumulative_input_tokens"))
    ):
        read = 0
    return PromptCacheObservation(
        mechanism=capability.mechanism,
        outcome=_outcome(read, write, caching_active=True),
        controllable=capability.controllable,
        reason=PromptCacheReason.VENDOR_MANAGED,
        cache_read_input_tokens=read,
        cache_creation_input_tokens=write,
    )
