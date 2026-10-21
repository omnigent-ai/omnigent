"""Model capability checks at Codex's native settings boundary."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from cachetools import TTLCache

from omnigent.harnesses.codex_native import app_server
from omnigent.harnesses.codex_native.bridge import read_codex_config_effort
from omnigent.server.smart_routing import RoutingSettings, parse_routing_tables


@pytest.fixture(autouse=True)
def _fresh_effort_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep each case's capability discovery out of the shared module caches."""
    monkeypatch.setattr(app_server, "_effort_catalog_cache", TTLCache(maxsize=8, ttl=60))
    monkeypatch.setattr(app_server, "_effort_catalog_misses", TTLCache(maxsize=8, ttl=60))


@pytest.mark.parametrize(
    ("requested", "fallback", "advertised", "expected"),
    [
        ("ultra", "medium", ["low", "medium", "high", "max", "ultra"], "medium"),
        ("high", "medium", ["low", "medium", "high", "max", "ultra"], "high"),
        ("ultra", "high", ["low", "max", "ultra"], "low"),
        ("ultra", "medium", ["max", "ultra"], "medium"),
    ],
)
def test_gateway_restrictions_still_apply_to_catalog_supported_efforts(
    monkeypatch: pytest.MonkeyPatch,
    requested: str,
    fallback: str,
    advertised: list[str],
    expected: str,
) -> None:
    """A CLI's built-in catalog does not override restrictions of the serving gateway."""
    settings = RoutingSettings(
        **parse_routing_tables(
            {
                "effort_caps": {
                    "gpt-5.6-sol": {"fallback": fallback, "unsupported": ["max", "ultra"]}
                }
            }
        )
    )
    monkeypatch.setattr(
        "omnigent.runtime._globals._caps", SimpleNamespace(routing_settings=settings)
    )
    catalog = [
        {
            "id": "gpt-5.6-sol",
            "supportedReasoningEfforts": [{"reasoningEffort": value} for value in advertised],
        }
    ]
    assert app_server.clamp_codex_effort_for_model(requested, "gpt-5.6-sol", catalog) == expected


@pytest.mark.parametrize(
    ("effort", "supported", "expected", "schema"),
    [
        ("minimal", ["low", "medium", "high", "xhigh"], "low", "id"),
        ("max", ["low", "medium", "high", "xhigh"], "xhigh", "id"),
        ("ultra", ["low", "high"], "high", "id"),
        ("medium", ["high", "low"], "low", "id"),
        ("max", ["high", "max", "ultra"], "max", "id"),
        ("ultra", ["high", "max", "ultra"], "ultra", "id"),
        ("none", ["none", "low"], "none", "id"),
        (None, ["low", "medium", "high"], None, "id"),
        ("max", ["low", "medium", "high", "xhigh"], "xhigh", "model"),
        ("max", ["low", "medium", "high", "xhigh"], "xhigh", "debug"),
    ],
)
def test_effort_uses_the_matching_models_advertised_levels(
    effort: str | None, supported: list[str], expected: str | None, schema: str
) -> None:
    """Both catalog shapes and model aliases use the model's own ordered ladder."""
    catalog: object
    if schema == "debug":
        catalog = {
            "models": [
                {
                    "slug": "gpt-5.6-sol",
                    "supported_reasoning_levels": [{"effort": value} for value in supported],
                }
            ]
        }
    else:
        catalog = [
            {
                schema: "gpt-5.6-sol",
                "supportedReasoningEfforts": [{"reasoningEffort": value} for value in supported],
            }
        ]
    assert (
        app_server.clamp_codex_effort_for_model(effort, "system.ai.gpt-5-6-sol", catalog)
        == expected
    )


@pytest.mark.parametrize(
    "catalog",
    [
        None,
        [],
        {"models": "unavailable"},
        [{"id": "gpt-5.6-sol"}],
        [{"id": "gpt-5.6-sol", "supportedReasoningEfforts": []}],
        [{"id": "gpt-5.6-sol", "supportedReasoningEfforts": "high"}],
        [{"id": "gpt-5.6-sol", "supportedReasoningEfforts": [None, {}, {"reasoningEffort": 1}]}],
        [
            {
                "id": "another-model",
                "isDefault": True,
                "supportedReasoningEfforts": [{"reasoningEffort": "low"}],
            }
        ],
    ],
)
def test_missing_capabilities_do_not_guess_another_models_effort(catalog: object) -> None:
    """Missing or malformed capabilities preserve the existing fallback rules."""
    assert app_server.clamp_codex_effort_for_model("ultra", "gpt-5.6-sol", catalog) == "ultra"
    assert app_server.clamp_codex_effort_for_model("ultra", None, catalog) == "ultra"
    assert app_server.clamp_codex_effort_for_model("ultra", "glm-5-2", catalog) == "medium"


async def test_live_effort_validation_reads_hidden_models_and_later_pages() -> None:
    client = AsyncMock(spec=app_server.CodexAppServerClient)
    client.request.side_effect = [
        {"result": {"data": [{"id": "unrelated-model"}], "nextCursor": "page2"}},
        {
            "result": {
                "data": [
                    {
                        "id": "gpt-5.4",
                        "hidden": True,
                        "supportedReasoningEfforts": [{"reasoningEffort": "xhigh"}],
                    }
                ],
                "nextCursor": None,
            }
        },
    ]

    assert await app_server.resolve_codex_effort_for_model(client, "max", "gpt-5.4") == "xhigh"
    assert client.request.await_args_list == [
        call("model/list", {"includeHidden": True}),
        call("model/list", {"includeHidden": True, "cursor": "page2"}),
    ]


@pytest.mark.parametrize(
    "config,expected",
    [
        ({}, "gpt-5.4"),
        ({"model_provider": None}, "gpt-5.4"),
        ({"model_provider": "openai"}, "gpt-5.4"),
        ({"model_providers": {"openai": {"base_url": None}}}, "gpt-5.4"),
        ({"model_provider": "gateway"}, "system.ai.gpt-5-4"),
        ({"model_provider": []}, "system.ai.gpt-5-4"),
        (None, "system.ai.gpt-5-4"),
        ({"openai_base_url": "https://gateway.example.test/v1"}, "system.ai.gpt-5-4"),
        ({"chatgpt_base_url": "https://chatgpt.com/backend-api/"}, "gpt-5.4"),
        (
            {"model_providers": {"openai": {"base_url": "https://gateway.example.test/v1"}}},
            "system.ai.gpt-5-4",
        ),
    ],
)
async def test_account_alias_uses_the_live_provider_config(
    tmp_path: Path, config: object, expected: str
) -> None:
    """Live route evidence wins over copied config; null means the built-in default."""
    (tmp_path / "config.toml").write_text('model_provider = "copied-gateway"\n')
    (tmp_path / "auth.json").write_text('{"tokens":{"refresh_token":"test-refresh-token"}}')
    client = AsyncMock(spec=app_server.CodexAppServerClient)
    client.request.side_effect = [
        {"result": {"config": config}},
        {"result": {"thread": {"modelProvider": "openai"}}},
        {"result": {"data": [{"id": "gpt-5.4"}]}},
    ]
    model, _catalog = await app_server.resolve_codex_chatgpt_model(
        client, "system.ai.gpt-5-4", thread_id="thread-account", codex_home=tmp_path
    )
    assert model == expected
    assert client.request.call_count == (3 if expected == "gpt-5.4" else 1)


@pytest.mark.parametrize("stalled_method", ["config/read", "thread/read", "model/list"])
async def test_account_alias_route_discovery_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stalled_method: str
) -> None:
    """An unresponsive config/read leaves the requested model unchanged."""
    (tmp_path / "auth.json").write_text('{"tokens":{"refresh_token":"test-refresh-token"}}')
    client = AsyncMock(spec=app_server.CodexAppServerClient)

    async def stalled(method: str, _params: object) -> dict[str, object]:
        if method == stalled_method:
            await asyncio.Event().wait()
        if method == "config/read":
            return {"result": {"config": {"model_provider": "openai"}}}
        return {"result": {"thread": {"modelProvider": "openai"}}}

    client.request.side_effect = stalled
    monkeypatch.setattr(app_server, "_EFFORT_CATALOG_TIMEOUT_SECONDS", 0.05)
    model, catalog = await app_server.resolve_codex_chatgpt_model(
        client, "system.ai.gpt-5-4", thread_id="thread-account", codex_home=tmp_path
    )
    assert model == "system.ai.gpt-5-4"
    methods = ["config/read", "thread/read", "model/list"]
    assert [item.args[0] for item in client.request.await_args_list] == methods[
        : methods.index(stalled_method) + 1
    ]
    if stalled_method == "model/list":
        await app_server.resolve_codex_effort_for_model(client, "max", model, catalog=catalog)
        assert client.request.call_count == 3


@pytest.mark.parametrize("thread", [{"modelProvider": "gateway"}, {}, None])
async def test_account_alias_requires_the_loaded_threads_provider(
    tmp_path: Path, thread: object
) -> None:
    (tmp_path / "auth.json").write_text('{"tokens":{"refresh_token":"test-refresh-token"}}')
    client = AsyncMock(spec=app_server.CodexAppServerClient)
    client.request.side_effect = [
        {"result": {"config": {"model_provider": "openai"}}},
        {"result": {"thread": thread}},
    ]
    model, _catalog = await app_server.resolve_codex_chatgpt_model(
        client, "system.ai.gpt-5-4", thread_id="thread-account", codex_home=tmp_path
    )
    assert model == "system.ai.gpt-5-4"
    assert client.request.call_count == 2


async def test_account_alias_metadata_shares_one_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "auth.json").write_text('{"tokens":{"refresh_token":"test-refresh-token"}}')
    client = AsyncMock(spec=app_server.CodexAppServerClient)

    async def delayed(method: str, _params: object) -> dict[str, object]:
        await asyncio.sleep(0.03)
        if method == "config/read":
            return {"result": {"config": {"model_provider": "openai"}}}
        return {"result": {"thread": {"modelProvider": "openai"}}}

    client.request.side_effect = delayed
    monkeypatch.setattr(app_server, "_EFFORT_CATALOG_TIMEOUT_SECONDS", 0.05)
    model, _catalog = await app_server.resolve_codex_chatgpt_model(
        client, "system.ai.gpt-5-4", thread_id="thread-account", codex_home=tmp_path
    )
    assert model == "system.ai.gpt-5-4"
    assert all(item.args[0] != "model/list" for item in client.request.await_args_list)


async def test_account_alias_metadata_preserves_cancellation(tmp_path: Path) -> None:
    (tmp_path / "auth.json").write_text('{"tokens":{"refresh_token":"test-refresh-token"}}')
    client = AsyncMock(spec=app_server.CodexAppServerClient)
    client.request.side_effect = [
        {"result": {"config": {"model_provider": "openai"}}},
        asyncio.CancelledError(),
    ]
    with pytest.raises(asyncio.CancelledError):
        await app_server.resolve_codex_chatgpt_model(
            client, "system.ai.gpt-5-4", thread_id="thread-account", codex_home=tmp_path
        )


@pytest.mark.parametrize("catalog_failure", [False, True], ids=["empty", "failed"])
async def test_alias_and_effort_share_unavailable_capabilities(
    tmp_path: Path, catalog_failure: bool
) -> None:
    """One settings request never repeats a failed or empty model/list call."""
    (tmp_path / "auth.json").write_text('{"tokens":{"refresh_token":"test-refresh-token"}}')
    client = AsyncMock(spec=app_server.CodexAppServerClient)
    client.request.side_effect = [
        {"result": {"config": {"model_provider": "openai"}}},
        {"result": {"thread": {"modelProvider": "openai"}}},
        RuntimeError("discovery unavailable") if catalog_failure else {"result": {"data": []}},
    ]
    model, catalog = await app_server.resolve_codex_chatgpt_model(
        client, "system.ai.gpt-5-4", thread_id="thread-account", codex_home=tmp_path
    )
    effort = await app_server.resolve_codex_effort_for_model(client, "max", model, catalog=catalog)
    assert (model, effort) == ("system.ai.gpt-5-4", "max")
    assert client.request.await_args_list == [
        call("config/read", {"includeLayers": False}),
        call("thread/read", {"threadId": "thread-account", "includeTurns": False}),
        call("model/list", {"includeHidden": True}),
    ]


@pytest.mark.parametrize("default", ["high", None])
async def test_effort_reset_requires_the_models_advertised_default(default: str | None) -> None:
    """A reset uses the actual default or fails instead of sending Codex a null no-op."""
    client = AsyncMock(spec=app_server.CodexAppServerClient)
    client.request.return_value = {
        "result": {
            "data": [
                {
                    "id": "gpt-5.4",
                    "defaultReasoningEffort": default,
                    "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
                }
            ]
        }
    }
    if default is None:
        with pytest.raises(ValueError, match="default reasoning effort"):
            await app_server.resolve_codex_effort_for_model(client, None, "gpt-5.4")
    else:
        assert await app_server.resolve_codex_effort_for_model(client, None, "gpt-5.4") == default


async def test_effort_reset_for_an_unlisted_model_is_refused() -> None:
    """A reset never borrows another model's default when the catalog lacks the model."""
    client = AsyncMock(spec=app_server.CodexAppServerClient)
    client.request.return_value = {
        "result": {
            "data": [
                {
                    "id": "gpt-5.4",
                    "defaultReasoningEffort": "high",
                    "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
                }
            ]
        }
    }
    with pytest.raises(ValueError, match="capabilities unavailable"):
        await app_server.resolve_codex_effort_for_model(client, None, "gpt-unlisted")


async def test_effort_reset_names_a_catalog_it_could_not_read() -> None:
    """A reset refused because discovery failed says so, unlike one for an unlisted model."""
    client = AsyncMock(spec=app_server.CodexAppServerClient)
    client.request.side_effect = ConnectionError("app-server unreachable")

    with pytest.raises(ValueError, match="Could not read the Codex model catalog"):
        await app_server.resolve_codex_effort_for_model(client, None, "gpt-5.4")


@pytest.mark.parametrize("boundary", ["same-server", "new-server", "expired"])
async def test_successful_catalog_discovery_is_shared_between_turn_clients(
    monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    now = 0.0
    monkeypatch.setattr(
        app_server, "_effort_catalog_cache", TTLCache(maxsize=2, ttl=60, timer=lambda: now)
    )
    first = AsyncMock(spec=app_server.CodexAppServerClient)
    second = AsyncMock(spec=app_server.CodexAppServerClient)
    for client, supported in ((first, ["low", "xhigh"]), (second, ["medium"])):
        client.request.return_value = {
            "result": {
                "data": [
                    {
                        "id": "gpt-5.4",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": value} for value in supported
                        ],
                    }
                ]
            }
        }
    transport = "ws://127.0.0.1:12345"
    assert (
        await app_server.resolve_codex_effort_for_model(
            first, "max", "gpt-5.4", transport=transport
        )
        == "xhigh"
    )
    if boundary == "new-server":
        transport = "ws://127.0.0.1:54321"
    elif boundary == "expired":
        now = 61.0
    expected = "low" if boundary == "same-server" else "medium"
    assert (
        await app_server.resolve_codex_effort_for_model(
            second, "minimal", "gpt-5.4", transport=transport
        )
        == expected
    )
    assert first.request.await_count == 1
    assert second.request.await_count == (0 if boundary == "same-server" else 1)


async def test_cached_catalog_without_the_model_is_read_again() -> None:
    """A model missing from the cached rows is looked up again instead of failing its reset."""
    client = AsyncMock(spec=app_server.CodexAppServerClient)
    first = {
        "id": "gpt-5.4",
        "defaultReasoningEffort": "high",
        "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
    }
    client.request.return_value = {"result": {"data": [first]}}
    transport = "ws://127.0.0.1:12345"
    assert (
        await app_server.resolve_codex_effort_for_model(
            client, None, "gpt-5.4", transport=transport
        )
        == "high"
    )
    added = {
        "id": "gpt-5.6-sol",
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [{"reasoningEffort": "medium"}],
    }
    client.request.return_value = {"result": {"data": [first, added]}}
    assert (
        await app_server.resolve_codex_effort_for_model(
            client, None, "gpt-5.6-sol", transport=transport
        )
        == "medium"
    )
    assert (
        await app_server.resolve_codex_effort_for_model(
            client, None, "gpt-5.4", transport=transport
        )
        == "high"
    )
    assert client.request.await_count == 2
    # A model the fresh rows still lack keeps the gateway fallback without refetching each turn.
    for _ in range(2):
        assert (
            await app_server.resolve_codex_effort_for_model(
                client, "ultra", "glm-5-2", transport=transport
            )
            == "medium"
        )
    assert client.request.await_count == 3


@pytest.mark.parametrize("failure", ["unavailable", "malformed", "empty", "timeout"])
async def test_live_catalog_failures_do_not_block_effort_updates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    client = AsyncMock(spec=app_server.CodexAppServerClient)
    if failure == "unavailable":
        client.request.side_effect = app_server.CodexAppServerResponseError(
            {"code": -32601, "message": "method unavailable"}
        )
    elif failure in ("malformed", "empty"):
        client.request.return_value = {"result": {"data": None if failure == "malformed" else []}}
    else:

        async def stalled(*_args: object) -> None:
            await asyncio.Event().wait()

        client.request.side_effect = stalled
        monkeypatch.setattr(app_server, "_EFFORT_CATALOG_TIMEOUT_SECONDS", 0.01)

    transport = str(tmp_path / "app-server.sock")
    assert (
        await app_server.resolve_codex_effort_for_model(
            client, "ultra", "gpt-5.6-sol", transport=transport
        )
        == "ultra"
    )
    assert (
        await app_server.resolve_codex_effort_for_model(
            client, "ultra", "glm-5-2", transport=transport
        )
        == "medium"
    )
    client.request.side_effect = None
    client.request.return_value = {
        "result": {
            "data": [
                {"id": "gpt-5.6-sol", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}
            ]
        }
    }
    assert (
        await app_server.resolve_codex_effort_for_model(
            client, "ultra", "gpt-5.6-sol", transport=transport
        )
        == "high"
    )


@pytest.mark.parametrize(
    ("model", "effort", "expected"),
    [
        ("gpt-5.4", "minimal", "low"),
        ("gpt-5.4", "max", "xhigh"),
        ("gpt-5.6-sol", "ultra", "ultra"),
        (None, "ultra", "ultra"),
        ("glm-5-2", "ultra", "medium"),
    ],
)
async def test_resume_applies_and_mirrors_supported_effort(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    model: str | None,
    effort: str,
    expected: str,
) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(
        (f'model = "{model}"\n' if model else "") + 'model_reasoning_effort = "medium"\n'
    )
    client = AsyncMock(spec=app_server.CodexAppServerClient)
    client.request.side_effect = lambda method, params: (
        {
            "result": {
                "data": [
                    {
                        "id": "gpt-5.4",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": value}
                            for value in ("low", "medium", "high", "xhigh")
                        ],
                    }
                ]
            }
        }
        if method == "model/list"
        else {"result": {}}
    )
    client_calls: list[tuple[str, str]] = []

    def _client_for_transport(transport: str, *, client_name: str) -> AsyncMock:
        client_calls.append((transport, client_name))
        return client

    monkeypatch.setattr(app_server, "client_for_transport", _client_for_transport)
    transport = str(tmp_path / "app-server.sock")

    await app_server.apply_codex_thread_effort(
        transport,
        "thread_resumed",
        effort,
        model=None if model == "gpt-5.4" else model,
        bridge_dir=tmp_path,
    )

    assert client_calls == [(transport, "omnigent-codex-native-effort")]

    client.request.assert_awaited_with(
        "thread/settings/update", {"threadId": "thread_resumed", "effort": expected}
    )
    assert read_codex_config_effort(tmp_path) == expected
    assert client.request.await_count == (2 if model else 1)
    client.connect.assert_awaited_once()
    client.close.assert_awaited_once()


async def test_resume_records_an_effort_its_config_write_lost(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A resumed effort whose config write fails stays recorded for later updates."""
    from omnigent.harnesses.codex_native import bridge as codex_native_bridge

    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text('model = "gpt-5.4"\nmodel_reasoning_effort = "minimal"\n')
    client = AsyncMock(spec=app_server.CodexAppServerClient)
    client.request.side_effect = lambda method, params: (
        {
            "result": {
                "data": [
                    {
                        "id": "gpt-5.4",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": value}
                            for value in ("low", "medium", "high", "xhigh")
                        ],
                    }
                ]
            }
        }
        if method == "model/list"
        else {"result": {}}
    )
    monkeypatch.setattr(app_server, "client_for_transport", lambda *args, **kwargs: client)
    monkeypatch.setattr(codex_native_bridge, "write_codex_config_effort", lambda *_: False)

    await app_server.apply_codex_thread_effort(
        str(tmp_path / "app-server.sock"), "thread_resumed", "minimal", bridge_dir=tmp_path
    )

    client.request.assert_awaited_with(
        "thread/settings/update", {"threadId": "thread_resumed", "effort": "low"}
    )
    assert read_codex_config_effort(tmp_path) == "minimal"
    assert codex_native_bridge.read_unmirrored_codex_settings(tmp_path) == {"effort": "low"}


@pytest.mark.parametrize("stalled_phase", ["connect", "update", "close"])
async def test_resume_effort_update_times_out_and_closes_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stalled_phase: str,
) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text('model_reasoning_effort = "medium"\n')
    client = AsyncMock(spec=app_server.CodexAppServerClient)

    async def stalled(*args: object, **kwargs: object) -> None:
        await asyncio.Event().wait()

    if stalled_phase == "connect":
        client.connect.side_effect = stalled
    elif stalled_phase == "update":
        client.request.side_effect = stalled
    else:
        # The update times out, then the closing handshake hangs too.
        client.request.side_effect = stalled
        client.close.side_effect = stalled
    monkeypatch.setattr(app_server, "client_for_transport", lambda *args, **kwargs: client)
    monkeypatch.setattr(app_server, "_EFFORT_SETTINGS_UPDATE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(app_server, "_EFFORT_CONNECT_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(app_server, "_EFFORT_CLOSE_TIMEOUT_SECONDS", 0.01)

    task = asyncio.create_task(
        app_server.apply_codex_thread_effort(
            str(tmp_path / "app-server.sock"),
            "thread_resumed",
            "high",
            bridge_dir=tmp_path,
        )
    )
    done, _ = await asyncio.wait({task}, timeout=0.5)
    if not done:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert done, f"A stalled {stalled_phase} must not block native resume"
    with pytest.raises(TimeoutError):
        task.result()
    assert read_codex_config_effort(tmp_path) == "medium"
    client.close.assert_awaited_once()
