"""Credential redaction at the opt-in harness diagnostic boundary."""

from __future__ import annotations

import time

import pytest

from omnigent.harnesses.diagnostics import bounded_diagnostic_tail, sanitize_diagnostic_text


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://probe-user:probe-pass@localhost:8080/v1", "http://[REDACTED]@localhost:8080/v1"),
        ("https://probe-user@example.invalid/v1", "https://[REDACTED]@example.invalid/v1"),
        ("https://:probe-pass@example.invalid/v1", "https://[REDACTED]@example.invalid/v1"),
        (
            "HTTPS://probe%40user:probe%3Apass@[::1]:8080/v1",
            "HTTPS://[REDACTED]@[::1]:8080/v1",
        ),
        ("wss://probe-user:probe-pass@example.invalid/v1", "wss://[REDACTED]@example.invalid/v1"),
    ],
)
def test_diagnostic_urls_redact_userinfo(url: str, expected: str) -> None:
    raw = f'url={url} status=400 request_id="synthetic-request-id"'
    sanitized = sanitize_diagnostic_text(raw)
    assert sanitized == f'url={expected} status=400 request_id="synthetic-request-id"'
    assert sanitize_diagnostic_text(sanitized) == sanitized


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080/v1/responses",
        "https://example.invalid/users/name@example.invalid",
        "https://example.invalid/v1?account=name@example.invalid",
    ],
)
def test_diagnostic_urls_preserve_noncredential_context(url: str) -> None:
    assert sanitize_diagnostic_text(f"url={url}") == f"url={url}"


@pytest.mark.parametrize("separator", [".", "-", "+"])
def test_long_non_url_identifiers_have_linear_redaction_cost(separator: str) -> None:
    text = ("a" + separator) * 32000
    started = time.perf_counter()
    assert sanitize_diagnostic_text(text) == text
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            'headers={"set-cookie": "session=probe-value; Path=/; HttpOnly", '
            '"x-request-id": "synthetic-request-id"}',
            'headers={"set-cookie": "[REDACTED]", "x-request-id": "synthetic-request-id"}',
        ),
        (
            'headers={"Cookie": "session=probe-value; other=probe-other"}',
            'headers={"Cookie": "[REDACTED]"}',
        ),
        (
            "headers={'SeT-CoOkIe': 'session=probe-value; Path=/'}",
            "headers={'SeT-CoOkIe': '[REDACTED]'}",
        ),
        (
            "Set-Cookie: session=probe-value; Path=/\nX-Request-Id: synthetic-request-id",
            "Set-Cookie: [REDACTED]\nX-Request-Id: synthetic-request-id",
        ),
        ("Cookie=session=probe-value; other=probe-other", "Cookie=[REDACTED]"),
        (
            'headers={"set-cookie": "unterminated-probe-value',
            'headers={"set-cookie": "[REDACTED]"',
        ),
        (
            'headers={"set-cookie": "first=probe-one", "set-cookie": "second=probe-two"}',
            'headers={"set-cookie": "[REDACTED]", "set-cookie": "[REDACTED]"}',
        ),
    ],
)
def test_diagnostic_headers_redact_complete_cookie_values(raw: str, expected: str) -> None:
    sanitized = sanitize_diagnostic_text(raw)
    assert sanitized == expected
    assert sanitize_diagnostic_text(sanitized) == sanitized


@pytest.mark.parametrize("backslashes", [1, 2, 3, 4])
def test_cookie_header_escaped_quotes_do_not_expose_suffixes(backslashes: int) -> None:
    # Rust HeaderValue debug escapes quotes but can retain preceding backslashes.
    escaped_quote = "\\" * backslashes + '"'
    raw = (
        'headers={"set-cookie": "session=probe-prefix'
        + escaped_quote
        + 'probe-suffix; Path=/", "x-request-id": "synthetic-request-id"}'
    )
    assert sanitize_diagnostic_text(raw) == (
        'headers={"set-cookie": "[REDACTED]", "x-request-id": "synthetic-request-id"}'
    )


@pytest.mark.parametrize("backslashes", [1, 2, 3, 4])
def test_cookie_trailing_backslashes_do_not_hide_later_cookies(backslashes: int) -> None:
    raw = (
        'headers={"set-cookie": "session=probe-first'
        + "\\" * backslashes
        + '", "x-request-id": "synthetic-request-id", "set-cookie": "other=probe-second"}'
    )
    sanitized = sanitize_diagnostic_text(raw)
    assert "probe-first" not in sanitized
    assert "probe-second" not in sanitized
    assert "synthetic-request-id" in sanitized
    assert sanitized.count("[REDACTED]") == 2
    assert sanitize_diagnostic_text(sanitized) == sanitized


@pytest.mark.parametrize("target", ["codex_client::default_client", "codex_http_client::client"])
def test_actual_codex_http_record_redacts_credentials_but_preserves_context(target: str) -> None:
    # Shape captured from both real binaries using a loopback-only synthetic provider.
    raw = (
        "DEBUG model_client.stream_responses_api{model=diagnostics-offline-model "
        'http.method="POST" api.path="/responses"}: ' + target + ": Request completed method=POST "
        "url=http://synthetic-url-user:synthetic-url-pass@127.0.0.1:8080/v1/responses "
        'status=400 Bad Request headers={"x-request-id": "synthetic-request-id", '
        '"set-cookie": "diag_session=synthetic-cookie-one; Path=/; HttpOnly", '
        r'"set-cookie": "diag_quoted=\"synthetic-cookie-prefix\\"'
        r'synthetic-cookie-suffix\"; Path=/"}'
        " version=HTTP/1.1 duration=12ms"
    )
    sanitized = sanitize_diagnostic_text(raw)
    for canary in (
        "synthetic-url-user",
        "synthetic-url-pass",
        "synthetic-cookie-one",
        "synthetic-cookie-prefix",
        "synthetic-cookie-suffix",
    ):
        assert canary not in sanitized
    for context in (
        target,
        "127.0.0.1:8080/v1/responses",
        "method=POST",
        "status=400",
        "synthetic-request-id",
        "duration=12ms",
    ):
        assert context in sanitized


def test_cookie_redaction_precedes_diagnostic_tail_clipping() -> None:
    raw = 'headers={"set-cookie": "' + "probe-cookie-value; " * 10000 + '"}'
    snapshot = bounded_diagnostic_tail([raw])
    assert snapshot["tail"] == 'headers={"set-cookie": "[REDACTED]"}'
    assert snapshot["truncated"] is False
