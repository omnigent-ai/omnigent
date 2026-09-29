"""The runner writes the per-session v2 opencode.json before booting ``opencode serve``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

import omnigent.harnesses.opencode_native.app_server as app_server
import omnigent.harnesses.opencode_native.bridge as bridge
import omnigent.harnesses.opencode_native.provider as provider
import omnigent.runner._entry as runner_entry
import omnigent.runner.native.orchestration as orchestration
from omnigent.spec import AgentSpec


class _StopBeforeBoot(Exception):
    pass


class _FakeServer:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def start(self) -> None:
        raise _StopBeforeBoot


class _StopAtSessionResolve(Exception):
    pass


class _FakeOpenCodeClient:
    def __init__(self) -> None:
        self.aclosed = False

    async def aclose(self) -> None:
        self.aclosed = True


class _FakeStartedServer:
    """A fake server whose ``start()`` succeeds so the launch reaches ``client()``."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.closed = False
        self._client = _FakeOpenCodeClient()

    async def start(self) -> None:
        return None

    def client(self) -> _FakeOpenCodeClient:
        return self._client

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def launch_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "bridges")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-user-config"))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://omnigent.test")
    seeded: list[Path] = []
    monkeypatch.setattr(bridge, "seed_opencode_auth", lambda d: seeded.append(d))
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "omnigent-config"))
    monkeypatch.setattr(provider, "resolve_bound_opencode_gateway", lambda **k: None)
    monkeypatch.setattr(provider, "resolve_databricks_gateway", lambda *a, **k: None)
    monkeypatch.setattr(provider, "managed_connect_opencode_config", lambda *a: None)
    monkeypatch.setattr(runner_entry, "_make_auth_token_factory", lambda *a, **k: None)
    monkeypatch.setattr(app_server, "OpenCodeNativeServer", _FakeServer)

    async def _launch_config(**kwargs: Any) -> Any:
        return orchestration._OpenCodeNativeLaunchConfig(
            workspace=tmp_path,
            policy_server_url="http://omnigent.test",
            terminal_launch_args=[],
            model_override="anthropic/claude-sonnet-4-5",
            external_session_id=None,
            fork_carry_history=False,
        )

    monkeypatch.setattr(orchestration, "_opencode_native_launch_config", _launch_config)
    return {"seeded": seeded}


async def _launch_until_boot(**kwargs: Any) -> Path:
    with pytest.raises(_StopBeforeBoot):
        await orchestration._auto_create_opencode_terminal(
            "conv_launch", cast(Any, object()), lambda *a: None, **kwargs
        )
    return bridge.bridge_dir_for_bridge_id("conv_launch")


async def test_launch_writes_v2_config_with_ask_all_permissions(
    launch_env: dict[str, Any],
) -> None:
    async def _ensure_relay(*args: Any, **kwargs: Any) -> None:
        return None

    bridge_dir = await _launch_until_boot(
        server_client=object(),
        ensure_comment_relay=_ensure_relay,
        agent_spec=AgentSpec(spec_version=1, name="rules", instructions="Agent rules."),
    )
    xdg = bridge_dir / "xdg-config"
    written = json.loads((xdg / "opencode" / "opencode.json").read_text(encoding="utf-8"))
    assert written["permissions"] == [{"action": "*", "resource": "*", "effect": "ask"}]
    assert written["mcp"]["servers"]["omnigent"]["codemode"] is False
    assert written["plugins"] == [str(bridge_dir / "omnigent-policy")]
    assert written["instructions"] == [str(xdg / "opencode" / "AGENTS.md")]
    agents_md = (xdg / "opencode" / "AGENTS.md").read_text(encoding="utf-8")
    # Author text first, then the framework-owned instructions.
    assert agents_md.startswith("Agent rules.")
    assert "Embedded browser: the browser_navigate" in agents_md
    assert written["model"] == "anthropic/claude-sonnet-4-5"
    assert not {"provider", "permission", "plugin"} & set(written)
    assert launch_env["seeded"] == [bridge_dir]


async def test_launch_without_server_still_writes_ask_all_config(
    launch_env: dict[str, Any],
) -> None:
    bridge_dir = await _launch_until_boot()
    written = json.loads(
        (bridge_dir / "xdg-config" / "opencode" / "opencode.json").read_text(encoding="utf-8")
    )
    assert written["permissions"] == [{"action": "*", "resource": "*", "effect": "ask"}]
    assert "mcp" not in written
    assert "plugins" not in written


async def test_launch_adopts_managed_connect_providers_plugins_and_model(
    launch_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import omnigent.host.databricks_credential as databricks_credential

    ucode_plugin = str(tmp_path / "bridges" / "omnigent-ucode-auth")
    managed = {
        "providers": {"databricks-ws": {"api": {"id": "databricks-ws"}}},
        "plugins": [ucode_plugin],
        "model": "databricks-ws/served-model",
    }
    monkeypatch.setattr(provider, "managed_connect_opencode_config", lambda *a: managed)
    monkeypatch.setattr(
        databricks_credential, "_read_sidecar", lambda path: {"workspace_host": "https://ws"}
    )
    monkeypatch.setattr(databricks_credential, "broker_token_command", lambda host: "mint-token")

    bridge_dir = await _launch_until_boot(server_client=object())
    written = json.loads(
        (bridge_dir / "xdg-config" / "opencode" / "opencode.json").read_text(encoding="utf-8")
    )
    assert written["providers"] == managed["providers"]
    assert written["plugins"] == [ucode_plugin, str(bridge_dir / "omnigent-policy")]
    assert written["model"] == "databricks-ws/served-model"
    assert written["permissions"] == [{"action": "*", "resource": "*", "effect": "ask"}]


async def test_launch_connects_env_provider_keys_before_resolving_session(
    launch_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_server, "OpenCodeNativeServer", _FakeStartedServer)
    monkeypatch.setattr(bridge, "seeded_provider_ids", lambda bridge_dir: frozenset({"anthropic"}))

    calls: list[tuple[Any, frozenset[str]]] = []
    resolve_calls: list[str] = []

    async def _fake_connect_env_provider_keys(client: Any, *, stored: Any = ()) -> list[str]:
        calls.append((client, frozenset(stored)))
        return []

    async def _fake_resolve(**kwargs: Any) -> str:
        resolve_calls.append("reached")
        raise _StopAtSessionResolve

    monkeypatch.setattr(bridge, "connect_env_provider_keys", _fake_connect_env_provider_keys)
    monkeypatch.setattr(orchestration, "_resolve_opencode_session", _fake_resolve)

    with pytest.raises(_StopAtSessionResolve):
        await orchestration._auto_create_opencode_terminal(
            "conv_launch", cast(Any, object()), lambda *a: None
        )

    assert len(calls) == 1
    assert calls[0][1] == frozenset({"anthropic"})
    assert resolve_calls == ["reached"]


async def test_launch_survives_env_provider_key_hand_off_failure(
    launch_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_server, "OpenCodeNativeServer", _FakeStartedServer)
    monkeypatch.setattr(bridge, "seeded_provider_ids", lambda bridge_dir: frozenset())

    resolve_calls: list[str] = []

    async def _fake_connect_env_provider_keys(client: Any, *, stored: Any = ()) -> list[str]:
        raise RuntimeError("boom")

    async def _fake_resolve(**kwargs: Any) -> str:
        resolve_calls.append("reached")
        raise _StopAtSessionResolve

    monkeypatch.setattr(bridge, "connect_env_provider_keys", _fake_connect_env_provider_keys)
    monkeypatch.setattr(orchestration, "_resolve_opencode_session", _fake_resolve)

    with pytest.raises(_StopAtSessionResolve):
        await orchestration._auto_create_opencode_terminal(
            "conv_launch", cast(Any, object()), lambda *a: None
        )

    assert resolve_calls == ["reached"]
