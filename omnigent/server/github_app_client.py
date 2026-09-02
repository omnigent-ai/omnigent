"""Async HTTP client for the GitHub App user + app flows.

The network half of the GitHub App integration. It sends the OAuth
token requests and reads the user endpoint, but never constructs
credentials itself: the App secrets and the form fields that carry them
are owned by :mod:`omnigent.server.github_app`
(:class:`~omnigent.server.github_app.GitHubAppConfig`), which this
module simply POSTs. Keeping the secret-owning code and the network
sink in separate modules is deliberate. See
``docs/GITHUB_APP_SETUP.md``.
"""

from __future__ import annotations

import httpx

from omnigent.server.github_app import (
    GitHubAppConfig,
    GitHubAppError,
    GitHubTokenSet,
    token_set_from_payload,
)

_TOKEN_ENDPOINT = "https://github.com/login/oauth/access_token"
_USER_ENDPOINT = "https://api.github.com/user"

_HTTP_TIMEOUT_S = 15.0


class GitHubAppClient:
    """Async HTTP client for the GitHub App user + app flows.

    Stateless beyond holding the config; every method opens its own
    short-lived :class:`httpx.AsyncClient` so the client is safe to build
    once and reuse across requests.
    """

    def __init__(
        self, config: GitHubAppConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._config = config
        # Injectable transport for tests (httpx.MockTransport); None uses
        # the real network.
        self._transport = transport

    def _http_client(self) -> httpx.AsyncClient:
        """Open an AsyncClient, honoring an injected test transport."""
        return httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S, transport=self._transport)

    async def exchange_code(self, code: str) -> GitHubTokenSet:
        """Exchange an authorization ``code`` for a user access token.

        :param code: The ``code`` GitHub returned to the callback.
        :returns: The resulting token set.
        :raises GitHubAppError: When GitHub rejects the exchange.
        """
        return await self._token_request(self._config.code_exchange_fields(code))

    async def refresh_token(self, refresh_token: str) -> GitHubTokenSet:
        """Exchange a refresh token for a fresh user access token.

        :param refresh_token: The stored ``ghr_…`` refresh token.
        :returns: The refreshed token set.
        :raises GitHubAppError: When GitHub rejects the refresh.
        """
        return await self._token_request(self._config.token_refresh_fields(refresh_token))

    async def fetch_login(self, access_token: str) -> tuple[str, int]:
        """Fetch the authenticated user's ``(login, id)``.

        :param access_token: A valid user access token.
        :returns: The GitHub login and numeric user id.
        :raises GitHubAppError: When the API call fails.
        """
        async with self._http_client() as client:
            resp = await client.get(
                _USER_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                },
            )
        if resp.status_code != 200:
            raise GitHubAppError(f"GitHub /user returned {resp.status_code}")
        data = resp.json()
        login = data.get("login")
        user_id = data.get("id")
        if not login or user_id is None:
            raise GitHubAppError("GitHub /user response missing login/id")
        return str(login), int(user_id)

    async def _token_request(self, fields: dict[str, str]) -> GitHubTokenSet:
        """POST the given form fields to the token endpoint and parse the reply."""
        async with self._http_client() as client:
            resp = await client.post(
                _TOKEN_ENDPOINT,
                data=fields,
                headers={"Accept": "application/json"},
            )
        if resp.status_code != 200:
            raise GitHubAppError(f"GitHub token endpoint returned {resp.status_code}")
        return token_set_from_payload(resp.json())
