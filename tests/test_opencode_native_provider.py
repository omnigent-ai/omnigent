"""Unit tests for opencode-native provider-config synthesis."""

from __future__ import annotations

import json
import stat
import sys
import types
from pathlib import Path

import pytest

from omnigent.opencode_native_provider import (
    DEFAULT_DATABRICKS_GATEWAY_MODEL,
    OpenCodeGatewayResolution,
    _gateway_endpoint_for_model,
    _strip_jsonc_comments,
    _strip_trailing_commas,
    build_opencode_model_default_config,
    build_opencode_omnigent_mcp_server,
    build_opencode_provider_config,
    maybe_merge_user_provider_config,
    resolve_databricks_gateway,
    write_opencode_provider_config,
)


def test_build_omnigent_mcp_server_points_serve_mcp_at_bridge_dir() -> None:
    block = build_opencode_omnigent_mcp_server(Path("/tmp/bridge-xyz"))
    assert set(block) == {"omnigent"}
    entry = block["omnigent"]
    assert entry["type"] == "local"
    assert entry["enabled"] is True
    cmd = entry["command"]
    # Launches the SHARED serve-mcp relay, pointed at THIS bridge dir.
    assert cmd[-3:] == ["serve-mcp", "--bridge-dir", "/tmp/bridge-xyz"]
    assert "omnigent.claude_native_bridge" in cmd
    assert entry.get("environment", {}).get("PYTHONUNBUFFERED") == "1"


def test_build_omnigent_mcp_server_honors_python_executable() -> None:
    block = build_opencode_omnigent_mcp_server(Path("/tmp/b"), python_executable="/custom/python")
    assert block["omnigent"]["command"][0] == "/custom/python"


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


def _install_fake_sdk(monkeypatch: pytest.MonkeyPatch, *, host: str, token: str | None) -> None:
    fake = types.ModuleType("databricks.sdk.core")

    class _Config:
        def __init__(self, *, profile: str) -> None:
            self.profile = profile
            self.host = host

        def authenticate(self) -> dict[str, str]:
            return {"Authorization": f"Bearer {token}"} if token else {}

    fake.Config = _Config  # type: ignore[attr-defined]
    # Ensure parent packages resolve for the dotted import.
    monkeypatch.setitem(sys.modules, "databricks", types.ModuleType("databricks"))
    monkeypatch.setitem(sys.modules, "databricks.sdk", types.ModuleType("databricks.sdk"))
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
    assert res.model_id == DEFAULT_DATABRICKS_GATEWAY_MODEL


def test_resolve_gateway_none_when_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_sdk(monkeypatch, host="https://ws.databricks.com", token=None)
    assert resolve_databricks_gateway("oss") is None


def test_build_mcp_block_stdio_and_http() -> None:
    from types import SimpleNamespace as N

    from omnigent.opencode_native_provider import build_opencode_mcp_block

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

    import omnigent.opencode_native_provider as prov

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
