"""Ties delegated auth together: token storage + device flow + refresh.

One :class:`AuthManager` per bot process. It is the single place that:

- resolves a Slack user's stored token into a :class:`ClientAuth` for the
  HTTP client pool (with a refresh callback that rotates + re-persists);
- runs the login device flow end-to-end, DMing the user the verification
  link and, on approval, persisting the minted tokens;
- logs a user out (revoke on the server + delete locally).

See ``designs/DEVICE_AUTH.md``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from omnigent_slack.oauth import (
    AuthorizationDeniedError,
    AuthorizationExpiredError,
    DeviceFlowClient,
    OAuthError,
    PendingLogin,
    start_login,
)
from omnigent_slack.omnigent import ClientAuth
from omnigent_slack.tokens import TokenStore

_logger = logging.getLogger(__name__)


def slack_client_id(team_name: str) -> str:
    """RFC 8628 ``client_id`` this integration presents to the server.

    A public string naming the requesting application, qualified by the
    Slack workspace name so an operator reading the server's consent page /
    audit log can tell which workspace's bot obtained the grant (e.g.
    ``"Slack-Omnigent-Acme Corp"``). Not the user — the per-user
    distinction lives in the token store key. Falls back to a bare
    ``"Slack-Omnigent"`` when the workspace name is unavailable.
    """
    team_name = team_name.strip()
    return f"Slack-Omnigent-{team_name}" if team_name else "Slack-Omnigent"


# Called after a (team, user, server) token is stored or removed, so the
# client pool can drop any cached client for that key and rebuild it with
# the new credential (or lack of one) on next use.
TokenChangedHook = Callable[[str, str, str], Awaitable[None]]


class AuthManager:
    """Delegated-auth orchestration for the Slack bot.

    :param token_store: The token backend — an encrypted (persistent) or
        in-memory store. ``None`` disables delegated auth entirely (only
        used in tests; the app always wires a store).
    :param on_token_changed: Optional hook fired after a token is stored
        (login) or deleted (logout), with ``(team_id, user_id,
        server_url)``. Wired to the pool so a stale cached client is
        rebuilt with the fresh token — without it, a client created
        during the pre-login probe (no token) is reused after login and
        keeps 401ing.
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

    def _new_client(self, server_url: str) -> DeviceFlowClient:
        """Construct a device-flow client for a server."""
        return DeviceFlowClient(server_url, client_secret=self._client_secret)

    @property
    def enabled(self) -> bool:
        """Whether delegated auth is usable (a token backend is wired)."""
        return self._tokens is not None

    async def resolve_auth(self, server_url: str, user_id: str) -> ClientAuth | None:
        """Build a :class:`ClientAuth` for the pool, or ``None`` if none stored.

        The refresh callback rotates the token via the server and
        persists the new pair; if the grant is gone it clears the stored
        token and returns ``None`` so the user is prompted to re-login.
        """
        if self._tokens is None:
            return None
        tokens = self._tokens
        # The pool keys clients by (server_url, user_id); the team is packed
        # into user_id as "team:user" (see pack_user_key) so the store can be
        # keyed per (team, user, server). These helpers unpack it.
        team, user = _team_of(user_id), _user_of(user_id)
        record = await tokens.get(team, user, server_url)
        if record is None:
            return None

        async def _refresh() -> str | None:
            current = await tokens.get(team, user, server_url)
            if current is None:
                return None
            # OIDC session JWTs carry no refresh token — nothing to rotate.
            # Drop the expired token so the next turn prompts a fresh login.
            if not current.refresh_token:
                await tokens.delete(team, user, server_url)
                return None
            client = self._new_client(server_url)
            try:
                pair = await client.refresh(current.refresh_token)
            except OAuthError:
                # Grant revoked/expired — drop the dead token so the next
                # turn prompts a fresh login instead of looping on 401s.
                await tokens.delete(team, user, server_url)
                return None
            finally:
                await client.aclose()
            await tokens.put(
                team,
                user,
                server_url,
                access_token=pair.access_token,
                refresh_token=pair.refresh_token,
            )
            return pair.access_token

        return ClientAuth(record.access_token, _refresh)

    async def has_token(self, team_id: str, user_id: str, server_url: str) -> bool:
        if self._tokens is None:
            return False
        return await self._tokens.get(team_id, user_id, server_url) is not None

    async def authorize(self, *, server_url: str, client_id: str) -> PendingLogin:
        """Start the login flow matching the server's auth mode.

        Probes the server (accounts → device grant; oidc → CLI-ticket
        flow) and returns a :class:`PendingLogin`. The caller shows
        ``verification_url`` to the user (e.g. in the setup modal) and
        then drives :meth:`await_authorization_in_background`. Raises
        :class:`OAuthError` if the flow can't be started — including for
        header/proxy-mode servers, which have no per-user login the bot
        can drive.

        :param client_id: The RFC 8628 client identifier to present in the
            device-grant flow (see :func:`slack_client_id`); ignored in
            OIDC mode, which has no client identifier.
        """
        assert self._tokens is not None, "delegated auth not enabled"
        return await start_login(
            server_url, client_id=client_id, client_secret=self._client_secret
        )

    def await_authorization_in_background(
        self,
        *,
        pending: PendingLogin,
        team_id: str,
        user_id: str,
        server_url: str,
        on_success: Callable[[], Awaitable[None]],
        on_failure: Callable[[str], Awaitable[None]],
    ) -> None:
        """Poll the pending login in the background, storing the token.

        On success the token is stored, the token-changed hook fires (so
        the client pool drops any stale tokenless client), and
        ``on_success`` runs — the setup flow uses it to advance the same
        modal to agent/host selection. On denial/expiry/error
        ``on_failure`` runs with a human-readable reason. UI-agnostic:
        this method never touches Slack directly.
        """
        task = asyncio.create_task(
            self._await_authorization(
                pending=pending,
                team_id=team_id,
                user_id=user_id,
                server_url=server_url,
                on_success=on_success,
                on_failure=on_failure,
            )
        )
        self._login_tasks.add(task)
        task.add_done_callback(self._login_tasks.discard)

    async def _await_authorization(
        self,
        *,
        pending: PendingLogin,
        team_id: str,
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
            # Never let an unexpected error kill the task silently — that
            # would strand the setup modal on "waiting for approval…"
            # forever. Report a generic failure so the user can retry.
            _logger.exception("Unexpected error during login poll server=%s", server_url)
            await on_failure("Login failed. Please try again.")
            return
        finally:
            await pending.close()

        assert self._tokens is not None
        await self._tokens.put(
            team_id,
            user_id,
            server_url,
            access_token=result.access_token,
            refresh_token=result.refresh_token,
        )
        # Drop the tokenless client cached during the pre-login probe so the
        # next request rebuilds it with the freshly stored token.
        if self._on_token_changed is not None:
            await self._on_token_changed(team_id, user_id, server_url)
        _logger.info("Login complete team=%s user=%s server=%s", team_id, user_id, server_url)
        await on_success()

    async def logout(self, team_id: str, user_id: str, server_url: str) -> None:
        """Revoke the grant on one server and delete the local token."""
        if self._tokens is None:
            return
        record = await self._tokens.get(team_id, user_id, server_url)
        if record is not None and record.refresh_token:
            await self._revoke(server_url, record.refresh_token)
        await self._tokens.delete(team_id, user_id, server_url)

    async def logout_all(self, team_id: str, user_id: str) -> int:
        """Revoke and delete every delegated token the user holds.

        Best-effort per server: a revoke that fails (server down, grant
        already gone) still proceeds to delete the local token, so a
        logout never leaves a usable token behind locally. Returns the
        number of server tokens cleared.
        """
        if self._tokens is None:
            return 0
        tokens = await self._tokens.list_for_user(team_id, user_id)
        for server_url, record in tokens:
            # Only device-grant tokens are server-revocable; an OIDC session
            # JWT (no refresh token) is just dropped locally and expires.
            if record.refresh_token:
                await self._revoke(server_url, record.refresh_token)
            await self._tokens.delete(team_id, user_id, server_url)
        return len(tokens)

    async def _revoke(self, server_url: str, refresh_token: str) -> None:
        client = self._new_client(server_url)
        try:
            await client.revoke(refresh_token)
        finally:
            await client.aclose()


# The pool's AuthResolver signature is (server_url, user_id); we pack the
# team into user_id as "team:user" so a single opaque key threads through
# without widening the pool's interface. These helpers unpack it.


def pack_user_key(team_id: str, user_id: str) -> str:
    return f"{team_id}:{user_id}"


def _team_of(packed: str) -> str:
    return packed.split(":", 1)[0] if ":" in packed else ""


def _user_of(packed: str) -> str:
    return packed.split(":", 1)[1] if ":" in packed else packed
