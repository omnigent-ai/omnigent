"""Session profiles tests for Codex app server."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]
from omnigent.harnesses.codex_native.app_server import (
    _FRAMEWORK_APPROVED_TOOLS,
    _build_native_codex_app_server_argv,
    framework_approved_tools,
)
from omnigent.inner.codex_executor import (
    _provider_codex_config_overrides,
)
from tests.harnesses.codex_native.app_server._support import (
    _PLAIN_TOOL_APPROVALS,
    _disable_codex_startup_rpc,
    _set_codex_version,
    _test_app_server,
)

_ROUTED_TOOL_APPROVALS = {
    "sys_session_rename": {"approval_mode": "approve"},
    "sys_session_create": {"approval_mode": "approve"},
    "sys_agent_list": {"approval_mode": "approve"},
    "sys_session_send": {"approval_mode": "approve"},
    "sys_read_inbox": {"approval_mode": "approve"},
}


def test_the_framework_tool_approvals_are_scoped_to_the_session_class() -> None:
    assert set(framework_approved_tools(routed_spawns=False)) == set(_PLAIN_TOOL_APPROVALS)
    assert set(framework_approved_tools(routed_spawns=True)) == set(_ROUTED_TOOL_APPROVALS)
    # The base set is a subset of the routed one, so a routed session never
    # loses an approval a plain session has.
    assert set(_FRAMEWORK_APPROVED_TOOLS) <= set(_ROUTED_TOOL_APPROVALS)


# ── The codex-native session classes ────────────────────────────────
#
# Everything below is a per-class snapshot of the private CODEX_HOME a
# codex-native session boots on. A plain session must be indistinguishable from
# a pre-Smart-Routing one: codex's bundled model catalog (no ``codex debug
# models`` probe), no ``spawn_agent`` routing gate in hooks.json, and only the
# one framework tool approval.
#
# Every Smart Routing session — pinned harness or auto — adds the extended
# catalog, the spawn gate and the routed-spawn approvals, because on this arm
# the spawn tools are neither gated nor pre-approved without them and the spawn
# simply stalls on a prompt nobody is watching. The home is therefore the same
# shape for pinned and auto; what separates them is the cross-family framing in
# ``developer_instructions``, which the launch site adds for auto-harness only
# (``test_routed_spawn_note_appends_then_restores_the_user_base``).
#
# The catalog-only shape is still reachable: on a codex too old for the spawn
# gate the advertisement is dropped, and the session degrades to it.


def _stub_model_catalog_probe(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the ``codex debug models`` probe and record its calls."""
    from omnigent.inner import codex_executor

    probes: list[str] = []

    def _probe(codex_path: str, source_home: Path, *, timeout: float) -> dict[str, Any]:
        del source_home, timeout
        probes.append(codex_path)
        return {
            "models": [
                {"slug": "gpt-5.6-luna", "visibility": "list", "supported_reasoning_levels": []}
            ]
        }

    monkeypatch.setattr(codex_executor, "_find_codex_cli", lambda: "/bin/codex")
    monkeypatch.setattr(codex_executor, "_MODEL_CATALOG_CACHE", {})
    monkeypatch.setattr(codex_executor, "_MODEL_CATALOG_FAILURES", {})
    monkeypatch.setattr(codex_executor, "_probe_codex_model_catalog", _probe)
    return probes


async def _start_codex_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    env: dict[str, str],
) -> tuple[Path, list[str]]:
    """Boot an app-server with *env* and return its home plus probe calls."""
    real_codex_home = tmp_path / "real-codex-home"
    real_codex_home.mkdir()
    (real_codex_home / "config.toml").write_text('model = "gpt-5.5"\n', encoding="utf-8")
    (real_codex_home / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [{"hooks": [{"type": "command", "command": "user-pre"}]}],
                    "Stop": [{"hooks": [{"type": "command", "command": "user-stop"}]}],
                }
            }
        ),
        encoding="utf-8",
    )
    codex_home = tmp_path / "codex-home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(real_codex_home))
    _disable_codex_startup_rpc(monkeypatch)
    probes = _stub_model_catalog_probe(monkeypatch)

    server = _test_app_server(tmp_path, codex_home, tmp_path / "bridge", workspace, env)
    await server.start()
    await server.close()
    return codex_home, probes


#: The regex the routing gate is registered under (codex flattens the tool name).
_SPAWN_MATCHER = r".*spawn_agent"


def _mcp_tool_approvals(codex_home: Path) -> dict[str, Any]:
    parsed = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    return parsed["mcp_servers"]["omnigent"]["tools"]


def _hook_matchers(codex_home: Path, event: str) -> list[str | None]:
    payload = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
    return [entry.get("matcher") for entry in payload["hooks"].get(event, [])]


async def test_a_plain_codex_native_session_looks_like_a_plain_codex_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home, probes = await _start_codex_home(tmp_path, monkeypatch, env={})

    assert probes == []
    assert not (codex_home / "model_catalog.json").exists()
    assert "model_catalog_json" not in (codex_home / "config.toml").read_text(encoding="utf-8")
    assert _mcp_tool_approvals(codex_home) == _PLAIN_TOOL_APPROVALS
    # The policy gate and the user's own hooks are a plain codex session's
    # pre-existing PreToolUse entries; what it must not gain is a gate on the
    # spawn tool, which stalls ~30 s on a wedged server before failing open.
    assert _SPAWN_MATCHER not in _hook_matchers(codex_home, "PreToolUse")


async def test_a_smart_routing_codex_native_session_gains_the_spawn_apparatus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pinned and auto-harness alike: the routed spawn has to be able to run.

    The pinned class used to be withheld the advertisement, which took the
    ``spawn_agent`` gate AND the four routed-spawn approvals with it — so a
    pinned Smart Routing session's spawns did not merely go unrouted, they
    stalled on an approval prompt nobody was watching.
    """
    from omnigent.inner.codex_executor import (
        CODEX_EXTENDED_CATALOG_ENV_VAR,
        CODEX_ROUTER_DIR_ENV_VAR,
        CODEX_ROUTER_SESSION_ID_ENV_VAR,
    )

    router_dir = tmp_path / "router"
    router_dir.mkdir()
    codex_home, probes = await _start_codex_home(
        tmp_path,
        monkeypatch,
        env={
            CODEX_EXTENDED_CATALOG_ENV_VAR: "1",
            CODEX_ROUTER_DIR_ENV_VAR: str(router_dir),
            CODEX_ROUTER_SESSION_ID_ENV_VAR: "conv_abc",
        },
    )

    assert probes == ["/bin/codex"]
    assert (codex_home / "model_catalog.json").is_file()
    assert "model_catalog_json" in (codex_home / "config.toml").read_text(encoding="utf-8")
    assert _mcp_tool_approvals(codex_home) == _ROUTED_TOOL_APPROVALS
    # Omnigent's policy hook stays first, then the spawn gate, then user hooks.
    assert _hook_matchers(codex_home, "PreToolUse") == [None, _SPAWN_MATCHER, None]


async def test_an_old_codex_degrades_a_routed_session_to_catalog_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Below the spawn gate's CLI floor the session launches, unrouted.

    The advertisement is dropped, so everything keyed off it falls back to the
    plain shape — no gate, no routed-spawn approvals — while the extended
    catalog (keyed off its own env var) stays. The gear still offers the
    subagent-routing row; the choice simply no-ops until codex is upgraded.
    """
    from omnigent.inner.codex_executor import (
        CODEX_EXTENDED_CATALOG_ENV_VAR,
        CODEX_ROUTER_DIR_ENV_VAR,
        CODEX_ROUTER_SESSION_ID_ENV_VAR,
    )

    router_dir = tmp_path / "router"
    router_dir.mkdir()
    _set_codex_version(monkeypatch, (0, 144, 9))
    codex_home, probes = await _start_codex_home(
        tmp_path,
        monkeypatch,
        env={
            CODEX_EXTENDED_CATALOG_ENV_VAR: "1",
            CODEX_ROUTER_DIR_ENV_VAR: str(router_dir),
            CODEX_ROUTER_SESSION_ID_ENV_VAR: "conv_abc",
        },
    )

    assert probes == ["/bin/codex"]
    assert (codex_home / "model_catalog.json").is_file()
    assert _mcp_tool_approvals(codex_home) == _PLAIN_TOOL_APPROVALS
    assert _SPAWN_MATCHER not in _hook_matchers(codex_home, "PreToolUse")


async def test_native_codex_materializes_provider_auth_for_app_server_and_tui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native app-server and remote TUI argv contain no provider secret."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    (source_home / "config.toml").write_text(
        'model_providers = { existing = { name = "Existing", '
        'base_url = "https://existing.invalid/v1", wire_api = "responses" } }\n',
        encoding="utf-8",
    )
    codex_home = tmp_path / "codex-home"
    bridge_dir = tmp_path / "bridge"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    _disable_codex_startup_rpc(monkeypatch)

    server = _test_app_server(tmp_path, codex_home, bridge_dir, workspace)
    server.config_overrides = [
        *_provider_codex_config_overrides(
            model="test-model",
            base_url="https://provider.invalid/v1",
            auth_command="credential-helper --token sk-sentinel-do-not-use",
            wire_api="responses",
        ),
        'approval_policy="never"',
        'sandbox_mode="danger-full-access"',
    ]
    await server.start()
    await server.close()

    app_server_argv = _build_native_codex_app_server_argv(
        tagged_argv0="codex session-tag",
        listen_url="ws://127.0.0.1:9876",
        config_overrides=server.config_overrides,
    )
    remote_argv = codex_native_app_server.build_codex_remote_args(
        codex_args=(),
        thread_id=None,
        remote_url="ws://127.0.0.1:9876",
        config_overrides=tuple(server.config_overrides),
    )
    assert all("sk-sentinel-do-not-use" not in arg for arg in app_server_argv)
    assert all("sk-sentinel-do-not-use" not in arg for arg in remote_argv)
    assert 'model_provider="omnigent_provider"' in app_server_argv
    assert 'model_provider="omnigent_provider"' in remote_argv
    assert 'approval_policy="never"' in app_server_argv
    assert 'sandbox_mode="danger-full-access"' in app_server_argv

    config_path = codex_home / "config.toml"
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    provider = config["model_providers"]["omnigent_provider"]
    assert config["model_providers"]["existing"]["name"] == "Existing"
    assert provider["base_url"] == "https://provider.invalid/v1"
    assert provider["auth"]["args"] == [
        "-c",
        "credential-helper --token sk-sentinel-do-not-use",
    ]
    assert provider["wire_api"] == "responses"
    assert stat.S_IMODE(codex_home.stat().st_mode) == 0o700
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_remote_codex_rejects_unmaterialized_provider_config() -> None:
    """Remote TUI construction fails closed on provider table overrides."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    provider_override = _provider_codex_config_overrides(
        model=None,
        base_url="https://provider.invalid/v1",
        auth_command="printf %s sk-sentinel-do-not-use",
        wire_api="responses",
    )[-1]

    with pytest.raises(ValueError, match="must be materialized"):
        codex_native_app_server.build_codex_remote_args(
            codex_args=(),
            thread_id=None,
            remote_url="ws://127.0.0.1:9876",
            config_overrides=(provider_override,),
        )
