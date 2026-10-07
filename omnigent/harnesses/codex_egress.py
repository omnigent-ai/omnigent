"""Name a Codex model-egress failure that Codex itself reports only as a retry.

An expired or untrusted TLS certificate makes Codex retry indefinitely
("Reconnecting... waiting for network") with no terminal turn event and no
cause in the notification. The launcher that started ``codex`` prints the
cause to stderr; pairing the two lets a turn fail fast with the real reason.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from omnigent.harnesses.diagnostics import sanitize_diagnostic_text

CERTIFICATE_FAILURE_CODE = "model_endpoint_certificate_rejected"
CERTIFICATE_FAILURE_TITLE = "Codex can't reach its model endpoint"
CERTIFICATE_REMEDIATION = (
    "Renew the TLS certificate used to reach the model endpoint (on a Databricks "
    "laptop, run dbcert), then send your message again."
)
_EVIDENCE_LIMIT = 300

_CERTIFICATE_EXPIRED = re.compile(
    r"certificate[_ ]?(?:has[_ ]|is[_ ])?expired"
    r"|expired[_ /]*certificate"
    r"|peer certificate:\s*expired",
    re.IGNORECASE,
)
_CERTIFICATE_REJECTED = re.compile(
    r"certificate[_ ]verify[_ ]failed"
    r"|unable to get local issuer certificate"
    r"|self[- ]signed certificate"
    r"|invalid peer certificate"
    r"|unknown[_ ]?(?:ca|issuer)\b"
    r"|certificate[_ ](?:is[_ ])?(?:not[_ ]trusted|unknown|revoked|invalid|rejected|required)"
    r"|(?:bad|missing)[_ /]+certificate",
    re.IGNORECASE,
)
# Fallback for retry notifications without a structured ``httpStatusCode``.
_CONNECTION_FAILURE_FRAGMENTS = (
    "waiting for network",
    "connection failed",
    "error sending request",
    "error trying to connect",
    "connection refused",
    "connection reset",
    "failed to connect",
    "network is unreachable",
)
_NO_STATUS_FIELD = object()


@dataclass(frozen=True)
class CertificateFailure:
    """A TLS certificate failure a Codex launcher printed to stderr.

    :param evidence: The sanitized stderr line, e.g. ``"Failed to fetch safe
        flags from proxy: [SSL: SSLV3_ALERT_CERTIFICATE_EXPIRED] ssl/tls alert
        certificate expired (_ssl.c:2580)"``.
    :param expired: Whether the line names an expired certificate rather than
        another trust failure.
    """

    evidence: str
    expired: bool

    @property
    def cause(self) -> str:
        """:returns: e.g. ``"the TLS certificate has expired"``."""
        if self.expired:
            return "the TLS certificate has expired"
        return "the TLS certificate was rejected"


def detect_certificate_failure(text: str | None) -> CertificateFailure | None:
    """Return the TLS certificate failure a stderr line reports, if any.

    Only lines naming a certificate problem count; an expired token or a
    gateway 401 is an auth failure, not a transport one.

    :param text: One stderr line.
    :returns: The failure, or ``None`` for ordinary output.
    """
    if not text:
        return None
    expired = _CERTIFICATE_EXPIRED.search(text) is not None
    if not expired and _CERTIFICATE_REJECTED.search(text) is None:
        return None
    evidence = " ".join(sanitize_diagnostic_text(text).split())
    return CertificateFailure(evidence=evidence[:_EVIDENCE_LIMIT], expired=expired)


def _http_status_from_error_info(info: object) -> object:
    """Return ``codexErrorInfo``'s HTTP status, or :data:`_NO_STATUS_FIELD` when it has none."""
    if not isinstance(info, Mapping):
        return _NO_STATUS_FIELD
    if "httpStatusCode" in info:
        return info.get("httpStatusCode")
    for value in info.values():
        if isinstance(value, Mapping) and "httpStatusCode" in value:
            return value.get("httpStatusCode")
    return _NO_STATUS_FIELD


def is_connection_retry(params: Mapping[str, object]) -> bool:
    """Whether a Codex ``error`` notification retries a request that got no HTTP response.

    A retry whose ``codexErrorInfo`` carries an HTTP status reached the endpoint
    over TLS, so a certificate cannot be what blocks it; a null status (or, for
    older shapes, connection-failure wording) is connection-level.

    :param params: The notification params, which must carry ``willRetry``.
    :returns: ``True`` for a connection-level retry.
    """
    if params.get("willRetry") is not True:
        return False
    error = params.get("error")
    if not isinstance(error, Mapping):
        return False
    status = _http_status_from_error_info(error.get("codexErrorInfo"))
    if status is not _NO_STATUS_FIELD:
        return status is None
    return is_connection_failure_text(
        " ".join(str(error.get(key) or "") for key in ("message", "additionalDetails"))
    )


def is_connection_failure_text(text: str | None) -> bool:
    """Whether Codex error text describes a request that never reached the endpoint.

    :param text: Error text, e.g. ``"Connection failed: error sending request"``.
    :returns: ``True`` for connection-level wording.
    """
    lowered = (text or "").lower()
    return any(fragment in lowered for fragment in _CONNECTION_FAILURE_FRAGMENTS)


def connection_retry_detail(params: Mapping[str, object]) -> str:
    """Describe a connection retry for the idle-watchdog failure reason.

    :param params: The ``error`` notification params.
    :returns: e.g. ``"Codex is reconnecting to its model endpoint (Reconnecting...
        waiting for network: Connection failed: error sending request)"``.
    """
    error = params.get("error")
    message = details = ""
    if isinstance(error, Mapping):
        message = str(error.get("message") or "").strip()
        details = str(error.get("additionalDetails") or "").strip()
    text = ": ".join(part for part in (message, details) if part) or "no detail reported"
    text = " ".join(sanitize_diagnostic_text(text).split())[:_EVIDENCE_LIMIT]
    return f"Codex is reconnecting to its model endpoint ({text})"


def certificate_failure_message(
    failure: CertificateFailure,
    *,
    model: str | None = None,
    codex_error: str | None = None,
) -> str:
    """The user-facing cause for a turn Codex could not get past a bad certificate.

    :param failure: The failure read off the launcher's stderr.
    :param model: The model the turn ran with, e.g. ``"gpt-5"``, or ``None``.
    :param codex_error: Codex's own failure text for the turn, kept alongside
        the certificate cause, or ``None``.
    :returns: e.g. ``"Codex could not connect to its model endpoint for gpt-5:
        the TLS certificate has expired (stderr: ...)."``
    """
    target = f" for {model}" if model else ""
    message = (
        f"Codex could not connect to its model endpoint{target}: "
        f"{failure.cause} (stderr: {failure.evidence})."
    )
    if codex_error:
        message = f"{message} Codex reported: {codex_error}"
    return message
