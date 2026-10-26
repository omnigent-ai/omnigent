"""Bounded evidence from Codex errors, without retaining provider command output."""

from __future__ import annotations

from omnigent.native.failure_telemetry import (
    FailureContext,
    normalize_failure_context,
    provider_error_context_from_text,
)


def codex_error_context(error: object, *, thread_id: object = None) -> FailureContext:
    """Keep typed error facts from current and older app-server wire shapes."""
    if not isinstance(error, dict):
        return {}
    info = error.get("codexErrorInfo")
    category: object = info if isinstance(info, str) else None
    status: object = None
    if isinstance(info, dict):
        category = info.get("type") or info.get("kind") or info.get("variant")
        status = info.get("httpStatusCode")
        # Current app-server enums use an externally tagged object.
        if category is None and len(info) == 1:
            key, value = next(iter(info.items()))
            if isinstance(value, dict):
                category = key
                status = value.get("httpStatusCode")
    context = provider_error_context_from_text(error.get("message"))
    context.update(provider_error_context_from_text(error.get("additionalDetails")))
    context.update(
        normalize_failure_context(
            {
                "native_error_category": category,
                "http_status": status,
                "native_session_id": thread_id,
            }
        )
    )
    return context


def codex_error_reason(message: str) -> str:
    """Use fixed reason labels; never copy commands, paths, or provider messages."""
    lowered = message[:4096].casefold()
    for fragment, reason in (
        ("application network permission was revoked", "network_permission_revoked"),
        ("application network policy is unavailable", "network_policy_unavailable"),
        ("failed to shutdown thread persistence", "thread_persistence_shutdown"),
        ("currently experiencing high demand", "provider_high_demand"),
    ):
        if fragment in lowered:
            return reason
    if "provider auth command" in lowered:
        if "produced an empty token" in lowered:
            return "provider_auth_empty_token"
        if "timed out after" in lowered:
            return "provider_auth_timeout"
        if "not found" in lowered or "no such file or directory" in lowered:
            return "provider_auth_executable_missing"
        return "provider_auth_failed"
    if "this endpoint " in lowered and " is deprecated" in lowered:
        return "provider_model_deprecated"
    return "unclassified"
