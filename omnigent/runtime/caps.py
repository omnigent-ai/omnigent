"""Runtime caps — operator-configured hard ceilings."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from omnigent.spec.types import LLMConfig, PolicySpec


@dataclass
class RuntimeCaps:
    """
    Operator-configured runtime policies for agent execution.

    These are deployment/security decisions that agents cannot
    override. Agent specs are clamped to these limits.

    :param execution_timeout: Max wall-clock time for the entire
        agent loop in seconds, e.g. ``7200``.
    :param sandbox_enabled: Whether to use ``srt`` sandboxing for
        local tool execution when available on PATH. ``True`` by
        default. This is a runtime security policy — agents cannot
        opt out. The agent spec controls ``container_image``
        (what container to use) and ``container_runtime`` (docker
        or podman). Note: ``container_runtime`` determines which
        binary is invoked via subprocess — it is validated to a
        fixed allowlist (``"docker"`` | ``"podman"``) at both
        the dataclass and parser layers.
    :param default_policies: Server-wide policies appended after
        per-agent policies on every session. Loaded from the
        ``policies:`` key in the server ``--config`` YAML
        at startup. ``[]`` means no server-wide policies (the
        default — no behaviour change when the key is absent).
    :param llm: Server-level LLM configuration for policy
        functions. Parsed from the ``llm:`` key in the server
        ``--config`` YAML at startup. When present, a
        :class:`~omnigent.policies.types.PolicyLLMClient`
        is built from this config and injected into every
        function policy's ``event["llm_client"]``.
        ``None`` when the key is absent — function policies
        see ``None`` in ``event["llm_client"]``.
    :param policy_llm_connection_factory: Optional callable invoked
        at engine-build time (i.e. per request) to supply the
        ``{"base_url", "api_key"}`` connection dict for the
        :class:`~omnigent.policies.types.PolicyLLMClient`. When
        provided its result takes precedence over any connection
        resolved from ``llm.connection`` / ``llm.profile``, so the
        LLM call is billed to the request caller rather than a
        static service-level credential. ``None`` falls back to the
        ``llm``-config-resolved connection.
    """

    execution_timeout: int = 7200
    sandbox_enabled: bool = True
    # Populated from ``policies:`` in the server --config YAML.
    # Stored as a list so the builder can append it without importing
    # the full GuardrailsSpec type at caps construction time.
    default_policies: list[PolicySpec] = field(default_factory=list)
    # Populated from ``llm:`` in the server --config YAML.
    # Used by the policy engine builder to construct a shared
    # PolicyLLMClient for function policy callables.
    llm: LLMConfig | None = None
    # Per-request connection resolver for the PolicyLLMClient.
    # Registered by the host application (e.g. omnigents_app.py) to
    # propagate the caller's auth token instead of using static
    # server-level credentials.
    policy_llm_connection_factory: Callable[[], dict[str, str] | None] | None = None
