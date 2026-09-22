"""Async HTTP client for the Databricks OAuth (U2M) flow.

Network half of the Connect Databricks integration: it POSTs the OAuth token
requests to the per-workspace token endpoint and reads the SCIM ``Me`` endpoint
for the connected user, but never constructs credentials itself — the app secret
and the form fields that carry it are owned by
:class:`~omnigent.server.databricks_app.DatabricksConfig`. Mirrors
:mod:`omnigent.server.github_app_client`. See ``designs/DATABRICKS_CONNECT.md``.
"""

from __future__ import annotations

import httpx

from omnigent.server.databricks_app import (
    DatabricksAppError,
    DatabricksConfig,
    DatabricksTokenSet,
    token_set_from_payload,
    token_url,
)

# SCIM "current user" on the workspace host — reports the connected identity.
_ME_PATH = "/api/2.0/preview/scim/v2/Me"
_HTTP_TIMEOUT_S = 15.0


class DatabricksAppClient:
    """Async HTTP client for the Databricks OAuth token + identity calls.

    Stateless beyond holding the config; every method opens its own short-lived
    :class:`httpx.AsyncClient`, so it is safe to build once and reuse. All calls
    are workspace-scoped — the ``workspace_host`` (an ``https://…`` origin) is
    passed per call, since Databricks OAuth endpoints are workspace-relative.
    """

    def __init__(
        self, config: DatabricksConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._config = config
        self._transport = transport

    def _http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S, transport=self._transport)

    async def exchange_code(
        self, workspace_host: str, code: str, *, code_verifier: str
    ) -> DatabricksTokenSet:
        """Exchange an authorization ``code`` (+ PKCE verifier) for tokens."""
        return await self._token_request(
            workspace_host, self._config.code_exchange_fields(code, code_verifier=code_verifier)
        )

    async def refresh_token(self, workspace_host: str, refresh_token: str) -> DatabricksTokenSet:
        """Exchange a refresh token for a fresh access token."""
        return await self._token_request(
            workspace_host, self._config.token_refresh_fields(refresh_token)
        )

    async def fetch_user(self, workspace_host: str, access_token: str) -> tuple[str, str]:
        """Fetch the authenticated user's ``(user_name, id)`` from SCIM ``Me``."""
        async with self._http_client() as client:
            resp = await client.get(
                f"{workspace_host}{_ME_PATH}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if resp.status_code != 200:
            raise DatabricksAppError(f"Databricks SCIM Me returned {resp.status_code}")
        data = resp.json()
        user_name = data.get("userName")
        user_id = data.get("id")
        if not user_name or user_id is None:
            raise DatabricksAppError("Databricks SCIM Me response missing userName/id")
        return str(user_name), str(user_id)

    async def _token_request(
        self, workspace_host: str, fields: dict[str, str]
    ) -> DatabricksTokenSet:
        async with self._http_client() as client:
            resp = await client.post(
                token_url(workspace_host),
                data=fields,
                headers={"Accept": "application/json"},
            )
        if resp.status_code != 200:
            raise DatabricksAppError(f"Databricks token endpoint returned {resp.status_code}")
        return token_set_from_payload(resp.json())
