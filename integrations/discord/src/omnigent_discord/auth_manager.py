"""Ties delegated auth together: token storage + device flow + refresh.

One :class:`AuthManager` per bot process. It is the single place that:

- resolves a Discord user's stored token into a :class:`ClientAuth` for the
  HTTP client pool (with a refresh callback that rotates + re-persists);
- runs the login device flow end-to-end, showing the user the verification
  link and, on approval, persisting the minted tokens;
- logs a user out (revoke on the server + delete locally).

Keys are the bare Discord user snowflake — globally unique, so unlike the
Slack integration there is no workspace to pack into the key.

See ``designs/DEVICE_AUTH.md``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from omnigent_bot_core.oauth import (
    AuthorizationDeniedError,
    AuthorizationExpiredError,
    DeviceFlowClient,
    OAuthError,
    PendingLogin,
    TokenResult,
    start_login,
)
from omnigent_bot_core.omnigent import ClientAuth, TokenRefreshTransientError

from omnigent_discord.tokens import TokenStore

_logger = logging.getLogger(__name__)


class _GrantDeadError(RuntimeError):
    """Refresh failed permanently — the grant is dead, drop the token."""


class _RefreshTransientError(RuntimeError):
    """Refresh failed transiently — keep the token and retry later."""


def discord_client_id(guild_name: str) -> str:
    """RFC 8628 ``client_id`` this integration presents to the server.

    A public string naming the requesting application, qualified by the
    Discord guild the login was started from so an operator reading the
    server's consent page / audit log can tell which community's bot obtained
    the grant (e.g. ``"Discord-Omnigent-Acme Guild"``). Not the user — the
    per-user distinction lives in the token store key. Falls back to a bare
    ``"Discord-Omnigent"`` when there is no guild (a DM) or the name is
    unavailable.
    """
    guild_name = guild_name.strip()
    return f"Discord-Omnigent-{guild_name}" if guild_name else "Discord-Omnigent"


# Called after a (user, server) token is stored or removed, so the client
# pool can drop any cached client for that key and rebuild it with the new
# credential (or lack of one) on next use.
TokenChangedHook = Callable[[str, str], Awaitable[None]]


class AuthManager:
    """Delegated-auth orchestration for the Discord bot.

    :param token_store: The token backend — an encrypted (persistent) or
        in-memory store. ``None`` disables delegated auth entirely (only used
        in tests; the app always wires a store).
    :param on_token_changed: Optional hook fired after a token is stored
        (login) or deleted (logout), with ``(user_id, server_url)``. Wired to
        the pool so a stale cached client is rebuilt with the fresh token —
        without it, a client created during the pre-login probe (no token) is
        reused after login and keeps 401ing.
    """

    def __init__(
        self,
        token_store: TokenStore | None,
        on_token_changed: TokenChangedHook | None = None,
        *,
        client_secret: str | None = None,
    ) -> None:
        self._tokens = token_store
        self._on_token_changed = on_token_changed
        # Optional device-grant client secret, sent on every client-facing
        # call (authorize / token / revoke) when the server requires it.
        self._client_secret = client_secret
        # Track in-flight login poll tasks so they aren't garbage collected.
        self._login_tasks: set[asyncio.Task[Any]] = set()
        # In-flight login poll per (user, server). A fresh ``/omnigent``
        # supersedes the prior attempt: without this, each re-run stacks
        # ANOTHER device grant + poll, so several polls race and approving one
        # browser code doesn't resolve the setup message bound to a different,
        # still-pending code — a stuck "waiting for approval…" screen.
        self._login_polls: dict[tuple[str, str], asyncio.Task[Any]] = {}

    def _new_client(self, server_url: str) -> DeviceFlowClient:
        """Construct a device-flow client for a server."""
        return DeviceFlowClient(server_url, client_secret=self._client_secret)

    def _spawn_login_poll(self, key: tuple[str, str], coro: Awaitable[None]) -> None:
        """Spawn a login poll for ``key``, superseding any prior one.

        Cancels an existing in-flight poll for the same (user, server) so a
        re-run of setup doesn't leave a stale poll racing the new one. Tracked
        in both ``_login_tasks`` (shutdown) and ``_login_polls`` (supersede).
        """
        existing = self._login_polls.pop(key, None)
        if existing is not None and not existing.done():
            existing.cancel()
        task = asyncio.ensure_future(coro)
        self._login_tasks.add(task)
        self._login_polls[key] = task

        def _cleanup(finished: asyncio.Task[Any]) -> None:
            self._login_tasks.discard(finished)
            # Only clear the map slot if it still points at THIS task (a newer
            # poll may have already replaced it).
            if self._login_polls.get(key) is finished:
                del self._login_polls[key]

        task.add_done_callback(_cleanup)

    async def shutdown(self) -> None:
        """Cancel in-flight login poll tasks (called on bot shutdown).

        Each poll can run for minutes (the login timeout) and holds an httpx
        client; cancelling them on shutdown avoids "Task was destroyed but it
        is pending" warnings and leaked connections.
        """
        tasks = list(self._login_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @property
    def enabled(self) -> bool:
        """Whether delegated auth is usable (a token backend is wired)."""
        return self._tokens is not None

    async def resolve_auth(self, server_url: str, user_id: str) -> ClientAuth | None:
        """Build a :class:`ClientAuth` for the pool, or ``None`` if none stored.

        The refresh callback rotates the token via the server and persists the
        new pair; if the grant is gone it clears the stored token and returns
        ``None`` so the user is prompted to re-login.
        """
        if self._tokens is None:
            return None
        tokens = self._tokens
        record = await tokens.get(user_id, server_url)
        if record is None:
            return None

        async def _refresh() -> str | None:
            current = await tokens.get(user_id, server_url)
            if current is None:
                return None
            # OIDC session JWTs carry no refresh token — nothing to rotate.
            # Drop the expired token so the next turn prompts a fresh login.
            if not current.refresh_token:
                await tokens.delete(user_id, server_url)
                return None
            try:
                pair = await self._rotate(server_url, current.refresh_token)
            except _GrantDeadError:
                # Grant permanently revoked/expired — drop the dead token so
                # the next turn prompts a fresh login instead of looping on 401s.
                await tokens.delete(user_id, server_url)
                return None
            except _RefreshTransientError as exc:
                # Network blip / 5xx — the refresh token is likely still valid.
                # Signal the caller to KEEP the current access token and fail
                # this attempt without prompting re-login; a later turn retries.
                _logger.info(
                    "Token refresh failed transiently server=%s user=%s", server_url, user_id
                )
                raise TokenRefreshTransientError(str(exc)) from exc
            # Some token endpoints rotate only the access token and keep the
            # existing refresh token implicitly (empty in the response). Retain
            # the prior refresh token in that case — overwriting it with ""
            # would make the NEXT refresh treat the grant as dead and log the
            # user out.
            refresh_token = pair.refresh_token or current.refresh_token
            await tokens.put(
                user_id,
                server_url,
                access_token=pair.access_token,
                refresh_token=refresh_token,
            )
            return pair.access_token

        return ClientAuth(record.access_token, _refresh)

    async def _rotate(self, server_url: str, refresh_token: str) -> TokenResult:
        """Rotate a refresh token via the server's device-grant endpoints.

        Raises :class:`_GrantDeadError` when the grant is permanently rejected
        (drop the token) or :class:`_RefreshTransientError` on a transient
        failure (keep the token, retry later). Distinguishing the two avoids
        discarding a still-valid refresh grant on a momentary network blip.
        """
        client = self._new_client(server_url)
        try:
            return await client.refresh(refresh_token)
        except OAuthError as exc:
            # Device-grant client raises OAuthError on any non-200; treat as a
            # dead grant.
            raise _GrantDeadError(str(exc)) from exc
        finally:
            await client.aclose()

    async def authorize(self, *, server_url: str, client_id: str) -> PendingLogin:
        """Start the login flow matching the server's auth mode.

        Probes the server (accounts → device grant; oidc → CLI-ticket flow)
        and returns a :class:`PendingLogin`. The caller shows
        ``verification_url`` to the user and then drives
        :meth:`await_authorization_in_background`. Raises :class:`OAuthError`
        if the flow can't be started — including for header/proxy-mode
        servers, which have no per-user login the bot can drive.

        :param client_id: The RFC 8628 client identifier to present in the
            device-grant flow (see :func:`discord_client_id`); ignored in OIDC
            mode, which has no client identifier.
        """
        assert self._tokens is not None, "delegated auth not enabled"
        return await start_login(
            server_url, client_id=client_id, client_secret=self._client_secret
        )

    def await_authorization_in_background(
        self,
        *,
        pending: PendingLogin,
        user_id: str,
        server_url: str,
        on_success: Callable[[], Awaitable[None]],
        on_failure: Callable[[str], Awaitable[None]],
    ) -> None:
        """Poll the pending login in the background, storing the token.

        On success the token is stored, the token-changed hook fires (so the
        client pool drops any stale tokenless client), and ``on_success`` runs
        — the setup flow uses it to advance the same ephemeral message to
        agent/host selection. On denial/expiry/error ``on_failure`` runs with
        a human-readable reason. UI-agnostic: this method never touches
        Discord directly.
        """
        self._spawn_login_poll(
            (user_id, server_url.rstrip("/")),
            self._await_authorization(
                pending=pending,
                user_id=user_id,
                server_url=server_url,
                on_success=on_success,
                on_failure=on_failure,
            ),
        )

    async def _await_authorization(
        self,
        *,
        pending: PendingLogin,
        user_id: str,
        server_url: str,
        on_success: Callable[[], Awaitable[None]],
        on_failure: Callable[[str], Awaitable[None]],
    ) -> None:
        try:
            result = await pending.poll()
        except AuthorizationDeniedError:
            await on_failure("You denied the login request. No access was granted.")
            return
        except AuthorizationExpiredError:
            await on_failure("That login link expired. Start setup again to retry.")
            return
        except OAuthError as exc:
            _logger.info("Login poll failed server=%s error=%s", server_url, exc)
            await on_failure("Login failed. Please try again.")
            return
        except Exception:
            # Never let an unexpected error kill the task silently — that would
            # strand the setup message on "waiting for approval…" forever.
            _logger.exception("Unexpected error during login poll server=%s", server_url)
            await on_failure("Login failed. Please try again.")
            return
        finally:
            await pending.close()

        assert self._tokens is not None
        await self._tokens.put(
            user_id,
            server_url,
            access_token=result.access_token,
            refresh_token=result.refresh_token,
        )
        # Drop the tokenless client cached during the pre-login probe so the
        # next request rebuilds it with the freshly stored token.
        if self._on_token_changed is not None:
            await self._on_token_changed(user_id, server_url)
        _logger.info("Login complete user=%s server=%s", user_id, server_url)
        await on_success()

    async def logout(self, user_id: str, server_url: str) -> None:
        """Revoke the grant on one server and delete the local token."""
        if self._tokens is None:
            return
        record = await self._tokens.get(user_id, server_url)
        if record is not None and record.refresh_token:
            await self._revoke(server_url, record.refresh_token)
        await self._tokens.delete(user_id, server_url)

    async def logout_all(self, user_id: str) -> int:
        """Revoke and delete every delegated token the user holds.

        Best-effort per server: a revoke that fails (server down, grant
        already gone) still proceeds to delete the local token, so a logout
        never leaves a usable token behind locally. Returns the number of
        server tokens cleared.
        """
        if self._tokens is None:
            return 0
        tokens = await self._tokens.list_for_user(user_id)
        for server_url, record in tokens:
            # Only device-grant tokens are server-revocable; an OIDC session
            # JWT (no refresh token) is just dropped locally and expires.
            if record.refresh_token:
                await self._revoke(server_url, record.refresh_token)
            await self._tokens.delete(user_id, server_url)
        return len(tokens)

    async def _revoke(self, server_url: str, refresh_token: str) -> None:
        client = self._new_client(server_url)
        try:
            await client.revoke(refresh_token)
        finally:
            await client.aclose()
