"""Microsoft Entra ID (keyless) authentication for Azure AI Foundry endpoints.

Builds an ``httpx.AsyncClient`` whose requests carry a bearer token from
``azure.identity.aio.DefaultAzureCredential``. Token caching and refresh are
left to the Azure SDK's bearer token provider.
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
import openai

AZURE_AI_SCOPE = "https://ai.azure.com/.default"

_OPENAI_V1_SUFFIX = "/openai/v1"
# Project names exclude "." and "%" so dot segments (raw or percent-encoded)
# and encoded slashes cannot redirect the token-bearing path.
_ALLOWED_PATH_RE = re.compile(r"^(?:/api/projects/[A-Za-z0-9_-]+)?(?:/openai/v1)?$")
_CONTROL_OR_SPACE_RE = re.compile(r"[\x00-\x20\x7f]")
# Only official Azure OpenAI / Foundry hosts may receive an Entra token:
# exactly one resource label in front of an allowed service suffix.
_ALLOWED_HOST_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?"
    r"\.(?:openai\.azure\.com|services\.ai\.azure\.com|cognitiveservices\.azure\.com)$"
)


def normalize_azure_endpoint(endpoint: str) -> str:
    """Normalize an Azure OpenAI v1 endpoint to ``https://host[/api/projects/<name>]/openai/v1/``.

    Accepts a bare resource root, a Foundry project URL, or a full
    ``/openai/v1`` URL on an official Azure OpenAI / Foundry host, so the
    Entra token is never sent elsewhere. Error messages never echo the input.

    :param endpoint: The configured endpoint, e.g.
        ``"https://example.openai.azure.com"``.
    :returns: The normalized base URL with a trailing slash.
    :raises ValueError: If the endpoint is not a plain HTTPS URL with a
        supported path.
    """
    candidate = endpoint.strip() if isinstance(endpoint, str) else ""
    if not candidate or _CONTROL_OR_SPACE_RE.search(candidate):
        raise ValueError("azure_endpoint must be a non-empty HTTPS URL without whitespace.")
    if "?" in candidate or "#" in candidate:
        raise ValueError("azure_endpoint must not include a query string or fragment.")
    try:
        parts = urlsplit(candidate)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        raise ValueError("azure_endpoint is not a valid URL.") from None
    if parts.scheme.lower() != "https":
        raise ValueError("azure_endpoint must use https.")
    if "@" in parts.netloc:
        raise ValueError("azure_endpoint must not include credentials.")
    if not hostname or not _ALLOWED_HOST_RE.match(hostname):
        raise ValueError(
            "azure_endpoint host must be <resource>.openai.azure.com, "
            "<resource>.services.ai.azure.com, or <resource>.cognitiveservices.azure.com."
        )
    if port not in (None, 443):
        raise ValueError("azure_endpoint must use the default HTTPS port.")
    path = parts.path.rstrip("/")
    if not _ALLOWED_PATH_RE.match(path):
        raise ValueError(
            "azure_endpoint must be a resource root, a /api/projects/<name> URL, "
            "or end in /openai/v1."
        )
    if not path.endswith(_OPENAI_V1_SUFFIX):
        path = f"{path}{_OPENAI_V1_SUFFIX}"
    return f"https://{hostname}{path}/"


class AzureEntraBearerAuth(httpx.Auth):
    """httpx auth that sets ``Authorization`` from an async token provider.

    Using ``httpx.Auth`` (not event hooks) means httpx strips the header on
    cross-origin redirects.

    :param token_provider: Async callable returning a bearer token, e.g. from
        ``azure.identity.aio.get_bearer_token_provider``.
    """

    def __init__(self, token_provider: Callable[[], Awaitable[str]]) -> None:
        self._token_provider = token_provider

    def sync_auth_flow(self, request: httpx.Request) -> Any:
        del request
        raise RuntimeError("AzureEntraBearerAuth only supports async clients.")

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        try:
            token = await self._token_provider()
        except Exception as error:
            # OpenAIError is not retried by the SDK; the Azure SDK already retries.
            raise openai.OpenAIError(
                "Azure Entra token acquisition failed; "
                "check the execution host identity configuration."
            ) from error
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


class AzureEntraAsyncClient(httpx.AsyncClient):
    """``httpx.AsyncClient`` that owns an async Azure credential and closes it once.

    :param credential: Async credential with an awaitable ``close()``.
    :param token_provider: Async callable returning a bearer token.
    :param kwargs: Forwarded to :class:`httpx.AsyncClient`.
    """

    def __init__(
        self,
        *,
        credential: Any,
        token_provider: Callable[[], Awaitable[str]],
        **kwargs: Any,
    ) -> None:
        super().__init__(auth=AzureEntraBearerAuth(token_provider), **kwargs)
        self._credential = credential
        self._credential_closed = False

    async def aclose(self) -> None:
        try:
            await super().aclose()
        finally:
            if not self._credential_closed:
                self._credential_closed = True
                await self._credential.close()


def build_azure_entra_http_client() -> AzureEntraAsyncClient:
    """Create an http client authenticated with ``DefaultAzureCredential``.

    :returns: An :class:`AzureEntraAsyncClient` that owns its credential.
    :raises ImportError: If ``azure-identity`` or ``aiohttp`` is not installed.
    """
    try:
        from azure.core.credentials_async import AsyncTokenCredential
        from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider

        # Constructing the async credential imports the aiohttp transport.
        credential = DefaultAzureCredential()
    except ImportError as exc:
        raise ImportError(
            "Azure Entra authentication requires 'azure-identity' and 'aiohttp'. "
            "Install them with: pip install 'omnigent[azure]'"
        ) from exc
    return AzureEntraAsyncClient(
        credential=credential,
        token_provider=get_bearer_token_provider(
            cast(AsyncTokenCredential, credential), AZURE_AI_SCOPE
        ),
    )
