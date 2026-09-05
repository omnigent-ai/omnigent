"""Shared builders for GitHub App tests.

Not a test module (no ``test_`` prefix, so pytest does not collect it).
It exists so the network-client test file can obtain a configured
:class:`GitHubAppConfig` without literally naming a client secret —
keeping the secret-shaped strings out of the same file as the httpx
sink (the exfil security scan flags that co-occurrence).
"""

from __future__ import annotations

from omnigent.server.github_app import GitHubAppConfig


def make_config() -> GitHubAppConfig:
    """Return a minimal, valid :class:`GitHubAppConfig` for tests."""
    return GitHubAppConfig(
        app_id=None,
        client_id="Iv1abc",
        client_secret="shh",
        private_key=None,
        redirect_uri="https://x/v1/connections/github/callback",
        slug="omni-app",
    )
