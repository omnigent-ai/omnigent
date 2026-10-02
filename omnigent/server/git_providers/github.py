"""GitHub connection facet: the per-user GitHub App connection and its credential.

The GitHub descriptor names this module as its ``connection`` facet. Server
modules are imported inside the methods, so loading the facet stays cheap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnigent.server.git_providers import ConnectionFacet

if TYPE_CHECKING:
    from fastapi import APIRouter

    from omnigent.connections.github import GithubConnectionStore
    from omnigent.server.auth import AuthProvider
    from omnigent.server.github_app import GitHubAppConfig
    from omnigent.server.github_app_client import GitHubAppClient
    from omnigent.stores.credential_store import SecretCipher


class GitHubConnection:
    """Connects a user's GitHub account through the GitHub App.

    The router also lists the user's repositories and branches for the new-chat
    picker.
    """

    repo_browser = True

    def config_from_env(self) -> GitHubAppConfig | None:
        """Read ``OMNIGENT_GITHUB_APP_*``; ``None`` when the App is not configured."""
        from omnigent.server.github_app import GitHubAppConfig

        return GitHubAppConfig.from_env()

    def make_store(self, db_uri: str, cipher: SecretCipher) -> GithubConnectionStore:
        """Return the store that keeps each user's GitHub connection."""
        from omnigent.connections.github import GithubConnectionStore

        return GithubConnectionStore(db_uri, cipher)

    def make_client(self, config: GitHubAppConfig) -> GitHubAppClient:
        """Return the GitHub App HTTP client."""
        from omnigent.server.github_app_client import GitHubAppClient

        return GitHubAppClient(config)

    def make_router(
        self,
        config: GitHubAppConfig,
        store: GithubConnectionStore,
        *,
        auth_provider: AuthProvider | None = None,
        client: GitHubAppClient | None = None,
    ) -> APIRouter:
        """Return the connect, callback, status, disconnect, repos, and branches routes."""
        from omnigent.server.routes.connections_github import create_connections_github_router

        return create_connections_github_router(
            config, store, auth_provider=auth_provider, client=client
        )

    async def resolve_credential(
        self,
        user_id: str,
        *,
        store: GithubConnectionStore,
        client: GitHubAppClient,
    ) -> dict[str, object] | None:
        """Return the user's GitHub token payload; an expiring token is refreshed first."""
        from omnigent.server.github_identity import resolve_github_credential

        return await resolve_github_credential(user_id, store=store, client=client)


CONNECTION: ConnectionFacet = GitHubConnection()
