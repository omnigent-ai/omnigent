"""Signed enrollment state + identity matching for the Databricks web-auth flow.

Pure, I/O-free core of the enrollment flow — the aiohttp server that uses these
lives in :mod:`omnigent_slack.webauth`, and the whole design is in
``docs/DATABRICKS_APP_WEBAUTH_DESIGN.md``.

- :func:`sign_state` / :func:`verify_state` — bind a browser enrollment session
  to the Slack ``(team, user, email)`` that requested it. Signed (HMAC-SHA256)
  and time-bounded by a short TTL. It is **not** single-use: a valid state can
  be re-submitted within the TTL. That's benign because the callback also
  requires the browser's ``X-Forwarded-Email`` to equal the signed ``email``
  (:func:`emails_match`), so a replay can only re-store the *same* user's own
  token under their *own* Slack id — an idempotent no-op, not a way to plant a
  different identity's token.
- :func:`emails_match` — constant-time email comparison for that identity check.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

# The forwarded-user header the Databricks Apps proxy injects on an
# authenticated browser request (lowercased; HTTP headers are case-insensitive).
FORWARDED_ACCESS_TOKEN_HEADER = "x-forwarded-access-token"
FORWARDED_EMAIL_HEADER = "x-forwarded-email"

# How long a signed enrollment ``state`` stays valid. The user clicks the link
# and completes SSO within seconds; a tight window bounds how long a leaked link
# is usable (though the email-match check already makes replay same-identity).
_DEFAULT_STATE_TTL_SECONDS = 600


class StateError(RuntimeError):
    """A ``state`` token was malformed, tampered with, or expired."""


@dataclass(frozen=True, slots=True)
class EnrollmentState:
    """The Slack identity a browser enrollment session is bound to.

    ``email`` is the Slack user's email (from Slack's ``users.info``), signed
    into the state so the callback can require the proxy-authenticated browser's
    ``X-Forwarded-Email`` to match it. Without that check the callback would
    store *whoever's* browser token under the Slack id in the state — a
    confused-deputy: a link bound to Slack user A, opened by victim V, would
    capture V's token under A. Binding the email closes it in both directions.
    """

    team_id: str
    user_id: str
    email: str
    # Slack workspace display name, carried only so the enrollment page can show
    # the human which Slack workspace they linked. Not security-relevant.
    team_name: str
    issued_at: int


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(payload: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()


def sign_state(
    team_id: str,
    user_id: str,
    email: str,
    secret: str,
    *,
    team_name: str = "",
    issued_at: int | None = None,
) -> str:
    """Return a signed, URL-safe ``state`` binding a browser session to a Slack user.

    The payload carries the ``(team_id, user_id)``, the Slack user's ``email``,
    the workspace ``team_name`` (display only), and an issue time; the signature
    (HMAC-SHA256 over the payload) makes it unforgeable without the secret.
    :func:`verify_state` checks the signature and TTL, and the callback checks
    the browser's ``X-Forwarded-Email`` against ``email``. ``issued_at`` is
    injectable for tests; production stamps ``time.time()``.
    """
    issued = int(issued_at if issued_at is not None else time.time())
    payload = json.dumps(
        {"t": team_id, "u": user_id, "e": email, "n": team_name, "i": issued},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    signature = _sign(payload, secret)
    return f"{_b64url_encode(payload)}.{_b64url_encode(signature)}"


def verify_state(
    state: str,
    secret: str,
    *,
    ttl_seconds: int = _DEFAULT_STATE_TTL_SECONDS,
    now: int | None = None,
) -> EnrollmentState:
    """Validate a ``state`` from :func:`sign_state`, returning the bound identity.

    Raises :class:`StateError` if the token is malformed, the signature doesn't
    match (constant-time compare), or it is older than ``ttl_seconds``. ``now``
    is injectable for tests.
    """
    try:
        payload_b64, signature_b64 = state.split(".", 1)
        payload = _b64url_decode(payload_b64)
        signature = _b64url_decode(signature_b64)
    except (ValueError, TypeError) as exc:  # split / base64 decode failures
        raise StateError("Malformed enrollment token.") from exc

    expected = _sign(payload, secret)
    if not hmac.compare_digest(signature, expected):
        raise StateError("Enrollment token signature did not match.")

    try:
        data = json.loads(payload)
        team_id = str(data["t"])
        user_id = str(data["u"])
        email = str(data["e"])
        team_name = str(data.get("n", ""))
        issued_at = int(data["i"])
    except (ValueError, KeyError, TypeError) as exc:
        raise StateError("Malformed enrollment token payload.") from exc

    current = int(now if now is not None else time.time())
    if current - issued_at > ttl_seconds:
        raise StateError("Enrollment link expired. Start again from Slack.")
    if issued_at - current > ttl_seconds:
        # Clock skew / future-dated token — reject rather than trust it.
        raise StateError("Enrollment token is not yet valid.")

    return EnrollmentState(
        team_id=team_id,
        user_id=user_id,
        email=email,
        team_name=team_name,
        issued_at=issued_at,
    )


def emails_match(a: str, b: str) -> bool:
    """Case-insensitive, whitespace-trimmed email equality (constant-time).

    Emails are case-insensitive in their domain (and, in practice, IdPs treat
    the local part that way too), so compare normalized. Constant-time to avoid
    leaking match progress, though these values aren't secret.
    """
    return hmac.compare_digest(a.strip().casefold(), b.strip().casefold())
