"""Tests for omnigent.inner.egress.aws_sigv4 — pure SigV4 resigning logic.

No proxy/asyncio involved: these exercise resign_request directly against
real botocore signing primitives.
"""

from __future__ import annotations

import datetime
import email.policy
from email.message import Message
from email.parser import BytesParser

import botocore.auth as botocore_auth
import pytest

from omnigent.inner.credential_proxy import AwsSigV4Credentials
from omnigent.inner.egress.aws_sigv4 import (
    UnsupportedAwsSigV4RequestError,
    has_presigned_query_auth,
    is_sigv4_authorization,
    resign_request,
)

_GARBAGE_AUTH = "AWS4-HMAC-SHA256 Credential=GARBAGE/x, SignedHeaders=host, Signature=deadbeef"


def _headers(values: dict[str, str]) -> Message:
    raw = "".join(f"{k}: {v}\r\n" for k, v in values.items()).encode() + b"\r\n"
    return BytesParser(policy=email.policy.HTTP).parsebytes(raw)


@pytest.fixture()
def pinned_clock(monkeypatch: pytest.MonkeyPatch):
    """Freeze botocore's signing clock to a fixed timestamp."""

    def _pin(dt: datetime.datetime) -> None:
        monkeypatch.setattr(botocore_auth, "get_current_datetime", lambda: dt)

    return _pin


def test_resign_matches_aws_published_get_object_vector(pinned_clock) -> None:
    """Reproduces AWS's published SigV4 GetObject-with-Range example byte-for-byte."""
    pinned_clock(datetime.datetime(2013, 5, 24, 0, 0, 0, tzinfo=datetime.timezone.utc))
    headers = _headers(
        {
            "Host": "examplebucket.s3.amazonaws.com",
            "x-amz-content-sha256": (
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            ),
            "Range": "bytes=0-9",
        }
    )
    credentials = AwsSigV4Credentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")

    result = resign_request(
        method="GET",
        url="https://examplebucket.s3.amazonaws.com/test.txt",
        headers=headers,
        body=b"",
        credentials=credentials,
        region="us-east-1",
        service="s3",
    )

    assert result["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


@pytest.mark.parametrize(
    "content_sha256",
    [
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "UNSIGNED-PAYLOAD",
        "STREAMING-UNSIGNED-PAYLOAD-TRAILER",
    ],
)
def test_resign_preserves_content_sha256_verbatim(content_sha256: str) -> None:
    """The client's X-Amz-Content-Sha256 -- hash or sentinel -- is never
    recomputed; it depends only on body content, never on which
    credentials signed the request."""
    headers = _headers(
        {
            "Host": "mybucket.s3.us-east-1.amazonaws.com",
            "x-amz-content-sha256": content_sha256,
            "Authorization": _GARBAGE_AUTH,
        }
    )
    credentials = AwsSigV4Credentials("AKIDREAL", "realsecret")

    result = resign_request(
        method="PUT",
        url="https://mybucket.s3.us-east-1.amazonaws.com/key",
        headers=headers,
        body=b"whatever bytes are actually being forwarded",
        credentials=credentials,
        region="us-east-1",
        service="s3",
    )

    assert result["X-Amz-Content-Sha256"] == content_sha256


def test_resign_strips_stale_headers_before_signing() -> None:
    """A security-token-less credential produces no X-Amz-Security-Token
    header at all in the output -- not merely "left unchanged"."""
    headers = _headers(
        {
            "Host": "b.s3.us-east-1.amazonaws.com",
            "x-amz-content-sha256": "UNSIGNED-PAYLOAD",
            "Authorization": _GARBAGE_AUTH,
            "X-Amz-Date": "20200101T000000Z",
            "X-Amz-Security-Token": "stale-garbage-token",
        }
    )
    credentials = AwsSigV4Credentials("AKIDREAL", "realsecret", session_token=None)

    result = resign_request(
        method="GET",
        url="https://b.s3.us-east-1.amazonaws.com/key",
        headers=headers,
        body=b"",
        credentials=credentials,
        region="us-east-1",
        service="s3",
    )

    assert result.get("X-Amz-Security-Token") is None
    assert "GARBAGE" not in result["Authorization"]
    assert result["X-Amz-Date"] != "20200101T000000Z"


def test_resign_includes_security_token_when_credentials_have_one() -> None:
    headers = _headers(
        {
            "Host": "b.s3.us-east-1.amazonaws.com",
            "x-amz-content-sha256": "UNSIGNED-PAYLOAD",
            "Authorization": _GARBAGE_AUTH,
        }
    )
    credentials = AwsSigV4Credentials("AKIDREAL", "realsecret", session_token="real-session-token")

    result = resign_request(
        method="GET",
        url="https://b.s3.us-east-1.amazonaws.com/key",
        headers=headers,
        body=b"",
        credentials=credentials,
        region="us-east-1",
        service="s3",
    )

    assert result["X-Amz-Security-Token"] == "real-session-token"
    assert "x-amz-security-token" in result["Authorization"].lower()


def test_resign_rejects_presigned_query_auth() -> None:
    headers = _headers({"Host": "b.s3.amazonaws.com"})
    credentials = AwsSigV4Credentials("AKIDREAL", "realsecret")

    with pytest.raises(UnsupportedAwsSigV4RequestError):
        resign_request(
            method="GET",
            url="https://b.s3.amazonaws.com/k?X-Amz-Signature=abc123",
            headers=headers,
            body=b"",
            credentials=credentials,
            region="us-east-1",
            service="s3",
        )


@pytest.mark.parametrize(
    "value,expected",
    [
        ("AWS4-HMAC-SHA256 Credential=x", True),
        ("aws4-hmac-sha256 Credential=x", True),
        ("Bearer sometoken", False),
        ("Basic dXNlcjpwYXNz", False),
        ("", False),
    ],
)
def test_is_sigv4_authorization(value: str, expected: bool) -> None:
    assert is_sigv4_authorization(value) is expected


@pytest.mark.parametrize(
    "query,expected",
    [
        ("X-Amz-Signature=abc", True),
        ("partNumber=1&uploadId=xyz", False),
        ("", False),
    ],
)
def test_has_presigned_query_auth(query: str, expected: bool) -> None:
    assert has_presigned_query_auth(query) is expected
