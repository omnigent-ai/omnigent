"""Resolve omitted repository branches through the launch owner's GitHub connection."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import httpx

from omnigent.connections.github import GithubConnectionStore
from omnigent.experimental.workspace_profiles.profiles import canonical_github_url, valid_branch
from omnigent.server import github_identity
from omnigent.server.github_app_client import GitHubAppClient

_REQUEST_TIMEOUT_S = 5.0
_METADATA_TIMEOUT_S = 10.0


class GitHubDefaultBranchResolver:
    """Best-effort lookup in the launcher's worker thread.

    Metadata shares a deadline; credential resolution inherits the store/client timeouts.
    """

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._integration: tuple[GithubConnectionStore, GitHubAppClient] | None = None
        self._transport = transport

    def configure(
        self, store: GithubConnectionStore | None, client: GitHubAppClient | None
    ) -> None:
        self._integration = (store, client) if store is not None and client is not None else None

    def __call__(self, owner: str, urls: Sequence[str]) -> dict[str, str]:
        integration = self._integration
        if integration is None or not owner:
            return {}
        canonical = dict.fromkeys(self._canonical_urls(urls))
        if not canonical:
            return {}
        return asyncio.run(self._resolve(owner, tuple(canonical), *integration))

    @staticmethod
    def _canonical_urls(urls: Sequence[str]) -> list[str]:
        canonical = []
        for url in urls:
            try:
                canonical.append(canonical_github_url(url))
            except ValueError:
                continue
        return canonical

    async def _resolve(
        self,
        owner: str,
        urls: Sequence[str],
        store: GithubConnectionStore,
        client: GitHubAppClient,
    ) -> dict[str, str]:
        branches: dict[str, str] = {}
        try:
            token = await github_identity.resolve_access_token(owner, store=store, client=client)
            if not token:
                return branches
            async with asyncio.timeout(_METADATA_TIMEOUT_S):
                async with httpx.AsyncClient(
                    transport=self._transport,
                    timeout=_REQUEST_TIMEOUT_S,
                    follow_redirects=False,
                    trust_env=False,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                    },
                ) as http:
                    for url in urls:
                        branch = await self._fetch_branch(http, url)
                        if branch is not None:
                            branches[url] = branch
        except Exception:  # noqa: BLE001 - metadata failure must preserve generic launch fallback
            return branches
        return branches

    @staticmethod
    async def _fetch_branch(http: httpx.AsyncClient, url: str) -> str | None:
        repository = url.removeprefix("https://github.com/").removesuffix(".git")
        try:
            async with asyncio.timeout(_REQUEST_TIMEOUT_S):
                response = await http.get(f"https://api.github.com/repos/{repository}")
                if response.status_code != 200:
                    return None
                payload = response.json()
                branch = payload.get("default_branch") if isinstance(payload, dict) else None
                return branch if isinstance(branch, str) and valid_branch(branch) else None
        except (httpx.HTTPError, ValueError, TimeoutError):
            return None
