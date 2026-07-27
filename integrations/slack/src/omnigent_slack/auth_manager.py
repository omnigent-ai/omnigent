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
from typing import Any, Protocol

from omnigent_slack.oauth import (
    AuthorizationDeniedError,
    AuthorizationExpiredError,
    DeviceFlowClient,
    OAuthError,
    PendingLogin,
    start_login,
)
from omnigent_slack.omnigent import ClientAuth, TokenRefreshTransientError
from omnigent_slack.tokens import TokenStore

_logger = logging.getLogger(__name__)


class _GrantDeadError(RuntimeError):
    """Refresh failed permanently — the grant is dead, drop the token."""


class _RefreshTransientError(RuntimeError):
    """Refresh failed transiently — keep the token and retry later."""


class RotatedToken(Protocol):
    """The shape a rotator returns: a fresh access + refresh pair."""

    access_token: str
    refresh_token: str


class TokenRotator(Protocol):
    """Refreshes/revokes a delegated token against its issuing authority.

    In ``databricks`` mode this is the workspace's custom OAuth app (refresh
    hits ``/oidc/v1/token``, not the Omnigent server), so the AuthManager takes
    it as a dependency rather than always assuming the device-grant endpoints.
    """

    async def refresh(self, refresh_token: str) -> RotatedToken: ...

    async def revoke(self, token: str) -> None: ...


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
        rotator: TokenRotator | None = None,
    ) -> None:
        self._tokens = token_store
        self._on_token_changed = on_token_changed
        # Optional device-grant client secret, sent on every client-facing
        # call (authorize / token / revoke) when the server requires it.
        self._client_secret = client_secret
        # Optional external rotator (databricks mode): refresh/revoke go to the
        # workspace OAuth app instead of the Omnigent server's /oauth/* endpoints.
        self._rotator = rotator
        # Track in-flight login poll tasks so they aren't garbage collected.
        self._login_tasks: set[asyncio.Task[Any]] = set()
        # In-flight login/enrollment poll per (team, user, server). A fresh
        # ``/omnigent`` (or a Slack-redelivered slash command) supersedes the
        # prior attempt: without this, each re-run stacks ANOTHER device-grant +
        # poll, so several polls race and approving one browser code doesn't
        # resolve the modal bound to a different, still-pending code — a stuck
        # "waiting for approval…" modal.
        self._login_polls: dict[tuple[str, str, str], asyncio.Task[Any]] = {}

    def _new_client(self, server_url: str) -> DeviceFlowClient:
        """Construct a device-flow client for a server."""
        return DeviceFlowClient(server_url, client_secret=self._client_secret)

    def _spawn_tracked(self, coro: Awaitable[None]) -> None:
        """Run ``coro`` as a background task tracked in ``_login_tasks``.

        Keeps the fire-and-forget bookkeeping (GC-safe reference + self-removal on
        completion) in one place, so every background poller is cancellable by
        :meth:`shutdown` and none can forget the ``add_done_callback``.
        """
        task = asyncio.ensure_future(coro)
        self._login_tasks.add(task)
        task.add_done_callback(self._login_tasks.discard)

    def _spawn_login_poll(self, key: tuple[str, str, str], coro: Awaitable[None]) -> None:
        """Spawn a login/enrollment poll for ``key``, superseding any prior one.

        Cancels an existing in-flight poll for the same (team, user, server) so a
        re-run of setup doesn't leave a stale poll racing the new one. Tracked in
        both ``_login_tasks`` (shutdown) and ``_login_polls`` (per-key supersede).
        """
        existing = self._login_polls.pop(key, None)
        if existing is not None and not existing.done():
            existing.cancel()
        task = asyncio.ensure_future(coro)
        self._login_tasks.add(task)
        self._login_polls[key] = task

        def _cleanup(t: asyncio.Task[Any]) -> None:
            self._login_tasks.discard(t)
            # Only clear the map slot if it still points at THIS task (a newer
            # poll may have already replaced it).
            if self._login_polls.get(key) is t:
                del self._login_polls[key]

        task.add_done_callback(_cleanup)

    async def shutdown(self) -> None:
        """Cancel in-flight login/enrollment poll tasks (called on bot shutdown).

        Each poll can run for minutes (the login/enrollment timeout) and holds an
        httpx client; cancelling them on shutdown avoids "Task was destroyed but
        it is pending" warnings and leaked connections.
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
            try:
                pair = await self._rotate(server_url, current.refresh_token)
            except _GrantDeadError:
                # Grant permanently revoked/expired — drop the dead token so the
                # next turn prompts a fresh login instead of looping on 401s.
                await tokens.delete(team, user, server_url)
                return None
            except _RefreshTransientError as exc:
                # Network blip / 5xx — the refresh token is likely still valid.
                # Signal the caller to KEEP the current access token and fail
                # this attempt without prompting re-login; a later turn retries.
                _logger.info(
                    "Token refresh failed transiently server=%s user=%s", server_url, user
                )
                raise TokenRefreshTransientError(str(exc)) from exc
            # Some token endpoints rotate only the access token and keep the
            # existing refresh token implicitly (empty in the response). Retain
            # the prior refresh token in that case — overwriting it with "" would
            # make the NEXT refresh treat the grant as dead and log the user out.
            refresh_token = pair.refresh_token or current.refresh_token
            await tokens.put(
                team,
                user,
                server_url,
                access_token=pair.access_token,
                refresh_token=refresh_token,
            )
            return pair.access_token

        return ClientAuth(record.access_token, _refresh)

    async def _rotate(self, server_url: str, refresh_token: str) -> RotatedToken:
        """Rotate a refresh token via the external rotator or device-grant client.

        Raises :class:`_GrantDeadError` when the grant is permanently rejected
        (drop the token) or :class:`_RefreshTransientError` on a transient
        failure (keep the token, retry later). Distinguishing the two avoids
        discarding a still-valid refresh grant on a momentary network blip.
        """
        if self._rotator is not None:
            try:
                return await self._rotator.refresh(refresh_token)
            except Exception as exc:
                # A rotator marks a permanently-dead grant with a truthy
                # ``grant_expired`` attribute; anything else is transient.
                if getattr(exc, "grant_expired", False):
                    raise _GrantDeadError(str(exc)) from exc
                raise _RefreshTransientError(str(exc)) from exc
        client = self._new_client(server_url)
        try:
            return await client.refresh(refresh_token)
        except OAuthError as exc:
            # Device-grant client raises OAuthError on any non-200; treat as a
            # dead grant (its historical behaviour, preserved).
            raise _GrantDeadError(str(exc)) from exc
        finally:
            await client.aclose()

    async def has_token(self, team_id: str, user_id: str, server_url: str) -> bool:
        if self._tokens is None:
            return False
        return await self._tokens.get(team_id, user_id, server_url) is not None

    async def current_access_token(
        self, team_id: str, user_id: str, server_url: str
    ) -> str | None:
        """The stored access token for a (user, server), or ``None`` if none.

        Used to baseline enrollment: a stale token from a prior sign-in can
        already exist, so "a token exists" is not the same as "the user just
        enrolled". Comparing this value tells the two apart.
        """
        if self._tokens is None:
            return None
        record = await self._tokens.get(team_id, user_id, server_url)
        return record.access_token if record is not None else None

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
        self._spawn_login_poll(
            (team_id, user_id, server_url.rstrip("/")),
            self._await_authorization(
                pending=pending,
                team_id=team_id,
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

    def await_enrollment_in_background(
        self,
        *,
        team_id: str,
        user_id: str,
        server_url: str,
        on_success: Callable[[], Awaitable[None]],
        on_failure: Callable[[str], Awaitable[None]],
        timeout_seconds: int = 600,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        """Poll the token store until a web-auth enrollment lands, then advance.

        The poll timeout matches the enrollment ``state`` TTL (600s) so the modal
        never gives up while the link is still valid — a first-time SSO + consent
        can take a couple of minutes, and a shorter poll would fire ``on_failure``
        and stop right as the user finishes, leaving the browser on the success
        page but the modal reporting a timeout.

        The Databricks web-auth flow completes out-of-band: the user finishes in
        their browser and the enrollment web server stores the token directly.
        There's no device code to poll, so we watch the shared store for the
        user's token to CHANGE (keyed by the Slack identity the signed ``state``
        bound the browser session to). Waiting for a change — not merely "a token
        exists" — is essential: a stale token from a prior sign-in can already be
        present, and firing ``on_success`` against it advances the modal before
        the fresh token lands, so validation 401s and the modal hangs. The
        baseline is captured when the poll task first runs — before the user can
        complete browser SSO, because the caller shows the enrollment link (via
        ``views_update``) only after spawning this poll — so the fresh write is
        always seen as a change; keep that ordering. On arrival ``on_success``
        runs (the setup modal advances to agent/host selection, mirroring the
        device flow); on timeout ``on_failure`` runs so the modal doesn't hang
        forever. UI-agnostic.
        """
        self._spawn_login_poll(
            (team_id, user_id, server_url.rstrip("/")),
            self._await_enrollment(
                team_id=team_id,
                user_id=user_id,
                server_url=server_url,
                on_success=on_success,
                on_failure=on_failure,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            ),
        )

    async def _await_enrollment(
        self,
        *,
        team_id: str,
        user_id: str,
        server_url: str,
        on_success: Callable[[], Awaitable[None]],
        on_failure: Callable[[str], Awaitable[None]],
        timeout_seconds: int,
        poll_interval_seconds: float,
    ) -> None:
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        # Baseline the token BEFORE the user can finish. A pre-existing (stale,
        # likely expired) token must not be mistaken for this enrollment — we wait
        # for the stored access token to differ from this baseline.
        baseline = await self.current_access_token(team_id, user_id, server_url)
        _logger.info(
            "Awaiting enrollment token change team=%s user=%s server=%s had_baseline=%s",
            team_id,
            user_id,
            server_url,
            baseline is not None,
        )
        try:
            while True:
                current = await self.current_access_token(team_id, user_id, server_url)
                if current is not None and current != baseline:
                    _logger.info(
                        "Web-auth enrollment complete team=%s user=%s server=%s",
                        team_id,
                        user_id,
                        server_url,
                    )
                    await on_success()
                    return
                if asyncio.get_event_loop().time() >= deadline:
                    await on_failure(
                        "That enrollment link expired before you finished. "
                        "Run /omnigent to try again."
                    )
                    return
                await asyncio.sleep(poll_interval_seconds)
        except Exception:
            # Never let an unexpected error strand the setup modal on "waiting…".
            _logger.exception("Unexpected error while awaiting enrollment server=%s", server_url)
            await on_failure("Sign-in failed. Please try again.")

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
        if self._rotator is not None:
            await self._rotator.revoke(refresh_token)
            return
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
