"""Tests for the credential-rejection detection that keeps an expired
token from being auto-reported as a crash (issue #3231)."""

from __future__ import annotations

import pytest

from omnigent.cli import credential_rejection_hint


class _ServerStyleError(Exception):
    """Stand-in for a server-side error that carries ``http_status``."""

    def __init__(self, http_status: int) -> None:
        super().__init__("boom")
        self.http_status = http_status


@pytest.mark.parametrize("status", [401, 403])
def test_hint_for_auth_status_codes(status: int) -> None:
    """A 401/403 client error yields a re-login hint naming ``omnigent login``."""
    from omnigent_client import OmnigentError

    hint = credential_rejection_hint(OmnigentError("Invalid access token", status))
    assert hint is not None
    assert "omnigent login" in hint
    assert str(status) in hint


def test_hint_reads_http_status_when_no_status_code() -> None:
    """Server-side errors expose ``http_status`` rather than ``status_code``."""
    hint = credential_rejection_hint(_ServerStyleError(403))
    assert hint is not None
    assert "omnigent login" in hint


@pytest.mark.parametrize("status", [400, 404, 409, 500])
def test_no_hint_for_non_auth_status(status: int) -> None:
    """Non-auth HTTP errors fall through to the normal crash handler (None)."""
    from omnigent_client import OmnigentError

    assert credential_rejection_hint(OmnigentError("nope", status)) is None


def test_no_hint_for_plain_exception() -> None:
    """A generic exception with no status is not a credential rejection."""
    assert credential_rejection_hint(ValueError("unrelated")) is None


def test_no_hint_when_status_missing() -> None:
    """An OmnigentError raised without a status is treated as a real crash."""
    from omnigent_client import OmnigentError

    assert credential_rejection_hint(OmnigentError("no status")) is None


def test_hint_detects_the_real_3231_payload() -> None:
    """
    The exact error shape from issue #3231 is recognized as a credential
    rejection.

    A proxy/auth 403 whose body is not the ``{"error": {...}}`` envelope
    makes the SDK stringify the whole body as the message while still
    carrying ``status_code=403``. Reproduce that through the real SDK
    ``raise_for_status`` so a change to the client's error shape can't
    silently regress the detection.
    """
    from omnigent_client import OmnigentError
    from omnigent_client._errors import raise_for_status

    body = {
        "error_code": 403,
        "message": "Invalid access token. [ReqId: 145ae51b-da74-40a7-ac56-0ad190834faf]",
    }
    with pytest.raises(OmnigentError) as excinfo:
        raise_for_status(403, body)

    hint = credential_rejection_hint(excinfo.value)
    assert hint is not None
    assert "omnigent login" in hint
