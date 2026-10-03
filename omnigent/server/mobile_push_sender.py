"""FCM HTTP v1 transport; provider responses and credentials are never logged."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from math import ceil
from time import monotonic
from typing import Any, Literal

import httpx

from omnigent.server.mobile_push_config import TOKEN_ENDPOINT, FcmConfig

_FCM_ERROR = "type.googleapis.com/google.firebase.fcm.v1.FcmError"
_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SendResult:
    outcome: Literal["sent", "prune", "retry", "discard", "refresh"]
    retry_after: int = 0


@dataclass
class FcmSender:
    config: FcmConfig
    client: httpx.AsyncClient = field(repr=False)
    _cached_token: str = field(default="", repr=False)
    _refresh_at: float = field(default=0.0, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _blocked_until: float = field(default=0.0, repr=False)
    _blocked_status: int = field(default=503, repr=False)
    _warning_at: dict[tuple[int, bool], float] = field(default_factory=dict, repr=False)

    def warn_auth_failure(self, status: int, *, third_party: bool = False) -> None:
        stamp = monotonic()
        failure_class = (status, third_party)
        if stamp >= self._warning_at.get(failure_class, float("-inf")) + 60:
            if third_party:
                _logger.warning(
                    "Mobile push authorization failed: status=%s errorCode=THIRD_PARTY_AUTH_ERROR",
                    status,
                )
            else:
                _logger.warning("Mobile push authorization failed: status=%s", status)
            self._warning_at[failure_class] = stamp

    def _backoff(self, status: int, retry_after: int) -> None:
        deadline = monotonic() + retry_after
        if deadline > self._blocked_until:
            self._blocked_status = status
        self._blocked_until = max(self._blocked_until, deadline)

    def _auth_failure(self, status: int, retry_after: int = 60) -> None:
        self._backoff(status, retry_after)
        self._cached_token = ""
        self.warn_auth_failure(status)

    async def access_token(self) -> str:
        async with self._lock:
            if monotonic() < self._blocked_until:
                raise _OAuthFailure(self._blocked_status, ceil(self._blocked_until - monotonic()))
            if self._cached_token and monotonic() < self._refresh_at:
                return self._cached_token
            response = await self.client.post(
                TOKEN_ENDPOINT,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": self.config.assertion(),
                },
                follow_redirects=False,
            )
            if response.status_code != 200:
                retry_after = _retry_after(response)
                if response.status_code in {400, 401, 403}:
                    self._auth_failure(response.status_code, max(60, retry_after))
                elif (
                    response.status_code == 429 or response.status_code >= 500
                ) and retry_after > 0:
                    self._backoff(response.status_code, retry_after)
                raise _OAuthFailure(response.status_code, retry_after)
            body = response.json()
            token = body.get("access_token")
            expires = body.get("expires_in")
            if (
                not isinstance(token, str)
                or not token
                or not isinstance(expires, (int, float))
                or expires <= 0
            ):
                raise _OAuthFailure(503, 0)
            self._cached_token = token
            self._refresh_at = monotonic() + max(0, min(expires, 3600) - 60)
            return token

    async def authorization(self) -> str | SendResult:
        try:
            return await self.access_token()
        except httpx.HTTPError:
            return SendResult("retry")
        except _OAuthFailure as error:
            auth_error = error.status in {400, 401, 403}
            minimum_delay = 60 if auth_error else 0
            if auth_error:
                remaining = ceil(self._blocked_until - monotonic())
                if remaining > 0:
                    minimum_delay = remaining
            return SendResult(
                "retry" if auth_error or error.status == 429 or error.status >= 500 else "discard",
                max(minimum_delay, error.retry_after),
            )
        except (ValueError, TypeError, AttributeError):
            return SendResult("retry")

    async def post(self, payload: dict[str, Any], token: str) -> SendResult:
        try:
            response = await self.client.post(
                self.config.send_endpoint,
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
                follow_redirects=False,
            )
        except httpx.HTTPError:
            return SendResult("retry")
        except (ValueError, TypeError, AttributeError):
            return SendResult("retry")
        if response.is_success:
            return SendResult("sent")
        if response.status_code == 429 or response.status_code >= 500:
            retry_after = _retry_after(response)
            if retry_after > 0:
                self._backoff(response.status_code, retry_after)
            return SendResult("retry", retry_after)
        try:
            provider_error = response.json().get("error", {})
            details = provider_error.get("details", [])
        except (ValueError, AttributeError):
            provider_error = {}
            details = []
        if not isinstance(provider_error, dict):
            provider_error = {}
        if response.status_code == 401:
            if isinstance(details, list) and any(
                isinstance(detail, dict)
                and detail.get("@type") == _FCM_ERROR
                and detail.get("errorCode") == "THIRD_PARTY_AUTH_ERROR"
                for detail in details
            ):
                self.warn_auth_failure(401, third_party=True)
                return SendResult("discard")
            if self._cached_token == token:
                self._cached_token = ""
            return SendResult("refresh")
        if response.status_code in {400, 403, 404} and isinstance(details, list):
            for detail in details:
                if not isinstance(detail, dict):
                    continue
                if detail.get("@type") == _FCM_ERROR and detail.get("errorCode") in {
                    "UNREGISTERED",
                    "SENDER_ID_MISMATCH",
                }:
                    return SendResult("prune")
                if (
                    response.status_code == 400
                    and detail.get("@type") == _FCM_ERROR
                    and detail.get("errorCode") == "INVALID_ARGUMENT"
                    and isinstance(provider_error.get("message"), str)
                    and "registration token is not a valid FCM registration token"
                    in provider_error["message"]
                ):
                    return SendResult("prune")
                if (
                    response.status_code == 400
                    and detail.get("@type") == "type.googleapis.com/google.rpc.BadRequest"
                ):
                    violations = detail.get("fieldViolations", [])
                    if isinstance(violations, list) and any(
                        isinstance(item, dict) and item.get("field") == "message.token"
                        for item in violations
                    ):
                        return SendResult("prune")
        if response.status_code == 403 and provider_error.get("status") == "PERMISSION_DENIED":
            retry_after = max(60, _retry_after(response))
            self._auth_failure(403, retry_after)
            return SendResult("retry", retry_after)
        return SendResult("discard")


class _OAuthFailure(Exception):
    def __init__(self, status: int, retry_after: int) -> None:
        super().__init__("FCM authorization failed")
        self.status = status
        self.retry_after = retry_after


def _retry_after(response: httpx.Response) -> int:
    value = response.headers.get("Retry-After", "0")
    try:
        delay = int(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            delay = ceil((retry_at - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            delay = 0
    return max(0, min(delay, 3600))
