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
    build_opencode_model_default_config,
    build_opencode_omnigent_mcp_server,
    build_opencode_provider_config,
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
