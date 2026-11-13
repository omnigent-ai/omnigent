"""Tests for llms.adapters.databricks — payload building and validation."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omnigent.llms.adapters.databricks import DatabricksAdapter


@pytest.fixture
def databricks_requests(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    requests: list[httpx.Request] = []
    client = httpx.AsyncClient

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if json.loads(request.content).get("stream"):
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content='data: {"choices": []}\n\ndata: [DONE]\n\n',
            )
        return httpx.Response(200, json={"choices": []})

    def make_client(**kwargs: Any) -> httpx.AsyncClient:
        return client(**{**kwargs, "transport": httpx.MockTransport(respond)})

    monkeypatch.setattr(httpx, "AsyncClient", make_client)
    return requests


@pytest.mark.parametrize("api_key", [None, ""])
@pytest.mark.parametrize(
    "base_url,stream",
    [
        (None, False),
        ("", False),
        ("https://workspace.example.test/ai-gateway/mlflow/v1", False),
        ("https://workspace.example.test/ai-gateway/mlflow/v1", True),
    ],
)
def test_profile_auth_refreshes_with_default_or_gateway_url(
    monkeypatch: pytest.MonkeyPatch,
    databricks_requests: list[httpx.Request],
    api_key: str | None,
    base_url: str | None,
    stream: bool,
) -> None:
    adapter = DatabricksAdapter()
    tokens = iter(["first-token", "refreshed-token"])
    profiles: list[str | None] = []

    def get_config(profile: str | None) -> SimpleNamespace:
        profiles.append(profile)
        return SimpleNamespace(
            host="https://workspace.example.test",
            authenticate=lambda: {"Authorization": f"Bearer {next(tokens)}"},
        )

    monkeypatch.setattr(adapter, "_get_sdk_config", get_config)
    connection: dict[str, Any] = {
        "profile": "gateway-profile",
        "extra_headers": {"X-Request-ID": "test-request"},
    }
    if base_url is not None:
        connection["base_url"] = base_url
    if api_key is not None:
        connection["api_key"] = api_key

    async def call_twice() -> None:
        for _ in range(2):
            response = await adapter.chat_completions(
                [], "test-model", None, stream, {}, connection_params=connection
            )
            if stream:
                assert not isinstance(response, dict)
                assert [chunk async for chunk in response] == [{"choices": []}]
            else:
                assert response == {"choices": []}

    asyncio.run(call_twice())
    assert profiles == ["gateway-profile", "gateway-profile"]
    assert [str(request.url) for request in databricks_requests] == [
        (base_url or "https://workspace.example.test/serving-endpoints") + "/chat/completions"
    ] * 2
    assert [request.headers["Authorization"] for request in databricks_requests] == [
        "Bearer first-token",
        "Bearer refreshed-token",
    ]
    assert connection.get("api_key") == api_key
    assert all(
        request.headers["X-Request-ID"] == "test-request" for request in databricks_requests
    )


@pytest.mark.parametrize("header", ["Authorization", "authorization"])
@pytest.mark.parametrize("value", ["", " \t"])
def test_profile_auth_ignores_blank_authorization_headers(
    monkeypatch: pytest.MonkeyPatch,
    databricks_requests: list[httpx.Request],
    header: str,
    value: str,
) -> None:
    adapter = DatabricksAdapter()
    monkeypatch.setattr(
        adapter,
        "_resolve_via_sdk",
        lambda profile: {
            "base_url": "https://workspace.example.test/serving-endpoints",
            "api_key": "profile-token",
        },
    )
    headers = {header: value, "X-Request-ID": "test-request"}
    connection: dict[str, Any] = {
        "profile": "gateway-profile",
        "base_url": "https://workspace.example.test/ai-gateway/mlflow/v1",
        "extra_headers": headers,
    }
    asyncio.run(
        adapter.chat_completions([], "test-model", None, False, {}, connection_params=connection)
    )
    assert databricks_requests[0].headers.get_list("Authorization") == ["Bearer profile-token"]
    assert databricks_requests[0].headers["X-Request-ID"] == "test-request"
    assert connection["extra_headers"] is headers
    assert headers == {header: value, "X-Request-ID": "test-request"}
    assert "api_key" not in connection


def test_gateway_url_fallback_keeps_selected_profile(
    monkeypatch: pytest.MonkeyPatch, databricks_requests: list[httpx.Request]
) -> None:
    from omnigent.llms.adapters import databricks as adapter_mod
    from omnigent.runtime.credentials.databricks import WorkspaceCreds

    adapter = DatabricksAdapter()
    monkeypatch.setattr(adapter, "_resolve_via_sdk", lambda profile: None)
    profiles: list[str | None] = []

    def resolve(profile: str | None) -> WorkspaceCreds:
        profiles.append(profile)
        return WorkspaceCreds("https://workspace.example.test", "fallback-token")

    monkeypatch.setattr(adapter_mod, "resolve_databricks_workspace", resolve)
    asyncio.run(
        adapter.chat_completions(
            [],
            "test-model",
            None,
            False,
            {},
            connection_params={
                "profile": "gateway-profile",
                "base_url": "https://workspace.example.test/ai-gateway/mlflow/v1",
            },
        )
    )
    assert profiles == ["gateway-profile"]
    assert databricks_requests[0].headers["Authorization"] == "Bearer fallback-token"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://other.example.test/ai-gateway/mlflow/v1",
        "http://workspace.example.test/ai-gateway/mlflow/v1",
        "https://workspace.example.test:8443/ai-gateway/mlflow/v1",
        "https://workspace.example.test@other.example.test/v1",
    ],
)
def test_profile_token_is_not_sent_to_another_origin(
    monkeypatch: pytest.MonkeyPatch,
    databricks_requests: list[httpx.Request],
    base_url: str,
) -> None:
    from omnigent.errors import ErrorCode, OmnigentError

    adapter = DatabricksAdapter()
    monkeypatch.setattr(
        adapter,
        "_resolve_via_sdk",
        lambda profile: {
            "base_url": "https://workspace.example.test/serving-endpoints",
            "api_key": "workspace-token",
        },
    )
    with pytest.raises(OmnigentError) as raised:
        asyncio.run(
            adapter.chat_completions(
                [],
                "test-model",
                None,
                False,
                {},
                connection_params={
                    "profile": "gateway-profile",
                    "base_url": base_url,
                    "extra_headers": {"X-Request-ID": "test-request"},
                },
            )
        )
    assert raised.value.code == ErrorCode.INVALID_INPUT
    assert "workspace-token" not in str(raised.value)
    assert databricks_requests == []


@pytest.mark.parametrize(
    "connection,expected_authorization",
    [
        pytest.param(
            {
                "base_url": "https://proxy.example.test/v1",
                "profile": "gateway-profile",
                "api_key": "explicit-key",
            },
            "Bearer explicit-key",
            id="api-key",
        ),
        pytest.param(
            {"base_url": "http://localhost:8123/v1"},
            None,
            id="url-only",
        ),
        pytest.param(
            {
                "base_url": "http://localhost:8123/v1",
                "extra_headers": {"Host": "gateway.example.test", "X-Proxy-Auth": "test-context"},
            },
            None,
            id="proxy-headers",
        ),
        *[
            pytest.param(
                {
                    "base_url": "https://proxy.example.test/v1",
                    "profile": "gateway-profile",
                    "extra_headers": {header: "Bearer header-key"},
                },
                "Bearer header-key",
                id=header,
            )
            for header in ("Authorization", "authorization")
        ],
    ],
)
def test_explicit_connection_does_not_resolve_workspace_credentials(
    monkeypatch: pytest.MonkeyPatch,
    databricks_requests: list[httpx.Request],
    connection: dict[str, Any],
    expected_authorization: str | None,
) -> None:
    adapter = DatabricksAdapter()

    def unexpected_resolution(profile: str | None) -> None:
        raise AssertionError("Explicit credentials must not resolve a Databricks profile")

    monkeypatch.setattr(adapter, "_resolve_via_sdk", unexpected_resolution)
    asyncio.run(
        adapter.chat_completions(
            [],
            "test-model",
            None,
            False,
            {},
            connection_params=connection,
        )
    )
    assert str(databricks_requests[0].url) == connection["base_url"] + "/chat/completions"
    assert databricks_requests[0].headers.get("Authorization") == expected_authorization
    for key, value in connection.get("extra_headers", {}).items():
        assert databricks_requests[0].headers[key] == value


def test_stream_options_stripped_from_streaming_payload() -> None:
    """
    Databricks model serving rejects ``stream_options`` with 400.

    The base ``OpenAICompatibleAdapter._build_payload`` always injects
    ``stream_options: {include_usage: true}`` when ``stream=True``.
    ``DatabricksAdapter`` must remove it before the request is sent.
    """
    adapter = DatabricksAdapter()
    payload: dict[str, Any] = adapter._build_payload(
        messages=[{"role": "user", "content": "hi"}],
        model="databricks-kimi-k2-6",
        tools=None,
        stream=True,
        extra={},
    )
    assert "stream_options" not in payload
    assert payload["stream"] is True


def test_non_streaming_payload_has_no_stream_options() -> None:
    """Non-streaming payloads never had stream_options; confirm still clean."""
    adapter = DatabricksAdapter()
    payload: dict[str, Any] = adapter._build_payload(
        messages=[{"role": "user", "content": "hi"}],
        model="databricks-kimi-k2-6",
        tools=None,
        stream=False,
        extra={},
    )
    assert "stream_options" not in payload
    assert "stream" not in payload


def test_missing_base_url_raises_when_no_auto_resolve(monkeypatch: Any) -> None:
    """
    When ``connection_params`` has no ``base_url`` and auto-resolution from
    ``~/.databrickscfg`` also fails, ``chat_completions`` raises
    ``OmnigentError``.
    """
    import asyncio

    from omnigent.errors import OmnigentError
    from omnigent.llms.adapters import databricks as adapter_mod

    def _raise(profile: Any) -> None:
        raise OSError("Could not resolve Databricks workspace credentials.")

    monkeypatch.setattr(adapter_mod, "resolve_databricks_workspace", _raise)

    adapter = DatabricksAdapter()
    monkeypatch.setattr(adapter, "_resolve_via_sdk", lambda profile: None)

    async def call() -> None:
        await adapter.chat_completions(
            messages=[{"role": "user", "content": "hi"}],
            model="databricks-kimi-k2-6",
            tools=None,
            stream=False,
            extra={},
            connection_params={"api_key": "tok"},  # base_url absent, no auto-resolve
        )

    try:
        asyncio.run(call())
        raise AssertionError("Expected OmnigentError was not raised")
    except OmnigentError as exc:
        assert "Could not resolve" in str(exc)


def test_auto_resolve_used_when_no_connection_params(monkeypatch: Any) -> None:
    """
    When ``connection_params`` is absent, the adapter calls
    :func:`~omnigent.runtime.credentials.databricks.resolve_databricks_workspace`
    and uses the result.

    We don't make a real HTTP call here — we just verify that the resolved
    credentials are forwarded to the parent ``chat_completions``.
    """
    import asyncio

    from omnigent.llms.adapters import databricks as adapter_mod
    from omnigent.runtime.credentials.databricks import WorkspaceCreds

    monkeypatch.setattr(
        adapter_mod,
        "resolve_databricks_workspace",
        lambda profile: WorkspaceCreds(host="https://workspace.example.test", token="dapi-tok"),
    )

    captured: list[dict[str, Any]] = []

    async def _fake_parent(
        self: Any,
        messages: Any,
        model: Any,
        tools: Any,
        stream: Any,
        extra: Any,
        *,
        connection_params: Any = None,
        timeout: Any = None,
    ) -> dict[str, Any]:
        captured.append({"connection_params": connection_params})
        return {}

    from omnigent.llms.adapters.openai import OpenAICompatibleAdapter

    monkeypatch.setattr(OpenAICompatibleAdapter, "chat_completions", _fake_parent)

    adapter = DatabricksAdapter()
    monkeypatch.setattr(adapter, "_resolve_via_sdk", lambda profile: None)
    asyncio.run(
        adapter.chat_completions(
            messages=[{"role": "user", "content": "hi"}],
            model="databricks-kimi-k2-6",
            tools=None,
            stream=False,
            extra={},
            connection_params=None,
        )
    )

    assert len(captured) == 1
    assert (
        captured[0]["connection_params"]["base_url"]
        == "https://workspace.example.test/serving-endpoints"
    )
    assert captured[0]["connection_params"]["api_key"] == "dapi-tok"
