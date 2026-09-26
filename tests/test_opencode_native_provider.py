"""Unit tests for opencode-native provider-config synthesis."""

from __future__ import annotations

import json
import stat
import sys
import types
from pathlib import Path

import pytest

from omnigent.harnesses.opencode_native.bridge import write_opencode_policy_plugin
from omnigent.harnesses.opencode_native.provider import (
    ASK_ALL_PERMISSIONS,
    OPENAI_COMPATIBLE_PACKAGE,
    OpenCodeGatewayResolution,
    _gateway_endpoint_for_model,
    _strip_jsonc_comments,
    _strip_trailing_commas,
    build_opencode_config,
    build_opencode_mcp_block,
    build_opencode_omnigent_mcp_server,
    build_opencode_provider_block,
    managed_connect_opencode_config,
    maybe_merge_user_provider_config,
    resolve_databricks_gateway,
    write_opencode_instructions,
    write_opencode_provider_config,
)


@pytest.fixture(autouse=True)
def _stub_catalog_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnigent.models.model_catalog.resolve_catalog_model",
        lambda provider_name, *, family, **kwargs: types.SimpleNamespace(
            model_id=f"catalog-{provider_name}-{family}-default"
        ),
    )


def test_build_omnigent_mcp_server_points_serve_mcp_at_bridge_dir() -> None:
    block = build_opencode_omnigent_mcp_server(Path("/tmp/bridge-xyz"))
    assert set(block) == {"omnigent"}
    entry = block["omnigent"]
    assert entry["type"] == "local"
    # Relay tools keep their names and are individually permission-gated.
    assert entry["codemode"] is False
    assert "enabled" not in entry
    # Milliseconds: must exceed the bridge's outer relay hop (330 s) so the
    # relay's clean timeout error beats opencode's client-side kill.
    assert entry["timeout"] == {"execution": 360_000}
    cmd = entry["command"]
    # Launches the SHARED serve-mcp relay, pointed at THIS bridge dir.
    assert cmd[-3:] == ["serve-mcp", "--bridge-dir", "/tmp/bridge-xyz"]
    assert "omnigent.harnesses.claude_native.bridge" in cmd
    assert entry.get("environment", {}).get("PYTHONUNBUFFERED") == "1"


def test_build_omnigent_mcp_server_honors_python_executable() -> None:
    block = build_opencode_omnigent_mcp_server(Path("/tmp/b"), python_executable="/custom/python")
    assert block["omnigent"]["command"][0] == "/custom/python"


@pytest.mark.parametrize(
    "server",
    [
        {"command": "python", "args": [1], "env": {}},
        {"command": "python", "args": [], "env": {"TOKEN": 1}},
    ],
)
def test_build_omnigent_mcp_server_rejects_non_string_values(
    monkeypatch: pytest.MonkeyPatch,
    server: dict[str, object],
) -> None:
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.bridge.build_mcp_config",
        lambda bridge_dir, *, python_executable=None: {"mcpServers": {"omnigent": server}},
    )

    with pytest.raises(ValueError, match="Claude MCP server"):
        build_opencode_omnigent_mcp_server(Path("/tmp/b"))


def test_qualified_model_joins_provider_and_endpoint() -> None:
    res = OpenCodeGatewayResolution(
        base_url="https://ws/serving-endpoints",
        api_key="tok",
        model_id="databricks-claude-sonnet-4-6",
        provider_id="databricks-gateway",
    )
    assert res.qualified_model == "databricks-gateway/databricks-claude-sonnet-4-6"


def test_ask_all_permissions_is_single_wildcard_ask_rule() -> None:
    assert ASK_ALL_PERMISSIONS == [{"action": "*", "resource": "*", "effect": "ask"}]


def test_build_provider_block_is_v2_shape() -> None:
    res = OpenCodeGatewayResolution(
        base_url="https://ws/serving-endpoints",
        api_key="sekret",
        model_id="databricks-claude-sonnet-4-6",
        model_ids=("databricks-claude-sonnet-4-6", "databricks-kimi-k3"),
    )
    block = build_opencode_provider_block(res)
    assert block == {
        "databricks-gateway": {
            "name": "Databricks AI Gateway",
            "package": OPENAI_COMPATIBLE_PACKAGE,
            "settings": {
                "baseURL": "https://ws/serving-endpoints",
                "apiKey": "sekret",
                "provider": "databricks-gateway",
            },
            "models": {
                "databricks-claude-sonnet-4-6": {"name": "databricks-claude-sonnet-4-6"},
                "databricks-kimi-k3": {"name": "databricks-kimi-k3"},
            },
        }
    }
    # No v1 keys.
    entry = block["databricks-gateway"]
    assert "npm" not in entry and "options" not in entry


def test_write_provider_config_is_0600_and_valid_json(tmp_path: Path) -> None:
    res = OpenCodeGatewayResolution(
        base_url="https://ws/serving-endpoints", api_key="tok", model_id="databricks-x"
    )
    path = write_opencode_provider_config(
        tmp_path, {"providers": build_opencode_provider_block(res)}
    )
    assert path == tmp_path / "opencode" / "opencode.json"
    # Token-bearing config must not be world/group readable.
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    parsed = json.loads(path.read_text())
    assert parsed["providers"]["databricks-gateway"]["settings"]["apiKey"] == "tok"


@pytest.mark.parametrize(
    "model_id,expected",
    [
        ("databricks-claude-sonnet-4-6", "databricks-claude-sonnet-4-6"),
        ("databricks/databricks-gpt-5-5", "databricks-gpt-5-5"),
        ("claude-opus-4", None),  # not a gateway endpoint name
        ("anthropic/claude-opus-4", None),
        (None, None),
    ],
)
def test_gateway_endpoint_normalization(model_id: str | None, expected: str | None) -> None:
    assert _gateway_endpoint_for_model(model_id) == expected


def test_resolve_gateway_none_without_profile() -> None:
    assert resolve_databricks_gateway(None) is None
    assert resolve_databricks_gateway("") is None


def test_resolve_gateway_none_when_sdk_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate databricks-sdk not installed: the import inside the function raises.
    monkeypatch.setitem(sys.modules, "databricks.sdk.core", None)
    assert resolve_databricks_gateway("oss") is None


def _install_fake_sdk(
    monkeypatch: pytest.MonkeyPatch,
    *,
    host: str,
    token: str | None,
    endpoints: list[tuple[str, str]] | None = None,
) -> None:
    fake = types.ModuleType("databricks.sdk.core")

    class _Config:
        def __init__(self, *, profile: str) -> None:
            self.profile = profile
            self.host = host

        def authenticate(self) -> dict[str, str]:
            return {"Authorization": f"Bearer {token}"} if token else {}

    fake.Config = _Config  # type: ignore[attr-defined]
    sdk = types.ModuleType("databricks.sdk")
    # Only expose WorkspaceClient (used for serving-endpoint discovery) when the
    # test supplies endpoints; otherwise the import fails and discovery no-ops.
    if endpoints is not None:

        class _WorkspaceClient:
            def __init__(self, *, config: object) -> None:
                self._config = config

            @property
            def serving_endpoints(self) -> object:
                eps = [types.SimpleNamespace(name=n, task=t) for n, t in endpoints]
                return types.SimpleNamespace(list=lambda: eps)

        sdk.WorkspaceClient = _WorkspaceClient  # type: ignore[attr-defined]
    # Ensure parent packages resolve for the dotted import.
    monkeypatch.setitem(sys.modules, "databricks", types.ModuleType("databricks"))
    monkeypatch.setitem(sys.modules, "databricks.sdk", sdk)
    monkeypatch.setitem(sys.modules, "databricks.sdk.core", fake)


def test_resolve_gateway_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_sdk(monkeypatch, host="https://ws.cloud.databricks.com/", token="abc123")
    res = resolve_databricks_gateway("oss", model_id="databricks-gpt-5-5")
    assert res is not None
    assert res.base_url == "https://ws.cloud.databricks.com/serving-endpoints"
    assert res.api_key == "abc123"
    assert res.model_id == "databricks-gpt-5-5"
    assert res.qualified_model == "databricks-gateway/databricks-gpt-5-5"


def test_resolve_gateway_defaults_non_gateway_model(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_sdk(monkeypatch, host="https://ws.databricks.com", token="t")
    res = resolve_databricks_gateway("oss", model_id="claude-opus-4")
    assert res is not None
    assert res.model_id == "catalog-databricks-claude-default"


def test_resolve_gateway_none_when_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_sdk(monkeypatch, host="https://ws.databricks.com", token=None)
    assert resolve_databricks_gateway("oss") is None


def test_resolve_gateway_lists_all_chat_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    # Discovery lists every chat serving-endpoint (pinned default first, embeddings
    # dropped) so opencode's in-session picker offers them all.
    _install_fake_sdk(
        monkeypatch,
        host="https://ws.databricks.com",
        token="t",
        endpoints=[
            ("databricks-kimi-k3", "llm/v1/chat"),
            ("databricks-claude-sonnet-4-6", "llm/v1/chat"),
            ("databricks-gte-large-en", "llm/v1/embeddings"),
            ("some-other-endpoint", "llm/v1/chat"),
        ],
    )
    res = resolve_databricks_gateway("oss", model_id="databricks-claude-sonnet-4-6")
    assert res is not None
    # pinned default first, embeddings + non-databricks dropped, de-duped
    assert res.model_ids == ("databricks-claude-sonnet-4-6", "databricks-kimi-k3")
    block = build_opencode_provider_block(res)
    models = block["databricks-gateway"]["models"]
    assert set(models) == {"databricks-claude-sonnet-4-6", "databricks-kimi-k3"}  # type: ignore[bad-argument-type]


def test_resolve_gateway_single_model_when_discovery_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No WorkspaceClient (endpoints=None) -> discovery no-ops, just the pinned model.
    _install_fake_sdk(monkeypatch, host="https://ws.databricks.com", token="t")
    res = resolve_databricks_gateway("oss", model_id="databricks-kimi-k3")
    assert res is not None
    assert res.model_ids == ("databricks-kimi-k3",)


def test_resolve_gateway_env_default_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    # No session model pinned -> the deployment env default steers the endpoint.
    _install_fake_sdk(monkeypatch, host="https://ws.databricks.com", token="t")
    monkeypatch.setenv("OMNIGENT_DATABRICKS_GATEWAY_MODEL", "databricks-kimi-k3")
    res = resolve_databricks_gateway("oss")
    assert res is not None
    assert res.model_id == "databricks-kimi-k3"


def test_resolve_gateway_session_model_beats_env_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_sdk(monkeypatch, host="https://ws.databricks.com", token="t")
    monkeypatch.setenv("OMNIGENT_DATABRICKS_GATEWAY_MODEL", "databricks-kimi-k3")
    res = resolve_databricks_gateway("oss", model_id="databricks-gpt-5-5")
    assert res is not None
    assert res.model_id == "databricks-gpt-5-5"


def test_resolve_gateway_env_default_ignored_when_not_gateway_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A non ``databricks-*`` env value is not a routable endpoint -> catalog wins.
    _install_fake_sdk(monkeypatch, host="https://ws.databricks.com", token="t")
    monkeypatch.setenv("OMNIGENT_DATABRICKS_GATEWAY_MODEL", "kimi-k3")
    res = resolve_databricks_gateway("oss")
    assert res is not None
    assert res.model_id == "catalog-databricks-claude-default"


def test_build_mcp_block_stdio_and_http() -> None:
    from types import SimpleNamespace as N

    from omnigent.harnesses.opencode_native.provider import build_opencode_mcp_block

    servers = [
        N(
            name="gh",
            transport="stdio",
            command="npx",
            args=["-y", "server-github"],
            env={"GITHUB_TOKEN": "x"},
            url=None,
            headers={},
            databricks_profile=None,
        ),
        N(
            name="remote",
            transport="http",
            url="https://mcp.example/sse",
            headers={"X-Key": "k"},
            databricks_profile=None,
            command=None,
            args=[],
            env={},
        ),
        # Unrepresentable (stdio without a command) → skipped.
        N(name="bad", transport="stdio", command=None, args=[], env={}, url=None, headers={}),
    ]
    block = build_opencode_mcp_block(servers)
    assert set(block) == {"gh", "remote"}
    assert block["gh"] == {
        "type": "local",
        "command": ["npx", "-y", "server-github"],
        "codemode": False,
        "environment": {"GITHUB_TOKEN": "x"},
    }
    assert block["remote"] == {
        "type": "remote",
        "url": "https://mcp.example/sse",
        "codemode": False,
        "headers": {"X-Key": "k"},
    }


def test_build_mcp_block_http_databricks_injects_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace as N

    import omnigent.harnesses.opencode_native.provider as prov

    monkeypatch.setattr(prov, "_databricks_bearer_token", lambda _p: "tok123")
    servers = [
        N(
            name="dbx",
            transport="http",
            url="https://ws/mcp",
            headers={},
            databricks_profile="oss",
            command=None,
            args=[],
            env={},
        )
    ]
    block = prov.build_opencode_mcp_block(servers)
    assert block["dbx"]["headers"] == {"Authorization": "Bearer tok123"}
    assert block["dbx"]["oauth"] is False  # bearer header → no OAuth discovery
    assert block["dbx"]["codemode"] is False


def test_strip_jsonc_comments_removes_line_and_block_comments() -> None:
    raw = """{
  // line comment
  "key": "value", /* block comment */
  "nested": /* another */ "val"
}"""
    cleaned = _strip_jsonc_comments(raw)
    assert "//" not in cleaned
    assert "/*" not in cleaned
    assert "*/" not in cleaned
    import json

    parsed = json.loads(cleaned)
    assert parsed == {"key": "value", "nested": "val"}


def test_strip_jsonc_comments_preserves_valid_json() -> None:
    raw = '{"key": "value", "nested": {"a": 1}}'
    assert _strip_jsonc_comments(raw) == raw


def test_strip_jsonc_comments_does_not_corrupt_urls() -> None:
    """URLs containing // must not have the // stripped."""
    raw = '{"baseURL": "https://my-gateway/v1"}'
    cleaned = _strip_jsonc_comments(raw)
    import json

    parsed = json.loads(cleaned)
    assert parsed["baseURL"] == "https://my-gateway/v1"


def _user_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str) -> None:
    cfg_dir = tmp_path / "cfg" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.jsonc").write_text(text, encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))


def test_merge_user_provider_config_noop_without_user_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nonexistent"))
    config = {"model": "anthropic/claude-sonnet-4-5"}
    assert maybe_merge_user_provider_config(config) == config


def test_merge_converts_v1_provider_to_v2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _user_config(
        monkeypatch,
        tmp_path,
        '{"provider": {"my-openai": {"npm": "@ai-sdk/openai-compatible", "name": "Mine", '
        '"options": {"baseURL": "https://gw/v1", "apiKey": "sk-", "headers": {"X-A": "1"}}, '
        '"models": {"gpt-4": {"name": "gpt-4", "id": "gpt-4-0613"}}}}}',
    )
    result = maybe_merge_user_provider_config({})
    assert "provider" not in result
    assert result["providers"]["my-openai"] == {
        "name": "Mine",
        "package": "aisdk:@ai-sdk/openai-compatible",
        "settings": {"baseURL": "https://gw/v1", "apiKey": "sk-"},
        "headers": {"X-A": "1"},
        "models": {"gpt-4": {"name": "gpt-4", "modelID": "gpt-4-0613"}},
    }
    assert result["$schema"] == "https://opencode.ai/config.json"


def test_merge_reads_v2_providers_and_v2_wins_over_v1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _user_config(
        monkeypatch,
        tmp_path,
        '{"provider": {"p": {"npm": "@ai-sdk/openai"}}, '
        '"providers": {"p": {"package": "@opencode/ai/providers/openai"}, '
        '"q": {"settings": {"baseURL": "https://q"}}}}',
    )
    result = maybe_merge_user_provider_config({})
    assert result["providers"]["p"] == {"package": "@opencode/ai/providers/openai"}
    assert result["providers"]["q"] == {"settings": {"baseURL": "https://q"}}


def test_merge_does_not_clobber_synthesized_providers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _user_config(monkeypatch, tmp_path, '{"providers": {"databricks-gateway": {"name": "user"}}}')
    config: dict[str, object] = {"providers": {"databricks-gateway": {"name": "synth"}}}
    result = maybe_merge_user_provider_config(config)
    assert result["providers"]["databricks-gateway"] == {"name": "synth"}


def test_merge_adopts_user_model_only_when_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _user_config(monkeypatch, tmp_path, '{"model": {"providerID": "anthropic", "model": "c-4"}}')
    assert maybe_merge_user_provider_config({})["model"] == "anthropic/c-4"
    assert maybe_merge_user_provider_config({"model": "openai/g"})["model"] == "openai/g"


def test_merge_adopts_user_string_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _user_config(monkeypatch, tmp_path, '{"model": "databricks/databricks-claude-opus-4-8"}')
    assert maybe_merge_user_provider_config({})["model"] == "databricks/databricks-claude-opus-4-8"


def test_merge_plugins_v1_and_v2_after_synthesized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _user_config(
        monkeypatch,
        tmp_path,
        '{"plugin": ["/opt/a", ["pkg-b", {"k": 1}], "", 42], '
        '"plugins": ["/opt/a", {"package": "pkg-c"}, {"bad": 1}]}',
    )
    result = maybe_merge_user_provider_config({"plugins": ["/b/omnigent-policy", "/opt/a"]})
    assert "plugin" not in result
    assert result["plugins"] == [
        "/b/omnigent-policy",
        "/opt/a",
        {"package": "pkg-b", "options": {"k": 1}},
        {"package": "pkg-c"},
    ]


def test_merge_mcp_v1_flat_and_v2_servers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _user_config(
        monkeypatch,
        tmp_path,
        '{"mcp": {"legacy": {"type": "local", "command": ["x"], "enabled": false,'
        ' "timeout": 5000}, "omnigent": {"type": "local", "command": ["user"]},'
        ' "servers": {"modern": {"type": "remote", "url": "https://m"}}}}',
    )
    config: dict[str, object] = {
        "mcp": {"servers": {"omnigent": {"type": "local", "command": ["r"]}}}
    }
    result = maybe_merge_user_provider_config(config)
    servers = result["mcp"]["servers"]
    assert servers["omnigent"] == {"type": "local", "command": ["r"]}  # synthesized wins
    assert servers["legacy"] == {
        "type": "local",
        "command": ["x"],
        "disabled": True,
        "timeout": {"catalog": 5000, "execution": 5000},
    }
    assert servers["modern"] == {"type": "remote", "url": "https://m"}


def test_merge_lifts_flat_synthesized_mcp_into_servers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pre-Task-63 callers may hand a flat ``mcp`` map; the merge nests it."""
    _user_config(
        monkeypatch,
        tmp_path,
        '{"mcp": {"servers": {"gh": {"type": "remote", "url": "https://gh"}}}}',
    )
    config: dict[str, object] = {"mcp": {"omnigent": {"type": "local", "command": ["r"]}}}
    result = maybe_merge_user_provider_config(config)
    assert result["mcp"] == {
        "servers": {
            "omnigent": {"type": "local", "command": ["r"]},
            "gh": {"type": "remote", "url": "https://gh"},
        }
    }


def test_merge_warns_and_continues_on_malformed_user_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _user_config(monkeypatch, tmp_path, "{not json")
    config = {"model": "anthropic/claude-sonnet-4-5"}
    with caplog.at_level("WARNING"):
        result = maybe_merge_user_provider_config(config)
    assert result == config
    assert any(
        "Failed to parse user OpenCode config" in record.message for record in caplog.records
    )


def test_merge_user_provider_config_handles_jsonc_comments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _user_config(
        monkeypatch,
        tmp_path,
        '{\n  // comment\n  "providers": {"p": {"settings":'
        ' {"baseURL": "https://x/v1"}}}, /* b */\n}',
    )
    assert maybe_merge_user_provider_config({})["providers"]["p"]["settings"]["baseURL"] == (
        "https://x/v1"
    )


def test_strip_trailing_commas_object() -> None:
    raw = '{"a": 1, "b": 2,}'
    assert _strip_trailing_commas(raw) == '{"a": 1, "b": 2}'


def test_strip_trailing_commas_array() -> None:
    raw = "[1, 2, 3,]"
    assert _strip_trailing_commas(raw) == "[1, 2, 3]"


def test_strip_trailing_commas_nested() -> None:
    raw = '{"a": [1, 2,], "b": {"c": 3,}}'
    assert _strip_trailing_commas(raw) == '{"a": [1, 2], "b": {"c": 3}}'


def test_strip_trailing_commas_noop_without_trailing_commas() -> None:
    raw = '{"a": 1, "b": [1, 2]}'
    assert _strip_trailing_commas(raw) == raw


def test_strip_trailing_commas_preserves_commas_inside_strings() -> None:
    """Commas followed by } or ] inside string literals must NOT be stripped."""
    raw = '{"note": "a, }", "list": "b, ]"}'
    assert _strip_trailing_commas(raw) == raw


def test_strip_trailing_commas_nested_with_string_values() -> None:
    """Trailing commas outside strings stripped; commas inside strings preserved."""
    raw = '{"a": "x, }", "b": [1, 2,],}'
    expected = '{"a": "x, }", "b": [1, 2]}'
    assert _strip_trailing_commas(raw) == expected


def test_build_mcp_block_preserves_custom_timeout() -> None:
    from types import SimpleNamespace as N

    from omnigent.harnesses.opencode_native.provider import build_opencode_mcp_block

    servers = [
        N(
            name="local_custom",
            transport="stdio",
            command="python",
            args=["-m", "custom_server"],
            env={},
            url=None,
            headers={},
            timeout=120,
        ),
        N(
            name="remote_custom",
            transport="http",
            url="https://remote.mcp/api",
            headers={},
            command=None,
            args=[],
            env={},
            timeout=45.5,
        ),
    ]
    block = build_opencode_mcp_block(servers)
    # MCPServerConfig.timeout is seconds; v2 timeouts are {catalog, execution} in ms.
    assert block["local_custom"]["timeout"] == {"catalog": 120_000, "execution": 120_000}
    assert block["remote_custom"]["timeout"] == {"catalog": 45_500, "execution": 45_500}


def test_extract_progress_token_variants() -> None:
    from omnigent.harnesses.claude_native.bridge import _extract_progress_token

    # Meta style (MCP standard)
    assert _extract_progress_token({"_meta": {"progressToken": "tok-123"}}) == "tok-123"
    assert _extract_progress_token({"_meta": {"progressToken": 42}}) == 42
    # Top-level fallback
    assert _extract_progress_token({"progressToken": "tok-456"}) == "tok-456"
    # None or malformed
    assert _extract_progress_token(None) is None
    assert _extract_progress_token({}) is None
    assert _extract_progress_token({"_meta": {}}) is None
    assert _extract_progress_token({"_meta": {"progressToken": ["invalid"]}}) is None


def test_mcp_progress_heartbeat_lifecycle() -> None:
    import itertools
    import threading
    import time

    lock = threading.Lock()
    written_messages: list[dict[str, object]] = []

    def fake_write(
        payload: dict[str, object],
        stdout_lock: threading.Lock,
        **_kwargs: object,
    ) -> None:
        with stdout_lock:
            written_messages.append(payload)

    import omnigent.harnesses.claude_native.bridge as bridge_mod

    orig_write = bridge_mod._write_jsonrpc
    bridge_mod._write_jsonrpc = fake_write
    try:
        # With interval = 0.05s, should emit progress notifications
        with bridge_mod._McpProgressHeartbeat("test-token", lock, interval_s=0.05):
            time.sleep(0.12)
        assert len(written_messages) >= 2
        assert all(m["method"] == "notifications/progress" for m in written_messages)
        assert all(m["params"]["progressToken"] == "test-token" for m in written_messages)
        progresses = [m["params"]["progress"] for m in written_messages]
        assert all(b > a for a, b in itertools.pairwise(progresses))

        # Once exited, no more messages are emitted
        count_at_exit = len(written_messages)
        time.sleep(0.1)
        assert len(written_messages) == count_at_exit
    finally:
        bridge_mod._write_jsonrpc = orig_write


_UCODE_PLUGIN_JS = (
    "// Generated by ucode. Keep Databricks auth fresh for model requests.\n"
    'const AUTH_COMMAND = ["ucode", "auth-token", "--force-refresh"]\n'
    "export const UcodeDatabricksAuth = async () => ({})\n"
)


def _ucode_home(tmp_path: Path, base_url: str) -> Path:
    ucode_dir = tmp_path / ".ucode" / "opencode-xdg" / "opencode"
    (ucode_dir / "plugin").mkdir(parents=True)
    (ucode_dir / "opencode.json").write_text(
        json.dumps(
            {
                "model": "databricks-anthropic/system.ai.claude-opus-4-8",
                "provider": {
                    "databricks-anthropic": {
                        "npm": "@ai-sdk/anthropic",
                        "options": {"baseURL": base_url, "apiKey": "stale"},
                        "models": {"system.ai.claude-opus-4-8": {"name": "Opus"}},
                    }
                },
            }
        )
    )
    (ucode_dir / "plugin" / "ucode-auth.js").write_text(_UCODE_PLUGIN_JS)
    return ucode_dir


def _sidecar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnigent.host.databricks_credential._read_sidecar",
        lambda path: {
            "server": "s",
            "host_id": "h",
            "host_token": "t",
            "workspace_host": "https://ws",
        },
    )


def test_managed_connect_opencode_config_is_v2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _ucode_home(tmp_path, "https://ws/serving-endpoints/anthropic")
    _sidecar(monkeypatch)
    session_xdg = tmp_path / "session-xdg"
    stale = session_xdg / "opencode" / "plugin" / "ucode-auth.js"
    stale.parent.mkdir(parents=True)
    stale.write_text("// v1 copy from an older launch\n")
    bridge_dir = tmp_path / "bridge"

    config = managed_connect_opencode_config(session_xdg, bridge_dir)

    assert config is not None
    assert config["model"] == "databricks-anthropic/system.ai.claude-opus-4-8"
    provider = config["providers"]["databricks-anthropic"]
    assert provider["package"] == "aisdk:@ai-sdk/anthropic"
    assert provider["settings"]["baseURL"] == "https://ws/serving-endpoints/anthropic"
    assert "provider" not in config and "plugin" not in config
    plugin_dir = bridge_dir / "omnigent-ucode-auth"
    assert config["plugins"] == [str(plugin_dir)]
    src = (plugin_dir / "server.js").read_text(encoding="utf-8")
    assert 'const PROVIDERS = ["databricks-anthropic"]' in src
    assert 'const AUTH_COMMAND = ["ucode", "auth-token", "--force-refresh"]' in src
    assert 'ctx.session.hook(\n        "http.request"' in src
    assert not stale.exists()  # v2 would auto-load and reject the v1 plugin


def test_managed_connect_opencode_config_none_without_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("omnigent.host.databricks_credential._read_sidecar", lambda path: None)
    assert managed_connect_opencode_config(tmp_path / "xdg", tmp_path / "bridge") is None


def test_managed_connect_opencode_config_declines_without_auth_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    ucode_dir = _ucode_home(tmp_path, "https://ws/x")
    (ucode_dir / "plugin" / "ucode-auth.js").write_text("// no command here\n")
    _sidecar(monkeypatch)
    assert managed_connect_opencode_config(tmp_path / "xdg", tmp_path / "bridge") is None


@pytest.mark.parametrize("bad_url", ["https://evil.example/x", "http://ws/x"])
def test_managed_connect_opencode_config_rejects_untrusted_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_url: str
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _ucode_home(tmp_path, bad_url)
    _sidecar(monkeypatch)
    assert managed_connect_opencode_config(tmp_path / "xdg", tmp_path / "bridge") is None


def test_ucode_auth_plugin_stamps_bearer(tmp_path: Path) -> None:
    import os
    import shutil
    import subprocess

    from omnigent.harnesses.opencode_native.bridge import write_plugin_package
    from omnigent.harnesses.opencode_native.provider import render_ucode_auth_plugin

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "mint").write_text("#!/bin/sh\necho tok-123\n")
    (bin_dir / "mint").chmod(0o755)
    plugin_dir = write_plugin_package(
        tmp_path,
        "omnigent-ucode-auth",
        source=render_ucode_auth_plugin(providers=["databricks-oss"], auth_command=["mint"]),
    )
    harness = tmp_path / "h.mjs"
    server_js_path = json.dumps(str(plugin_dir / "server.js"))
    harness.write_text(
        'import { pathToFileURL } from "node:url"\n'
        f"const mod = await import(pathToFileURL({server_js_path}).href)\n"
        "const hooks = []\n"
        "await mod.default.setup({ session: { hook: async (name, cb, opts) => {\n"
        "  hooks.push({ name, cb, opts })\n"
        "} } })\n"
        'const req = hooks.find((h) => h.name === "http.request")\n'
        'const ev = { request: new Request("https://ws/x", '
        '{ method: "POST", body: "{}" }) }\n'
        "await req.cb(ev)\n"
        "console.log(JSON.stringify({\n"
        "  id: mod.default.id,\n"
        "  hooks: hooks.map((h) => h.name + ':' + h.opts.providerID),\n"
        '  auth: ev.request.headers.get("authorization"),\n'
        "  body: await ev.request.text(),\n"
        "}))\n",
        encoding="utf-8",
    )
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    out = json.loads(
        subprocess.run(
            [node, str(harness)], capture_output=True, text=True, env=env, timeout=30, check=True
        ).stdout
    )
    assert out == {
        "id": "omnigent-ucode-auth",
        "hooks": ["http.request:databricks-oss", "http.response:databricks-oss"],
        "auth": "Bearer tok-123",
        "body": "{}",
    }


def test_build_opencode_config_minimal_is_ask_all() -> None:
    cfg = build_opencode_config(
        model=None, gateway=None, mcp_servers={}, plugin_paths=[], instructions=None
    )
    assert cfg == {
        "$schema": "https://opencode.ai/config.json",
        "permissions": [{"action": "*", "resource": "*", "effect": "ask"}],
    }


def test_build_opencode_config_full_v2_shape() -> None:
    gateway = OpenCodeGatewayResolution(
        base_url="https://ws/serving-endpoints", api_key="tok", model_id="databricks-x"
    )
    cfg = build_opencode_config(
        model="anthropic/claude-sonnet-4-5",
        gateway=gateway,
        mcp_servers={"omnigent": {"type": "local", "command": ["py"], "codemode": False}},
        plugin_paths=["/b/ucode-auth", "/b/omnigent-policy", "/b/omnigent-policy"],
        instructions="/x/opencode/AGENTS.md",
    )
    # The gateway pins the model to its own provider.
    assert cfg["model"] == "databricks-gateway/databricks-x"
    assert set(cfg["providers"]) == {"databricks-gateway"}
    assert cfg["mcp"] == {
        "servers": {"omnigent": {"type": "local", "command": ["py"], "codemode": False}}
    }
    assert cfg["plugins"] == ["/b/ucode-auth", "/b/omnigent-policy"]
    assert cfg["instructions"] == ["/x/opencode/AGENTS.md"]
    for v1_key in ("provider", "permission", "plugin"):
        assert v1_key not in cfg


def test_build_opencode_config_keeps_only_deny_rules_after_ask_all() -> None:
    cfg = build_opencode_config(
        model="openai/gpt-5.5",
        gateway=None,
        mcp_servers={},
        plugin_paths=[],
        instructions=None,
        permissions=[
            {"action": "shell", "resource": "rm *", "effect": "deny"},
            {"action": "read", "resource": "*", "effect": "allow"},
        ],
    )
    assert cfg["permissions"] == [
        {"action": "*", "resource": "*", "effect": "ask"},
        {"action": "shell", "resource": "rm *", "effect": "deny"},
    ]
    assert cfg["model"] == "openai/gpt-5.5"


def test_build_opencode_config_extra_providers_and_bad_model() -> None:
    cfg = build_opencode_config(
        model="big-pickle",
        gateway=None,
        mcp_servers={},
        plugin_paths=[],
        instructions=None,
        extra_providers={"databricks-oss": {"package": "aisdk:@ai-sdk/openai"}},
    )
    assert cfg["providers"] == {"databricks-oss": {"package": "aisdk:@ai-sdk/openai"}}
    assert "model" not in cfg  # not provider/model


def test_write_opencode_instructions_writes_global_agents_md(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-cfg"))
    session_xdg = tmp_path / "session-xdg"
    path = write_opencode_instructions(session_xdg, "  Be terse.\n")
    assert path is not None
    assert path == session_xdg / "opencode" / "AGENTS.md"
    assert path.read_text(encoding="utf-8") == "Be terse.\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_opencode_instructions_keeps_user_agents_md(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    user_dir = tmp_path / "user-cfg" / "opencode"
    user_dir.mkdir(parents=True)
    (user_dir / "AGENTS.md").write_text("User rules.\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-cfg"))
    path = write_opencode_instructions(tmp_path / "s", "Agent rules.")
    assert path is not None
    assert path.read_text(encoding="utf-8") == "User rules.\n\nAgent rules.\n"


def test_write_opencode_instructions_removes_stale_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nothing"))
    session_xdg = tmp_path / "s"
    assert write_opencode_instructions(session_xdg, "x") is not None
    assert write_opencode_instructions(session_xdg, "   ") is None
    assert not (session_xdg / "opencode" / "AGENTS.md").exists()


def test_runner_assembly_shape_matches_v2_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mirror of the runner's opencode.json assembly (orchestration.py)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-user-config"))
    bridge_dir = tmp_path / "bridge"
    xdg = bridge_dir / "xdg-config"
    mcp_servers = build_opencode_mcp_block([])
    mcp_servers.update(build_opencode_omnigent_mcp_server(bridge_dir))
    plugin_paths = [str(write_opencode_policy_plugin(bridge_dir))]
    instructions = write_opencode_instructions(xdg, "Agent rules.")
    config = maybe_merge_user_provider_config(
        build_opencode_config(
            model="anthropic/claude-sonnet-4-5",
            gateway=None,
            mcp_servers=mcp_servers,
            plugin_paths=plugin_paths,
            instructions=str(instructions),
        )
    )
    path = write_opencode_provider_config(xdg, config)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["permissions"] == [{"action": "*", "resource": "*", "effect": "ask"}]
    assert written["mcp"]["servers"]["omnigent"]["codemode"] is False
    assert written["plugins"] == [str(bridge_dir / "omnigent-policy")]
    assert written["instructions"] == [str(xdg / "opencode" / "AGENTS.md")]
    assert written["model"] == "anthropic/claude-sonnet-4-5"
    assert not {"provider", "permission", "plugin"} & set(written)


def test_runner_imports_v2_builders() -> None:
    import omnigent.harnesses.opencode_native.provider as prov

    for name in (
        "build_opencode_config",
        "build_opencode_mcp_block",
        "build_opencode_omnigent_mcp_server",
        "managed_connect_opencode_config",
        "maybe_merge_user_provider_config",
        "resolve_bound_opencode_gateway",
        "resolve_databricks_gateway",
        "write_opencode_instructions",
        "write_opencode_provider_config",
    ):
        assert hasattr(prov, name), name
    for removed in ("build_opencode_provider_config", "build_opencode_model_default_config"):
        assert not hasattr(prov, removed), removed
