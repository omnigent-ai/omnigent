"""Tests for the always-on CLI diagnostics log."""

from __future__ import annotations

import io
import logging
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from omnigent import cli_diagnostics


@dataclass(frozen=True)
class _LoggerSnapshot:
    """
    Logger state captured before a test configures CLI diagnostics.

    :param handlers: Handlers present before the test started.
    :param level: Numeric logging level configured before the test started.
    :param propagate: Whether the logger propagated before the test started.
    """

    handlers: list[logging.Handler]
    level: int
    propagate: bool


class _FailingRedirectedStderr:
    """
    Stderr stub whose close path fails after exposing the original stream.

    :param original: Terminal stderr stream that ``restore_stderr`` should
        restore before attempting to close the redirected stream.
    """

    def __init__(self, original: io.TextIOBase) -> None:
        """
        Create the failing redirected stderr stub.

        :param original: Terminal stderr stream saved for restoration.
        :returns: ``None``.
        """
        self._original_stderr = original

    def close(self) -> None:
        """
        Raise the close failure that ``restore_stderr`` must log.

        :returns: ``None``.
        :raises OSError: Always, to exercise the diagnostics path.
        """
        raise OSError("close failed")


def _capture_logger_snapshots() -> dict[str, _LoggerSnapshot]:
    """
    Capture package logger state mutated by ``setup_cli_logging``.

    :returns: Snapshot keyed by logger name.
    """
    snapshots: dict[str, _LoggerSnapshot] = {}
    for name in ("", "omnigent", "omnigent_ui_sdk", "databricks.sdk"):
        logger = logging.getLogger(name)
        snapshots[name] = _LoggerSnapshot(
            handlers=list(logger.handlers),
            level=logger.level,
            propagate=logger.propagate,
        )
    return snapshots


def _restore_logger_snapshots(snapshots: dict[str, _LoggerSnapshot]) -> None:
    """
    Restore package loggers after ``setup_cli_logging`` added file handlers.

    :param snapshots: Logger state returned by
        :func:`_capture_logger_snapshots`.
    :returns: ``None``.
    """
    for name, snapshot in snapshots.items():
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            if handler not in snapshot.handlers:
                handler.close()
        for handler in snapshot.handlers:
            logger.addHandler(handler)
        logger.setLevel(snapshot.level)
        logger.propagate = snapshot.propagate


@pytest.fixture
def isolated_cli_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[None]:
    """
    Isolate CLI diagnostics global logging state for a test.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary home directory root for diagnostics logs.
    :returns: Iterator yielding control to the test body.
    """
    snapshots = _capture_logger_snapshots()
    original_stderr = sys.stderr
    monkeypatch.setenv("HOME", str(tmp_path))
    yield
    cli_diagnostics.restore_stderr()
    sys.stderr = original_stderr
    _restore_logger_snapshots(snapshots)


def test_redirect_stderr_to_log_redacts_direct_stderr_writes(
    isolated_cli_diagnostics: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Redirected raw stderr writes must honor the diagnostics redaction contract.

    :param isolated_cli_diagnostics: Fixture isolating logging globals.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: ``None``.
    """
    del isolated_cli_diagnostics
    terminal_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", terminal_stderr)
    ctx = cli_diagnostics.setup_cli_logging(["run", "agent.yaml"])

    cli_diagnostics.redirect_stderr_to_log()
    print("MY_API_KEY=super-secret-token", file=sys.stderr)
    sys.stderr.write("Authorization: Bearer sk-directsecret12345\n")
    sys.stderr.flush()
    cli_diagnostics.restore_stderr()

    log_text = ctx.path.read_text(encoding="utf-8")
    assert "super-secret-token" not in log_text, (
        f"redirected stderr leaked an API key into the CLI diagnostics log: {log_text!r}"
    )
    assert "sk-directsecret12345" not in log_text, (
        f"redirected stderr leaked an SDK token into the CLI diagnostics log: {log_text!r}"
    )
    assert "[REDACTED]" in log_text, (
        f"redirected stderr should preserve context with redacted values, got: {log_text!r}"
    )
    assert terminal_stderr.getvalue() == "", (
        f"redirected stderr should not paint into the terminal, got: "
        f"{terminal_stderr.getvalue()!r}"
    )


@pytest.mark.parametrize(
    ("text", "removed", "preserved"),
    [
        (
            "Authorization: Bearer eyJhbGciOiJexposedOPAQUE12345",
            ("eyJhbGciOiJexposedOPAQUE12345",),
            ("[REDACTED]",),
        ),
        (
            "Authorization: Basic dXNlcjpodW50ZXIy",
            ("dXNlcjpodW50ZXIy",),
            (),
        ),
        (
            "clone failed for https://alice:s3cr3tpassw0rd@registry.internal/repo",
            ("alice", "s3cr3tpassw0rd"),
            ("registry.internal",),
        ),
    ],
    ids=["authorization-bearer", "authorization-basic", "url-basic-auth"],
)
def test_redact_secrets_scrubs_structured_credentials(
    text: str, removed: tuple[str, ...], preserved: tuple[str, ...]
) -> None:
    """Structured credentials are removed while useful context remains."""
    scrubbed = cli_diagnostics.redact_secrets(text)
    for value in removed:
        assert value not in scrubbed
    for value in preserved:
        assert value in scrubbed


@pytest.mark.parametrize(
    ("text", "removed", "preserved"),
    [
        (
            'Authorization: Digest username="alice", response="digest-response-secret"',
            ("alice", "digest-response-secret"),
            ("Authorization: [REDACTED]",),
        ),
        (
            "Authorization: AWS4-HMAC-SHA256 Credential="
            + "AKIA"
            + "QWERTYUIOP123456"
            + "/date/region/service/aws4_request, Signature=aws-signature-secret",
            ("QWERTYUIOP123456", "aws-signature-secret"),
            ("Authorization: [REDACTED]",),
        ),
        (
            '{"Authorization": "Basic dXNlcjpodW50ZXIy", "status": 401}',
            ("dXNlcjpodW50ZXIy",),
            ('"Authorization": "[REDACTED]"', '"status": 401'),
        ),
        (
            "Authorization: Basic\r\n dXNlcjpodW50ZXIy\r\nX-Request: failed",
            ("dXNlcjpodW50ZXIy",),
            ("Authorization: [REDACTED]", "X-Request: failed"),
        ),
        (
            "Authorization: Token 0123456789abcdef0123",
            ("0123456789abcdef0123",),
            ("Authorization: [REDACTED]",),
        ),
        (
            "Authorization: 0123456789abcdef0123",
            ("0123456789abcdef0123",),
            ("Authorization: [REDACTED]",),
        ),
        (
            "Authorization: GNAP+Sig 0123456789abcdef0123",
            ("0123456789abcdef0123",),
            ("Authorization: [REDACTED]",),
        ),
        (
            "Authorization='GNAP+Sig 0123456789abcdef0123'; status=401",
            ("0123456789abcdef0123",),
            ("Authorization='[REDACTED]'", "status=401"),
        ),
    ],
    ids=[
        "digest",
        "aws4",
        "quoted-json",
        "folded-basic",
        "token",
        "scheme-less",
        "gnap-sig",
        "matching-quote",
    ],
)
def test_redact_secrets_scrubs_complete_authorization_values(
    text: str, removed: tuple[str, ...], preserved: tuple[str, ...]
) -> None:
    """Complete structured Authorization values are redacted."""
    scrubbed = cli_diagnostics.redact_secrets(text)
    for value in removed:
        assert value not in scrubbed
    for value in preserved:
        assert value in scrubbed


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Basic YTo=",
        "Authorization: Bearer abc",
    ],
    ids=["short-basic", "short-bearer"],
)
def test_redact_secrets_scrubs_short_authorization_values(text: str) -> None:
    """Explicit Authorization keys redact even short valid credentials."""
    assert cli_diagnostics.redact_secrets(text) == "Authorization: [REDACTED]"


def test_redact_secrets_scrubs_short_keyless_bearer_value() -> None:
    """A keyless Bearer redacts any non-empty value — even prose-shaped ones."""
    assert cli_diagnostics.redact_secrets("bearer of bad news") == "bearer [REDACTED] bad news"


def test_redact_secrets_handles_unterminated_quoted_authorization_linearly() -> None:
    """An unterminated quoted value with backslashes is scanned in linear time."""
    text = 'Authorization: "' + "\\" * 34
    started_at = time.perf_counter()
    scrubbed = cli_diagnostics.redact_secrets(text)
    elapsed = time.perf_counter() - started_at
    assert scrubbed == 'Authorization: "[REDACTED]'
    assert elapsed < 0.5


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_redact_secrets_scrubs_folded_quoted_authorization(newline: str) -> None:
    """Indented LF and CRLF continuations remain inside a quoted value."""
    text = f'Authorization: "abc{newline} secretcredential"'
    assert cli_diagnostics.redact_secrets(text) == 'Authorization: "[REDACTED]"'


def test_redact_secrets_limits_unterminated_quoted_authorization_folds() -> None:
    """A malformed quoted value cannot consume an indented stack trace."""
    text = 'Authorization: "abc\n secretcredential\n frame one\n frame two'
    assert cli_diagnostics.redact_secrets(text) == (
        'Authorization: "[REDACTED]\n frame one\n frame two'
    )


def test_redact_secrets_scrubs_after_planted_authorization_marker() -> None:
    """A planted marker cannot shield a later Authorization credential."""
    text = "Authorization: [REDACTED] actualcredential"
    assert cli_diagnostics.redact_secrets(text) == "Authorization: [REDACTED]"


def test_redact_secrets_requires_authorization_key_boundary() -> None:
    """A longer unrelated key ending in authorization remains unchanged."""
    text = "preauthorization: successful"
    assert cli_diagnostics.redact_secrets(text) == text


def test_redact_secrets_stops_unquoted_authorization_at_eol() -> None:
    """An unquoted Authorization value cannot consume the following line."""
    text = "Authorization: abcdefghijk\nNext-Line: ok"
    assert cli_diagnostics.redact_secrets(text) == "Authorization: [REDACTED]\nNext-Line: ok"


def test_redact_secrets_does_not_fold_bare_newline_after_authorization_colon() -> None:
    """A following diagnostic line is not an unindented header continuation."""
    text = "Authorization:\nNext-Line: ok"
    assert cli_diagnostics.redact_secrets(text) == "Authorization:[REDACTED]\nNext-Line: ok"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Authorization: Bearer abcdefghijk, retrying", "Authorization: [REDACTED]"),
        ("(bearer abcdefghijk)", "(bearer [REDACTED])"),
        ('"bearer abcdefghijk"', '"bearer [REDACTED]"'),
    ],
    ids=["authorization-comma", "parenthesized", "quoted"],
)
def test_redact_secrets_scrubs_punctuation_terminated_bearer(text: str, expected: str) -> None:
    """Punctuation around bearer credentials cannot prevent redaction."""
    assert cli_diagnostics.redact_secrets(text) == expected


@pytest.mark.parametrize(
    ("url", "removed"),
    [
        ("https://:credential@host.example/path", ("credential",)),
        ("https://credential:@host.example/path", ("credential",)),
        ("https://tokenvalue@host.example/path", ("tokenvalue",)),
        ("https://u:p@ss@host.example/path", ("u:p@ss",)),
        ("https://u@ss@host.example/path", ("u@ss",)),
    ],
    ids=["empty-user", "empty-password", "no-password", "at-in-password", "at-in-username"],
)
def test_redact_secrets_scrubs_url_authority_userinfo(url: str, removed: tuple[str, ...]) -> None:
    """Only userinfo inside the URL authority is redacted."""
    scrubbed = cli_diagnostics.redact_secrets(url)
    assert scrubbed == "https://[REDACTED]@host.example/path"
    for value in removed:
        assert value not in scrubbed


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://user:pass@host_name%2Einternal/path",
            "https://[REDACTED]@host_name%2Einternal/path",
        ),
        (
            "https://user:pass@[fe80::1%25eth0]/path",
            "https://[REDACTED]@[fe80::1%25eth0]/path",
        ),
        (
            "https://user:pass@münchen.example/path",
            "https://[REDACTED]@münchen.example/path",
        ),
    ],
    ids=["reg-name", "ipv6-zone", "unicode-iri"],
)
def test_redact_secrets_accepts_url_host_characters(url: str, expected: str) -> None:
    """Valid reg-name, IPv6 zone, and Unicode host characters terminate URL userinfo."""
    assert cli_diagnostics.redact_secrets(url) == expected


@pytest.mark.parametrize(
    "secret",
    [
        "xoxb" + "-1234567890-abcdefslacktoken",
        "ghp_" + "a1b2c3d4e5" * 3 + "abcdef",
        "AKIA" + "QWERTYUIOP123456",
    ],
    ids=["slack", "github", "aws"],
)
def test_redact_secrets_scrubs_opaque_shapes(secret: str) -> None:
    """Supported opaque secret shapes are redacted."""
    scrubbed = cli_diagnostics.redact_secrets(f"boot failed: {secret} rejected")
    assert secret not in scrubbed
    assert "[REDACTED]" in scrubbed


@pytest.mark.parametrize(
    "secret",
    [
        "g\u2800hp_" + "A" * 20,
        "dap\u2800i" + "A" * 20,
        "AK\u2800IA" + "A" * 16,
        "sk\u2800-" + "A" * 20,
        "g\u200bhp_" + "A" * 20,
        "g\u0338hp_" + "A" * 20,
        "Authori\u2800zation: Basic dXNlcjpodW50ZXIy",
    ],
    ids=[
        "braille-github-anchor",
        "braille-databricks-anchor",
        "braille-aws-anchor",
        "braille-openai-anchor",
        "zero-width-github-anchor",
        "combining-github-anchor",
        "braille-authorization-anchor",
    ],
)
def test_redact_secrets_absorbs_nonspace_splitters_inside_anchors(secret: str) -> None:
    """Any non-space, non-newline splitter is absorbed throughout an anchor."""
    assert cli_diagnostics.redact_secrets(secret).endswith("[REDACTED]")


@pytest.mark.parametrize(
    "secret",
    [
        "g" + "\u2800" * 65 + "hp_" + "A" * 20,
        "g" + "hp_" + "A" * 10 + "\u2800" * 65 + "A" * 10,
    ],
    ids=["anchor", "body"],
)
def test_redact_secrets_absorbs_long_nonspace_splitter_runs(secret: str) -> None:
    """A splitter run longer than the former cap remains inside the credential."""
    assert cli_diagnostics.redact_secrets(secret) == "[REDACTED]"


@pytest.mark.parametrize(
    "text",
    [
        "\u03bb" + "g" + "hp_" + "A" * 20,
        "\u00e9Bearer " + "a" * 11,
    ],
    ids=["github", "bearer"],
)
def test_redact_secrets_ignores_non_ascii_letter_before_anchor(text: str) -> None:
    """A non-ASCII letter cannot shield an ASCII credential anchor."""
    assert cli_diagnostics.redact_secrets(text).endswith("[REDACTED]")


def test_redact_secrets_stops_at_splitter_after_length_floor() -> None:
    """A non-boundary splitter after the body floor preserves the diagnostic tail."""
    suffix = "\u2800abcdefghijklmnopqrstuvwxyz0123456789"
    secret = "g" + "hp_" + "A" * 20 + suffix
    assert cli_diagnostics.redact_secrets(secret) == "[REDACTED]" + suffix


def test_redact_secrets_preserves_punctuation_delimited_suffix() -> None:
    """Diagnostic punctuation and fields after a complete credential remain visible."""
    suffix = ";status=401;reason=expired"
    text = "g" + "hp_" + "A" * 20 + suffix
    assert cli_diagnostics.redact_secrets(text) == "[REDACTED]" + suffix


@pytest.mark.parametrize(
    ("secret", "split_index"),
    [
        ("Bearer abcdefghijk", 11),
        ("".join(("dapi", "FAKE", "TEST", "0123456789")), 7),
        ("".join(("xoxb", "-", "FAKE", "-", "TEST", "-", "TOKEN")), 9),
        ("".join(("ghp_", "FAKE", "TEST", "TOKEN", "PLACEHOLDER")), 12),
        ("".join(("AKIA", "FAKE", "TEST", "ONLY", "0000")), 10),
        ("".join(("ASIA", "FAKE", "TEST", "ONLY", "0000")), 10),
    ],
    ids=["bearer", "dapi", "slack", "github", "aws-akia", "aws-asia"],
)
def test_redact_secrets_redacts_one_literal_interior_space(
    secret: str,
    split_index: int,
) -> None:
    """One genuine word gap cannot split an otherwise credential-shaped token."""
    spaced = f"{secret[:split_index]} {secret[split_index:]}"
    scrubbed = cli_diagnostics.redact_secrets(spaced)
    assert spaced not in scrubbed
    assert "[REDACTED]" in scrubbed


def test_redact_secrets_preserves_prefixed_prose_with_word_gaps() -> None:
    """A bare fixed-prefix anchor with only prose after it stays readable."""
    text = "ghp_ is a prefix used in documentation"
    assert cli_diagnostics.redact_secrets(text) == text


def test_redact_secrets_rejects_bearer_repetition_in_linear_time() -> None:
    """Bearer-like prose cannot trigger super-linear regex backtracking."""
    text = ("bearer a " * 1600) + "!"
    started_at = time.perf_counter()
    scrubbed = cli_diagnostics.redact_secrets(text)
    elapsed = time.perf_counter() - started_at
    assert scrubbed == ("bearer [REDACTED] " * 1600) + "!"
    assert elapsed < 0.1


@pytest.mark.parametrize(
    ("secret", "split_index"),
    [
        ("".join(("dapi", "FAKE", "TEST", "0123456789")), 7),
        ("".join(("xoxb", "-", "FAKE", "-", "TEST", "-", "TOKEN")), 9),
        ("".join(("ghp_", "FAKE", "TEST", "TOKEN", "PLACEHOLDER")), 12),
        ("".join(("AKIA", "FAKE", "TEST", "ONLY", "0000")), 10),
        ("".join(("ASIA", "FAKE", "TEST", "ONLY", "0000")), 10),
    ],
    ids=["dapi", "slack", "github", "aws-akia", "aws-asia"],
)
def test_redact_secrets_rejects_multiple_interior_spaces(
    secret: str,
    split_index: int,
) -> None:
    """Tolerance is bounded to one literal interior space (fixed-prefix families).

    ``Bearer`` is keyed and floorless, so it redacts the pre-gap fragment
    instead; its bounded-gap behavior is covered by the keyed-value tests.
    """
    spaced = f"{secret[:split_index]}  {secret[split_index:]}"
    assert cli_diagnostics.redact_secrets(spaced) == spaced


def test_redact_secrets_does_not_join_literal_space_inside_http_scheme() -> None:
    """The shared redactor does not rewrite non-canonical URL schemes."""
    text = "ht tps://alice:secret@host.example/path"
    assert cli_diagnostics.redact_secrets(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "GET https://app.example.com:8443?login_hint=alice@example.com failed 302",
        "GET https://[2001:db8::1]:8443/path#owner@example.com failed 302",
        "https://example.com:8080 retry user@host.com done",
        "request to https://api.example.com:443 failed for user alice@corp.com",
    ],
    ids=["query-at-sign", "ipv6-fragment-at-sign", "host-port-prose", "host-port-email"],
)
def test_redact_secrets_preserves_non_userinfo_urls(text: str) -> None:
    """Query and fragment ``@`` signs are outside URL userinfo."""
    assert cli_diagnostics.redact_secrets(text) == text


def test_redact_secrets_is_idempotent() -> None:
    """Repeated formatter and stderr passes preserve the first redaction."""
    text = '{"Authorization": "GNAP+Sig abcdefghijk", "status": "request failed"}'
    once = cli_diagnostics.redact_secrets(text)
    assert cli_diagnostics.redact_secrets(once) == once
    assert once == '{"Authorization": "[REDACTED]", "status": "request failed"}'
    assert "request failed" in once


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Bearer abcdefghijkl", "Bearer [REDACTED]"),
        ("Bearer abc123", "Bearer [REDACTED]"),
        ("API_TOKEN=abcdefghijkl", "API_TOKEN=[REDACTED]"),
        ("password=hunter2", "password=[REDACTED]"),
    ],
    ids=["bearer", "short-bearer", "env", "short-env"],
)
def test_redact_secrets_is_idempotent_for_keyed_values(text: str, expected: str) -> None:
    """Bearer and env redactions survive the double-redacting stderr path."""
    once = cli_diagnostics.redact_secrets(text)
    assert once == expected
    assert cli_diagnostics.redact_secrets(once) == once


def test_redact_secrets_stops_at_existing_marker_on_repeat() -> None:
    """A completed marker is a boundary, so a second pass keeps later fields."""
    text = "password=hunter2 status=401"
    once = cli_diagnostics.redact_secrets(text)

    assert once == "password=[REDACTED] status=401"
    assert cli_diagnostics.redact_secrets(once) == once


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Bearer [REDACTED]actualsecret", "Bearer [REDACTED]"),
        ("password=[REDACTED]actualsecret", "password=[REDACTED]"),
    ],
    ids=["bearer", "env"],
)
def test_redact_secrets_scrubs_marker_glued_values(text: str, expected: str) -> None:
    """A planted marker glued to a value cannot shield the glued suffix."""
    once = cli_diagnostics.redact_secrets(text)
    assert once == expected
    assert cli_diagnostics.redact_secrets(once) == once


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Bearer [REDACTED]\u2800actualsecret", "Bearer [REDACTED]"),
        ("password=[REDACTED]\u2800actualsecret", "password=[REDACTED]"),
    ],
    ids=["bearer-splitter", "env-splitter"],
)
def test_redact_secrets_scrubs_marker_separated_values(text: str, expected: str) -> None:
    """A planted marker cannot shield a suffix joined by a non-boundary splitter."""
    once = cli_diagnostics.redact_secrets(text)
    assert once == expected
    assert cli_diagnostics.redact_secrets(once) == once


@pytest.mark.parametrize("quote", ['"', "'"])
def test_redact_secrets_scrubs_quoted_multiline_env_value(quote: str) -> None:
    """A quoted env value runs to its closing quote across a folded newline."""
    text = f"DATABASE_PASSWORD={quote}line-one\n line-two{quote} status=401"
    scrubbed = cli_diagnostics.redact_secrets(text)
    assert scrubbed == f"DATABASE_PASSWORD={quote}[REDACTED]{quote} status=401"
    assert "line-one" not in scrubbed
    assert "line-two" not in scrubbed
    assert cli_diagnostics.redact_secrets(scrubbed) == scrubbed


def test_redact_secrets_caps_unquoted_authorization_folds() -> None:
    """An unquoted Authorization value folds once; the traceback survives."""
    text = (
        "Authorization: Bearer abc\n"
        "  trace-line-one\n"
        "  trace-line-two\n"
        "  trace-line-three\n"
        "X-Next: ok"
    )
    assert cli_diagnostics.redact_secrets(text) == (
        "Authorization: [REDACTED]\n  trace-line-two\n  trace-line-three\nX-Next: ok"
    )


def test_redact_secrets_scans_authorization_fold_flood_linearly() -> None:
    """Repeated unquoted folded headers cannot trigger suffix rescans."""
    text = "Authorization: x\n" + " Authorization: x\n" * 4000
    started_at = time.perf_counter()
    scrubbed = cli_diagnostics.redact_secrets(text)
    elapsed = time.perf_counter() - started_at

    assert "Authorization: x" not in scrubbed
    assert elapsed < 0.5


def test_redact_secrets_preserves_gapless_bearer_prose() -> None:
    """Prose continuing the anchor word ("bearers") is not widened into a value."""
    text = "the bearers of these tokens"
    assert cli_diagnostics.redact_secrets(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "the bearers submitted the tokens",
        "bearers received",
        "bearerform filed",
    ],
    ids=["plural-sentence", "plural-short", "compound"],
)
def test_redact_secrets_does_not_bridge_gapless_bearer_prose_across_words(text: str) -> None:
    """The gapless length floor counts only characters glued to the anchor."""
    assert cli_diagnostics.redact_secrets(text) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("DATABASE_PASSWORD=hunter2", "DATABASE_PASSWORD=[REDACTED]"),
        ("API_KEY=a1b2c3", "API_KEY=[REDACTED]"),
        ("Bearer abc123", "Bearer [REDACTED]"),
        ("password=x", "password=[REDACTED]"),
    ],
    ids=["password", "api-key", "bearer", "single-char"],
)
def test_redact_secrets_scrubs_short_keyed_values(text: str, expected: str) -> None:
    """Keyed anchors redact any non-empty value — no length floor applies."""
    assert cli_diagnostics.redact_secrets(text) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("p@ssw0rd!", "[REDACTED]"),
        ("https://user:pass@host/path", "[REDACTED]"),
    ],
    ids=["punctuation", "url-shaped"],
)
def test_redact_secrets_scrubs_full_unquoted_env_value(value: str, expected: str) -> None:
    """An unquoted keyed value spans every non-whitespace character."""
    text = f"DATABASE_PASSWORD={value} status=401"
    assert cli_diagnostics.redact_secrets(text) == f"DATABASE_PASSWORD={expected} status=401"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "password\u00a0=supersecretvalue",
            "password\u00a0=[REDACTED]",
        ),
        (
            "API_KEY\u2009= abcdefghijkl",
            "API_KEY\u2009= [REDACTED]",
        ),
        (
            "token:\neyJhbGciOiJIUzI1NiJ9.payload.signature",
            "token:\n[REDACTED]",
        ),
        (
            "password=\nsupersecretvalue",
            "password=\n[REDACTED]",
        ),
        (
            "password =\n  supersecretvalue",
            "password =\n  [REDACTED]",
        ),
        (
            "token:\r\nsecretvalue123",
            "token:\r\n[REDACTED]",
        ),
        (
            "password=\n\nnot the value",
            "password=\n\nnot the value",
        ),
    ],
    ids=[
        "nbsp-before-equals",
        "thin-space-before-equals",
        "jwt-after-newline",
        "value-after-newline",
        "value-after-indented-fold",
        "value-after-crlf",
        "blank-line-ends-fold",
    ],
)
def test_redact_secrets_scrubs_env_values_across_gaps_and_folds(text: str, expected: str) -> None:
    """Unicode word gaps around ``:=`` and one folded newline still match."""
    assert cli_diagnostics.redact_secrets(text) == expected


@pytest.mark.parametrize("prefix", ["AKIA", "ASIA"])
def test_redact_secrets_scrubs_overlong_aws_key(prefix: str) -> None:
    """Overlong AWS keys are redacted; short and lowercase runs are preserved."""
    key = prefix + "A" * 17
    scrubbed = cli_diagnostics.redact_secrets(f"boot failed with {key} end")
    assert key not in scrubbed
    assert "[REDACTED]" in scrubbed
    assert cli_diagnostics.redact_secrets(prefix + "A" * 15) == prefix + "A" * 15
    assert cli_diagnostics.redact_secrets(prefix.lower() + "a" * 17) == prefix.lower() + "a" * 17


def test_setup_cli_logging_uses_data_dir_cli_destination(
    isolated_cli_diagnostics: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CLI diagnostics live under ``<data-dir>/logs/cli``."""
    del isolated_cli_diagnostics
    data_dir = tmp_path / "data"
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(data_dir))

    ctx = cli_diagnostics.setup_cli_logging(["run", "agent.yaml"])

    assert ctx.path.parent == data_dir / "logs" / "cli"
    assert ctx.path.name.startswith("cli-")


def test_setup_cli_logging_honors_debug_level(
    isolated_cli_diagnostics: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OMNIGENT_LOG_LEVEL=DEBUG`` makes debug records reach cli logs."""
    del isolated_cli_diagnostics
    monkeypatch.setenv("OMNIGENT_LOG_LEVEL", "DEBUG")
    ctx = cli_diagnostics.setup_cli_logging(["run", "agent.yaml"])

    logging.getLogger("omnigent.test").debug("debug-visible")

    assert "debug-visible" in ctx.path.read_text(encoding="utf-8")


def test_restore_stderr_returns_writes_to_original_terminal(
    isolated_cli_diagnostics: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``restore_stderr`` must return subsequent writes to the original stream.

    :param isolated_cli_diagnostics: Fixture isolating logging globals.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: ``None``.
    """
    del isolated_cli_diagnostics
    terminal_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", terminal_stderr)
    ctx = cli_diagnostics.setup_cli_logging(["run", "agent.yaml"])

    cli_diagnostics.redirect_stderr_to_log()
    print("during-tui", file=sys.stderr)
    cli_diagnostics.restore_stderr()
    print("after-tui", file=sys.stderr)

    log_text = ctx.path.read_text(encoding="utf-8")
    assert "during-tui" in log_text, (
        f"stderr written during the TUI lifetime should land in the log: {log_text!r}"
    )
    assert "after-tui" not in log_text, (
        f"stderr written after restore should not keep landing in the log: {log_text!r}"
    )
    assert terminal_stderr.getvalue() == "after-tui\n", (
        f"stderr was not restored to the original terminal stream: {terminal_stderr.getvalue()!r}"
    )


def test_log_cli_error_hint_uses_original_stderr_when_redirected(
    isolated_cli_diagnostics: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Fatal-error hints must stay visible even while TUI stderr is redirected.

    :param isolated_cli_diagnostics: Fixture isolating logging globals.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: ``None``.
    """
    del isolated_cli_diagnostics
    terminal_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", terminal_stderr)
    ctx = cli_diagnostics.setup_cli_logging(["run", "agent.yaml"])
    cli_diagnostics.redirect_stderr_to_log()

    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        cli_diagnostics.log_cli_error_hint(exc)

    cli_diagnostics.restore_stderr()

    hint = terminal_stderr.getvalue()
    assert hint == f"Details logged to {ctx.path}\n", (
        f"fatal-error hint should print to the original stderr stream, got: {hint!r}"
    )
    log_text = ctx.path.read_text(encoding="utf-8")
    assert "Fatal CLI error: boom" in log_text, (
        f"fatal exception context was not written to the diagnostics log: {log_text!r}"
    )
    assert "Details logged to" not in log_text, (
        f"user-facing fatal-error hint should not be redirected into the log: {log_text!r}"
    )


def test_stale_host_hint_recommends_generic_stop_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tunnel rejection recovery should stop stale Omnigent processes."""
    terminal_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", terminal_stderr)

    cli_diagnostics.print_stale_host_hint()

    hint = terminal_stderr.getvalue()
    assert "runner tunnel rejection (HTTP 401)" in hint
    assert "stale host processes" in hint
    assert "`omnigent stop`" in hint
    assert "existing Omnigent host instances" in hint
    assert "omnigent setup" not in hint


def test_stale_host_hint_names_configured_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under a wrapper deployment the hint suggests the wrapper, not naked `omnigent`."""
    terminal_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", terminal_stderr)
    monkeypatch.setenv("OMNIGENT_WRAPPER_COMMAND", "isaac omni")

    cli_diagnostics.print_stale_host_hint()

    hint = terminal_stderr.getvalue()
    assert "`isaac omni stop`" in hint
    # The naked binary token must not be suggested when it would be refused.
    assert "`omnigent stop`" not in hint


def test_redirect_stderr_to_log_retargets_existing_logging_stderr_handlers(
    isolated_cli_diagnostics: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Existing stderr-backed logging handlers must follow TUI stderr redirect.

    :param isolated_cli_diagnostics: Fixture isolating logging globals.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: ``None``.
    """
    del isolated_cli_diagnostics
    terminal_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", terminal_stderr)
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(terminal_stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    databricks_logger = logging.getLogger("databricks.sdk")
    databricks_logger.handlers.clear()
    databricks_logger.setLevel(logging.WARNING)
    databricks_logger.propagate = True
    ctx = cli_diagnostics.setup_cli_logging(["run", "agent.yaml"])

    cli_diagnostics.redirect_stderr_to_log()
    databricks_logger.warning(
        "Databricks CLI v0.295.0 does not support --force-refresh "
        "(requires >= v0.296.0). The CLI's token cache may provide stale tokens."
    )
    cli_diagnostics.restore_stderr()
    databricks_logger.warning("after TUI")

    log_text = ctx.path.read_text(encoding="utf-8")
    warning_line = (
        "WARNING:databricks.sdk:Databricks CLI v0.295.0 does not support --force-refresh"
    )
    assert warning_line in log_text, (
        f"stderr-backed third-party logging should land in the CLI log: {log_text!r}"
    )
    assert "after TUI" not in log_text, (
        f"logging handlers should be restored after the TUI exits: {log_text!r}"
    )
    assert terminal_stderr.getvalue() == "WARNING:databricks.sdk:after TUI\n", (
        f"third-party logging painted into the terminal during TUI redirect: "
        f"{terminal_stderr.getvalue()!r}"
    )


def test_redirect_stderr_logs_open_failures(
    isolated_cli_diagnostics: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``redirect_stderr_to_log`` must record failures instead of swallowing them.

    :param isolated_cli_diagnostics: Fixture isolating logging globals.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: ``None``.
    """
    del isolated_cli_diagnostics
    terminal_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", terminal_stderr)
    ctx = cli_diagnostics.setup_cli_logging(["run", "agent.yaml"])

    def _raise_open_failure(*_args: object, **_kwargs: object) -> io.TextIOWrapper:
        """
        Stand in for ``open`` when the redirected stderr file cannot be opened.

        :param _args: Positional arguments passed by ``redirect_stderr_to_log``.
        :param _kwargs: Keyword arguments passed by ``redirect_stderr_to_log``.
        :returns: Never returns.
        :raises OSError: Always, to exercise the diagnostics path.
        """
        raise OSError("open failed")

    monkeypatch.setattr(cli_diagnostics, "open", _raise_open_failure, raising=False)

    cli_diagnostics.redirect_stderr_to_log()

    assert sys.stderr is terminal_stderr, (
        "redirect_stderr_to_log should leave stderr alone when opening the diagnostics file fails."
    )
    log_text = ctx.path.read_text(encoding="utf-8")
    assert "Failed to redirect stderr to CLI log: open failed" in log_text, (
        f"stderr redirect setup failures must be captured in the diagnostics log: {log_text!r}"
    )
    assert "Traceback" in log_text, (
        f"stderr redirect setup failures should include traceback context: {log_text!r}"
    )


def test_restore_stderr_logs_close_failures(
    isolated_cli_diagnostics: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``restore_stderr`` must log close failures after restoring the terminal.

    :param isolated_cli_diagnostics: Fixture isolating logging globals.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: ``None``.
    """
    del isolated_cli_diagnostics
    terminal_stderr = io.StringIO()
    ctx = cli_diagnostics.setup_cli_logging(["run", "agent.yaml"])
    monkeypatch.setattr(sys, "stderr", _FailingRedirectedStderr(terminal_stderr))

    cli_diagnostics.restore_stderr()

    assert sys.stderr is terminal_stderr, (
        "restore_stderr should restore the original terminal stream before "
        "closing the redirected stream."
    )
    log_text = ctx.path.read_text(encoding="utf-8")
    assert "Failed to close redirected stderr: close failed" in log_text, (
        f"redirected stderr close failures must be captured in the diagnostics log: {log_text!r}"
    )
    assert "Traceback" in log_text, (
        f"redirected stderr close failures should include traceback context: {log_text!r}"
    )


def test_main_logs_click_exceptions(
    isolated_cli_diagnostics: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    Click-handled command errors must still reach the diagnostics log.

    :param isolated_cli_diagnostics: Fixture isolating logging globals.
    :param monkeypatch: Pytest monkeypatch fixture.
    :param capsys: Pytest capture fixture for terminal stderr.
    :returns: ``None``.
    """
    del isolated_cli_diagnostics
    from omnigent import cli as cli_module

    # An unsupported --harness is a deterministic ClickException trigger that
    # raises before any daemon/network work. (A bare `omnigent run` no longer
    # errors — it drops into first-run `configure harnesses` — so it can't be
    # the trigger here.)
    monkeypatch.setattr(sys, "argv", ["omnigent", "run", "--harness", "not-a-real-harness"])
    # Isolate from any real ~/.omnigent/config.yaml on the developer's machine.
    monkeypatch.setattr(cli_module, "_load_global_config", dict)

    with pytest.raises(SystemExit) as exc_info:
        cli_module.main()

    assert exc_info.value.code == 1, (
        f"ClickException should preserve Click's exit code, got {exc_info.value.code!r}"
    )
    terminal = capsys.readouterr()
    assert "Error: Unsupported harness 'not-a-real-harness'" in terminal.err, (
        f"Click's normal user-facing error output changed: {terminal.err!r}"
    )
    path = cli_diagnostics.current_cli_log_path()
    assert path is not None, "main() should set up the active CLI diagnostics log."
    log_text = path.read_text(encoding="utf-8")
    assert "Click CLI error: Unsupported harness 'not-a-real-harness'" in log_text, (
        f"ClickException was not captured in the diagnostics log: {log_text!r}"
    )
    assert "Traceback" in log_text, (
        f"ClickException log entry should include traceback context: {log_text!r}"
    )


@pytest.mark.asyncio
async def test_slash_command_exceptions_reach_cli_log(
    isolated_cli_diagnostics: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    REPL slash-command exceptions must be recorded in the CLI diagnostics log.

    :param isolated_cli_diagnostics: Fixture isolating logging globals.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: ``None``.
    """
    del isolated_cli_diagnostics
    from omnigent_ui_sdk import RichBlockFormatter

    from omnigent.repl._repl import handle_slash_command
    from tests.repl.helpers import CapturingHost

    class _SessionWithoutModelSetter:
        """
        Session stub matching the broken adapter surface from the REPL.

        It intentionally exposes ``model_override`` and ``is_streaming``
        but not ``set_model_override`` so ``/model <name>`` raises the
        production AttributeError this regression covers.
        """

        model_override: str | None = None
        is_streaming = False

    terminal_stderr = io.StringIO()
    monkeypatch.setattr(sys, "stderr", terminal_stderr)
    ctx = cli_diagnostics.setup_cli_logging(["run", "agent.yaml"])
    host = CapturingHost()

    await handle_slash_command(
        "/model openai/gpt-5.4-mini",
        _SessionWithoutModelSetter(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        host,
        RichBlockFormatter(),
    )

    assert "Error: '_SessionWithoutModelSetter' object has no attribute" in host.text, (
        f"slash-command failures should still render inline for the user: {host.text!r}"
    )
    log_text = ctx.path.read_text(encoding="utf-8")
    assert "Slash command failed: /model" in log_text, (
        f"slash-command failures must be captured in the diagnostics log: {log_text!r}"
    )
    assert "AttributeError" in log_text, (
        f"slash-command diagnostics should include the exception type: {log_text!r}"
    )
    assert "openai/gpt-5.4-mini" not in log_text, (
        f"slash-command diagnostics should not copy command arguments into the log: {log_text!r}"
    )


def test_safe_mtime_returns_zero_for_vanished_file(tmp_path: Path) -> None:
    """A file present at glob time but gone before stat resolves to 0.0, not a raise."""
    real = tmp_path / "cli-real.log"
    real.write_text("x")
    assert cli_diagnostics._safe_mtime(real) > 0.0
    assert cli_diagnostics._safe_mtime(tmp_path / "cli-gone.log") == 0.0


def test_prune_old_logs_survives_file_vanishing_mid_sort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent ``omnigent run`` launches race to prune the same logs; a file
    globbed by one but deleted by the other must not crash the sort (previously a
    FileNotFoundError in the stat sort key aborted CLI startup)."""
    real = [tmp_path / f"cli-{i:03d}.log" for i in range(cli_diagnostics.MAX_LOG_FILES + 3)]
    for p in real:
        p.write_text("x")
    vanished = tmp_path / "cli-vanished.log"  # globbed, then deleted by a peer run
    monkeypatch.setattr(Path, "glob", lambda self, pattern: [*real, vanished])

    cli_diagnostics._prune_old_logs(tmp_path)  # must not raise

    surviving = [p for p in real if p.exists()]
    assert len(surviving) == cli_diagnostics.MAX_LOG_FILES  # newest kept, oldest pruned
