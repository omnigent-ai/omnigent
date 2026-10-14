"""Credential redaction at the opt-in harness diagnostic boundary."""

from __future__ import annotations

import time

import pytest

from omnigent.harnesses.diagnostics import (
    bounded_diagnostic_tail,
    detect_launcher_question,
    detect_sign_in_prompt,
    may_end_with_answer_cue,
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
        # Keys echoed into the pane while the launcher waits are not its output.
        (
            "dbcert: If the browser does not open automatically, please open the following URL:\n"
            "\n\thttps://databricks.okta.com/oauth2/v1/authorize?client_id=0oa1&state=T4IU\n\n"
            "^[^[^[^[\n^[[A\n",
            "https://databricks.okta.com/oauth2/v1/authorize?client_id=0oa1&state=T4IU",
            None,
        ),
        # dbcert's real prompt: the address is the last thing printed while it waits.
        (
            "dbcert: Certificate requested by: omnigent\n"
            "dbcert: Logging in via SSO...\n"
            "dbcert: If the browser does not open automatically, please open the following URL:\n"
            "\n\thttps://databricks.okta.com/oauth2/v1/authorize?client_id=0oa1&state=T4IU\n\n",
            "https://databricks.okta.com/oauth2/v1/authorize?client_id=0oa1&state=T4IU",
            None,
        ),
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


def test_detect_sign_in_prompt_ignores_an_address_the_launcher_moved_past() -> None:
    """
    Once the sign-in completes, the launcher and the agent print below the address.

    Codex draws its interface inline, so dbcert's lines stay on screen above
    the banner; the address is no longer a pending prompt. A card asking the
    host for the live link must then hear "nothing pending" instead of opening
    the spent address again.
    """
    screen = (
        "dbcert: If the browser does not open automatically, please open the following URL:\n"
        "\n\thttps://databricks.okta.com/oauth2/v1/authorize?client_id=0oa1&state=T4IU\n\n"
        "dbcert: All credentials successfully written\n"
        "dbcert: Successfully copied certificates for bazel cache\n"
        "Cloning hosted gateway models from gpt-5.5 because gpt-5.2 is not bundled\n"
        "\u256d\u2500\u2500 OpenAI Codex (v0.156.1) \u2500\u2500\u256e\n"
        "\u203a Ask Codex to do anything\n"
    )
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


_WORKSPACE = "https://example.cloud.databricks.com"
_AUTHORIZE_URL = (
    f"{_WORKSPACE}/oidc/v1/authorize?response_type=code&client_id=example-client"
    "&redirect_uri=http%3A%2F%2Flocalhost%3A8020%2Foauth-callback"
    "&state=abc123&code_challenge=REDACTED&code_challenge_method=S256"
    "&scope=offline_access+all-apis"
)
_OAUTH_FLOW_LINES = (
    f"[INFO] Cached token expired or missing for {_WORKSPACE}. Starting OAuth flow...\n"
    "[INFO] Waiting for OAuth callback on port 8020. "
    "Please open this URL in your browser:\n"
    f"{_AUTHORIZE_URL}\n"
)
_PAUSED_AUTO_OPEN_LINES = (
    "[WARNING] Not opening a browser tab automatically: the last authentication attempt "
    "did not complete within 120s, so the launcher is pausing auto-open to avoid piling up "
    "tabs while you're away. Open this URL in your browser to authenticate:\n"
    f"{_AUTHORIZE_URL}\n\n\n"
)


@pytest.mark.parametrize("reprint", ["", _PAUSED_AUTO_OPEN_LINES])
def test_detect_sign_in_prompt_prefers_the_endpoint_over_a_cue_adjacent_host(reprint: str) -> None:
    """
    A launcher names the workspace host above the real authorize link.

    The host sits next to "open this URL" wording but is not the link; the
    authorize address is, including when the launcher prints it again.
    """
    prompt = detect_sign_in_prompt(_OAUTH_FLOW_LINES + reprint)
    assert prompt is not None
    assert prompt.url == _AUTHORIZE_URL
    assert prompt.code is None


def test_detect_sign_in_prompt_ignores_an_endpoint_the_launcher_moved_past() -> None:
    """Output after the cue-adjacent host still marks a preferred endpoint as stale."""
    screen = (
        _OAUTH_FLOW_LINES
        + "[INFO] Token cached for the workspace\n"
        + "╭── OpenAI Codex (v0.156.1) ──╮\n"
        + "› Ask Codex to do anything\n"
    )
    assert detect_sign_in_prompt(screen) is None


def test_detect_sign_in_prompt_ignores_output_that_extends_a_bare_host() -> None:
    """A line that only starts with the fallback address is agent output, not a re-print."""
    screen = (
        "[INFO] Cached token expired for https://host.example.com. Starting sign-in...\n"
        "Opened https://host.example.com/pr/123 for review\n"
    )
    assert detect_sign_in_prompt(screen) is None


def test_detect_sign_in_prompt_ignores_a_later_endpoint_after_the_prompt_completed() -> None:
    """An auth-shaped docs link below a finished prompt must not revive it."""
    screen = (
        "Open the following URL to sign in:\n"
        "https://dbcert.example.com/x\n"
        "Signed in as someone@example.com.\n"
        "Agent ready. Help: https://example.com/login\n"
    )
    assert detect_sign_in_prompt(screen) is None


def test_detect_sign_in_prompt_ignores_a_docs_link_below_the_waiting_line() -> None:
    """The stale check runs from the first qualifying address, not from the chosen link."""
    screen = (
        "Enter code ABCD-1234 at https://x.example/verify\n"
        "waiting for sign-in...\n"
        "See https://docs.example.com/oauth/troubleshooting\n"
    )
    assert detect_sign_in_prompt(screen) is None


def test_detect_sign_in_prompt_takes_the_latest_link_of_a_retrying_launcher() -> None:
    """A retry prints a fresh authorize link; the earlier attempt's link is dead."""
    retry_url = _AUTHORIZE_URL.replace("state=abc123", "state=def456")
    screen = (
        _OAUTH_FLOW_LINES
        + "[WARNING] The last authentication attempt did not complete. "
        + "Open this URL in your browser to authenticate:\n"
        + f"{retry_url}\n"
    )
    prompt = detect_sign_in_prompt(screen)
    assert prompt is not None
    assert prompt.url == retry_url


def test_detect_sign_in_prompt_accepts_a_link_printed_again_after_a_trailer() -> None:
    """A re-printed link has no trailer wording of its own, so it must count as the prompt."""
    url = "https://example.okta.com/oauth2/v1/authorize?client_id=0oa1&state=T4IU"
    screen = (
        "Please open this URL in your browser:\n"
        f"\t{url}\n"
        "Still waiting. Please open this URL in your browser:\n"
        f"\t{url}\n"
    )
    prompt = detect_sign_in_prompt(screen)
    assert prompt is not None
    assert prompt.url == url


def test_detect_sign_in_prompt_does_not_treat_an_auth_shaped_hostname_as_the_endpoint() -> None:
    """Only the path, query, or fragment marks an endpoint, never the workspace host."""
    authorize_url = "https://sso.corp.example.com/oidc/v1/authorize?response_type=code&state=x"
    screen = (
        "[INFO] Cached token expired or missing for https://sso.corp.example.com. "
        "Starting OAuth flow...\n"
        "Please open this URL in your browser:\n"
        f"{authorize_url}\n"
    )
    prompt = detect_sign_in_prompt(screen)
    assert prompt is not None
    assert prompt.url == authorize_url


# What Databricks' ``ug`` launcher prints before Claude Code runs when the
# installed Claude Code is older than it requires; it then blocks on a keypress.
_UG_UPGRADE_SCREEN = (
    "Generating claude-code MCP client config...\n"
    "No changes made to /home/user/.claude.json.\n"
    "Managed settings drift detected, applying updates...\n"
    "Installing ug CLI...\n"
    "ug CLI installed (0.1.0+5f7eed4).\n"
    "! ug requires Claude Code 2.1.290 or newer. Your current version is Claude Code 2.1.287.\n"
    "Upgrade Claude Code if available? (Y/n) \n\n\n"
)


@pytest.mark.parametrize(
    ("screen", "expected"),
    [
        (_UG_UPGRADE_SCREEN, "Upgrade Claude Code if available? (Y/n)"),
        ("Continue with the install? [y/N]: ", "Continue with the install? [y/N]:"),
        ("Overwrite the existing settings (yes/no)?", "Overwrite the existing settings (yes/no)?"),
        ("Proceed  (Y/N)\n", "Proceed (Y/N)"),
        # Keys echoed into the waiting pane are not output the launcher printed.
        (_UG_UPGRADE_SCREEN + "^[^[\n^[[A\n", "Upgrade Claude Code if available? (Y/n)"),
        (
            "Upgrade Claude Code if available? (Y/n) ^[[B",
            "Upgrade Claude Code if available? (Y/n)",
        ),
    ],
)
def test_detect_launcher_question_lifts_the_waiting_question(screen: str, expected: str) -> None:
    assert detect_launcher_question(screen) == expected


@pytest.mark.parametrize(
    "screen",
    [
        None,
        "",
        "\n\n",
        "Starting MCP servers: omnigent",
        # A cue mid-line is prose, not a prompt awaiting a key.
        "Answer (Y/n) to each question as it appears.\n",
        "Run with --yes to skip the [y/N] prompts and continue.\n",
        # Output below the question means the launcher moved on.
        _UG_UPGRADE_SCREEN + "Upgrading Claude Code...\n",
        _UG_UPGRADE_SCREEN + "╭── Claude Code ──╮\n❯ \n",
        # A fraction or a path is not an answer cue.
        "Downloaded 3/4 files (1/2)\n",
        # Mismatched delimiters are prose, not a cue.
        "Continue (y/n]\n",
    ],
)
def test_detect_launcher_question_ignores_other_screens(screen: str | None) -> None:
    assert detect_launcher_question(screen) is None


def test_detect_launcher_question_keeps_the_cue_of_a_long_line() -> None:
    question = detect_launcher_question("x" * 200 + " (y/n)")
    assert question is not None
    assert len(question) <= 120
    assert question.startswith("xxxx")
    assert question.endswith("(y/n)")


@pytest.mark.parametrize(
    "screen",
    [
        _UG_UPGRADE_SCREEN,
        # The cue wrapped at the pane edge: only its tail is on the last row.
        "Upgrade Claude Code if available? (Y/\nn) \n",
        "Continue? [y/N]: ^[[B",
    ],
)
def test_may_end_with_answer_cue_accepts_a_closing_bracket_tail(screen: str) -> None:
    assert may_end_with_answer_cue(screen)


@pytest.mark.parametrize(
    "screen",
    [
        None,
        "",
        "Loading Omnigent...\n",
        "Claude Code v2.1.283\n",
        _UG_UPGRADE_SCREEN + "Upgrading...\n",
        # A long line ending in a bracket is ordinary output, not a wrapped cue.
        "JSON Parse error: Unrecognized token\n  at parse (unknown)\n",
        "  ⏵⏵ auto mode on (shift+tab to cycle)\n",
    ],
)
def test_may_end_with_answer_cue_rejects_other_tails(screen: str | None) -> None:
    assert not may_end_with_answer_cue(screen)


def test_sign_in_next_step_names_the_agent_and_carries_no_address() -> None:
    """The next step never embeds the one-time link; the card fetches it live."""
    step = sign_in_next_step("Codex")
    assert step == (
        "Open the sign-in link and sign in. Codex continues on its own once the "
        "sign-in completes; then send your message again."
    )
    assert "http" not in step
