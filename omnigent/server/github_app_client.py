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
# Repos the token can access (App-scoped), most-recently-pushed first.
_USER_REPOS_ENDPOINT = "https://api.github.com/user/repos"
_REPOS_PER_PAGE = 100
# Cap the walk so a user with thousands of repos gets a bounded, fast
# response for the picker (the newest ~300 by push time).
_REPOS_MAX_PAGES = 3

_REPO_BRANCHES_ENDPOINT = "https://api.github.com/repos/{full_name}/branches"
_BRANCHES_PER_PAGE = 100
# Cap the branch walk the same way — a busy repo can have hundreds of
# branches, but the picker only needs a bounded, fast list.
_BRANCHES_MAX_PAGES = 3

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

    async def list_repos(self, access_token: str) -> tuple[list[dict[str, object]], bool]:
        """List repos the authenticated user can access, App-scoped.

        Reads ``/user/repos`` most-recently-pushed first, following up to
        :data:`_REPOS_MAX_PAGES` pages. Returns a compact projection for the
        new-chat repo picker (not the full GitHub payload).

        :param access_token: A valid user access token.
        :returns: ``(repos, truncated)`` — repos as
            ``{full_name, clone_url, default_branch, private, pushed_at}`` newest
            first, and ``truncated=True`` when the page cap was hit and more
            repos almost certainly exist (so the UI can say the list is partial
            rather than silently dropping them).
        :raises GitHubAppError: When the API call fails.
        """
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }
        repos: list[dict[str, object]] = []
        truncated = False
        async with self._http_client() as client:
            for page in range(1, _REPOS_MAX_PAGES + 1):
                resp = await client.get(
                    _USER_REPOS_ENDPOINT,
                    params={"per_page": _REPOS_PER_PAGE, "page": page, "sort": "pushed"},
                    headers=headers,
                )
                if resp.status_code != 200:
                    raise GitHubAppError(f"GitHub /user/repos returned {resp.status_code}")
                batch = resp.json()
                if not isinstance(batch, list) or not batch:
                    break
                for entry in batch:
                    if not isinstance(entry, dict) or not entry.get("full_name"):
                        continue
                    repos.append(
                        {
                            "full_name": entry["full_name"],
                            "clone_url": entry.get("clone_url"),
                            "default_branch": entry.get("default_branch"),
                            "private": bool(entry.get("private")),
                            "pushed_at": entry.get("pushed_at"),
                        }
                    )
                if len(batch) < _REPOS_PER_PAGE:
                    break
            else:
                # Ran the full page cap without an early break → the last page
                # was full, so there are almost certainly more repos than shown.
                truncated = True
        return repos, truncated

    async def list_branches(self, access_token: str, full_name: str) -> list[str]:
        """List branch names for ``full_name`` (``owner/repo``), App-scoped.

        Reads ``/repos/{full_name}/branches`` following up to
        :data:`_BRANCHES_MAX_PAGES` pages, for the per-repo branch picker.

        :param access_token: A valid user access token.
        :param full_name: The repository's ``owner/name``.
        :returns: Branch names in the order GitHub returns them.
        :raises GitHubAppError: When the API call fails.
        """
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }
        url = _REPO_BRANCHES_ENDPOINT.format(full_name=full_name)
        branches: list[str] = []
        async with self._http_client() as client:
            for page in range(1, _BRANCHES_MAX_PAGES + 1):
                resp = await client.get(
                    url,
                    params={"per_page": _BRANCHES_PER_PAGE, "page": page},
                    headers=headers,
                )
                if resp.status_code != 200:
                    raise GitHubAppError(
                        f"GitHub /repos/{full_name}/branches returned {resp.status_code}"
                    )
                batch = resp.json()
                if not isinstance(batch, list) or not batch:
                    break
                for entry in batch:
                    if isinstance(entry, dict) and entry.get("name"):
                        branches.append(str(entry["name"]))
                if len(batch) < _BRANCHES_PER_PAGE:
                    break
        return branches

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
