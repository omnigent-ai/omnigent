"""Unit tests for opencode-native provider-config synthesis."""

from __future__ import annotations

import json
import stat
import sys
import types
from pathlib import Path

import pytest

from omnigent.harnesses.opencode_native.bridge import (
    build_gateway_auth_plugin_js,
    write_opencode_gateway_auth_plugin,
)
from omnigent.harnesses.opencode_native.provider import (
    OpenCodeGatewayResolution,
    _gateway_endpoint_for_model,
    _strip_jsonc_comments,
    _strip_trailing_commas,
    build_opencode_model_default_config,
    build_opencode_omnigent_mcp_server,
    build_opencode_provider_config,
    disable_autoloaded_free_providers,
    managed_connect_opencode_config,
    maybe_merge_user_provider_config,
    resolve_config_gateway_providers,
    resolve_databricks_gateway,
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


@pytest.fixture(autouse=True)
def _no_gateway_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable live gateway discovery by default."""
    monkeypatch.setattr(
        "omnigent.harnesses.opencode_native.provider._mint_gateway_discovery_token",
        lambda families: None,
    )


def test_build_omnigent_mcp_server_points_serve_mcp_at_bridge_dir() -> None:
    block = build_opencode_omnigent_mcp_server(Path("/tmp/bridge-xyz"))
    assert set(block) == {"omnigent"}
    entry = block["omnigent"]
    assert entry["type"] == "local"
    assert entry["enabled"] is True
    # Milliseconds: must exceed the bridge's outer relay hop (330 s) so the
    # relay's clean timeout error beats opencode's client-side kill.
    assert entry["timeout"] == 360_000
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


def test_build_model_default_config_pins_model_without_provider_block() -> None:
    cfg = build_opencode_model_default_config("anthropic/claude-sonnet-4-5")
    assert cfg == {
        "$schema": "https://opencode.ai/config.json",
        "model": "anthropic/claude-sonnet-4-5",
    }
    # No provider block: opencode resolves the provider from the model prefix.
    assert "provider" not in cfg


def test_model_default_config_round_trips_through_writer(tmp_path: Path) -> None:
    path = write_opencode_provider_config(
        tmp_path, build_opencode_model_default_config("openai/gpt-5.5")
    )
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["model"] == "openai/gpt-5.5"


def test_qualified_model_joins_provider_and_endpoint() -> None:
    res = OpenCodeGatewayResolution(
        base_url="https://ws/serving-endpoints",
        api_key="tok",
        model_id="databricks-claude-sonnet-4-6",
        provider_id="databricks-gateway",
    )
    assert res.qualified_model == "databricks-gateway/databricks-claude-sonnet-4-6"


def test_build_provider_config_shape() -> None:
    res = OpenCodeGatewayResolution(
        base_url="https://ws/serving-endpoints",
        api_key="sekret",
        model_id="databricks-claude-sonnet-4-6",
    )
    cfg = build_opencode_provider_config(res)
    block = cfg["provider"]["databricks-gateway"]
    assert block["npm"] == "@ai-sdk/openai-compatible"
    assert block["options"] == {"baseURL": "https://ws/serving-endpoints", "apiKey": "sekret"}
    assert "databricks-claude-sonnet-4-6" in block["models"]
    assert cfg["$schema"].endswith("config.json")


def test_write_provider_config_is_0600_and_valid_json(tmp_path: Path) -> None:
    res = OpenCodeGatewayResolution(
        base_url="https://ws/serving-endpoints", api_key="tok", model_id="databricks-x"
    )
    path = write_opencode_provider_config(tmp_path, build_opencode_provider_config(res))
    assert path == tmp_path / "opencode" / "opencode.json"
    # Token-bearing config must not be world/group readable.
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    parsed = json.loads(path.read_text())
    assert parsed["provider"]["databricks-gateway"]["options"]["apiKey"] == "tok"


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
    cfg = build_opencode_provider_config(res)
    models = cfg["provider"]["databricks-gateway"]["models"]  # type: ignore[index]
    assert set(models) == {"databricks-claude-sonnet-4-6", "databricks-kimi-k3"}


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
        "enabled": True,
        "environment": {"GITHUB_TOKEN": "x"},
    }
    assert block["remote"] == {
        "type": "remote",
        "url": "https://mcp.example/sse",
        "enabled": True,
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


def test_merge_user_provider_config_noop_without_user_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No user config file → config returned unchanged."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nonexistent"))

    config = {"model": "anthropic/claude-sonnet-4-5"}
    result = maybe_merge_user_provider_config(config)
    assert result == config


def test_merge_user_provider_config_adds_user_providers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """User's provider definitions are merged into the synthesized config."""
    cfg_dir = tmp_path / "cfg" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.jsonc").write_text(
        '{"provider": {"my-openai": {"npm": "@ai-sdk/openai-compatible", '
        '"options": {"baseURL": "https://my-gateway/v1", "apiKey": "sk-"}, '
        '"models": {"gpt-4": {"name": "gpt-4"}}}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    config: dict[str, object] = {}
    result = maybe_merge_user_provider_config(config)

    assert "provider" in result
    providers = result["provider"]
    assert isinstance(providers, dict)
    assert "my-openai" in providers
    assert providers["my-openai"]["options"]["baseURL"] == "https://my-gateway/v1"
    # Synthesized $schema should have been added.
    assert "$schema" in result


def test_merge_user_provider_config_does_not_clobber_synthesized_providers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A user provider with the same key as a synthesized one is NOT overwritten."""
    cfg_dir = tmp_path / "cfg" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.jsonc").write_text(
        '{"provider": {"databricks-gateway": {"options": {"baseURL": "http://evil"}}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    config = {
        "provider": {
            "databricks-gateway": {
                "npm": "@ai-sdk/openai-compatible",
                "options": {
                    "baseURL": "https://real-databricks/serving-endpoints",
                    "apiKey": "tok",
                },
            }
        }
    }
    result = maybe_merge_user_provider_config(config)
    assert (
        result["provider"]["databricks-gateway"]["options"]["baseURL"]
        == "https://real-databricks/serving-endpoints"
    )


def test_merge_user_provider_config_adopts_user_model_when_synthesized_has_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """User's default model is adopted when the synthesized config pins none.

    Regression: with no gateway and no spec model_override, the synthesized
    config had no ``model`` key; opencode-native then picked its own default
    over the merged models map (landing on a served Gemini endpoint) instead of
    the user's configured Claude default. The merge now carries ``model``.
    """
    cfg_dir = tmp_path / "cfg" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.json").write_text(
        '{"model": "databricks/databricks-claude-opus-4-8", '
        '"provider": {"databricks": {"npm": "@ai-sdk/openai-compatible", '
        '"options": {"baseURL": "https://ws/serving-endpoints", "apiKey": "t"}, '
        '"models": {"databricks-claude-opus-4-8": {"name": "Claude"}, '
        '"databricks-gemini-2-5-pro": {"name": "Gemini"}}}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    config: dict[str, object] = {}  # no gateway, no model_override
    result = maybe_merge_user_provider_config(config)

    assert result["model"] == "databricks/databricks-claude-opus-4-8"


def test_merge_user_provider_config_does_not_override_synthesized_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A synthesized ``model`` (gateway / spec override) wins over the user's."""
    cfg_dir = tmp_path / "cfg" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.json").write_text(
        '{"model": "databricks/databricks-claude-opus-4-8", '
        '"provider": {"databricks": {"models": {"m": {"name": "m"}}}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    config: dict[str, object] = {"model": "databricks-gateway/pinned-model"}
    result = maybe_merge_user_provider_config(config)

    assert result["model"] == "databricks-gateway/pinned-model"


def test_merge_user_provider_config_carries_model_without_user_providers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """User's default model is adopted even when the user declares no providers."""
    cfg_dir = tmp_path / "cfg" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.json").write_text(
        '{"model": "databricks/databricks-claude-opus-4-8"}',
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    config: dict[str, object] = {}
    result = maybe_merge_user_provider_config(config)

    assert result["model"] == "databricks/databricks-claude-opus-4-8"


def test_merge_user_provider_config_preserves_plugins_and_deduplicates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Global plugins survive per-session config synthesis."""
    cfg_dir = tmp_path / "cfg" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.jsonc").write_text(
        '{"plugin": ["/opt/pulse-agents-harnesses/marshal-opencode", '
        '"/opt/pulse-agents-harnesses/other"]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    config: dict[str, object] = {
        "plugin": ["/tmp/omnigent-policy.js", "/opt/pulse-agents-harnesses/other"]
    }
    result = maybe_merge_user_provider_config(config)

    assert result["plugin"] == [
        "/tmp/omnigent-policy.js",
        "/opt/pulse-agents-harnesses/other",
        "/opt/pulse-agents-harnesses/marshal-opencode",
    ]


def test_merge_user_provider_config_skips_non_string_plugins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Malformed plugin entries are dropped rather than propagated."""
    cfg_dir = tmp_path / "cfg" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.jsonc").write_text(
        '{"plugin": ["/opt/pulse-agents-harnesses/marshal-opencode", {"bad": "entry"}, "", 42]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    config: dict[str, object] = {"plugin": ["/tmp/omnigent-policy.js"]}
    result = maybe_merge_user_provider_config(config)

    assert result["plugin"] == [
        "/tmp/omnigent-policy.js",
        "/opt/pulse-agents-harnesses/marshal-opencode",
    ]


def test_merge_user_provider_config_merges_alongside_synthesized_providers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """User providers appear alongside the synthesized ones when keys differ."""
    cfg_dir = tmp_path / "cfg" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.jsonc").write_text(
        '{"provider": {"my-openai": {"options": {"baseURL": "http://my-gw/v1"}}}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    config = {
        "provider": {
            "databricks-gateway": {
                "options": {"baseURL": "https://dbx/serving-endpoints", "apiKey": "tok"},
            }
        }
    }
    result = maybe_merge_user_provider_config(config)
    providers = result["provider"]
    assert "databricks-gateway" in providers
    assert "my-openai" in providers
    assert providers["my-openai"]["options"]["baseURL"] == "http://my-gw/v1"


def test_merge_user_provider_config_handles_jsonc_comments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The user's JSONC file with comments is parsed correctly."""
    cfg_dir = tmp_path / "cfg" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.jsonc").write_text(
        "{\n"
        "  // my custom provider\n"
        '  "provider": {\n'
        '    "my-openai": {\n'
        '      "options": {"baseURL": "https://my-gw/v1"}\n'
        "    }\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    result = maybe_merge_user_provider_config({})
    assert result["provider"]["my-openai"]["options"]["baseURL"] == "https://my-gw/v1"


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


def test_merge_user_provider_config_handles_jsonc_trailing_commas(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Trailing commas in JSONC are handled (they're valid in JSONC but not JSON)."""
    cfg_dir = tmp_path / "cfg" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.jsonc").write_text(
        "{\n"
        '  "provider": {\n'
        '    "my-openai": {\n'
        '      "options": {"baseURL": "https://my-gw/v1",},\n'  # trailing comma
        "    },\n"  # trailing comma
        "  },\n"  # trailing comma
        "}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    result = maybe_merge_user_provider_config({})
    assert result["provider"]["my-openai"]["options"]["baseURL"] == "https://my-gw/v1"


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
    # MCPServerConfig.timeout is seconds; the opencode entry is milliseconds.
    assert block["local_custom"]["timeout"] == 120_000
    assert block["remote_custom"]["timeout"] == 45_500


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


def test_managed_connect_opencode_config_consumes_ucode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a managed connect host, opencode reuses ucode's generated config
    (provider block + system.ai model) and its refreshing auth plugin, copied into
    the per-session XDG dir."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # ucode's generated opencode config + auth plugin (its own XDG root).
    ucode_dir = tmp_path / ".ucode" / "opencode-xdg" / "opencode"
    (ucode_dir / "plugin").mkdir(parents=True)
    (ucode_dir / "opencode.json").write_text(
        json.dumps(
            {
                "model": "databricks-anthropic/system.ai.claude-opus-4-8",
                "provider": {"databricks-anthropic": {"options": {"baseURL": "https://ws/x"}}},
            }
        )
    )
    (ucode_dir / "plugin" / "ucode-auth.js").write_text("// ucode auth plugin\n")
    # A managed connect host (broker sidecar present) — and the ucode config
    # already exists, so no on-demand configure is triggered.
    monkeypatch.setattr(
        "omnigent.host.databricks_credential._read_sidecar",
        lambda path: {
            "server": "s",
            "host_id": "h",
            "host_token": "t",
            "workspace_host": "https://ws",
        },
    )

    session_xdg = tmp_path / "session-xdg"
    config = managed_connect_opencode_config(session_xdg)

    assert config is not None
    assert config["model"] == "databricks-anthropic/system.ai.claude-opus-4-8"  # system.ai
    assert "databricks-anthropic" in config["provider"]
    # auth plugin copied into the session dir and registered.
    session_plugin = session_xdg / "opencode" / "plugin" / "ucode-auth.js"
    assert session_plugin.exists()
    assert config["plugin"] == [str(session_plugin)]


def test_managed_connect_opencode_config_none_without_sidecar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No broker sidecar (e.g. a laptop) → None, so opencode's normal launch is
    untouched off a managed sandbox."""
    monkeypatch.setattr("omnigent.host.databricks_credential._read_sidecar", lambda path: None)
    assert managed_connect_opencode_config(Path("/tmp/unused-xdg")) is None


@pytest.mark.parametrize("bad_url", ["https://evil.example/x", "http://ws/x"])
def test_managed_connect_opencode_config_rejects_untrusted_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_url: str
) -> None:
    """A ucode config whose provider baseURL is not HTTPS on the sidecar's
    workspace host (a stale file from a prior connection, or a tampered one) is
    refused, so the freshly-minted broker bearer is never forwarded to an
    unverified origin."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ucode_dir = tmp_path / ".ucode" / "opencode-xdg" / "opencode"
    (ucode_dir / "plugin").mkdir(parents=True)
    (ucode_dir / "opencode.json").write_text(
        json.dumps(
            {
                "model": "databricks-anthropic/system.ai.claude-opus-4-8",
                "provider": {"databricks-anthropic": {"options": {"baseURL": bad_url}}},
            }
        )
    )
    (ucode_dir / "plugin" / "ucode-auth.js").write_text("// ucode auth plugin\n")
    monkeypatch.setattr(
        "omnigent.host.databricks_credential._read_sidecar",
        lambda path: {
            "server": "s",
            "host_id": "h",
            "host_token": "t",
            "workspace_host": "https://ws",  # bad_url points elsewhere / non-HTTPS
        },
    )

    assert managed_connect_opencode_config(tmp_path / "session-xdg") is None


_GATEWAY_CONFIG_YAML = """
providers:
  gateway:
    kind: gateway
    default: true
    anthropic:
      base_url: https://ws.example.com/ai-gateway/anthropic
      auth_command: databricks-token --host ws.example.com
      models:
        default: eng_dev.ai_gateway.omni-claude
    openai:
      base_url: https://ws.example.com/ai-gateway/openai/v1
      auth_command: databricks-token --host ws.example.com
      wire_api: chat
      models:
        default: eng_dev.ai_gateway.omni-gpt
"""


def _write_gateway_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    (tmp_path / "config.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))


def test_config_gateway_synthesizes_both_family_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config.yaml gateway with anthropic+openai families → two provider blocks."""
    _write_gateway_config(tmp_path, monkeypatch, _GATEWAY_CONFIG_YAML)

    resolution = resolve_config_gateway_providers()

    assert resolution is not None
    providers = resolution.config["provider"]
    assert set(providers) == {"gateway-anthropic", "gateway-openai"}

    anthropic = providers["gateway-anthropic"]
    assert anthropic["npm"] == "@ai-sdk/anthropic"
    assert anthropic["options"]["baseURL"] == "https://ws.example.com/ai-gateway/anthropic/v1"
    assert anthropic["models"] == {
        "eng_dev.ai_gateway.omni-claude": {"name": "eng_dev.ai_gateway.omni-claude"}
    }

    openai = providers["gateway-openai"]
    assert openai["npm"] == "@ai-sdk/openai-compatible"
    assert openai["options"]["baseURL"] == "https://ws.example.com/ai-gateway/openai/v1"
    assert openai["models"] == {
        "eng_dev.ai_gateway.omni-gpt": {"name": "eng_dev.ai_gateway.omni-gpt"}
    }

    assert resolution.config["model"] == "gateway-anthropic/eng_dev.ai_gateway.omni-claude"


def test_config_gateway_enumerates_all_tier_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All configured tiers (high/med/low/default) land in the models map."""
    body = """
providers:
  gateway:
    kind: gateway
    default: true
    anthropic:
      base_url: https://ws.example.com/ai-gateway/anthropic
      auth_command: databricks-token
      models:
        default: eng_dev.ai_gateway.omni-claude-high
        high: eng_dev.ai_gateway.omni-claude-high
        med: eng_dev.ai_gateway.omni-claude-med
        low: eng_dev.ai_gateway.omni-claude-low
"""
    _write_gateway_config(tmp_path, monkeypatch, body)

    resolution = resolve_config_gateway_providers()

    assert resolution is not None
    models = resolution.config["provider"]["gateway-anthropic"]["models"]
    assert set(models) == {
        "eng_dev.ai_gateway.omni-claude-high",
        "eng_dev.ai_gateway.omni-claude-med",
        "eng_dev.ai_gateway.omni-claude-low",
    }
    assert resolution.config["model"] == "gateway-anthropic/eng_dev.ai_gateway.omni-claude-high"


def _fake_model_service(
    model_id: str,
    *,
    responses: bool = False,
    anthropic: bool = False,
    efforts: tuple[str, ...] = (),
) -> types.SimpleNamespace:
    """A minimal stand-in for a discovered ``ModelEntry`` (id + wire_apis + reasoning)."""
    from omnigent.models.model_metadata import ModelWireAPI

    wire: set[ModelWireAPI] = set()
    if responses:
        wire.add(ModelWireAPI.OPENAI_RESPONSES)
    if anthropic:
        wire.add(ModelWireAPI.ANTHROPIC_MESSAGES)
    reasoning = types.SimpleNamespace(efforts=tuple(efforts)) if efforts else None
    return types.SimpleNamespace(
        id=model_id,
        metadata=types.SimpleNamespace(wire_apis=frozenset(wire), reasoning=reasoning),
    )


_GATEWAY_CONFIG_WITH_REVOKED_YAML = """
providers:
  gateway:
    kind: gateway
    default: true
    anthropic:
      base_url: https://ws.example.com/ai-gateway/anthropic
      auth_command: databricks-token
      models:
        default: eng_dev.ai_gateway.omni-claude-high
    openai:
      base_url: https://ws.example.com/ai-gateway/openai/v1
      auth_command: databricks-token
      wire_api: chat
      models:
        default: eng_dev.ai_gateway.omni-gpt-high
        glm: eng_dev.ai_gateway.glm-4-7
        grok: eng_dev.ai_gateway.grok-4-6
"""


def test_config_gateway_groups_effort_capable_models_via_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discovery groups effort-capable models into a Responses block, GLM into chat."""
    _write_gateway_config(tmp_path, monkeypatch, _GATEWAY_CONFIG_WITH_REVOKED_YAML)
    monkeypatch.setattr(
        "omnigent.harnesses.opencode_native.provider._mint_gateway_discovery_token",
        lambda families: "tok",
    )

    def _fake_fetch(
        host: str,
        token: str,
        *,
        model_services_parent: str | None = None,
        strict_details: bool = False,
    ):
        assert host == "https://ws.example.com"
        assert model_services_parent == "schemas/eng_dev.ai_gateway"
        return (
            _fake_model_service("eng_dev.ai_gateway.omni-claude-high"),
            _fake_model_service("eng_dev.ai_gateway.omni-gpt-high", responses=True),
            _fake_model_service("eng_dev.ai_gateway.omni-gpt-med", responses=True),
            _fake_model_service("eng_dev.ai_gateway.kimi-k3", responses=True),
            _fake_model_service("eng_dev.ai_gateway.glm-4-7", responses=True),
        )

    monkeypatch.setattr(
        "omnigent.models.model_catalog.fetch_databricks_model_service_entries", _fake_fetch
    )

    resolution = resolve_config_gateway_providers()

    assert resolution is not None
    providers = resolution.config["provider"]
    assert set(providers["gateway-anthropic"]["models"]) == {"eng_dev.ai_gateway.omni-claude-high"}

    responses = providers["gateway-openai-responses"]
    assert responses["npm"] == "@ai-sdk/openai"
    assert set(responses["models"]) == {
        "eng_dev.ai_gateway.omni-gpt-high",
        "eng_dev.ai_gateway.omni-gpt-med",
        "eng_dev.ai_gateway.kimi-k3",
    }
    assert all(m.get("reasoning") is True for m in responses["models"].values())
    assert "gateway-openai-responses" in resolution.auth_commands

    chat = providers["gateway-openai"]
    assert chat["npm"] == "@ai-sdk/openai-compatible"
    assert set(chat["models"]) == {"eng_dev.ai_gateway.glm-4-7"}
    assert "reasoning" not in chat["models"]["eng_dev.ai_gateway.glm-4-7"]

    all_ids = set().union(*(set(p["models"]) for p in providers.values()))
    assert "eng_dev.ai_gateway.grok-4-6" not in all_ids


def test_config_gateway_applies_and_clamps_reasoning_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session effort is written to Responses models, clamped per ceiling."""
    _write_gateway_config(tmp_path, monkeypatch, _GATEWAY_CONFIG_WITH_REVOKED_YAML)
    monkeypatch.setattr(
        "omnigent.harnesses.opencode_native.provider._mint_gateway_discovery_token",
        lambda families: "tok",
    )

    def _fake_fetch(
        host: str,
        token: str,
        *,
        model_services_parent: str | None = None,
        strict_details: bool = False,
    ):
        return (
            _fake_model_service(
                "eng_dev.ai_gateway.omni-gpt-high",
                responses=True,
                efforts=("low", "medium", "high", "xhigh", "max"),
            ),
            _fake_model_service(
                "eng_dev.ai_gateway.kimi-k3",
                responses=True,
                efforts=("low", "medium", "high"),  # ceiling: high
            ),
        )

    monkeypatch.setattr(
        "omnigent.models.model_catalog.fetch_databricks_model_service_entries", _fake_fetch
    )

    resolution = resolve_config_gateway_providers(reasoning_effort="max")

    assert resolution is not None
    models = resolution.config["provider"]["gateway-openai-responses"]["models"]
    assert models["eng_dev.ai_gateway.omni-gpt-high"]["options"]["reasoningEffort"] == "max"
    assert models["eng_dev.ai_gateway.kimi-k3"]["options"]["reasoningEffort"] == "high"


def test_config_gateway_omits_reasoning_effort_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No session effort (or a clear sentinel) leaves reasoningEffort unset."""
    _write_gateway_config(tmp_path, monkeypatch, _GATEWAY_CONFIG_WITH_REVOKED_YAML)
    monkeypatch.setattr(
        "omnigent.harnesses.opencode_native.provider._mint_gateway_discovery_token",
        lambda families: "tok",
    )

    def _fake_fetch(
        host: str,
        token: str,
        *,
        model_services_parent: str | None = None,
        strict_details: bool = False,
    ):
        return (
            _fake_model_service(
                "eng_dev.ai_gateway.omni-gpt-high", responses=True, efforts=("low", "high", "max")
            ),
        )

    monkeypatch.setattr(
        "omnigent.models.model_catalog.fetch_databricks_model_service_entries", _fake_fetch
    )

    for effort in (None, "default"):
        resolution = resolve_config_gateway_providers(reasoning_effort=effort)
        assert resolution is not None
        model = resolution.config["provider"]["gateway-openai-responses"]["models"][
            "eng_dev.ai_gateway.omni-gpt-high"
        ]
        assert model["reasoning"] is True
        assert "options" not in model


def test_clamp_effort_ladder() -> None:
    """Effort clamps to the highest advertised level not exceeding the request."""
    from omnigent.harnesses.opencode_native.provider import _clamp_effort

    gpt = ("low", "medium", "high", "xhigh", "max")
    kimi = ("low", "medium", "high")
    assert _clamp_effort("max", gpt) == "max"  # advertised → exact
    assert _clamp_effort("max", kimi) == "high"  # over ceiling → clamp down
    assert _clamp_effort("medium", kimi) == "medium"  # advertised → exact
    assert _clamp_effort("minimal", kimi) == "low"  # below all → lowest advertised
    assert _clamp_effort("high", ()) is None  # no ladder → nothing to set


def test_config_gateway_falls_back_to_static_when_discovery_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no discovery token, the resolver keeps the static config tiers."""
    _write_gateway_config(tmp_path, monkeypatch, _GATEWAY_CONFIG_WITH_REVOKED_YAML)

    resolution = resolve_config_gateway_providers()

    assert resolution is not None
    openai_models = set(resolution.config["provider"]["gateway-openai"]["models"])
    assert "eng_dev.ai_gateway.glm-4-7" in openai_models
    assert "eng_dev.ai_gateway.grok-4-6" in openai_models


def test_discovery_falls_back_to_static_when_listing_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model-services API error is swallowed and the static tiers are used."""
    _write_gateway_config(tmp_path, monkeypatch, _GATEWAY_CONFIG_WITH_REVOKED_YAML)
    monkeypatch.setattr(
        "omnigent.harnesses.opencode_native.provider._mint_gateway_discovery_token",
        lambda families: "tok",
    )

    def _boom(
        host: str,
        token: str,
        *,
        model_services_parent: str | None = None,
        strict_details: bool = False,
    ):
        raise RuntimeError("listing denied")

    monkeypatch.setattr(
        "omnigent.models.model_catalog.fetch_databricks_model_service_entries", _boom
    )

    resolution = resolve_config_gateway_providers()

    assert resolution is not None
    openai_models = set(resolution.config["provider"]["gateway-openai"]["models"])
    assert "eng_dev.ai_gateway.glm-4-7" in openai_models


def test_config_gateway_explicit_responses_default_routes_to_responses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicitly selecting a discovered Responses model uses the Responses provider.

    Discovery knows the model's wire API, so the explicit selection must land on
    the Responses provider (keeping its reasoning effort), not the chat group that
    merely carries it as the configured default — and not be duplicated there.
    """
    _write_gateway_config(tmp_path, monkeypatch, _GATEWAY_CONFIG_WITH_REVOKED_YAML)
    monkeypatch.setattr(
        "omnigent.harnesses.opencode_native.provider._mint_gateway_discovery_token",
        lambda families: "tok",
    )

    def _fake_fetch(
        host: str,
        token: str,
        *,
        model_services_parent: str | None = None,
        strict_details: bool = False,
    ):
        return (
            _fake_model_service(
                "eng_dev.ai_gateway.omni-gpt-high", responses=True, efforts=("low", "high", "max")
            ),
            _fake_model_service("eng_dev.ai_gateway.glm-4-7", responses=True),  # GLM → chat
        )

    monkeypatch.setattr(
        "omnigent.models.model_catalog.fetch_databricks_model_service_entries", _fake_fetch
    )

    resolution = resolve_config_gateway_providers(
        model_override="eng_dev.ai_gateway.omni-gpt-high", reasoning_effort="high"
    )

    assert resolution is not None
    assert (
        resolution.config["model"] == "gateway-openai-responses/eng_dev.ai_gateway.omni-gpt-high"
    )
    providers = resolution.config["provider"]
    responses = providers["gateway-openai-responses"]["models"]
    assert responses["eng_dev.ai_gateway.omni-gpt-high"]["options"]["reasoningEffort"] == "high"
    # The Responses model is not duplicated into the chat provider.
    assert "eng_dev.ai_gateway.omni-gpt-high" not in providers["gateway-openai"]["models"]


def test_config_gateway_discovers_each_family_schema_on_shared_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two families sharing a host but different UC schemas each keep their models.

    A successful listing of one schema must not replace the other family's
    configured/served tiers with an empty discovered list.
    """
    body = """
providers:
  gateway:
    kind: gateway
    default: true
    anthropic:
      base_url: https://ws.example.com/ai-gateway/anthropic
      auth_command: databricks-token
      models:
        default: team_a.gateway.omni-claude
    openai:
      base_url: https://ws.example.com/ai-gateway/openai/v1
      auth_command: databricks-token
      wire_api: chat
      models:
        default: team_b.gateway.house-gpt
"""
    _write_gateway_config(tmp_path, monkeypatch, body)
    monkeypatch.setattr(
        "omnigent.harnesses.opencode_native.provider._mint_gateway_discovery_token",
        lambda families: "tok",
    )

    def _fake_fetch(
        host: str,
        token: str,
        *,
        model_services_parent: str | None = None,
        strict_details: bool = False,
    ):
        if model_services_parent == "schemas/team_a.gateway":
            return (_fake_model_service("team_a.gateway.omni-claude"),)
        if model_services_parent == "schemas/team_b.gateway":
            return (
                _fake_model_service("team_b.gateway.house-gpt"),
                _fake_model_service("team_b.gateway.house-gpt-pro"),
            )
        raise AssertionError(f"unexpected schema {model_services_parent!r}")

    monkeypatch.setattr(
        "omnigent.models.model_catalog.fetch_databricks_model_service_entries", _fake_fetch
    )

    resolution = resolve_config_gateway_providers()

    assert resolution is not None
    providers = resolution.config["provider"]
    assert set(providers["gateway-anthropic"]["models"]) == {"team_a.gateway.omni-claude"}
    # OpenAI keeps its OWN schema's models (incl. the non-default tier), not A's.
    openai_models = set(providers["gateway-openai"]["models"])
    assert openai_models == {"team_b.gateway.house-gpt", "team_b.gateway.house-gpt-pro"}
    assert "team_a.gateway.omni-claude" not in openai_models


def test_config_gateway_openai_scoped_default_pins_openai(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider whose config default is ``openai`` launches its GPT default, not Claude."""
    body = """
providers:
  gateway:
    kind: gateway
    default: openai
    anthropic:
      base_url: https://ws.example.com/ai-gateway/anthropic
      auth_command: databricks-token
      models:
        default: eng_dev.ai_gateway.omni-claude
    openai:
      base_url: https://ws.example.com/ai-gateway/openai/v1
      auth_command: databricks-token
      wire_api: chat
      models:
        default: eng_dev.ai_gateway.omni-gpt
"""
    _write_gateway_config(tmp_path, monkeypatch, body)

    resolution = resolve_config_gateway_providers()

    assert resolution is not None
    assert resolution.config["model"] == "gateway-openai/eng_dev.ai_gateway.omni-gpt"


@pytest.mark.parametrize(
    "models_block",
    [
        # A direct alias: default -> pro -> concrete endpoint.
        """
      models:
        default: pro
        pro: eng_dev.ai_gateway.deepseek-v4-pro
""",
        # An alias chain: default -> fast -> pro -> concrete endpoint.
        """
      models:
        default: fast
        fast: pro
        pro: eng_dev.ai_gateway.deepseek-v4-pro
""",
    ],
)
def test_config_gateway_fallback_resolves_alias_tiers_to_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, models_block: str
) -> None:
    """With discovery unavailable, configured tiers resolve to concrete endpoints.

    The advertised models and the pinned default must be the concrete endpoint id
    the gateway accepts, never an intermediate ``models:`` alias.
    """
    body = f"""
providers:
  gateway:
    kind: gateway
    default: true
    openai:
      base_url: https://ws.example.com/ai-gateway/openai/v1
      auth_command: databricks-token
      wire_api: chat
{models_block}
"""
    _write_gateway_config(tmp_path, monkeypatch, body)

    resolution = resolve_config_gateway_providers()

    assert resolution is not None
    assert resolution.config["model"] == "gateway-openai/eng_dev.ai_gateway.deepseek-v4-pro"
    models = set(resolution.config["provider"]["gateway-openai"]["models"])
    assert models == {"eng_dev.ai_gateway.deepseek-v4-pro"}
    assert "pro" not in models and "fast" not in models


def test_config_gateway_default_selects_responses_when_catalog_is_responses_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unset model pins the Responses provider when discovery classes the default there.

    With a Responses-only catalog the chat group is authoritatively empty, so the
    configured default must not be claimed by chat; it pins the Responses provider
    and keeps its reasoning effort.
    """
    body = """
providers:
  gateway:
    kind: gateway
    default: true
    openai:
      base_url: https://ws.example.com/ai-gateway/openai/v1
      auth_command: databricks-token
      wire_api: chat
      models:
        default: eng_dev.ai_gateway.omni-gpt-high
"""
    _write_gateway_config(tmp_path, monkeypatch, body)
    monkeypatch.setattr(
        "omnigent.harnesses.opencode_native.provider._mint_gateway_discovery_token",
        lambda families: "tok",
    )

    def _fake_fetch(
        host: str,
        token: str,
        *,
        model_services_parent: str | None = None,
        strict_details: bool = False,
    ):
        return (
            _fake_model_service(
                "eng_dev.ai_gateway.omni-gpt-high", responses=True, efforts=("low", "high", "max")
            ),
        )

    monkeypatch.setattr(
        "omnigent.models.model_catalog.fetch_databricks_model_service_entries", _fake_fetch
    )

    resolution = resolve_config_gateway_providers(reasoning_effort="high")

    assert resolution is not None
    assert (
        resolution.config["model"] == "gateway-openai-responses/eng_dev.ai_gateway.omni-gpt-high"
    )
    providers = resolution.config["provider"]
    responses = providers["gateway-openai-responses"]["models"]
    assert responses["eng_dev.ai_gateway.omni-gpt-high"]["options"]["reasoningEffort"] == "high"
    # The chat provider must not claim the Responses-only default.
    assert (
        "gateway-openai" not in providers
        or "eng_dev.ai_gateway.omni-gpt-high" not in providers["gateway-openai"]["models"]
    )


def test_config_gateway_reconstructs_saved_responses_selection_during_outage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A saved ``<responses>/<model>`` selection survives a discovery outage.

    The Responses provider is not discovered while the workspace is unreachable,
    but an explicit saved selection must still be recognized and its provider
    reconstructed — never silently switched to another provider's default.
    """
    body = """
providers:
  gateway:
    kind: gateway
    default: true
    openai:
      base_url: https://ws.example.com/ai-gateway/openai/v1
      auth_command: databricks-token
      wire_api: chat
      models:
        default: eng_dev.ai_gateway.omni-gpt-high
"""
    _write_gateway_config(tmp_path, monkeypatch, body)
    # Discovery is unavailable (autouse _no_gateway_discovery stubs the token).

    resolution = resolve_config_gateway_providers(
        model_override="gateway-openai-responses/eng_dev.ai_gateway.omni-gpt-high"
    )

    assert resolution is not None
    assert (
        resolution.config["model"] == "gateway-openai-responses/eng_dev.ai_gateway.omni-gpt-high"
    )
    responses = resolution.config["provider"]["gateway-openai-responses"]
    assert responses["npm"] == "@ai-sdk/openai"
    assert "eng_dev.ai_gateway.omni-gpt-high" in responses["models"]


def test_config_gateway_honors_explicit_opencode_default_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider marked ``default: opencode`` is selected over a bare family default."""
    body = """
providers:
  gw-a:
    kind: gateway
    default: anthropic
    anthropic:
      base_url: https://a.example.com/ai-gateway/anthropic
      auth_command: databricks-token
      models:
        default: eng_dev.ai_gateway.omni-claude
  gw-b:
    kind: gateway
    default: opencode
    openai:
      base_url: https://b.example.com/ai-gateway/openai/v1
      auth_command: databricks-token
      wire_api: chat
      models:
        default: eng_dev.ai_gateway.omni-gpt
"""
    _write_gateway_config(tmp_path, monkeypatch, body)

    resolution = resolve_config_gateway_providers()

    assert resolution is not None
    # The opencode-scoped default (gw-b) wins over the anthropic-scoped gw-a.
    assert resolution.config["model"] == "gw-b-openai/eng_dev.ai_gateway.omni-gpt"


def test_list_config_gateway_opencode_models_resolves_alias_tiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The setup picker offers concrete endpoint ids, never intermediate aliases."""
    from omnigent.cli_config import _list_config_gateway_opencode_models

    body = """
providers:
  gateway:
    kind: gateway
    default: true
    openai:
      base_url: https://ws.example.com/ai-gateway/openai/v1
      auth_command: databricks-token
      wire_api: chat
      models:
        default: pro
        pro: eng_dev.ai_gateway.deepseek-v4-pro
"""
    _write_gateway_config(tmp_path, monkeypatch, body)

    assert _list_config_gateway_opencode_models() == [
        "gateway-openai/eng_dev.ai_gateway.deepseek-v4-pro"
    ]


def test_config_gateway_openai_default_survives_discovery_replacing_its_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``default: openai`` still launches OpenAI when discovery replaces its model.

    The configured OpenAI default is no longer served, but discovery supplies a
    replacement; the selected family must be exhausted (pin the replacement)
    before falling back to the still-valid Anthropic default.
    """
    body = """
providers:
  gateway:
    kind: gateway
    default: openai
    anthropic:
      base_url: https://ws.example.com/ai-gateway/anthropic
      auth_command: databricks-token
      models:
        default: eng_dev.ai_gateway.claude-current
    openai:
      base_url: https://ws.example.com/ai-gateway/openai/v1
      auth_command: databricks-token
      wire_api: chat
      models:
        default: eng_dev.ai_gateway.gpt-old
"""
    _write_gateway_config(tmp_path, monkeypatch, body)
    monkeypatch.setattr(
        "omnigent.harnesses.opencode_native.provider._mint_gateway_discovery_token",
        lambda families: "tok",
    )

    def _fake_fetch(host: str, token: str, *, model_services_parent=None, strict_details=False):
        # gpt-old is gone; discovery returns a replacement plus the anthropic default.
        return (
            _fake_model_service("eng_dev.ai_gateway.claude-current", anthropic=True),
            _fake_model_service("eng_dev.ai_gateway.gpt-replacement"),
        )

    monkeypatch.setattr(
        "omnigent.models.model_catalog.fetch_databricks_model_service_entries", _fake_fetch
    )

    resolution = resolve_config_gateway_providers()

    assert resolution is not None
    assert resolution.config["model"] == "gateway-openai/eng_dev.ai_gateway.gpt-replacement"


def test_config_gateway_responses_only_opencode_default_falls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Responses-only ``default: opencode`` provider is skipped for a usable one.

    Its scope differs from the ``default: anthropic`` gateway's, so both are legal;
    the ineligible OpenCode default must not resolve ``None`` when a supported
    family default exists.
    """
    body = """
providers:
  gw-openai:
    kind: gateway
    default: opencode
    openai:
      base_url: https://o.example.com/ai-gateway/openai/v1
      auth_command: databricks-token
      wire_api: responses
      models:
        default: eng_dev.ai_gateway.omni-gpt
  gw-anthropic:
    kind: gateway
    default: anthropic
    anthropic:
      base_url: https://a.example.com/ai-gateway/anthropic
      auth_command: databricks-token
      models:
        default: eng_dev.ai_gateway.omni-claude
"""
    _write_gateway_config(tmp_path, monkeypatch, body)

    resolution = resolve_config_gateway_providers()

    assert resolution is not None
    assert resolution.config["model"] == "gw-anthropic-anthropic/eng_dev.ai_gateway.omni-claude"


def test_config_gateway_databricks_opencode_default_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand-written ``databricks`` ``default: opencode`` is not driveable here."""
    body = """
providers:
  dbx:
    kind: databricks
    profile: ws
    default: opencode
"""
    _write_gateway_config(tmp_path, monkeypatch, body)

    assert resolve_config_gateway_providers() is None


def test_config_gateway_alias_override_with_suffix_pins_stripped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A qualified alias override resolves to a concrete, suffix-stripped id.

    The pinned model must equal the registered model exactly — never a suffixed
    ``[1m]`` variant that is absent from the generated provider block.
    """
    body = """
providers:
  gateway:
    kind: gateway
    default: true
    anthropic:
      base_url: https://ws.example.com/ai-gateway/anthropic
      auth_command: databricks-token
      models:
        default: eng_dev.ai_gateway.omni-claude
        pro: eng_dev.ai_gateway.omni-claude-pro[1m]
"""
    _write_gateway_config(tmp_path, monkeypatch, body)

    resolution = resolve_config_gateway_providers(model_override="gateway-anthropic/pro")

    assert resolution is not None
    models = resolution.config["provider"]["gateway-anthropic"]["models"]
    assert "eng_dev.ai_gateway.omni-claude-pro" in models
    assert "eng_dev.ai_gateway.omni-claude-pro[1m]" not in models
    assert resolution.config["model"] == "gateway-anthropic/eng_dev.ai_gateway.omni-claude-pro"


def test_config_gateway_qualified_openai_override_reconciles_to_responses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chat-qualified picker selection routes to Responses when discovery says so.

    The picker prefixes openai models with the chat provider; if discovery
    classifies the model as Responses, launch must reconcile it to the Responses
    provider rather than pin it onto the chat endpoint.
    """
    _write_gateway_config(tmp_path, monkeypatch, _GATEWAY_CONFIG_WITH_REVOKED_YAML)
    monkeypatch.setattr(
        "omnigent.harnesses.opencode_native.provider._mint_gateway_discovery_token",
        lambda families: "tok",
    )

    def _fake_fetch(host: str, token: str, *, model_services_parent=None, strict_details=False):
        return (_fake_model_service("eng_dev.ai_gateway.omni-gpt-high", responses=True),)

    monkeypatch.setattr(
        "omnigent.models.model_catalog.fetch_databricks_model_service_entries", _fake_fetch
    )

    resolution = resolve_config_gateway_providers(
        model_override="gateway-openai/eng_dev.ai_gateway.omni-gpt-high"
    )

    assert resolution is not None
    assert (
        resolution.config["model"] == "gateway-openai-responses/eng_dev.ai_gateway.omni-gpt-high"
    )


def test_responses_only_openai_not_advertised_to_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Responses-only openai gateway is not offered to OpenCode anywhere.

    Launch skips the Responses wire, so readiness (``provider_families``), the
    picker, and resolution must all agree it is not OpenCode-capable — otherwise
    setup could save a default and the picker a selection that launch ignores.
    """
    from omnigent.cli_config import _list_config_gateway_opencode_models
    from omnigent.onboarding.provider_config import (
        OPENAI_FAMILY,
        OPENCODE_SURFACE,
        get_default_provider,
        load_config,
        provider_families,
    )

    body = """
providers:
  gateway:
    kind: gateway
    default: true
    openai:
      base_url: https://ws.example.com/ai-gateway/openai/v1
      auth_command: databricks-token
      wire_api: responses
      models:
        default: eng_dev.ai_gateway.omni-gpt
"""
    _write_gateway_config(tmp_path, monkeypatch, body)

    entry = get_default_provider(load_config(), OPENAI_FAMILY)
    assert entry is not None
    surfaces = provider_families(entry)
    assert OPENCODE_SURFACE not in surfaces  # readiness/defaults
    assert OPENAI_FAMILY in surfaces  # still serves codex/pi
    assert _list_config_gateway_opencode_models() == []  # picker
    assert resolve_config_gateway_providers() is None  # launch


def test_gateway_model_family_classification() -> None:
    """Discovered model-services route to opencode's three groups like pi."""
    from omnigent.harnesses.opencode_native.provider import _gateway_model_family
    from omnigent.models.model_metadata import ModelWireAPI

    def fam(model_id: str, *, responses: bool = False) -> str | None:
        return _gateway_model_family(_fake_model_service(model_id, responses=responses))

    assert fam("eng_dev.ai_gateway.omni-claude-high") == "anthropic"
    assert fam("eng_dev.ai_gateway.omni-gpt-high", responses=True) == "openai-responses"
    assert fam("eng_dev.ai_gateway.kimi-k3", responses=True) == "openai-responses"
    assert fam("eng_dev.ai_gateway.glm-4-7", responses=True) == "openai"
    assert fam("eng_dev.ai_gateway.grok-4-6") == "openai"
    assert fam("system.ai.gemini-2-5-flash") is None
    assert fam("system.ai.llama-4") is None
    # An alias with no "claude" in the name but ANTHROPIC_MESSAGES in wire_apis
    # must route to the anthropic group (wire_api takes precedence over name).
    anthropic_by_wire = types.SimpleNamespace(
        id="eng_dev.ai_gateway.omni-sonnet-high",
        metadata=types.SimpleNamespace(
            wire_apis=frozenset({ModelWireAPI.ANTHROPIC_MESSAGES}),
            reasoning=None,
        ),
    )
    assert _gateway_model_family(anthropic_by_wire) == "anthropic"


def test_derive_model_services_parent_and_host() -> None:
    """The UC parent and workspace origin are derived from config, no new fields."""
    from omnigent.harnesses.opencode_native.provider import (
        _derive_model_services_parent,
        _gateway_host_from_base_url,
    )

    families = [
        (
            "openai",
            "@ai-sdk/openai-compatible",
            types.SimpleNamespace(
                models={"default": "eng_dev.ai_gateway.omni-gpt-high"},
                base_url="https://ws.example.com/ai-gateway/openai/v1",
            ),
        )
    ]
    assert _derive_model_services_parent(families) == "schemas/eng_dev.ai_gateway"
    assert _gateway_host_from_base_url(families[0][2].base_url) == "https://ws.example.com"
    bare = [("openai", "npm", types.SimpleNamespace(models={"default": "gpt-4"}, base_url="x"))]
    assert _derive_model_services_parent(bare) is None


_GATEWAY_CONFIG_TWO_SCHEMAS_YAML = """
providers:
  gateway:
    kind: gateway
    default: true
    anthropic:
      base_url: https://ws.example.com/ai-gateway/anthropic
      auth_command: databricks-token
      models:
        default: eng_dev.anth_gw.omni-claude-high
    openai:
      base_url: https://ws.example.com/ai-gateway/openai/v1
      auth_command: databricks-token
      wire_api: chat
      models:
        default: eng_dev.openai_gw.omni-gpt-high
"""


def test_discover_gateway_models_per_schema_partitions_family_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each family discovers from its own Unity-Catalog schema so models in one
    family's schema are not missed when both families share a workspace host.

    Regression: previously a single (host, parent) pair was derived from all
    families, so if Anthropic's schema was used for the entire listing the
    OpenAI family's models (in a different schema) were never fetched.
    """
    _write_gateway_config(tmp_path, monkeypatch, _GATEWAY_CONFIG_TWO_SCHEMAS_YAML)
    monkeypatch.setattr(
        "omnigent.harnesses.opencode_native.provider._mint_gateway_discovery_token",
        lambda families: "tok",
    )

    def _fetch_by_schema(
        host: str,
        token: str,
        *,
        model_services_parent: str | None = None,
        strict_details: bool = False,
    ):
        # Each schema contains only that family's models; verify separate calls.
        if model_services_parent == "schemas/eng_dev.anth_gw":
            return (_fake_model_service("eng_dev.anth_gw.omni-claude-high"),)
        if model_services_parent == "schemas/eng_dev.openai_gw":
            return (_fake_model_service("eng_dev.openai_gw.omni-gpt-high", responses=True),)
        return ()

    monkeypatch.setattr(
        "omnigent.models.model_catalog.fetch_databricks_model_service_entries",
        _fetch_by_schema,
    )

    resolution = resolve_config_gateway_providers()

    assert resolution is not None
    providers = resolution.config["provider"]
    # Both schemas reached: Anthropic models from anth_gw, GPT from openai_gw.
    assert "eng_dev.anth_gw.omni-claude-high" in providers["gateway-anthropic"]["models"]
    assert "eng_dev.openai_gw.omni-gpt-high" in providers["gateway-openai-responses"]["models"]


@pytest.mark.parametrize(
    ("config", "expect_disabled"),
    [
        # A replacement provider block → hide the free tier.
        (
            {
                "$schema": "https://opencode.ai/config.json",
                "provider": {"gateway-anthropic": {"npm": "@ai-sdk/anthropic"}},
                "model": "gateway-anthropic/eng_dev.ai_gateway.omni-claude-high",
            },
            True,
        ),
        # Empty config → nothing to disable.
        ({}, False),
        # Only MCP/plugin wiring (no provider, no model) → keep the free tier usable.
        (
            {
                "$schema": "https://opencode.ai/config.json",
                "mcp": {"omnigent": {"type": "local"}},
                "permission": "ask",
            },
            False,
        ),
        # An explicitly selected opencode/... model → preserve it.
        ({"$schema": "https://opencode.ai/config.json", "model": "opencode/big-pickle"}, False),
    ],
)
def test_disable_autoloaded_free_providers(
    config: dict[str, object], expect_disabled: bool
) -> None:
    """Hide opencode's free tier only when a replacement provider/model exists."""
    disable_autoloaded_free_providers(config)
    if expect_disabled:
        assert config["disabled_providers"] == ["opencode"]
    else:
        assert "disabled_providers" not in config


def test_disable_autoloaded_free_providers_survives_writer(tmp_path: Path) -> None:
    """The disabled_providers key round-trips through the config writer."""
    config = disable_autoloaded_free_providers(
        build_opencode_model_default_config(
            "gateway-anthropic/eng_dev.ai_gateway.omni-claude-high"
        )
    )
    path = write_opencode_provider_config(tmp_path, config)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["disabled_providers"] == ["opencode"]


def test_config_gateway_auth_command_writes_placeholder_key_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An auth_command family gets only the factory placeholder, never a minted token."""
    _write_gateway_config(tmp_path, monkeypatch, _GATEWAY_CONFIG_YAML)

    resolution = resolve_config_gateway_providers()

    assert resolution is not None
    for block in resolution.config["provider"].values():
        assert block["options"]["apiKey"] == "omnigent-gateway-auth-plugin"
    assert resolution.auth_commands == {
        "gateway-anthropic": "databricks-token --host ws.example.com",
        "gateway-openai": "databricks-token --host ws.example.com",
    }


def test_config_gateway_static_key_written_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A static api_key family writes options.apiKey inline (no auth_command)."""
    body = """
providers:
  gateway:
    kind: key
    default: true
    anthropic:
      base_url: https://api.anthropic.com
      api_key: sk-test-literal
      models:
        default: claude-sonnet-4-6
"""
    _write_gateway_config(tmp_path, monkeypatch, body)

    resolution = resolve_config_gateway_providers()

    assert resolution is not None
    anthropic = resolution.config["provider"]["gateway-anthropic"]
    assert anthropic["options"]["apiKey"] == "sk-test-literal"
    assert resolution.auth_commands == {}


@pytest.mark.parametrize(
    ("override", "expected_model"),
    [
        # A bare id routes to the family that lists/implies it (suffix stripped).
        (
            "eng_dev.ai_gateway.omni-claude-opus[1m]",
            "gateway-anthropic/eng_dev.ai_gateway.omni-claude-opus",
        ),
        ("eng_dev.ai_gateway.omni-gpt", "gateway-openai/eng_dev.ai_gateway.omni-gpt"),
        # A gateway-qualified id keeps its provider (never double-prefixed).
        (
            "gateway-anthropic/eng_dev.ai_gateway.selected-model[1m]",
            "gateway-anthropic/eng_dev.ai_gateway.selected-model",
        ),
        (
            "gateway-openai/eng_dev.ai_gateway.selected-model[1m]",
            "gateway-openai/eng_dev.ai_gateway.selected-model",
        ),
        # A non-gateway provider selection is declined, never rerouted.
        ("anthropic/claude-sonnet-4-6", None),
        ("openai/gpt-5.5", None),
        ("google/gemini-2.5-pro", None),
        ("opencode/big-pickle", None),
        # An unlisted id over several families is ambiguous → declined.
        ("an-id-no-family-lists", None),
    ],
)
def test_config_gateway_override_routing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: str,
    expected_model: str | None,
) -> None:
    """Override routing over a both-families gateway: pin the right provider or decline."""
    _write_gateway_config(tmp_path, monkeypatch, _GATEWAY_CONFIG_YAML)

    resolution = resolve_config_gateway_providers(model_override=override)

    if expected_model is None:
        assert resolution is None
    else:
        assert resolution is not None
        assert resolution.config["model"] == expected_model
        pinned_model = expected_model.split("/", 1)[1]
        provider_id = expected_model.split("/", 1)[0]
        assert pinned_model in resolution.config["provider"][provider_id]["models"]


@pytest.mark.parametrize(
    ("family", "family_base_url", "wire", "override"),
    [
        # Anthropic-only gateway, an unlisted GPT id → recognized as openai, which
        # is unavailable here → decline (do not pin onto the anthropic surface).
        ("anthropic", "https://ws.example.com/ai-gateway/anthropic", None, "some-gpt-5"),
        # OpenAI-only gateway, an unlisted Claude id → recognized as anthropic,
        # which is unavailable here → decline.
        ("openai", "https://ws.example.com/ai-gateway/openai/v1", "chat", "some-claude-x"),
        # A single-family gateway must not swallow another provider's selection.
        (
            "anthropic",
            "https://ws.example.com/ai-gateway/anthropic",
            None,
            "google/gemini-2.5-pro",
        ),
    ],
)
def test_config_gateway_single_family_declines_cross_family_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    family: str,
    family_base_url: str,
    wire: str | None,
    override: str,
) -> None:
    """A lone family declines an override whose recognized family it cannot serve."""
    wire_line = f"      wire_api: {wire}\n" if wire else ""
    body = f"""
providers:
  gateway:
    kind: gateway
    default: true
    {family}:
      base_url: {family_base_url}
      auth_command: databricks-token
{wire_line}      models:
        default: eng_dev.ai_gateway.house-default
"""
    _write_gateway_config(tmp_path, monkeypatch, body)

    assert resolve_config_gateway_providers(model_override=override) is None


def test_config_gateway_unlisted_override_pins_the_sole_family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a single family the target is unambiguous, so an override still pins."""
    body = """
providers:
  gateway:
    kind: gateway
    default: true
    anthropic:
      base_url: https://ws.example.com/ai-gateway/anthropic
      auth_command: databricks-token
      models:
        default: eng_dev.ai_gateway.omni-claude
"""
    _write_gateway_config(tmp_path, monkeypatch, body)

    resolution = resolve_config_gateway_providers(model_override="an-id-no-family-lists")

    assert resolution is not None
    assert resolution.config["model"] == "gateway-anthropic/an-id-no-family-lists"
    assert "an-id-no-family-lists" in resolution.config["provider"]["gateway-anthropic"]["models"]


def test_config_gateway_slash_model_id_kept_whole_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slash-bearing model id (e.g. ``zai-org/GLM-4.7``) is not split on ``/``.

    Only a synthesized gateway provider id (``gateway-openai``) qualifies an
    override; any other slash belongs to the model id itself, so the whole
    string is matched against the family and routed through the gateway rather
    than bypassing it.
    """
    body = """
providers:
  gateway:
    kind: gateway
    default: true
    openai:
      base_url: https://ws.example.com/ai-gateway/openai/v1
      auth_command: databricks-token
      wire_api: chat
      models:
        default: zai-org/GLM-4.7
"""
    _write_gateway_config(tmp_path, monkeypatch, body)

    resolution = resolve_config_gateway_providers(model_override="zai-org/GLM-4.7")

    assert resolution is not None
    assert resolution.config["model"] == "gateway-openai/zai-org/GLM-4.7"
    assert "zai-org/GLM-4.7" in resolution.config["provider"]["gateway-openai"]["models"]


def test_config_gateway_skips_openai_responses_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An openai family that is not Chat-Completions wire is skipped (not driveable)."""
    body = """
providers:
  gateway:
    kind: gateway
    default: true
    openai:
      base_url: https://ws.example.com/ai-gateway/openai/v1
      auth_command: databricks-token
      wire_api: responses
      models:
        default: eng_dev.ai_gateway.omni-gpt
"""
    _write_gateway_config(tmp_path, monkeypatch, body)

    assert resolve_config_gateway_providers() is None


def test_config_gateway_omitted_wire_api_keeps_openai_family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OpenAI family with no ``wire_api`` is driven as chat, not discarded."""
    body = """
providers:
  gateway:
    kind: gateway
    default: true
    openai:
      base_url: https://ws.example.com/ai-gateway/openai/v1
      auth_command: databricks-token
      models:
        default: eng_dev.ai_gateway.omni-gpt
"""
    _write_gateway_config(tmp_path, monkeypatch, body)

    resolution = resolve_config_gateway_providers()

    assert resolution is not None
    assert resolution.config["model"] == "gateway-openai/eng_dev.ai_gateway.omni-gpt"
    block = resolution.config["provider"]["gateway-openai"]
    assert block["npm"] == "@ai-sdk/openai-compatible"
    assert "eng_dev.ai_gateway.omni-gpt" in block["models"]


def test_config_gateway_omitted_wire_api_openai_default_pins_gpt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With both families and ``default: openai`` but no ``wire_api``, GPT still wins."""
    body = """
providers:
  gateway:
    kind: gateway
    default: openai
    anthropic:
      base_url: https://ws.example.com/ai-gateway/anthropic
      auth_command: databricks-token
      models:
        default: eng_dev.ai_gateway.omni-claude
    openai:
      base_url: https://ws.example.com/ai-gateway/openai/v1
      auth_command: databricks-token
      models:
        default: eng_dev.ai_gateway.omni-gpt
"""
    _write_gateway_config(tmp_path, monkeypatch, body)

    resolution = resolve_config_gateway_providers()

    assert resolution is not None
    assert resolution.config["model"] == "gateway-openai/eng_dev.ai_gateway.omni-gpt"
    assert "gateway-openai" in resolution.config["provider"]


def test_config_gateway_returns_none_for_subscription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subscription default is not driveable here → None (fall back to other paths)."""
    body = """
providers:
  claude:
    kind: subscription
    default: true
    cli: claude
"""
    _write_gateway_config(tmp_path, monkeypatch, body)

    assert resolve_config_gateway_providers() is None


def test_config_gateway_returns_none_without_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No config.yaml at all → None."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    assert resolve_config_gateway_providers() is None


def test_gateway_discovery_mints_per_origin_without_mixing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each origin's discovery token is minted only from that origin's families.

    Families on different workspaces must never share a credential: every mint
    call sees families of exactly one origin, and each origin is minted from its
    own family.
    """
    from omnigent.harnesses.opencode_native import provider as prov
    from omnigent.onboarding.provider_config import FamilyConfig

    anthropic = FamilyConfig(
        base_url="https://ws-a.example.com/ai-gateway/anthropic",
        auth_command="mint-a",
        models={"default": "eng_dev.ai_gateway.omni-claude"},
    )
    openai = FamilyConfig(
        base_url="https://ws-b.example.com/ai-gateway/openai/v1",
        auth_command="mint-b",
        wire_api="chat",
        models={"default": "eng_dev.ai_gateway.omni-gpt"},
    )
    families = [
        ("anthropic", "@ai-sdk/anthropic", anthropic),
        ("openai", "@ai-sdk/openai-compatible", openai),
    ]

    mint_calls: list[list[str | None]] = []

    def _capture(fams: list) -> None:
        mint_calls.append([prov._gateway_host_from_base_url(f[2].base_url) for f in fams])
        return

    monkeypatch.setattr(prov, "_mint_gateway_discovery_token", _capture)

    prov._discover_gateway_models(families)

    assert mint_calls, "discovery should attempt a mint per origin"
    # No mint call ever mixes origins ...
    for hosts in mint_calls:
        assert len(set(hosts)) == 1
    # ... and each origin is minted from its own family.
    assert {hosts[0] for hosts in mint_calls} == {
        "https://ws-a.example.com",
        "https://ws-b.example.com",
    }


def test_config_gateway_keeps_configured_models_for_undiscovered_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A catalog from one workspace is never assigned to another workspace's family.

    Anthropic lives on workspace A, OpenAI on workspace B. Discovery succeeds only
    for A; the OpenAI (B) provider must keep its configured models and must not
    advertise A's discovered ids.
    """
    body = """
providers:
  gateway:
    kind: gateway
    default: true
    anthropic:
      base_url: https://ws-a.example.com/ai-gateway/anthropic
      auth_command: databricks-token
      models:
        default: eng_dev.ai_gateway.omni-claude
    openai:
      base_url: https://ws-b.example.com/ai-gateway/openai/v1
      auth_command: databricks-token
      wire_api: chat
      models:
        default: acme.gw.house-gpt
"""
    _write_gateway_config(tmp_path, monkeypatch, body)
    monkeypatch.setattr(
        "omnigent.harnesses.opencode_native.provider._mint_gateway_discovery_token",
        lambda families: "tok",
    )

    def _fake_fetch(
        host: str,
        token: str,
        *,
        model_services_parent: str | None = None,
        strict_details: bool = False,
    ):
        # Only workspace A answers; workspace B's listing fails.
        if host == "https://ws-a.example.com":
            return (_fake_model_service("eng_dev.ai_gateway.omni-claude"),)
        raise RuntimeError("workspace B listing denied")

    monkeypatch.setattr(
        "omnigent.models.model_catalog.fetch_databricks_model_service_entries", _fake_fetch
    )

    resolution = resolve_config_gateway_providers()

    assert resolution is not None
    providers = resolution.config["provider"]
    # A's discovered id populates only the anthropic (A) provider.
    assert set(providers["gateway-anthropic"]["models"]) == {"eng_dev.ai_gateway.omni-claude"}
    # B keeps its configured model and never inherits A's discovered id.
    openai_models = set(providers["gateway-openai"]["models"])
    assert openai_models == {"acme.gw.house-gpt"}
    assert "eng_dev.ai_gateway.omni-claude" not in openai_models


def test_config_gateway_skips_undriveable_openai_default_for_anthropic_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OpenAI subscription default must not hide a usable Anthropic gateway."""
    body = """
providers:
  my-openai:
    kind: subscription
    default: openai
    cli: codex
  gw:
    kind: gateway
    default: anthropic
    anthropic:
      base_url: https://ws.example.com/ai-gateway/anthropic
      auth_command: databricks-token
      models:
        default: eng_dev.ai_gateway.omni-claude
"""
    _write_gateway_config(tmp_path, monkeypatch, body)

    resolution = resolve_config_gateway_providers(model_override="eng_dev.ai_gateway.omni-claude")

    assert resolution is not None
    assert resolution.config["model"] == "gw-anthropic/eng_dev.ai_gateway.omni-claude"
    assert "gw-anthropic" in resolution.config["provider"]


def test_gateway_auth_plugin_injects_bearer_per_request() -> None:
    """The generated plugin hooks chat.headers and reads the provider id it carries."""
    js = build_gateway_auth_plugin_js()

    assert "OMNIGENT_OPENCODE_AUTH_COMMAND" in js
    assert '"chat.headers"' in js
    assert 'output.headers["Authorization"] = "Bearer " + token' in js
    assert "input?.provider?.id" in js


def test_write_gateway_auth_plugin_creates_file(tmp_path: Path) -> None:
    """The plugin writer materializes the JS module in the bridge dir."""
    path = write_opencode_gateway_auth_plugin(tmp_path)
    assert path.exists()
    assert path.name == "omnigent-gateway-auth.js"
    assert '"chat.headers"' in path.read_text(encoding="utf-8")
