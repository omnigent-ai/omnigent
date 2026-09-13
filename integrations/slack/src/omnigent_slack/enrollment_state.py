"""Signed enrollment state + identity matching for the Databricks OAuth flow.

Pure, I/O-free core of the enrollment flow — the aiohttp server that uses these
lives in :mod:`omnigent_slack.webauth`, and the whole design is in
``docs/DATABRICKS_APP_WEBAUTH_DESIGN.md``.

- :func:`sign_state` / :func:`verify_state` — the ``state`` round-tripped through
  the Databricks authorization-code redirect. It binds the browser session to
  the Slack ``(team, user, email)`` that requested it and carries a single-use
  ``nonce`` the callback uses to look up the PKCE ``code_verifier`` the bot
  generated (the verifier itself never travels in the state — only its lookup
  key). Signed (HMAC-SHA256) and TTL-bounded. The nonce is consumed on use, so
  a replayed redirect finds no verifier and is refused; the callback also
  requires the OAuth-authenticated email to equal the signed ``email``
  (:func:`emails_match`), closing the confused-deputy.
- :func:`emails_match` — constant-time email comparison for that identity check.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

# How long a signed enrollment ``state`` stays valid. The user clicks the link
# and completes SSO within seconds; a tight window bounds how long a leaked link
# is usable (though the nonce makes replay a no-op and the email-match check
# makes it same-identity anyway).
_DEFAULT_STATE_TTL_SECONDS = 600


def new_nonce() -> str:
    """A random, single-use lookup key tying a ``state`` to its PKCE verifier."""
    return secrets.token_urlsafe(24)


class StateError(RuntimeError):
    """A ``state`` token was malformed, tampered with, or expired."""


@dataclass(frozen=True, slots=True)
class EnrollmentState:
    """The Slack identity a browser enrollment session is bound to.

    ``email`` is the Slack user's email (from Slack's ``users.info``), signed
    into the state so the callback can require the OAuth-authenticated browser's
    email to match it. Without that check the callback would store *whoever's*
    token under the Slack id in the state — a confused-deputy: a link bound to
    Slack user A, opened by victim V, would capture V's token under A. Binding
    the email closes it in both directions. ``nonce`` is the single-use key the
    callback uses to fetch the PKCE ``code_verifier`` the bot stored server-side.
    """

    team_id: str
    user_id: str
    email: str
    # Slack workspace display name, carried only so the enrollment page can show
    # the human which Slack workspace they linked. Not security-relevant.
    team_name: str
    # Single-use lookup key for the PKCE code_verifier held server-side.
    nonce: str
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
    nonce: str,
    team_name: str = "",
    issued_at: int | None = None,
) -> str:
    """Return a signed, URL-safe ``state`` binding a browser session to a Slack user.

    The payload carries the ``(team_id, user_id)``, the Slack user's ``email``,
    the workspace ``team_name`` (display only), the single-use ``nonce`` (PKCE
    verifier lookup key), and an issue time; the signature (HMAC-SHA256 over the
    payload) makes it unforgeable without the secret. :func:`verify_state`
    checks the signature and TTL, and the callback checks the OAuth-authenticated
    email against ``email``. ``issued_at`` is injectable for tests; production
    stamps ``time.time()``.
    """
    issued = int(issued_at if issued_at is not None else time.time())
    payload = json.dumps(
        {"t": team_id, "u": user_id, "e": email, "n": team_name, "c": nonce, "i": issued},
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
        nonce = str(data["c"])
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
        nonce=nonce,
        issued_at=issued_at,
    )


def emails_match(a: str, b: str) -> bool:
    """Case-insensitive, whitespace-trimmed email equality (constant-time).

    Emails are case-insensitive in their domain (and, in practice, IdPs treat
    the local part that way too), so compare normalized. Constant-time to avoid
    leaking match progress, though these values aren't secret. The casefolded
    strings are UTF-8 encoded before comparison because ``hmac.compare_digest``
    rejects ``str`` inputs containing non-ASCII characters — an internationalized
    email would otherwise raise ``TypeError`` and 500 the callback.
    """
    return hmac.compare_digest(
        a.strip().casefold().encode("utf-8"), b.strip().casefold().encode("utf-8")
    )
