"""Request reconstruction tests for the model signer."""

from __future__ import annotations

import pytest

from omnigent.inner.model_egress import FrozenModelRoute
from omnigent.inner.model_signing import SigningRejected, reconstruct_signed_request

_HOST = "workspace.cloud.databricks.com"
_PATH = "/serving-endpoints/openai/responses"
_PLACEHOLDER = "oa_cred_session-placeholder"
_TOKEN = "real-token-only-the-signer-holds"
_ROUTE = FrozenModelRoute(method="POST", host=_HOST, path=_PATH)


def _reconstruct(
    *,
    method: str = "POST",
    connect_host: str = _HOST,
    sni_host: str = _HOST,
    request_host: str = _HOST,
    target: str = _PATH,
    headers: list[tuple[str, str]] | None = None,
    body: bytes = b'{"model":"test"}',
):
    return reconstruct_signed_request(
        route=_ROUTE,
        placeholder=_PLACEHOLDER,
        bearer_token=_TOKEN,
        method=method,
        connect_host=connect_host,
        sni_host=sni_host,
        request_host=request_host,
        target=target,
        headers=headers
        or [
            ("Authorization", f"Bearer {_PLACEHOLDER}"),
            ("Content-Type", "application/json"),
        ],
        body=body,
    )


def test_reconstructs_authority_and_bearer_from_frozen_inputs() -> None:
    request = _reconstruct(
        headers=[
            ("Authorization", f"Bearer {_PLACEHOLDER}"),
            ("Content-Type", "application/json"),
            ("Content-Length", "999"),
            ("Connection", "keep-alive"),
        ]
    )

    assert request.method == "POST"
    assert request.host == _HOST
    assert request.path == _PATH
    assert request.header("Authorization") == f"Bearer {_TOKEN}"
    assert request.header("Content-Length") == str(len(request.body))
    assert request.header("Connection") is None
    assert _PLACEHOLDER not in repr(request)
    assert _TOKEN not in repr(request)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("connect_host", "attacker.example"),
        ("sni_host", "attacker.example"),
        ("request_host", "attacker.example"),
        ("target", "/api/2.0/secrets/list"),
        ("target", f"{_PATH}?debug=true"),
        ("method", "GET"),
    ],
)
def test_authority_mismatch_is_rejected(field: str, value: str) -> None:
    with pytest.raises(SigningRejected):
        _reconstruct(**{field: value})


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        f"bearer {_PLACEHOLDER}",
        f"Bearer  {_PLACEHOLDER}",
        f"Basic {_PLACEHOLDER}",
        "Bearer foreign-token",
        f"Bearer {_PLACEHOLDER}, Bearer other",
    ],
)
def test_only_exact_placeholder_authorization_is_accepted(
    authorization: str | None,
) -> None:
    headers = [("Content-Type", "application/json")]
    if authorization is not None:
        headers.append(("Authorization", authorization))

    with pytest.raises(SigningRejected):
        _reconstruct(headers=headers)


def test_duplicate_authorization_is_rejected() -> None:
    with pytest.raises(SigningRejected):
        _reconstruct(
            headers=[
                ("Authorization", f"Bearer {_PLACEHOLDER}"),
                ("Authorization", f"Bearer {_PLACEHOLDER}"),
                ("Content-Type", "application/json"),
            ]
        )


@pytest.mark.parametrize(
    "header",
    [
        ("Upgrade", "websocket"),
        ("Expect", "100-continue"),
        ("Transfer-Encoding", "chunked"),
        ("Proxy-Authorization", "Basic anything"),
        ("X-Forwarded-Host", "attacker.example"),
        ("X-HTTP-Method-Override", "GET"),
    ],
)
def test_ambiguous_routing_and_framing_headers_are_rejected(
    header: tuple[str, str],
) -> None:
    with pytest.raises(SigningRejected):
        _reconstruct(
            headers=[
                ("Authorization", f"Bearer {_PLACEHOLDER}"),
                ("Content-Type", "application/json"),
                header,
            ]
        )


def test_placeholder_literal_in_other_header_or_target_is_rejected() -> None:
    with pytest.raises(SigningRejected):
        _reconstruct(
            headers=[
                ("Authorization", f"Bearer {_PLACEHOLDER}"),
                ("Content-Type", "application/json"),
                ("User-Agent", _PLACEHOLDER),
            ]
        )
    with pytest.raises(SigningRejected):
        _reconstruct(target=f"{_PATH}/{_PLACEHOLDER}")


def test_placeholder_in_json_body_does_not_expand_authority() -> None:
    request = _reconstruct(body=f'{{"input":"{_PLACEHOLDER}"}}'.encode())

    assert request.host == _HOST
    assert request.path == _PATH
    assert request.header("Authorization") == f"Bearer {_TOKEN}"


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b'{"unterminated":',
        b'{"valid":true} trailing',
        b"\xff",
    ],
)
def test_body_must_be_one_bounded_json_document(body: bytes) -> None:
    with pytest.raises(SigningRejected):
        _reconstruct(body=body)


def test_body_size_is_checked_before_signing() -> None:
    with pytest.raises(SigningRejected, match="body exceeds"):
        reconstruct_signed_request(
            route=_ROUTE,
            placeholder=_PLACEHOLDER,
            bearer_token=_TOKEN,
            method="POST",
            connect_host=_HOST,
            sni_host=_HOST,
            request_host=_HOST,
            target=_PATH,
            headers=[
                ("Authorization", f"Bearer {_PLACEHOLDER}"),
                ("Content-Type", "application/json"),
            ],
            body=b'{"payload":"too large"}',
            max_body_bytes=8,
        )
