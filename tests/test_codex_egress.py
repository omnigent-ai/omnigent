"""Classification of Codex egress failures the launcher names and Codex only retries."""

from __future__ import annotations

import pytest

from omnigent.harnesses.codex_egress import (
    CERTIFICATE_REMEDIATION,
    certificate_failure_message,
    connection_retry_detail,
    detect_certificate_failure,
    is_connection_failure_text,
    is_connection_retry,
)

_DBCERT_LINE = (
    "Failed to fetch safe flags from proxy: [SSL: SSLV3_ALERT_CERTIFICATE_EXPIRED] "
    "ssl/tls alert certificate expired (_ssl.c:2580)"
)


@pytest.mark.parametrize(
    ("line", "expired"),
    [
        (_DBCERT_LINE, True),
        ("Missing/Expired Certificate", True),
        ("\x1b[31mMissing/Expired Certificate\x1b[0m Please run 'dbcert' on your laptop", True),
        ("error sending request: invalid peer certificate: Expired", True),
        ("the server certificate has expired", True),
        (
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            "unable to get local issuer certificate",
            False,
        ),
        ("invalid peer certificate: UnknownIssuer", False),
        ("ssl/tls alert certificate required", False),
        ("self-signed certificate in certificate chain", False),
    ],
)
def test_detect_certificate_failure_names_certificate_problems(line: str, expired: bool) -> None:
    failure = detect_certificate_failure(line)
    assert failure is not None
    assert failure.expired is expired
    assert "\x1b" not in failure.evidence


@pytest.mark.parametrize(
    "line",
    [
        "",
        "Reconnecting... 3/5",
        "ERROR: unexpected status 401 Unauthorized: {}, url: https://h/x",
        "token expired; run codex login",
        "INFO codex_http_client::custom_ca: using system root certificates because no CA "
        "override environment variable was selected",
        "WARN failed to warm featured plugin ids cache: error sending request for url "
        "(https://chatgpt.com/backend-api/plugins/featured)",
    ],
)
def test_detect_certificate_failure_ignores_other_output(line: str) -> None:
    assert detect_certificate_failure(line) is None


def test_detect_certificate_failure_redacts_and_bounds_evidence() -> None:
    line = "certificate expired for https://user:secret@proxy.example/v1 " + "x" * 1000
    failure = detect_certificate_failure(line)
    assert failure is not None
    assert "secret" not in failure.evidence
    assert "https://[REDACTED]@proxy.example/v1" in failure.evidence
    assert len(failure.evidence) <= 300


def _retry(error: dict[str, object], *, will_retry: bool = True) -> dict[str, object]:
    return {"threadId": "thread_1", "turnId": "turn_1", "willRetry": will_retry, "error": error}


# What codex-cli 0.154 emits when the TLS handshake to the model endpoint fails.
_CONNECTION_FAILED: dict[str, object] = {
    "message": "Reconnecting... waiting for network",
    "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": None}},
    "additionalDetails": "Connection failed: error sending request",
}


def test_is_connection_retry_recognizes_reconnect_without_http_status() -> None:
    assert is_connection_retry(_retry(_CONNECTION_FAILED)) is True


@pytest.mark.parametrize(
    "params",
    [
        _retry(_CONNECTION_FAILED, will_retry=False),
        _retry(
            {
                **_CONNECTION_FAILED,
                "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": 503}},
            }
        ),
        _retry({"message": "Rate limited", "codexErrorInfo": {"httpStatusCode": 429}}),
        _retry(
            {"message": "You've hit your usage limit.", "codexErrorInfo": "usageLimitExceeded"}
        ),
        {"willRetry": True},
    ],
)
def test_is_connection_retry_rejects_requests_that_reached_the_endpoint(
    params: dict[str, object],
) -> None:
    assert is_connection_retry(params) is False


def test_is_connection_retry_falls_back_to_message_text() -> None:
    assert is_connection_retry(_retry({"message": "Reconnecting... waiting for network"})) is True
    assert (
        is_connection_retry(_retry({"message": "stream closed before response.completed"}))
        is False
    )


def test_connection_retry_detail_names_codex_reason() -> None:
    assert connection_retry_detail(_retry(_CONNECTION_FAILED)) == (
        "Codex is reconnecting to its model endpoint (Reconnecting... waiting for network: "
        "Connection failed: error sending request)"
    )
    assert connection_retry_detail({"willRetry": True}) == (
        "Codex is reconnecting to its model endpoint (no detail reported)"
    )


def test_certificate_failure_message_names_model_cause_and_evidence() -> None:
    failure = detect_certificate_failure(_DBCERT_LINE)
    assert failure is not None
    assert certificate_failure_message(failure, model="gpt-5") == (
        "Codex could not connect to its model endpoint for gpt-5: the TLS certificate has "
        f"expired (stderr: {_DBCERT_LINE})."
    )
    assert certificate_failure_message(failure).startswith(
        "Codex could not connect to its model endpoint: the TLS certificate has expired"
    )
    assert "run dbcert" in CERTIFICATE_REMEDIATION


def test_connection_retry_detail_redacts_and_bounds_codex_text() -> None:
    params = _retry(
        {
            "message": "Reconnecting... waiting for network",
            "additionalDetails": (
                "error sending request for url (https://user:secret@proxy.example/v1) "
                + "x" * 1000
            ),
        }
    )
    detail = connection_retry_detail(params)
    assert "secret" not in detail
    assert "https://[REDACTED]@proxy.example/v1" in detail
    assert len(detail) <= 300 + len("Codex is reconnecting to its model endpoint ()")


def test_is_connection_failure_text_matches_connection_level_wording() -> None:
    assert is_connection_failure_text("stream disconnected: error sending request") is True
    assert is_connection_failure_text("Connection failed: error trying to connect") is True
    assert is_connection_failure_text("tool exited with code 1") is False
    assert is_connection_failure_text(None) is False


def test_certificate_failure_message_keeps_codex_text() -> None:
    failure = detect_certificate_failure(_DBCERT_LINE)
    assert failure is not None
    message = certificate_failure_message(failure, codex_error="stream disconnected")
    assert message.endswith("). Codex reported: stream disconnected")
