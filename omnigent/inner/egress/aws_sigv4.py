"""AWS SigV4 request re-signing for the secretless credential proxy.

Rebuilds a request's SigV4 signature (``Authorization``, ``X-Amz-Date`` /
``Date``, ``X-Amz-Security-Token``) from scratch using the real
credentials, over the literal ``(method, url, headers, body)`` forwarded
upstream. The body — and its ``X-Amz-Content-Sha256`` declaration, whatever
mode it's in — is never touched: see :func:`resign_request` for why.

Wraps :mod:`botocore.auth` as the actual signing crypto rather than
hand-rolling the HMAC canonical-request algorithm. ``botocore`` is a lazy
import (:func:`_ensure_botocore`), gated behind the ``s3`` extra, matching
:mod:`omnigent.stores.artifact_store.s3`'s ``_ensure_boto3`` convention —
importing this module never requires ``botocore`` unless
:func:`resign_request` is actually called.
"""

from __future__ import annotations

import copy
from email.message import Message
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from omnigent.inner.credential_proxy import AwsSigV4Credentials

# Auth-related headers that always come from the resign step, never from
# the client's (garbage-signed) request — deleted unconditionally before
# signing so no stale artifact of any kind survives, then re-set only for
# the ones the real signing pass actually produces.
_AUTH_MANAGED_HEADERS = ("Authorization", "X-Amz-Date", "Date", "X-Amz-Security-Token")


class UnsupportedAwsSigV4RequestError(Exception):
    """Raised for a SigV4 request shape this proxy refuses to resign.

    Only presigned query-string auth (``?X-Amz-Signature=...``) — a
    different signing mode, not produced by ordinary ``boto3`` API calls —
    falls in this category. Chunked/streaming payloads (the default for
    S3 uploads) are NOT rejected: see the module and :func:`resign_request`
    docstrings for why they need no special handling.
    """


def is_sigv4_authorization(value: str) -> bool:
    """
    Whether *value* (an ``Authorization`` header value) is SigV4-shaped.

    :param value: The header value, e.g.
        ``"AWS4-HMAC-SHA256 Credential=..., SignedHeaders=..., Signature=..."``.
    :returns: ``True`` iff it case-insensitively starts with
        ``"AWS4-HMAC-SHA256 "``.
    """
    return value.strip().upper().startswith("AWS4-HMAC-SHA256 ")


def has_presigned_query_auth(query: str) -> bool:
    """
    Whether *query* carries presigned SigV4 auth.

    :param query: A URL query string (without the leading ``?``).
    :returns: ``True`` iff it contains ``"X-Amz-Signature="``.
    """
    return "X-Amz-Signature=" in query


def _ensure_botocore() -> None:
    """
    Verify that ``botocore`` is installed.

    :raises ImportError: If the package is not available.
    """
    try:
        import botocore  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "AWS SigV4 credential-proxy resigning requires 'botocore'. "
            "Install with: pip install botocore (or 'pip install omnigent[s3]')."
        ) from exc


def resign_request(
    *,
    method: str,
    url: str,
    headers: Message,
    body: bytes,
    credentials: AwsSigV4Credentials,
    region: str,
    service: str = "s3",
) -> Message:
    """
    Rebuild a request's SigV4 auth headers with the real credentials.

    Returns a **new** :class:`~email.message.Message` with
    ``Authorization`` / ``X-Amz-Date`` / ``Date`` / ``X-Amz-Security-Token``
    replaced. Every other header — including ``X-Amz-Content-Sha256``,
    ``Content-Encoding``, ``X-Amz-Trailer``,
    ``X-Amz-Decoded-Content-Length``, ``x-amz-acl``, ``x-amz-meta-*``, etc.
    — is forwarded exactly as received, and *body* is never re-encoded.

    **Why the body and ``X-Amz-Content-Sha256`` are never touched.** A
    naive resign would recompute the payload hash from *body*. That's
    wrong: current AWS S3 clients default to chunked uploads
    (``Content-Encoding: aws-chunked`` with
    ``X-Amz-Content-Sha256: STREAMING-UNSIGNED-PAYLOAD-TRAILER``) for
    *every* upload regardless of size — a checksum-driven default, not a
    size threshold. In that mode *body* is aws-chunked wire framing
    (``<hex-len>\\r\\n<data>\\r\\n`` chunks plus a trailing
    non-cryptographic checksum), and hashing it directly would hash the
    framing, not the payload — and would be pointless anyway, since this
    streaming mode doesn't bind the SigV4 signature to the payload at all
    (that's the point of "unsigned payload": TLS plus the trailing
    checksum cover integrity instead). The value already present in
    ``X-Amz-Content-Sha256`` — whether a real hex hash, ``UNSIGNED-PAYLOAD``,
    or the streaming-trailer sentinel — depends only on body content, never
    on which credentials signed the request, so the original client's
    value (even though signed with placeholder keys) is already correct
    and must be preserved verbatim. This is also why chunked and
    non-chunked uploads need no special-casing here: forwarding the body
    unchanged and preserving this one header is the same code path either
    way. (There is no per-chunk cryptographic signature chain to
    reproduce in the modern streaming-trailer mode — that was the older,
    now-unused classic streaming mode.)

    :param method: HTTP method, e.g. ``"PUT"``.
    :param url: Full request URL including query string, e.g.
        ``"https://mybucket.s3.us-east-1.amazonaws.com/key?partNumber=1"``.
    :param headers: The client's (garbage-signed) request headers, parsed.
        Not mutated — a copy is returned.
    :param body: The literal bytes forwarded upstream, whatever framing.
    :param credentials: The real credential to sign with.
    :param region: AWS region for the credential scope.
    :param service: SigV4 service name.
    :returns: A new :class:`~email.message.Message` with the auth-managed
        headers replaced.
    :raises UnsupportedAwsSigV4RequestError: If *url* carries presigned
        query-string auth.
    :raises ImportError: If ``botocore`` is not installed.
    """
    if has_presigned_query_auth(urlsplit(url).query):
        raise UnsupportedAwsSigV4RequestError(
            "presigned query-string auth (X-Amz-Signature in the query "
            "string) is not supported by the aws_sigv4 credential proxy — "
            "only header-based SigV4 requests (the shape a boto3 client "
            "produces) can be resigned"
        )

    _ensure_botocore()
    from botocore.auth import S3SigV4Auth, SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    class _PreserveContentSha256S3SigV4Auth(S3SigV4Auth):  # type: ignore[misc]
        """``S3SigV4Auth`` that leaves ``X-Amz-Content-Sha256`` untouched.

        ``S3SigV4Auth._modify_request_before_signing`` unconditionally
        deletes and recomputes ``X-Amz-Content-Sha256`` from
        ``request.body`` — see :func:`resign_request`'s docstring for why
        that would be wrong here. Delegating to the grandparent
        ``SigV4Auth``'s ``_modify_request_before_signing`` (which only
        touches this header when ``payload_signing_enabled`` is explicitly
        disabled in ``request.context`` — never set here) skips that
        rewrite while keeping ``S3SigV4Auth``'s other correct S3-specific
        behavior, notably no URL path normalization (required for S3 keys
        with special characters).
        """

        def _modify_request_before_signing(self, request: object) -> None:
            SigV4Auth._modify_request_before_signing(self, request)  # type: ignore[missing-attribute]

    filtered_headers = {
        name: value
        for name, value in headers.items()
        if name.lower() not in {h.lower() for h in _AUTH_MANAGED_HEADERS}
    }
    aws_request = AWSRequest(method=method, url=url, data=body, headers=filtered_headers)
    signing_credentials = Credentials(
        credentials.access_key_id,
        credentials.secret_access_key,
        credentials.session_token,
    )
    signer = _PreserveContentSha256S3SigV4Auth(signing_credentials, service, region)
    signer.add_auth(aws_request)

    resigned = copy.deepcopy(headers)
    for name in _AUTH_MANAGED_HEADERS:
        del resigned[name]
    for name in _AUTH_MANAGED_HEADERS:
        value = aws_request.headers.get(name)
        if value is not None:
            resigned[name] = value
    return resigned
