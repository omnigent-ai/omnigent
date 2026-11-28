"""Tests for Azure Entra (keyless) authentication in the openai-agents executor."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import httpx
import openai
import pytest

from omnigent.inner import azure_entra
from omnigent.inner.azure_entra import AzureEntraAsyncClient, normalize_azure_endpoint
from omnigent.inner.openai_agents_sdk_executor import OpenAIAgentsSDKExecutor


class _FakeCredential:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


def _token_sequence(*tokens: str):
    remaining = list(tokens)

    async def provider() -> str:
        return remaining.pop(0)

    return provider


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("https://res.openai.azure.com", "https://res.openai.azure.com/openai/v1/"),
        ("https://res.openai.azure.com/", "https://res.openai.azure.com/openai/v1/"),
        (
            "https://res.services.ai.azure.com/api/projects/proj",
            "https://res.services.ai.azure.com/api/projects/proj/openai/v1/",
        ),
        (
            "https://res.services.ai.azure.com/api/projects/proj/openai/v1",
            "https://res.services.ai.azure.com/api/projects/proj/openai/v1/",
        ),
        ("https://RES.OpenAI.Azure.com:443/openai/v1/", "https://res.openai.azure.com/openai/v1/"),
        (
            "https://res.cognitiveservices.azure.com",
            "https://res.cognitiveservices.azure.com/openai/v1/",
        ),
    ],
)
def test_normalize_azure_endpoint_accepts_supported_shapes(endpoint: str, expected: str) -> None:
    assert normalize_azure_endpoint(endpoint) == expected


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        "http://res.openai.azure.com",
        "https://user:secret@res.openai.azure.com",
        "https://res.openai.azure.com/openai/v1?api-version=1",
        "https://res.openai.azure.com/#frag",
        "https://res.openai.azure.com/other/path",
        "https://res.openai.azure.com:notaport/",
        "https://[::1/",
        "https://res.openai.azure.com/\nopenai/v1",
        "https:///openai/v1",
        "https://attacker.example.com/openai/v1",
        "https://res.openai.azure.com.evil.test/openai/v1",
        "https://res.openai.azure.com.evil.test",
        "https://openai.azure.com/openai/v1",
        "https://a.b.openai.azure.com/openai/v1",
        "https://-res.openai.azure.com",
        "https://res.management.azure.com",
        "https://res.openai.azure.com:8443/openai/v1",
        "https://[::1]/openai/v1",
        "https://[2001:db8::1]:443/",
        "https://res.openai.azure.com/evil/openai/v1",
        "https://res.openai.azure.com/api/projects/./openai/v1",
        "https://res.openai.azure.com/api/projects/../openai/v1",
        "https://res.openai.azure.com/api/projects/%2e%2e/openai/v1",
        "https://res.openai.azure.com/api/projects/a%2Fb/openai/v1",
        "https://res.openai.azure.com/api/projects/p.q",
        "https://res.openai.azure.com/api/projects/p/extra/openai/v1",
        "https://res.openai.azure.com/openai/v1/openai/v1",
    ],
)
def test_normalize_azure_endpoint_rejects_unsafe_input(endpoint: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        normalize_azure_endpoint(endpoint)
    assert "secret" not in str(excinfo.value)


def test_azure_extra_declares_async_transport() -> None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    extras = tomllib.loads(pyproject.read_text())["project"]["optional-dependencies"]
    assert any(dep.startswith("azure-core[aio]") for dep in extras["azure"])


def test_missing_async_transport_raises_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("azure.identity.aio")
    monkeypatch.setitem(sys.modules, "aiohttp", None)
    with pytest.raises(ImportError, match=r"omnigent\[azure\]"):
        azure_entra.build_azure_entra_http_client()


async def test_client_refreshes_token_per_request_and_closes_credential_once() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={})

    credential = _FakeCredential()
    client = AzureEntraAsyncClient(
        credential=credential,
        token_provider=_token_sequence("tok-1", "tok-2"),
        transport=httpx.MockTransport(handler),
    )
    await client.get("https://res.openai.azure.com/openai/v1/models")
    await client.get("https://res.openai.azure.com/openai/v1/models")
    assert seen == ["Bearer tok-1", "Bearer tok-2"]

    await client.aclose()
    await client.aclose()
    assert credential.close_calls == 1


async def test_failed_credential_sends_no_request() -> None:
    sent: list[httpx.Request] = []

    async def failing_provider() -> str:
        raise RuntimeError("no Azure credential available")

    client = AzureEntraAsyncClient(
        credential=_FakeCredential(),
        token_provider=failing_provider,
        transport=httpx.MockTransport(lambda request: sent.append(request) or httpx.Response(200)),
    )
    with pytest.raises(openai.OpenAIError, match="token acquisition failed") as excinfo:
        await client.get("https://res.openai.azure.com/openai/v1/models")
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert sent == []
    await client.aclose()


async def test_token_failure_is_not_retried_by_openai_sdk() -> None:
    calls = 0
    sent: list[httpx.Request] = []

    async def failing_provider() -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("no Azure credential available")

    http_client = AzureEntraAsyncClient(
        credential=_FakeCredential(),
        token_provider=failing_provider,
        transport=httpx.MockTransport(lambda request: sent.append(request) or httpx.Response(200)),
    )
    client = openai.AsyncOpenAI(
        api_key="unused",
        base_url="https://res.openai.azure.com/openai/v1/",
        http_client=http_client,
        max_retries=2,
    )
    with pytest.raises(openai.OpenAIError, match="token acquisition failed") as excinfo:
        await client.models.list()
    assert not isinstance(excinfo.value, openai.APIConnectionError)
    assert calls == 1
    assert sent == []
    await client.close()


async def test_cross_origin_redirect_drops_token() -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = request.headers.get("Authorization")
        if request.url.host == "res.openai.azure.com":
            return httpx.Response(302, headers={"Location": "https://attacker.example.test/x"})
        return httpx.Response(200)

    client = AzureEntraAsyncClient(
        credential=_FakeCredential(),
        token_provider=_token_sequence("tok-1", "tok-2"),
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    await client.get("https://res.openai.azure.com/openai/v1/models")
    assert seen == {"res.openai.azure.com": "Bearer tok-1", "attacker.example.test": None}
    await client.aclose()


def _install_fake_http_client(monkeypatch: pytest.MonkeyPatch) -> list[_FakeCredential]:
    created: list[_FakeCredential] = []

    def fake_build() -> AzureEntraAsyncClient:
        credential = _FakeCredential()
        created.append(credential)
        return AzureEntraAsyncClient(
            credential=credential,
            token_provider=_token_sequence("tok"),
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        )

    monkeypatch.setattr(azure_entra, "build_azure_entra_http_client", fake_build)
    return created


async def test_executor_builds_azure_client_and_closes_credential_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _install_fake_http_client(monkeypatch)
    executor = OpenAIAgentsSDKExecutor(
        azure_entra=True,
        base_url_override="https://res.openai.azure.com",
        model="my-deployment",
    )
    assert str(executor._client.base_url) == "https://res.openai.azure.com/openai/v1/"
    await executor.close()
    await executor.close()
    assert [credential.close_calls for credential in created] == [1]


@pytest.mark.parametrize(
    "overrides",
    [
        {"api_key": "sk-test"},
        {"profile": "dev"},
        {"gateway_host": "https://example.test"},
        {"gateway_auth_command": "printf %s tok"},
        {"base_url_override": None},
        {"model": None},
        {"base_url_override": "https://res.openai.azure.com/?x=1"},
        {"base_url_override": "https://attacker.example.com/openai/v1"},
        {"base_url_override": "https://res.openai.azure.com.evil.test"},
        {"base_url_override": "https://[::1]/openai/v1"},
        {"client": object()},
    ],
)
def test_executor_rejects_conflicts_without_creating_credential(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, object]
) -> None:
    created = _install_fake_http_client(monkeypatch)
    kwargs: dict[str, object] = {
        "base_url_override": "https://res.openai.azure.com",
        "model": "my-deployment",
        **overrides,
    }
    with pytest.raises(ValueError):
        OpenAIAgentsSDKExecutor(azure_entra=True, **kwargs)  # type: ignore[arg-type]
    assert created == []


@pytest.mark.parametrize(
    ("raw", "expected"), [("", False), ("0", False), ("TRUE", True), ("1", True)]
)
def test_harness_marker_parses_strict_bool(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool
) -> None:
    from omnigent.inner.openai_agents_sdk_harness import _ENV_AZURE_ENTRA, _parse_strict_bool

    monkeypatch.setenv(_ENV_AZURE_ENTRA, raw)
    assert _parse_strict_bool(_ENV_AZURE_ENTRA) is expected


def test_harness_marker_rejects_ambiguous_value(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.inner.openai_agents_sdk_harness import _ENV_AZURE_ENTRA, _parse_strict_bool

    monkeypatch.setenv(_ENV_AZURE_ENTRA, "yes")
    with pytest.raises(OSError):
        _parse_strict_bool(_ENV_AZURE_ENTRA)
