"""Async HTTP client for the GitHub App user + app flows.

The network half of the GitHub App integration. It sends the OAuth
token requests and reads the user endpoint, but never constructs
credentials itself: the App secrets and the form fields that carry them
are owned by :mod:`omnigent.server.github_app`
(:class:`~omnigent.server.github_app.GitHubAppConfig`), which this
module simply POSTs. Keeping the secret-owning code and the network
sink in separate modules is deliberate. See
``designs/GITHUB_APP_SANDBOX_AUTH.md``.
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

_REPO_PULLS_ENDPOINT = "https://api.github.com/repos/{full_name}/pulls"
_PULLS_PER_PAGE = 100
# One page of open PRs, newest first, is plenty for "PRs opened this session".
_PULLS_MAX_PAGES = 2

_REPO_PULL_COMMITS_ENDPOINT = "https://api.github.com/repos/{full_name}/pulls/{number}/commits"

# Search endpoint used to find a session's PRs by the Open-in-Omnigent link in
# their body — across ALL repos, so PRs the agent opened via the GitHub MCP in a
# repo the session never cloned are still found.
_SEARCH_ISSUES_ENDPOINT = "https://api.github.com/search/issues"
_SEARCH_PER_PAGE = 100
_PULL_COMMITS_PER_PAGE = 100

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

    async def list_pulls(self, access_token: str, full_name: str) -> list[dict[str, object]]:
        """List pull requests for ``full_name`` (``owner/repo``), newest first.

        Reads ``/repos/{full_name}/pulls?state=all`` (up to
        :data:`_PULLS_MAX_PAGES` pages) so OPEN, CLOSED, and MERGED PRs are all
        returned — the "PRs opened this session" panel shows anything created
        during the session regardless of its current state. Caller-side
        filtering (by author and creation time) scopes the raw list to a
        session.

        :param access_token: A valid user access token.
        :param full_name: The repository's ``owner/name``.
        :returns: PRs as ``{number, title, html_url, head_ref, draft, state,
            merged, author_login, created_at}``, newest first. ``state`` is
            ``"open"``/``"closed"``; ``merged`` is ``True`` for a merged PR.
        :raises GitHubAppError: When the API call fails.
        """
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }
        url = _REPO_PULLS_ENDPOINT.format(full_name=full_name)
        pulls: list[dict[str, object]] = []
        async with self._http_client() as client:
            for page in range(1, _PULLS_MAX_PAGES + 1):
                resp = await client.get(
                    url,
                    params={
                        "state": "all",
                        "sort": "created",
                        "direction": "desc",
                        "per_page": _PULLS_PER_PAGE,
                        "page": page,
                    },
                    headers=headers,
                )
                if resp.status_code != 200:
                    raise GitHubAppError(
                        f"GitHub /repos/{full_name}/pulls returned {resp.status_code}"
                    )
                batch = resp.json()
                if not isinstance(batch, list) or not batch:
                    break
                for entry in batch:
                    if not isinstance(entry, dict) or entry.get("number") is None:
                        continue
                    head = entry.get("head") if isinstance(entry.get("head"), dict) else {}
                    user = entry.get("user") if isinstance(entry.get("user"), dict) else {}
                    pulls.append(
                        {
                            "number": entry.get("number"),
                            "title": entry.get("title"),
                            "html_url": entry.get("html_url"),
                            "head_ref": head.get("ref"),
                            "draft": bool(entry.get("draft")),
                            "state": entry.get("state"),
                            "merged": entry.get("merged_at") is not None,
                            "author_login": user.get("login"),
                            "created_at": entry.get("created_at"),
                            "body": entry.get("body") or "",
                        }
                    )
                if len(batch) < _PULLS_PER_PAGE:
                    break
        return pulls

    async def search_pulls(self, access_token: str, query: str) -> list[dict[str, object]]:
        """Search issues/PRs matching *query*, mapped to the session-PR shape.

        Uses ``GET /search/issues`` (one request), so a session's PRs are found
        across every repo by the Open-in-Omnigent link in their body — including
        repos the session never cloned (PRs opened via the GitHub MCP). Search
        results carry no ``head`` ref, so ``head_ref`` is ``None`` (the panel
        only uses it as a title fallback, and ``title`` is always present).

        :param access_token: A valid user access token.
        :param query: A GitHub issues-search query, e.g.
            ``'<session_id> in:body type:pr author:<login>'``.
        :returns: PRs as ``{number, title, html_url, head_ref, draft, state,
            merged, author_login, created_at, body, repo}``, where ``repo`` is
            ``owner/name`` and ``merged`` reflects ``pull_request.merged_at``.
        :raises GitHubAppError: When the API call fails.
        """
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }
        pulls: list[dict[str, object]] = []
        async with self._http_client() as client:
            resp = await client.get(
                _SEARCH_ISSUES_ENDPOINT,
                params={"q": query, "per_page": _SEARCH_PER_PAGE},
                headers=headers,
            )
            if resp.status_code != 200:
                raise GitHubAppError(f"GitHub /search/issues returned {resp.status_code}")
            payload = resp.json()
            items = payload.get("items") if isinstance(payload, dict) else None
            for entry in items or []:
                if not isinstance(entry, dict) or entry.get("number") is None:
                    continue
                user = entry.get("user") if isinstance(entry.get("user"), dict) else {}
                pr = (
                    entry.get("pull_request")
                    if isinstance(entry.get("pull_request"), dict)
                    else {}
                )
                # ``repository_url`` is ``…/repos/{owner}/{name}`` — the only place
                # the repo is named in a search result.
                repo_url = str(entry.get("repository_url") or "")
                repo = repo_url.split("/repos/", 1)[-1] if "/repos/" in repo_url else ""
                pulls.append(
                    {
                        "number": entry.get("number"),
                        "title": entry.get("title"),
                        "html_url": entry.get("html_url"),
                        "head_ref": None,
                        "draft": bool(entry.get("draft")),
                        "state": entry.get("state"),
                        "merged": pr.get("merged_at") is not None,
                        "author_login": user.get("login"),
                        "created_at": entry.get("created_at"),
                        "body": entry.get("body") or "",
                        "repo": repo,
                    }
                )
        return pulls

    async def list_pull_commit_messages(
        self, access_token: str, full_name: str, number: int
    ) -> list[str]:
        """Return the commit messages of PR ``number`` in ``full_name``.

        Used to confirm a PR belongs to a session by looking for the
        ``Omnigent-Session`` trailer the sandbox stamps on its commits. Reads
        the first page (a PR's commit count is small in practice).

        :param access_token: A valid user access token.
        :param full_name: The repository's ``owner/name``.
        :param number: The pull request number.
        :returns: Commit messages (possibly empty).
        :raises GitHubAppError: When the API call fails.
        """
        url = _REPO_PULL_COMMITS_ENDPOINT.format(full_name=full_name, number=number)
        async with self._http_client() as client:
            resp = await client.get(
                url,
                params={"per_page": _PULL_COMMITS_PER_PAGE},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github+json",
                },
            )
        if resp.status_code != 200:
            raise GitHubAppError(
                f"GitHub /repos/{full_name}/pulls/{number}/commits returned {resp.status_code}"
            )
        batch = resp.json()
        if not isinstance(batch, list):
            return []
        messages: list[str] = []
        for entry in batch:
            commit = entry.get("commit") if isinstance(entry, dict) else None
            if isinstance(commit, dict) and isinstance(commit.get("message"), str):
                messages.append(commit["message"])
        return messages

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
