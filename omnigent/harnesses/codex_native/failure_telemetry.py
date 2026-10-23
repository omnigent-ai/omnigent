"""Bounded provider evidence from Codex's explicit terminal error payloads."""

from __future__ import annotations

import json
import re

from omnigent.native.failure_telemetry import FailureContext, normalize_failure_context

_FLAT_VARIANT_KEYS = ("type", "kind", "variant")
# codex-cli formats a rejection as ``unexpected status <code> <reason>: <body>, url: <url>``,
# with ``<body>`` the gateway's JSON envelope; the message starts with it, so anchoring
# at the start keeps rejections quoted inside other errors from matching.
_GATEWAY_REJECTION = re.compile(r"\Aunexpected status (?P<code>[1-5][0-9]{2})\b[^:\n]*:\s*")
_GATEWAY_MESSAGE_MAX_CHARS = 16384


def codex_error_info(info: object) -> tuple[str | None, int | None]:
    """Read both flat and externally tagged CodexErrorInfo enum representations."""
    variant = None
    status = None
    if isinstance(info, str):
        variant = info
    elif isinstance(info, dict):
        variant = next(
            (
                info[key]
                for key in _FLAT_VARIANT_KEYS
                if isinstance(info.get(key), str) and info[key]
            ),
            None,
        )
        status = info.get("httpStatusCode")
        if variant is None:
            # Externally tagged: ``{"<variant>": {"httpStatusCode": N}}``, tolerating
            # sibling metadata keys beside the tag. Prefer the nested dict carrying a
            # valid status, then one naming the key; a flat status stays otherwise.
            tagged = [
                (key, value)
                for key, value in info.items()
                if isinstance(key, str)
                and key not in _FLAT_VARIANT_KEYS
                and isinstance(value, dict)
            ]
            if tagged:
                key, value = min(
                    tagged,
                    key=lambda item: (
                        type(item[1].get("httpStatusCode")) is not int,
                        "httpStatusCode" not in item[1],
                    ),
                )
                variant = key
                if type(value.get("httpStatusCode")) is int:
                    status = value["httpStatusCode"]
    valid_status = status if type(status) is int and 100 <= status <= 599 else None
    return variant, valid_status


def _gateway_rejection(message: str) -> tuple[int | None, dict[str, object]]:
    """Return the HTTP status and JSON error envelope of a gateway rejection message."""
    text = message[:_GATEWAY_MESSAGE_MAX_CHARS]
    match = _GATEWAY_REJECTION.match(text)
    if match is None:
        return None, {}
    status = int(match["code"])
    body = text[match.end() :]
    if not body.startswith("{"):
        return status, {}
    try:
        decoded, _ = json.JSONDecoder().raw_decode(body)
    except (ValueError, RecursionError):
        return status, {}
    return status, decoded if isinstance(decoded, dict) else {}


def _first_present(*values: object) -> object:
    """First candidate usable as evidence: non-blank text or an int; the rest are fallbacks."""
    for value in values:
        if (isinstance(value, str) and value.strip()) or type(value) is int:
            return value
    return None


def codex_failure_context(payload: dict[str, object], *, message: str) -> FailureContext:
    """Keep error-body request IDs separate from Omnigent's event transport ID."""
    variant, status = codex_error_info(payload.get("codexErrorInfo"))
    gateway_status, envelope = _gateway_rejection(message)
    nested = envelope.get("error")
    error = nested if isinstance(nested, dict) else envelope
    details = payload.get("additionalDetails")
    text = message
    if isinstance(details, str) and details.strip():
        text = f"{message}\n{details.strip()}"
    # Fields reported directly on the payload win over the parsed gateway body.
    return normalize_failure_context(
        {
            "native_harness": "codex-native",
            "native_error_category": variant,
            "native_error_message": text,
            "http_status": _first_present(status, gateway_status),
            "provider_error_type": _first_present(
                payload.get("provider_error_type"), error.get("type")
            ),
            "provider_error_code": _first_present(
                payload.get("provider_error_code"), error.get("error_code"), error.get("code")
            ),
            "provider_error_param": _first_present(
                payload.get("provider_error_param"), error.get("param")
            ),
            "native_error_request_id": _first_present(
                payload.get("request_id"), envelope.get("request_id")
            ),
            "gateway_request_id": _first_present(
                payload.get("gateway_request_id"), envelope.get("gateway_request_id")
            ),
            "provider_request_id": _first_present(
                payload.get("provider_request_id"), envelope.get("provider_request_id")
            ),
        }
    )
