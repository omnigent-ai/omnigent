"""Detect a runner whose every call to the server is refused on credentials.

The state lives here, not in ``omnigent.runner._entry``, because a direct-spawned
runner runs that module as ``__main__`` while other runner code imports it by name.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import httpx

# A runner whose every server call is refused on credentials for this long exits
# with the sign-in code, so its session reports the cause instead of going silent.
CREDENTIAL_REJECTION_WINDOW_S = 300.0
CREDENTIAL_REJECTION_MIN_FAILURES = 3


def refuses_credentials(response: httpx.Response) -> bool:
    """Return whether ``response`` refuses the caller's credential itself.

    A 403 is left out: the server also answers 403 when a valid user lacks
    permission on a session.
    """
    if response.status_code == 401:
        return True
    if not response.is_redirect:
        return False
    # The Databricks Apps front door sends an expired bearer to its OAuth login
    # flow: ``/oidc/`` is the provider mount, ``/.auth/`` its callback path.
    location = response.headers.get("location", "")
    return "/oidc/" in location or "/.auth/" in location


class CredentialRejections:
    """Track whether every recent server call was refused on credentials."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._first_rejected_at: float | None = None
        self._rejected_count = 0
        # Called once, from any thread, when the rejection has persisted.
        self.on_rejected: Callable[[], None] | None = None

    def record(self, *, rejected: bool) -> None:
        """Record one server call's outcome; any accepted call resets the streak."""
        with self._lock:
            if not rejected:
                self._first_rejected_at = None
                self._rejected_count = 0
                return
            now = time.monotonic()
            if self._first_rejected_at is None:
                self._first_rejected_at = now
            self._rejected_count += 1
            callback = self.on_rejected
            if (
                callback is None
                or self._rejected_count < CREDENTIAL_REJECTION_MIN_FAILURES
                or now - self._first_rejected_at < CREDENTIAL_REJECTION_WINDOW_S
            ):
                return
            self.on_rejected = None
        callback()


credential_rejections = CredentialRejections()
