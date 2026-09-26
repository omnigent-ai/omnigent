"""Credential redaction at the opt-in harness diagnostic boundary."""

from __future__ import annotations

import time

import pytest

from omnigent.harnesses.diagnostics import (
    bounded_diagnostic_tail,
    detect_sign_in_prompt,
    sanitize_diagnostic_text,
    sign_in_next_step,
)


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


@pytest.mark.parametrize(
    ("screen", "expected_url", "expected_code"),
    [
        (
            "dbexec: launcher 1.2.3\nSign in to continue:\n"
            "  https://signin.example.com/device\n  code: HQ7M-2KPD\nwaiting for sign-in...",
            "https://signin.example.com/device",
            "HQ7M-2KPD",
        ),
        (
            "Visit https://login.example.com/activate?user_code=ABCD1234 "
            "and enter the code ABCD1234.",
            "https://login.example.com/activate?user_code=ABCD1234",
            "ABCD1234",
        ),
        # A numeric-only token is not a device code; the address alone is still useful.
        ("Enter code 123456 at https://x.example/verify.", "https://x.example/verify", None),
    ],
)
def test_detect_sign_in_prompt_lifts_url_and_code(
    screen: str, expected_url: str, expected_code: str | None
) -> None:
    prompt = detect_sign_in_prompt(screen)
    assert prompt is not None
    assert prompt.url == expected_url
    assert prompt.code == expected_code


@pytest.mark.parametrize("screen", [None, "", "Starting MCP servers: omnigent", "code: HQ7M-2KPD"])
def test_detect_sign_in_prompt_requires_an_address(screen: str | None) -> None:
    """A code without a link gives the user nothing to open, so it is not a prompt."""
    assert detect_sign_in_prompt(screen) is None


@pytest.mark.parametrize(
    "screen",
    [
        # An agent banner quoting its instructions, with a docs pointer.
        "│ Logfood ingests eng data. Look them up at https://go/eng-data-access.\n"
        "└ SessionStart says: Open this session in Omnigent: http://127.0.0.1:8931/c/abc\n",
        # Ordinary tool output.
        "Opened https://github.com/omnigent-ai/omnigent/pull/3792) for review.\n",
        "Tracked in https://linear.app/omnigent/issues/OMNI-1006\n",
    ],
)
def test_detect_sign_in_prompt_ignores_ordinary_addresses(screen: str) -> None:
    """A running agent's screen is full of links; none of them is a sign-in gate."""
    assert detect_sign_in_prompt(screen) is None


def test_detect_sign_in_prompt_accepts_a_device_flow_by_its_instructions() -> None:
    """A device flow is recognised by its wording or its auth-shaped address."""
    prompt = detect_sign_in_prompt(
        "! First copy your one-time code: 1A2B-3C4D\n"
        "Press Enter to open https://github.com/login/device in your browser...\n"
    )
    assert prompt is not None
    assert prompt.url == "https://github.com/login/device"
    assert prompt.code == "1A2B-3C4D"


def test_sign_in_next_step_names_the_agent_and_carries_no_address() -> None:
    """The next step never embeds the one-time link; the card fetches it live."""
    step = sign_in_next_step("Codex")
    assert step == (
        "Open the sign-in link and sign in. Codex continues on its own once the "
        "sign-in completes; then send your message again."
    )
    assert "http" not in step
