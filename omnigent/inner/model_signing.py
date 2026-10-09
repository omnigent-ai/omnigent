"""Fail-closed request reconstruction for the model signer."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .model_egress import FrozenModelRoute

_HEADER_NAME = re.compile(r"\A[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_REJECTED_HEADERS = frozenset(
    {
        "expect",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "x-http-method-override",
        "x-original-url",
        "x-rewrite-url",
    }
)
_HOP_BY_HOP_HEADERS = frozenset({"connection", "keep-alive"})


class SigningRejected(ValueError):
    """A request failed signer authorization or reconstruction."""


@dataclass(frozen=True)
class SignedModelRequest:
    """Reconstructed upstream request with credential-bearing fields redacted."""

    method: str
    host: str
    path: str
    body: bytes = field(repr=False)
    headers: tuple[tuple[str, str], ...] = field(repr=False)

    def header(self, name: str) -> str | None:
        """Return one reconstructed header value by case-insensitive name."""
        lowered = name.lower()
        for header_name, value in self.headers:
            if header_name.lower() == lowered:
                return value
        return None

    def __repr__(self) -> str:
        return (
            f"SignedModelRequest(method={self.method!r}, host={self.host!r}, "
            f"path={self.path!r}, body_bytes={len(self.body)})"
        )


def reconstruct_signed_request(
    *,
    route: FrozenModelRoute,
    placeholder: str,
    bearer_token: str,
    method: str,
    connect_host: str,
    sni_host: str,
    request_host: str,
    target: str,
    headers: Sequence[tuple[str, str]],
    body: bytes,
    max_body_bytes: int = 10 * 1024 * 1024,
) -> SignedModelRequest:
    """Authorize and rebuild one queryless JSON model request."""
    _validate_secret_value(placeholder, label="placeholder")
    _validate_secret_value(bearer_token, label="bearer token")
    if placeholder in target:
        raise SigningRejected("placeholder is not allowed in the request target")

    parsed_target = urlsplit(target)
    if (
        not target.startswith("/")
        or parsed_target.scheme
        or parsed_target.netloc
        or parsed_target.fragment
    ):
        raise SigningRejected("request target must be an origin-form path")
    if not route.matches(
        method=method,
        host=request_host,
        path=parsed_target.path,
        query=parsed_target.query,
    ):
        raise SigningRejected("request is outside the frozen model route")

    expected_host = route.host
    if any(
        candidate.lower() != expected_host for candidate in (connect_host, sni_host, request_host)
    ):
        raise SigningRejected("CONNECT, SNI, and inner request authority must match")

    normalized = _validate_headers(headers, placeholder)
    authorization = normalized.get("authorization", [])
    if authorization != [f"Bearer {placeholder}"]:
        raise SigningRejected("exactly one Bearer session placeholder is required")

    content_types = normalized.get("content-type", [])
    if content_types != ["application/json"]:
        raise SigningRejected("Content-Type must be exactly application/json")
    if "content-encoding" in normalized:
        raise SigningRejected("compressed request bodies are not supported")
    if len(normalized.get("content-length", [])) > 1:
        raise SigningRejected("duplicate Content-Length is not allowed")

    if max_body_bytes < 1 or len(body) > max_body_bytes:
        raise SigningRejected("request body exceeds the configured limit")
    _validate_json_body(body)

    rebuilt: list[tuple[str, str]] = [
        ("Host", expected_host),
        ("Authorization", f"Bearer {bearer_token}"),
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
    ]
    accept = normalized.get("accept", [])
    if len(accept) > 1:
        raise SigningRejected("duplicate Accept is not allowed")
    if accept:
        rebuilt.append(("Accept", accept[0]))

    return SignedModelRequest(
        method=route.method,
        host=expected_host,
        path=route.path,
        headers=tuple(rebuilt),
        body=body,
    )


def _validate_headers(
    headers: Sequence[tuple[str, str]],
    placeholder: str,
) -> dict[str, list[str]]:
    normalized: dict[str, list[str]] = {}
    for name, value in headers:
        if _HEADER_NAME.fullmatch(name) is None:
            raise SigningRejected("request contains an invalid header name")
        if any(char in value for char in ("\r", "\n", "\x00")):
            raise SigningRejected("request contains an invalid header value")
        lowered = name.lower()
        if lowered in _REJECTED_HEADERS:
            raise SigningRejected(f"header {name!r} is not supported")
        if lowered == "forwarded" or lowered.startswith(("x-forwarded-", "proxy-")):
            raise SigningRejected(f"routing header {name!r} is not allowed")
        if lowered == "connection" and "upgrade" in value.lower():
            raise SigningRejected("protocol upgrades are not supported")
        if lowered != "authorization" and placeholder in value:
            raise SigningRejected("placeholder is not allowed in forwarded headers")
        if lowered not in _HOP_BY_HOP_HEADERS:
            normalized.setdefault(lowered, []).append(value)
    return normalized


def _validate_secret_value(value: str, *, label: str) -> None:
    if not value or any(char.isspace() or ord(char) < 0x20 for char in value):
        raise SigningRejected(f"{label} is malformed")


def _validate_json_body(body: bytes) -> None:
    try:
        text = body.decode("utf-8")
        json.loads(
            text,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SigningRejected("request body must be one valid JSON document") from exc
