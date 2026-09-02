"""Tests for codex-native app-server policy-hook trust handling."""

from __future__ import annotations

import json
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

from omnigent.codex_native_app_server import (
    _FRAMEWORK_APPROVED_TOOLS,
    _POLICY_HOOK_TIMEOUT_SECONDS,
    CodexNativeAppServer,
    _build_native_codex_app_server_argv,
    _codex_policy_hooks_settings,
    _hooks_list_diagnostics,
    _model_discovery_cache,
    _our_policy_hooks_from_list,
    _sync_codex_developer_instructions,
    build_codex_native_server,
    discover_codex_model_options,
    framework_approved_tools,
    trust_all_codex_hooks,
    trust_codex_router_hooks,
    trust_native_policy_hooks,
)
from omnigent.codex_native_hook import _EVALUATE_POLICY_TIMEOUT_S
from omnigent.inner.codex_executor import (
    _populate_codex_home_config,
    _provider_codex_config_overrides,
)


async def test_discover_codex_model_options_strips_secrets_and_stops_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-launch discovery uses an empty home, no credentials, and clean teardown."""
    from omnigent import codex_native_app_server

    captured_env: dict[str, str] = {}

    class _FakeProcess:
        pid = None
        returncode: int | None = None
        terminated = False

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -1

        async def wait(self) -> int:
            self.returncode = 0 if self.returncode is None else self.returncode
            return self.returncode

    process = _FakeProcess()

    async def _fake_start(
        *,
        codex_path: str,
        listen_url: str,
        env: dict[str, str],
        cwd: Path,
    ) -> _FakeProcess:
        assert codex_path == "/test/codex"
        assert listen_url.startswith("ws://127.0.0.1:")
        assert cwd.is_dir()
        assert Path(env["CODEX_HOME"]).is_dir()
        captured_env.update(env)
        return process

    async def _fake_wait(process: _FakeProcess, port: int) -> None:
        assert process is not None
        assert port > 0

    class _FakeClient:
        def __init__(self, *, ws_url: str, client_name: str) -> None:
            assert ws_url.startswith("ws://127.0.0.1:")
            assert client_name == "omnigent-codex-model-discovery"

        async def connect(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def request(
            self,
            method: str,
            params: dict[str, object],
        ) -> dict[str, object]:
            assert method == "model/list"
            assert params == {"includeHidden": False}
            return {
                "result": {
                    "data": [
                        {
                            "id": "coding-model",
                            "model": "coding-model",
                            "isDefault": True,
                        }
                    ],
                    "nextCursor": None,
                }
            }

    monkeypatch.setattr(
        codex_native_app_server,
        "_clean_codex_env",
        lambda: {
            "PATH": "/bin",
            "OPENAI_API_KEY": "openai-secret",
            "OPENAI_BASE_URL": "https://example.invalid/v1",
            "DATABRICKS_BEARER": "databricks-secret",
            "DATABRICKS_CODEX_TOKEN": "databricks-secret",
        },
    )
    monkeypatch.setattr(
        codex_native_app_server,
        "_start_codex_model_discovery_process",
        _fake_start,
    )
    monkeypatch.setattr(codex_native_app_server, "_wait_for_discovery_listener", _fake_wait)
    monkeypatch.setattr(codex_native_app_server, "CodexAppServerClient", _FakeClient)
    _model_discovery_cache.clear()

    options = await discover_codex_model_options(codex_path="/test/codex")

    assert options == [{"id": "coding-model", "model": "coding-model", "isDefault": True}]
    assert captured_env == {"PATH": "/bin", "CODEX_HOME": captured_env["CODEX_HOME"]}
    assert process.terminated is True
    _model_discovery_cache.clear()


# Spelled out per session class rather than derived from the constants under
# test: a comprehension over ``_FRAMEWORK_APPROVED_TOOLS`` passes no matter what
# is added to it, so it can never catch the approval surface growing.
#
# A plain codex session pre-approves exactly the one tool the framework calls
# unprompted on any session. Any Smart Routing session — pinned harness or auto
# — additionally pre-approves the four its routed spawns run on: discover the
# agent, start the routed child, deliver the task, collect the result. Nobody is
# watching for an approval prompt in the middle of a spawn.
_PLAIN_TOOL_APPROVALS = {"sys_session_rename": {"approval_mode": "approve"}}
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


def test_sync_developer_instructions_preserves_and_restores_user_config(tmp_path: Path) -> None:
    """Framework instructions append without replacing the user's Codex guidance."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text(
        'model = "gpt-5.5"\ndeveloper_instructions = "Keep user guidance."\n',
        encoding="utf-8",
    )

    _sync_codex_developer_instructions(codex_home, "Rename the session.")
    _sync_codex_developer_instructions(codex_home, "Rename the session.")

    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert config["model"] == "gpt-5.5"
    assert config["developer_instructions"] == ("Keep user guidance.\n\nRename the session.")

    _sync_codex_developer_instructions(codex_home, None)

    resumed_config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert resumed_config["developer_instructions"] == "Keep user guidance."


def test_sync_developer_instructions_survives_reseeded_config(tmp_path: Path) -> None:
    """A persisted sidecar restores the original base after config reseeding."""
    codex_home = tmp_path / "codex-home"
    source_home = tmp_path / "source-home"
    codex_home.mkdir()
    source_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text(
        'developer_instructions = "Keep original guidance."\n',
        encoding="utf-8",
    )
    (source_home / "config.toml").write_text(
        'developer_instructions = "New shared guidance."\n',
        encoding="utf-8",
    )

    _sync_codex_developer_instructions(codex_home, "Rename the session.")
    config_path.unlink()
    _populate_codex_home_config(codex_home, source_home)

    reseeded = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert reseeded["developer_instructions"] == "New shared guidance."

    _sync_codex_developer_instructions(codex_home, None)

    resumed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert resumed["developer_instructions"] == "Keep original guidance."


def test_sync_developer_instructions_recovers_legacy_augmented_config(tmp_path: Path) -> None:
    """A missing sidecar does not capture an existing framework suffix as user base."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text(
        'developer_instructions = "Keep user guidance.\\n\\nRename the session."\n',
        encoding="utf-8",
    )

    _sync_codex_developer_instructions(codex_home, "Rename the session.")

    active = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert active["developer_instructions"] == "Keep user guidance.\n\nRename the session."

    _sync_codex_developer_instructions(codex_home, None)

    resumed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert resumed["developer_instructions"] == "Keep user guidance."


def test_sync_developer_instructions_skips_invalid_config(tmp_path: Path) -> None:
    """Optional title metadata never blocks Codex startup on malformed config."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text("invalid = [", encoding="utf-8")

    _sync_codex_developer_instructions(codex_home, "Rename the session.")

    assert config_path.read_text(encoding="utf-8") == "invalid = ["


_CWD = "/home/user/repo"
_OUR_COMMAND = "/venv/bin/python -m omnigent.codex_native_hook evaluate-policy --bridge-dir /b"
_USER_COMMAND = "bash /home/user/.config/llm-cli/hooks/guard.sh"


def _hook(key: str, command: str, trust: str, current_hash: str = "sha256:h") -> dict[str, Any]:
    """
    Build a ``hooks/list`` hook metadata entry.

    :param key: Hook key, e.g. ``"/b/codex-home/hooks.json:pre_tool_use:0:0"``.
    :param command: Hook command string (used to identify ownership).
    :param trust: Trust status, e.g. ``"untrusted"`` / ``"trusted"``.
    :param current_hash: The hook's content hash, e.g. ``"sha256:h"``.
    :returns: A hook metadata dict shaped like ``hooks/list`` output.
    """
    return {
        "key": key,
        "command": command,
        "trustStatus": trust,
        "currentHash": current_hash,
    }


def test_hooks_list_empty_result_does_not_fall_back_to_envelope() -> None:
    """A valid empty result remains authoritative over envelope metadata."""
    listed = {
        "result": {},
        "data": [{"cwd": _CWD, "hooks": [_hook("k1", _OUR_COMMAND, "trusted")]}],
    }

    assert _our_policy_hooks_from_list(listed, _CWD) == []
    assert "returned no hooks" in _hooks_list_diagnostics(listed, _CWD)


@dataclass
class _Req:
    """
    One recorded JSON-RPC request issued to the fake client.

    :param method: RPC method name, e.g. ``"hooks/list"``.
    :param params: RPC params dict.
    """

    method: str
    params: dict[str, Any]


@dataclass
class _FakeCodexClient:
    """
    Fake Codex app-server client scripted for the trust flow.

    Returns the current hook set for ``hooks/list`` and, on
    ``config/batchWrite``, flips a hook to ``trusted`` when the written
    ``trusted_hash`` matches the hook's ``currentHash`` (mirroring codex's
    real trust evaluation). ``flip_on_trust=False`` simulates a hash
    mismatch where trust never takes.

    :param hooks: Initial hook metadata (mutated as trust is written).
    :param flip_on_trust: Whether a matching trusted_hash flips trust.
    """

    hooks: list[dict[str, Any]]
    flip_on_trust: bool = True
    requests: list[_Req] = field(default_factory=list)

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Handle one scripted RPC request.

        :param method: RPC method, e.g. ``"hooks/list"`` or
            ``"config/batchWrite"``.
        :param params: RPC params.
        :returns: A response envelope matching the real app-server shape.
        """
        self.requests.append(_Req(method=method, params=params))
        if method == "hooks/list":
            return {"result": {"data": [{"cwd": _CWD, "hooks": self.hooks}]}}
        if method == "config/batchWrite":
            if self.flip_on_trust:
                written = params["edits"][0]["value"]
                for hook in self.hooks:
                    update = written.get(hook["key"])
                    if update and update.get("trusted_hash") == hook["currentHash"]:
                        hook["trustStatus"] = "trusted"
            return {"result": {"status": "ok"}}
        raise AssertionError(f"unexpected RPC method {method!r}")


def _batchwrite_calls(client: _FakeCodexClient) -> list[_Req]:
    """
    Return the config/batchWrite requests the trust flow issued.

    :param client: The fake client after the flow ran.
    :returns: Recorded batchWrite requests (empty if none issued).
    """
    return [r for r in client.requests if r.method == "config/batchWrite"]


async def _fake_wait_until_ready(self: CodexNativeAppServer) -> None:
    """
    Skip app-server socket probing in startup unit tests.

    :param self: The app-server wrapper under test.
    :returns: None.
    """


async def _fake_trust_policy_hooks(self: CodexNativeAppServer) -> None:
    """
    Skip Codex ``hooks/list`` RPCs in startup unit tests.

    :param self: The app-server wrapper under test.
    :returns: None.
    """


def _disable_codex_startup_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Patch Codex startup RPC waits for unit tests.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.setattr(CodexNativeAppServer, "_wait_until_ready", _fake_wait_until_ready)
    monkeypatch.setattr(CodexNativeAppServer, "_trust_policy_hooks", _fake_trust_policy_hooks)


def test_build_codex_native_server_profile_error_names_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Missing Databricks profile errors identify the runner-visible profile.

    The native Codex terminal can fail before the TUI launches if the
    runner process cannot resolve the Databricks profile it was given.
    The message must include that profile name so operators can tell a
    stale/missing runner env apart from a generic Codex startup failure.
    """
    monkeypatch.setattr(
        "omnigent.codex_native_app_server._find_codex_cli",
        lambda: sys.executable,
    )
    monkeypatch.setattr(
        "omnigent.codex_native_app_server._databricks_gateway_host",
        lambda _profile: None,
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(tmp_path / "missing-databrickscfg"))

    with pytest.raises(OSError, match="profile 'oss'"):
        build_codex_native_server(
            socket_path=tmp_path / "codex.sock",
            codex_home=tmp_path / "codex-home",
            cwd=tmp_path,
            model=None,
            profile="oss",
            bridge_dir=tmp_path / "bridge",
            ap_server_url=None,
            ap_auth_headers={},
        )


def test_build_codex_native_server_uses_profile_host_without_static_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Native Codex accepts Databricks CLI OAuth profiles without static tokens.

    A default Omnigent install may not include ``databricks-sdk`` in the
    runner process. In that case a bearer cannot be minted at startup, but the
    profile's host is still enough: Codex gets an ``auth.command`` that runs
    ``databricks auth token --profile`` at request time.
    """
    monkeypatch.setattr(
        "omnigent.codex_native_app_server._find_codex_cli",
        lambda: sys.executable,
    )
    cfg_path = tmp_path / "databrickscfg"
    cfg_path.write_text(
        "\n".join(
            [
                "[oss]",
                "host = https://example.cloud.databricks.com",
                "auth_type = databricks-cli",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg_path))

    # This test exercises the profile-host base URL + auth command, not model
    # resolution. Force live codex discovery offline so the build makes no
    # model-services network call for the profile host; the explicit
    # ``model="test-model"`` is then used as-is.
    def _discovery_offline(_profile: str | None) -> object:
        raise RuntimeError("model discovery is offline in this test")

    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
        _discovery_offline,
    )

    app_server = build_codex_native_server(
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        cwd=tmp_path,
        model="test-model",
        profile="oss",
        bridge_dir=tmp_path / "bridge",
        ap_server_url=None,
        ap_auth_headers={},
    )

    overrides = "\n".join(app_server.config_overrides)
    assert "https://example.cloud.databricks.com/ai-gateway/codex/v1" in overrides
    assert 'databricks auth token --profile \\"oss\\"' in overrides


def test_build_codex_native_server_without_bypass_emits_no_bypass_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The default (``bypass_sandbox=False``) writes no approval/sandbox overrides.

    Guards the safe default: an app-server built without the opt-in must
    leave Codex's normal approval-prompt + own-sandbox stance untouched, so
    no ``approval_policy`` / ``sandbox_mode`` override leaks in. A regression
    that always emitted them would silently disable the sandbox for every
    native Codex session.
    """
    monkeypatch.setattr(
        "omnigent.codex_native_app_server._find_codex_cli",
        lambda: sys.executable,
    )
    app_server = build_codex_native_server(
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        cwd=tmp_path,
        model=None,
        profile=None,
        bridge_dir=tmp_path / "bridge",
        ap_server_url=None,
        ap_auth_headers={},
    )

    overrides = "\n".join(app_server.config_overrides)
    assert "approval_policy" not in overrides
    assert "sandbox_mode" not in overrides


def test_build_codex_native_server_bypass_emits_full_access_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``bypass_sandbox=True`` puts the app-server threads into the bypass stance.

    The ``--remote`` TUI launched with
    ``--dangerously-bypass-approvals-and-sandbox`` fixes the thread's
    approval/sandbox stance, but the chat/forwarder seam drives the SAME
    thread through the app-server, so the app-server config must match —
    ``approval_policy="never"`` (no prompts a headless seam can't answer)
    and ``sandbox_mode="danger-full-access"`` (commands run with no command
    sandbox, the #657 ask). Without these the app-server-driven turns would
    keep prompting / keep the sandbox even though the TUI bypassed it.
    """
    monkeypatch.setattr(
        "omnigent.codex_native_app_server._find_codex_cli",
        lambda: sys.executable,
    )
    app_server = build_codex_native_server(
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        cwd=tmp_path,
        model=None,
        profile=None,
        bridge_dir=tmp_path / "bridge",
        ap_server_url=None,
        ap_auth_headers={},
        bypass_sandbox=True,
    )

    assert 'approval_policy="never"' in app_server.config_overrides
    assert 'sandbox_mode="danger-full-access"' in app_server.config_overrides


@pytest.mark.parametrize(
    ("model", "expected_pin"),
    [
        pytest.param(None, "gpt-5.4-mini", id="default-launch"),
        pytest.param("gpt-5.5", "gpt-5.5", id="explicit-pick"),
    ],
)
def test_build_codex_native_server_pins_profile_resolved_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str | None,
    expected_pin: str,
) -> None:
    """
    A profile launch pins the model it routes to, in codex's own spelling.

    A launch naming no model still gets a concrete model from the profile's
    catalog via ``-c model=``, which outranks the ``config.toml`` copied from
    the user's shared home. Leaving ``pinned_model`` unset there let the
    forwarder mirror the shared file's stale model back as this session's
    ``model_override`` (live-caught: a session running
    ``databricks-gpt-5-6-luna`` reported the shared file's ``gpt-5.4``).
    The pin uses codex's spelling because that is the vocabulary
    ``config.toml`` and every reader of it — including the web catalog's
    row ids — compare in.
    """
    from omnigent import codex_native_app_server

    monkeypatch.setattr(
        "omnigent.codex_native_app_server._find_codex_cli",
        lambda: sys.executable,
    )
    monkeypatch.setattr(
        codex_native_app_server,
        "_databricks_launch_materialization",
        lambda *, model, profile: codex_native_app_server._DatabricksLaunchMaterialization(
            config_overrides=[f'model="{model or "databricks-gpt-5-4-mini"}"'],
            model=model or "databricks-gpt-5-4-mini",
            host="https://ws.example",
        ),
    )

    app_server = build_codex_native_server(
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        cwd=tmp_path,
        model=model,
        profile="oss",
        bridge_dir=tmp_path / "bridge",
        ap_server_url=None,
        ap_auth_headers={},
    )

    assert app_server.pinned_model == expected_pin


@pytest.mark.parametrize(
    ("model", "profile", "extra_overrides"),
    [
        # Subscription: an explicit pick and a catalog-resolved Default.
        ("gpt-5.5", None, ['model_provider="openai"']),
        ("gpt-5.6-terra", None, ['model_provider="openai"']),
        # cli-config: the user's own provider table rides the config copy.
        ("gpt-5.5", None, ['model_provider="Databricks"']),
        # Databricks profile: Default and an explicit pick.
        (None, "oss", None),
        ("databricks-gpt-5-5", "oss", None),
    ],
)
def test_launch_argv_and_config_pin_name_the_same_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str | None,
    profile: str | None,
    extra_overrides: list[str] | None,
) -> None:
    """
    The argv ``-c model=`` and the config copy's pin never drift apart.

    Both artifacts are written from one resolved value, on EVERY provider
    shape and for Default and explicit picks alike — the structural end of
    the stale-config-line class where a copied ``model =`` governed a
    session the argv never named. On the profile shape the file
    deliberately holds codex's own spelling (the vocabulary its readers
    compare in) while argv carries the wire spelling; the guard asserts
    they name the same model, and byte-identity everywhere else.
    """
    from omnigent import codex_native_app_server
    from omnigent.codex_model_vocabulary import comparable_model_id

    monkeypatch.setattr(
        "omnigent.codex_native_app_server._find_codex_cli",
        lambda: sys.executable,
    )
    monkeypatch.setattr(
        codex_native_app_server,
        "_databricks_launch_materialization",
        lambda *, model, profile: codex_native_app_server._DatabricksLaunchMaterialization(
            config_overrides=[f'model="{model or "databricks-gpt-5-6-luna"}"'],
            model=model or "databricks-gpt-5-6-luna",
            host="https://ws.example",
        ),
    )

    app_server = build_codex_native_server(
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "codex-home",
        cwd=tmp_path,
        model=model,
        profile=profile,
        bridge_dir=tmp_path / "bridge",
        ap_server_url=None,
        ap_auth_headers={},
        extra_config_overrides=list(extra_overrides) if extra_overrides else None,
    )

    argv_models = [
        override.split("=", 1)[1]
        for override in app_server.config_overrides
        if override.split("=", 1)[0] == "model"
    ]
    assert len(argv_models) == 1, app_server.config_overrides
    argv_model = json.loads(argv_models[0]) if argv_models[0].startswith('"') else argv_models[0]
    pinned = app_server.pinned_model
    assert pinned, "every launch must pin a model"
    assert comparable_model_id(argv_model) == comparable_model_id(pinned)
    if profile is None:
        assert argv_model == pinned


async def test_codex_launch_catalog_reads_the_store_then_probes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The launch catalog is store-first; a miss probes once and persists."""
    from omnigent import codex_native_app_server

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        codex_native_app_server,
        "resolve_native_codex_launch",
        lambda *, model, spec=None: codex_native_app_server.NativeCodexLaunch(
            config_overrides=['model_provider="openai"'], model=model, profile=None
        ),
    )
    calls: list[int] = []

    async def _fake_probe(*, codex_path: str | None = None) -> list[dict[str, object]]:
        del codex_path
        calls.append(1)
        return [{"id": "gpt-5.6-terra", "model": "gpt-5.6-terra", "isDefault": True}]

    monkeypatch.setattr(codex_native_app_server, "probe_codex_model_options", _fake_probe)

    first = await codex_native_app_server.codex_launch_catalog()
    second = await codex_native_app_server.codex_launch_catalog()
    assert first == second
    assert first == [{"id": "gpt-5.6-terra", "model": "gpt-5.6-terra", "isDefault": True}]
    assert len(calls) == 1, "the second read must come from the store, not a re-probe"


async def test_codex_launch_catalog_is_stale_reads_the_default_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Stale only when the default shape's stored entry is past the TTL.
    """
    from omnigent import codex_native_app_server, model_catalog_store

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        codex_native_app_server,
        "resolve_native_codex_launch",
        lambda *, model, spec=None: codex_native_app_server.NativeCodexLaunch(
            config_overrides=[], model=model, profile=None
        ),
    )
    fingerprint = codex_native_app_server.codex_catalog_fingerprint(
        codex_native_app_server.resolve_native_codex_launch(model=None)
    )
    assert await codex_native_app_server.codex_launch_catalog_is_stale() is False
    model_catalog_store.write_catalog(
        "codex-native", fingerprint, [{"id": "gpt-5.6-terra", "isDefault": True}]
    )
    assert await codex_native_app_server.codex_launch_catalog_is_stale() is False
    path = model_catalog_store.catalog_path("codex-native", fingerprint)
    old = path.stat().st_mtime - (model_catalog_store.CATALOG_STALE_AFTER_S + 60)
    import os

    os.utime(path, (old, old))
    assert await codex_native_app_server.codex_launch_catalog_is_stale() is True


async def test_codex_launch_catalog_is_stale_unresolvable_launch_is_not_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A broken provider config means no catalog to distrust — never a crash.
    """
    from omnigent import codex_native_app_server

    def _boom(*, model: object, spec: object = None) -> object:
        raise RuntimeError("broken provider config")

    monkeypatch.setattr(codex_native_app_server, "resolve_native_codex_launch", _boom)
    assert await codex_native_app_server.codex_launch_catalog_is_stale() is False


# ── catalog fingerprint keys on the CLI binary ───────────


def _default_codex_launch() -> Any:
    """A bare ``model=None`` launch shape for fingerprinting."""
    from omnigent import codex_native_app_server

    return codex_native_app_server.NativeCodexLaunch(config_overrides=[], model=None, profile=None)


def test_codex_catalog_fingerprint_changes_when_the_cli_is_upgraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An upgraded Codex CLI misses the catalog its predecessor wrote.

    The catalog stores the model names one binary answered with. Without
    the binary in the key, an in-place upgrade keeps serving the old names
    until the entry ages out.
    """
    from omnigent import codex_native_app_server

    codex = tmp_path / "codex"
    codex.write_text("old build")
    codex.chmod(0o755)
    # Resolve through the same override ladder the probe launches with.
    monkeypatch.setenv("OMNIGENT_CODEX_PATH", str(codex))
    launch = _default_codex_launch()

    before = codex_native_app_server.codex_catalog_fingerprint(launch)

    codex.write_text("a longer, newer build")
    after = codex_native_app_server.codex_catalog_fingerprint(launch)

    assert before != after


def test_codex_catalog_fingerprint_is_stable_for_one_binary(tmp_path: Path) -> None:
    """An unchanged binary keeps its catalog, so no probe is repaid."""
    from omnigent import codex_native_app_server

    codex = tmp_path / "codex"
    codex.write_text("build")
    launch = _default_codex_launch()

    assert codex_native_app_server.codex_catalog_fingerprint(
        launch, codex_path=str(codex)
    ) == codex_native_app_server.codex_catalog_fingerprint(launch, codex_path=str(codex))


def test_codex_catalog_fingerprint_survives_a_missing_binary(tmp_path: Path) -> None:
    """A binary the resolver cannot find still yields a usable key."""
    from omnigent import codex_native_app_server

    fingerprint = codex_native_app_server.codex_catalog_fingerprint(
        _default_codex_launch(), codex_path=str(tmp_path / "absent")
    )

    assert isinstance(fingerprint, str) and fingerprint


def _test_app_server(
    tmp_path: Path,
    codex_home: Path,
    bridge_dir: Path,
    workspace: Path,
    env: dict[str, str] | None = None,
) -> CodexNativeAppServer:
    """
    Build a Codex app-server wrapper for startup unit tests.

    :param tmp_path: Test temp directory, e.g. ``Path("/tmp/test")``.
    :param codex_home: Private Codex home to write.
    :param bridge_dir: Bridge directory for the generated MCP args.
    :param workspace: Working directory for the subprocess.
    :param env: Process env for the app-server, carrying the routing
        signals the session class is read from. ``None`` is a plain session.
    :returns: Configured app-server wrapper.
    """
    return CodexNativeAppServer(
        codex_path=sys.executable,
        socket_path=tmp_path / "codex.sock",
        codex_home=codex_home,
        env=dict(env or {}),
        config_overrides=[],
        cwd=workspace,
        bridge_dir=bridge_dir,
        python_executable="/new/python",
    )


async def test_start_upserts_mcp_server_config_across_relaunches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Codex native startup upserts MCP config across relaunches.

    Repeated native terminal startup uses the same private ``CODEX_HOME``.
    The generated config must remain valid TOML and the user's real
    symlinked config must stay untouched.
    """
    real_codex_home = tmp_path / "real-codex-home"
    real_codex_home.mkdir()
    source_config = real_codex_home / "config.toml"
    original = """\
[projects."/repo"]
trust_level = "trusted"

[mcp_servers.omnigent] # stale generated table
command = "/old/python"
args = ["old"]

[mcp_servers.omnigent.env] # stale generated env
OLD = "1"

[mcp_servers.omnigent.tools.sys_session_rename] # stale generated approval
approval_mode = "prompt"

[mcp_servers.other]
command = "other"
args = []
"""
    source_config.write_text(original, encoding="utf-8")

    codex_home = tmp_path / "codex-home"
    bridge_dir = tmp_path / "bridge"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(real_codex_home))
    _disable_codex_startup_rpc(monkeypatch)

    server = _test_app_server(tmp_path, codex_home, bridge_dir, workspace)
    await server.start()
    await server.close()
    await server.start()
    await server.close()

    assert source_config.read_text(encoding="utf-8") == original
    config_path = codex_home / "config.toml"
    assert not config_path.is_symlink()
    rendered = config_path.read_text(encoding="utf-8")
    assert rendered.count("[mcp_servers.omnigent]") == 1
    assert "[mcp_servers.omnigent.env]" not in rendered
    parsed = tomllib.loads(rendered)
    assert parsed["mcp_servers"]["other"]["command"] == "other"
    assert parsed["mcp_servers"]["omnigent"] == {
        "command": "/new/python",
        "args": [
            "-I",
            "-m",
            "omnigent.claude_native_bridge",
            "serve-mcp",
            "--bridge-dir",
            str(bridge_dir),
        ],
        "tools": _PLAIN_TOOL_APPROVALS,
    }


async def test_start_writes_fresh_mcp_config_without_leading_blanks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Codex native startup writes fresh MCP config without leading blanks.

    Codex should be able to read a newly-created private ``config.toml``
    without cosmetic leading whitespace from the generated section
    separator logic.
    """
    real_codex_home = tmp_path / "real-codex-home"
    real_codex_home.mkdir()
    codex_home = tmp_path / "codex-home"
    bridge_dir = tmp_path / "bridge"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(real_codex_home))
    _disable_codex_startup_rpc(monkeypatch)

    server = _test_app_server(tmp_path, codex_home, bridge_dir, workspace)
    await server.start()
    await server.close()

    rendered = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert rendered.startswith("[mcp_servers.omnigent]\n")
    assert stat.S_IMODE(codex_home.stat().st_mode) == 0o700
    assert stat.S_IMODE((codex_home / "config.toml").stat().st_mode) == 0o600
    parsed = tomllib.loads(rendered)
    assert parsed["mcp_servers"]["omnigent"] == {
        "command": "/new/python",
        "args": [
            "-I",
            "-m",
            "omnigent.claude_native_bridge",
            "serve-mcp",
            "--bridge-dir",
            str(bridge_dir),
        ],
        "tools": _PLAIN_TOOL_APPROVALS,
    }


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
    from omnigent import codex_native_app_server

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
    from omnigent import codex_native_app_server

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


async def test_untrusted_hook_is_trusted_via_batchwrite() -> None:
    """
    An untrusted Omnigent hook is trusted with its currentHash.

    This is the core flow: list → write trusted_hash → verify trusted.
    It fails if the batchWrite omits our key, writes the wrong hash, or
    skips the re-verification (which would let a still-untrusted hook
    through, silently disabling enforcement).
    """
    client = _FakeCodexClient(hooks=[_hook("k1", _OUR_COMMAND, "untrusted", "sha256:abc")])
    await trust_native_policy_hooks(client, cwd=_CWD)

    writes = _batchwrite_calls(client)
    assert len(writes) == 1  # exactly one trust write issued
    edit = writes[0].params["edits"][0]
    assert edit["keyPath"] == "hooks.state"
    assert edit["mergeStrategy"] == "upsert"
    # The written trusted_hash must equal the hook's reported currentHash.
    assert edit["value"] == {"k1": {"trusted_hash": "sha256:abc"}}
    # reloadUserConfig is required so the running thread hot-reloads trust.
    assert writes[0].params["reloadUserConfig"] is True


async def test_already_trusted_hook_skips_batchwrite() -> None:
    """
    A hook already trusted issues no config write.

    Avoids a redundant config.toml write + reload on every session start.
    Fails if the flow writes trust unconditionally.
    """
    client = _FakeCodexClient(hooks=[_hook("k1", _OUR_COMMAND, "trusted")])
    await trust_native_policy_hooks(client, cwd=_CWD)
    assert _batchwrite_calls(client) == []  # nothing to trust → no write


def test_write_codex_policy_hooks_file_merges_user_hooks(tmp_path: Path) -> None:
    """User hooks symlinked into the private home are merged into hooks.json.

    _write_codex_policy_hooks_file replaces the symlink with a merged
    regular file containing both the Omnigent policy hooks and the user's
    hooks, so user hooks fire alongside policy enforcement.
    """
    from omnigent.codex_native_app_server import _write_codex_policy_hooks_file

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    # Simulate what _populate_codex_home_config does: symlink the user's hooks.json
    user_hooks = tmp_path / "user-hooks.json"
    user_hooks.write_text(
        '{"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}'
    )
    (codex_home / "hooks.json").symlink_to(user_hooks)

    _write_codex_policy_hooks_file(codex_home, bridge_dir, sys.executable)

    hooks_path = codex_home / "hooks.json"
    assert not hooks_path.is_symlink(), "symlink must be replaced by a regular file"
    payload = json.loads(hooks_path.read_text())
    hooks = payload["hooks"]
    # Policy hooks present
    assert "PreToolUse" in hooks
    assert "PostToolUse" in hooks
    assert "UserPromptSubmit" in hooks
    # User's SessionStart hook merged in
    assert "SessionStart" in hooks
    assert hooks["SessionStart"][0]["hooks"][0]["command"] == "echo hi"


def test_write_codex_policy_hooks_file_no_symlink_unchanged(tmp_path: Path) -> None:
    """Without a symlink, hooks.json is written with only policy hooks."""
    from omnigent.codex_native_app_server import _write_codex_policy_hooks_file

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    _write_codex_policy_hooks_file(codex_home, bridge_dir, sys.executable)

    payload = json.loads((codex_home / "hooks.json").read_text())
    assert set(payload["hooks"]) == {"PreToolUse", "PostToolUse", "UserPromptSubmit"}


def test_write_codex_policy_hooks_file_merges_router_hooks(tmp_path: Path) -> None:
    """Routing hooks share the one hooks.json codex loads, user hooks kept."""
    from omnigent.codex_native_app_server import _write_codex_policy_hooks_file

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    router_dir = tmp_path / "router"
    router_dir.mkdir()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    user_hooks = tmp_path / "user-hooks.json"
    user_hooks.write_text(
        '{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo bye"}]}]}}'
    )

    _write_codex_policy_hooks_file(
        codex_home,
        bridge_dir,
        sys.executable,
        router_bridge_dir=router_dir,
        router_session_id="conv_abc",
        user_hooks_source=user_hooks,
    )

    hooks = json.loads((codex_home / "hooks.json").read_text())["hooks"]
    commands = [h["command"] for entry in hooks["PreToolUse"] for h in entry["hooks"]]
    assert any("codex_router_hook" in c and "--session-id conv_abc" in c for c in commands)
    assert any("codex_policy_hook" in c or "policy" in c for c in commands)
    # The user's own hook survives alongside our route-subagent gate.
    assert hooks["Stop"][0]["hooks"][0]["command"] == "echo bye"


def test_user_prompt_submit_carries_the_route_turn_hook(tmp_path: Path) -> None:
    """First-message routing rides the UserPromptSubmit entry codex trusts.

    Pins the launch-path invariant a UI-created terminal session depends on:
    the ``route-turn`` command is registered, points at the SAME bridge dir
    the runner advertises ``turn_router.json`` in, and lives under the
    policy-hook module so the trust handshake covers it. A hook codex loads
    but never trusts is a silent fail-open, and one pointed at a different
    directory finds no advertisement and falls open too.
    """
    from omnigent.codex_native_app_server import (
        _POLICY_HOOK_MODULE,
        _our_policy_hooks_from_list,
        _write_codex_policy_hooks_file,
    )
    from omnigent.runner.turn_routing import HARNESS_HOOK_TIMEOUT_S

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    user_hooks = tmp_path / "user-hooks.json"
    user_hooks.write_text(
        '{"hooks": {"UserPromptSubmit": [{"hooks": '
        '[{"type": "command", "command": "echo mine"}]}]}}'
    )

    _write_codex_policy_hooks_file(
        codex_home,
        bridge_dir,
        sys.executable,
        user_hooks_source=user_hooks,
        turn_routing=True,
    )

    hooks = json.loads((codex_home / "hooks.json").read_text())["hooks"]
    commands = [h for entry in hooks["UserPromptSubmit"] for h in entry["hooks"]]
    routing = [h for h in commands if "route-turn" in h["command"]]
    assert len(routing) == 1
    assert f"--bridge-dir {bridge_dir}" in routing[0]["command"]
    assert "--harness codex-native" in routing[0]["command"]
    assert routing[0]["timeout"] == HARNESS_HOOK_TIMEOUT_S
    # Trust is filtered by module, so route-turn must ride the policy one.
    assert _POLICY_HOOK_MODULE in routing[0]["command"]
    listed = {"result": {"data": [{"cwd": "/repo", "hooks": commands}]}}
    assert len(_our_policy_hooks_from_list(listed, "/repo")) == 2
    # The user's own prompt hook survives the merge.
    assert any(h["command"] == "echo mine" for h in commands)


async def test_missing_hook_raises() -> None:
    """
    No discovered Omnigent hook fails loud (anti fail-open).

    If our hook was never registered/loaded, enforcement would silently
    not run. The flow must raise rather than return quietly. Fails if a
    missing hook is tolerated.
    """
    # Only a user-owned hook is present; ours is absent.
    client = _FakeCodexClient(hooks=[_hook("u1", _USER_COMMAND, "untrusted")])
    with pytest.raises(RuntimeError, match="not discovered"):
        await trust_native_policy_hooks(client, cwd=_CWD)
    # We must never touch a hook that isn't ours.
    assert _batchwrite_calls(client) == []


async def test_still_untrusted_after_write_raises() -> None:
    """
    A hook that stays untrusted after the write fails loud.

    Simulates a trust write that didn't take (e.g. hash mismatch). The
    flow must detect the still-untrusted state on re-list and raise, not
    proceed with a silently-skipped policy gate.
    """
    client = _FakeCodexClient(hooks=[_hook("k1", _OUR_COMMAND, "untrusted")], flip_on_trust=False)
    with pytest.raises(RuntimeError, match="still untrusted"):
        await trust_native_policy_hooks(client, cwd=_CWD)
    # The write was attempted before the failure was detected.
    assert len(_batchwrite_calls(client)) == 1


async def test_user_hooks_are_never_trusted() -> None:
    """
    Only Omnigent hooks are trusted; user-declared hooks are left alone.

    The private CODEX_HOME symlinks the user's config.toml, which may
    declare its own hooks. Auto-trusting those would be a security hole.
    Fails if the user's hook key appears in the trust write.
    """
    client = _FakeCodexClient(
        hooks=[
            _hook("ours", _OUR_COMMAND, "untrusted", "sha256:ours"),
            _hook("theirs", _USER_COMMAND, "untrusted", "sha256:theirs"),
        ]
    )
    await trust_native_policy_hooks(client, cwd=_CWD)
    writes = _batchwrite_calls(client)
    assert len(writes) == 1
    written = writes[0].params["edits"][0]["value"]
    # Only our key is trusted; the user's hook is never touched.
    assert written == {"ours": {"trusted_hash": "sha256:ours"}}


# --- Enriched, self-diagnosing trust/discovery errors -----------------


async def test_missing_hook_error_reports_zero_hooks_loaded() -> None:
    """
    Discovery failure with no hooks loaded names the likely cause.

    When codex loads zero hooks (the symptom of an invalid per-session
    config.toml → codex falls back to defaults), the "not discovered"
    error must say so, not just report the bare cwd. Fails if the
    diagnostic suffix is dropped, which is what made the original report
    impossible to triage.
    """
    client = _FakeCodexClient(hooks=[])
    with pytest.raises(RuntimeError, match="loaded none"):
        await trust_native_policy_hooks(client, cwd=_CWD)


async def test_missing_hook_error_reports_module_mismatch() -> None:
    """
    Discovery failure with only foreign hooks reports "0 ours".

    If codex listed hooks for the cwd but none are ours (e.g. a stale /
    renamed hook command from an out-of-date install), the error must
    distinguish that from "no hooks at all". Fails if the per-entry
    ownership count is not surfaced.
    """
    client = _FakeCodexClient(hooks=[_hook("u1", _USER_COMMAND, "untrusted")])
    with pytest.raises(RuntimeError, match="0 ours"):
        await trust_native_policy_hooks(client, cwd=_CWD)


async def test_still_untrusted_error_includes_status_message() -> None:
    """
    A hook that stays untrusted surfaces codex's own statusMessage.

    Codex reports *why* a hook cannot be trusted in ``statusMessage``
    (e.g. a managed-hooks requirement rejecting a user hook). The trust
    handshake otherwise discards it; the error must carry it through so
    the cause is visible. Fails if statusMessage is not included.
    """
    hook = {
        "key": "k1",
        "command": _OUR_COMMAND,
        "trustStatus": "untrusted",
        "currentHash": "sha256:abc",
        "isManaged": False,
        "statusMessage": "managed hooks only",
    }
    client = _FakeCodexClient(hooks=[hook], flip_on_trust=False)
    with pytest.raises(RuntimeError, match="managed hooks only"):
        await trust_native_policy_hooks(client, cwd=_CWD)


async def test_still_untrusted_hints_old_codex_when_hash_missing() -> None:
    """
    Missing currentHash/trustStatus points at an old codex version.

    codex < 0.129 omits ``currentHash``/``trustStatus`` from
    ``hooks/list``; the trust write then writes nothing and the hook
    stays untrusted. The error must name the version cause rather than
    the misleading bare "still untrusted". Fails if the version hint is
    absent when the protocol fields are missing.
    """
    hook = {"key": "k1", "command": _OUR_COMMAND, "trustStatus": None, "currentHash": None}
    client = _FakeCodexClient(hooks=[hook])
    with pytest.raises(RuntimeError, match=r"older than 0\.129\.0"):
        await trust_native_policy_hooks(client, cwd=_CWD)


# --- Codex version gate + fail-open startup ---------------------------


def _set_codex_version(
    monkeypatch: pytest.MonkeyPatch, version: tuple[int, int, int] | None
) -> None:
    """
    Stub the codex version probe used by :meth:`start`.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param version: Version tuple to report, e.g. ``(0, 128, 0)``, or
        ``None`` to simulate an unparseable ``codex --version``.
    :returns: None.
    """

    async def _fake_version(_codex_path: str) -> tuple[int, int, int] | None:
        return version

    monkeypatch.setattr("omnigent.codex_native_app_server._codex_cli_version", _fake_version)


async def test_old_codex_skips_policy_hook_and_records_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    codex < 0.129 starts without registering the policy hook.

    The session must NOT be blocked (fail-open): start() returns, no
    hooks.json is written (codex could never trust it), and the reason is
    recorded for the web-UI notice. Fails if startup raises (the old
    blocking behavior) or if the hook is registered against an
    un-trustable codex.
    """
    real_codex_home = tmp_path / "real-codex-home"
    real_codex_home.mkdir()
    codex_home = tmp_path / "codex-home"
    bridge_dir = tmp_path / "bridge"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(real_codex_home))
    monkeypatch.setattr(CodexNativeAppServer, "_wait_until_ready", _fake_wait_until_ready)
    _set_codex_version(monkeypatch, (0, 128, 0))

    server = _test_app_server(tmp_path, codex_home, bridge_dir, workspace)
    # ap_server_url present → enforcement was intended → this is the
    # security-relevant degrade path.
    server.ap_server_url = "http://127.0.0.1:9999"
    await server.start()
    try:
        # Hook was NOT registered: codex < 0.129 can never trust it.
        assert not (codex_home / "hooks.json").exists()
        # Reason is recorded so the caller can surface a web-UI notice.
        assert server.policy_hook_disabled_reason is not None
        assert "0.128.0" in server.policy_hook_disabled_reason
        assert "0.129.0" in server.policy_hook_disabled_reason
    finally:
        await server.close()


async def test_supported_codex_registers_hook_and_enforces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    codex >= 0.129 registers the hook and reports enforcement active.

    Fails if the version gate wrongly disables a supported codex (which
    would silently drop enforcement on every modern session).
    """
    real_codex_home = tmp_path / "real-codex-home"
    real_codex_home.mkdir()
    codex_home = tmp_path / "codex-home"
    bridge_dir = tmp_path / "bridge"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(real_codex_home))
    _disable_codex_startup_rpc(monkeypatch)
    _set_codex_version(monkeypatch, (0, 129, 0))

    server = _test_app_server(tmp_path, codex_home, bridge_dir, workspace)
    await server.start()
    try:
        # Hook registered for a supported codex.
        assert (codex_home / "hooks.json").exists()
        # None == enforcement active (no degrade reason).
        assert server.policy_hook_disabled_reason is None
    finally:
        await server.close()


async def test_unknown_codex_version_treated_as_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An unparseable codex version does not disable enforcement.

    A flaky/odd ``codex --version`` must not silently drop policy
    enforcement — we proceed to register + trust (a real trust failure is
    then caught separately). Fails if ``None`` is treated as "too old".
    """
    real_codex_home = tmp_path / "real-codex-home"
    real_codex_home.mkdir()
    codex_home = tmp_path / "codex-home"
    bridge_dir = tmp_path / "bridge"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(real_codex_home))
    _disable_codex_startup_rpc(monkeypatch)
    _set_codex_version(monkeypatch, None)

    server = _test_app_server(tmp_path, codex_home, bridge_dir, workspace)
    await server.start()
    try:
        assert (codex_home / "hooks.json").exists()
        assert server.policy_hook_disabled_reason is None
    finally:
        await server.close()


async def test_old_codex_with_routing_armed_keeps_user_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Arming subagent routing on old codex must not delete the user's hooks.

    On codex < 0.129 no generated hooks file is written, so the user's
    ``hooks.json`` has to stay symlinked into the private home. Fails if
    the routing arm drops the symlink and nothing takes its place.
    """
    from omnigent.inner.codex_executor import CODEX_ROUTER_DIR_ENV_VAR

    real_codex_home = tmp_path / "real-codex-home"
    real_codex_home.mkdir()
    user_hooks = real_codex_home / "hooks.json"
    user_hooks.write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "user-stop"}]}]}})
    )
    codex_home = tmp_path / "codex-home"
    bridge_dir = tmp_path / "bridge"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    router_dir = tmp_path / "router"
    router_dir.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(real_codex_home))
    monkeypatch.setattr(CodexNativeAppServer, "_wait_until_ready", _fake_wait_until_ready)
    _set_codex_version(monkeypatch, (0, 128, 0))

    server = _test_app_server(tmp_path, codex_home, bridge_dir, workspace)
    server.env = {CODEX_ROUTER_DIR_ENV_VAR: str(router_dir)}
    await server.start()
    try:
        hooks_path = codex_home / "hooks.json"
        assert hooks_path.exists()
        assert json.loads(hooks_path.read_text())["hooks"]["Stop"]
        assert server.router_hooks_registered is False
    finally:
        await server.close()


async def test_trust_failure_is_fail_open_with_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A trust handshake failure degrades the session instead of blocking it.

    This is the core behavior change: a hook that can't be trusted (e.g.
    "not discovered" on an otherwise-supported codex) must NOT raise out
    of start() — the session runs, the reason is recorded for a web-UI
    notice. Fails if start() re-raises (the old blocking behavior) or
    leaves the reason unset.
    """
    real_codex_home = tmp_path / "real-codex-home"
    real_codex_home.mkdir()
    codex_home = tmp_path / "codex-home"
    bridge_dir = tmp_path / "bridge"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(real_codex_home))
    monkeypatch.setattr(CodexNativeAppServer, "_wait_until_ready", _fake_wait_until_ready)
    _set_codex_version(monkeypatch, (0, 136, 0))

    async def _raise_trust(_self: CodexNativeAppServer) -> None:
        raise RuntimeError("Omnigent policy hook was not discovered for cwd ...")

    monkeypatch.setattr(CodexNativeAppServer, "_trust_policy_hooks", _raise_trust)

    server = _test_app_server(tmp_path, codex_home, bridge_dir, workspace)
    server.ap_server_url = "http://127.0.0.1:9999"
    await server.start()  # must NOT raise
    try:
        # Hook was registered (supported codex) but trust failed → degrade.
        assert (codex_home / "hooks.json").exists()
        assert server.policy_hook_disabled_reason is not None
        # The underlying trust error is carried into the reason.
        assert "not discovered" in server.policy_hook_disabled_reason
        assert "could not be trusted" in server.policy_hook_disabled_reason
    finally:
        await server.close()


def test_policy_hooks_timeout_outlasts_the_hooks_request_budget() -> None:
    """The codex hook timeout must outlast the hook's own AP request budget.

    A TOOL_CALL ASK is resolved server-side: the hook's POST to
    ``/policies/evaluate`` blocks (up to ``_EVALUATE_POLICY_TIMEOUT_S``) while
    the server parks the gate as a URL elicitation. Codex kills the hook
    subprocess after the ``timeout`` it reads from ``hooks.json``. If that
    timeout were shorter than the request budget, codex would kill the hook
    mid-park and run the tool before the ASK verdict arrived — the regression
    that let sub-agent tool calls slip past the cost gate (it was 30s).
    """
    settings = _codex_policy_hooks_settings(Path("/b"), "/venv/bin/python")
    hooks = settings["hooks"]
    # All registered phases share the same command hook; assert the timeout on
    # each so none can silently regress independently. UserPromptSubmit gates
    # the request phase and also blocks on a server-side ASK park, so it needs
    # the same generous timeout as the tool phases.
    pre = hooks["PreToolUse"][0]["hooks"][0]
    post = hooks["PostToolUse"][0]["hooks"][0]
    prompt = hooks["UserPromptSubmit"][0]["hooks"][0]
    assert pre["timeout"] == _POLICY_HOOK_TIMEOUT_SECONDS
    assert post["timeout"] == _POLICY_HOOK_TIMEOUT_SECONDS
    assert prompt["timeout"] == _POLICY_HOOK_TIMEOUT_SECONDS
    # The invariant that actually prevents the bug: codex must wait at least as
    # long as the hook itself will block on the server. If this fails (e.g. the
    # constant is dropped back to 30), the gate becomes advisory for every
    # native tool call, sub-agent or not.
    assert _POLICY_HOOK_TIMEOUT_SECONDS >= _EVALUATE_POLICY_TIMEOUT_S


def test_policy_hooks_register_user_prompt_submit() -> None:
    """The request-phase gate must be wired onto UserPromptSubmit.

    For native sessions the server-level ``_evaluate_input_policy`` skips
    message events, so this hook is the sole REQUEST gate. If it were dropped
    from ``hooks.json``, native prompts (web-UI-injected and direct-terminal
    alike) would reach the model with no request-phase policy at all.
    """
    settings = _codex_policy_hooks_settings(Path("/b"), "/venv/bin/python")
    hooks = settings["hooks"]
    assert "UserPromptSubmit" in hooks
    prompt_hook = hooks["UserPromptSubmit"][0]["hooks"][0]
    # Same evaluate-policy command as the tool phases.
    assert prompt_hook["command"] == hooks["PreToolUse"][0]["hooks"][0]["command"]


class TestPinCodexConfigModel:
    """_pin_codex_config_model seeds the per-session config.toml model."""

    def test_replaces_top_level_model_only(self, tmp_path: Path) -> None:
        """The top-level ``model`` line is replaced; lookalike keys survive.

        ``model_provider`` / ``model_reasoning_effort`` also start with
        "model", and keys inside tables must never be touched — both were
        plausible regressions for a line-match implementation.
        """
        from omnigent.codex_native_app_server import _pin_codex_config_model

        config = tmp_path / "config.toml"
        config.write_text(
            'model = "gpt-5.5"\n'
            'model_provider = "Databricks"\n'
            'model_reasoning_effort = "xhigh"\n'
            "[profiles.default]\n"
            'model = "table-scoped-stays"\n',
            encoding="utf-8",
        )
        _pin_codex_config_model(tmp_path, "databricks-gpt-5-4-mini")
        text = config.read_text(encoding="utf-8")
        assert 'model = "databricks-gpt-5-4-mini"' in text.splitlines()[0]
        assert 'model_provider = "Databricks"' in text
        assert 'model_reasoning_effort = "xhigh"' in text
        assert 'model = "table-scoped-stays"' in text
        assert "gpt-5.5" not in text

    def test_inserts_model_when_absent(self, tmp_path: Path) -> None:
        """A config with no top-level ``model`` gains one as the first line."""
        from omnigent.codex_native_app_server import _pin_codex_config_model

        config = tmp_path / "config.toml"
        config.write_text("[profiles.default]\nx = 1\n", encoding="utf-8")
        _pin_codex_config_model(tmp_path, "gpt-5.5")
        lines = config.read_text(encoding="utf-8").splitlines()
        assert lines[0] == 'model = "gpt-5.5"'
        assert "[profiles.default]" in lines

    def test_materializes_symlink_without_touching_source(self, tmp_path: Path) -> None:
        """A symlinked config.toml is copied per-session; the shared source
        keeps its own model line (the live-caught clobber scenario)."""
        from omnigent.codex_native_app_server import _pin_codex_config_model

        shared = tmp_path / "shared-config.toml"
        shared.write_text('model = "gpt-5.5"\n', encoding="utf-8")
        home = tmp_path / "codex-home"
        home.mkdir()
        (home / "config.toml").symlink_to(shared)
        _pin_codex_config_model(home, "databricks-gpt-5-4-mini")
        assert not (home / "config.toml").is_symlink()
        assert 'model = "databricks-gpt-5-4-mini"' in (home / "config.toml").read_text(
            encoding="utf-8"
        )
        assert shared.read_text(encoding="utf-8") == 'model = "gpt-5.5"\n'

    def test_read_back_by_forwarder_mirror_source(self, tmp_path: Path) -> None:
        """The forwarder's mirror source reads back exactly the pinned model.

        This is the regression the pin exists for: the mirror previously
        reported the shared file's stale model and overwrote the child's
        ``model_override``.
        """
        from omnigent.codex_native_app_server import _pin_codex_config_model
        from omnigent.codex_native_bridge import read_codex_config_model

        home = tmp_path / "codex-home"
        home.mkdir()
        (home / "config.toml").write_text('model = "gpt-5.5"\n', encoding="utf-8")
        bridge_dir = tmp_path
        # read_codex_config_model resolves codex-home under the bridge dir.
        _pin_codex_config_model(home, "databricks-gpt-5-4-mini")
        assert read_codex_config_model(bridge_dir) == "databricks-gpt-5-4-mini"


# --- Subagent-routing hook trust ---------------------------------------
#
# Empirically (codex-cli 0.145.0) ``--dangerously-bypass-hook-trust`` does
# NOT make hooks run under ``codex app-server``: an untrusted routing hook
# stayed silent with the flag and fired only once its
# ``hooks.state.<key>.trusted_hash`` was persisted. The routing hooks
# therefore need their own trust pass; the policy pass filters by module
# and would leave them untrusted (a silent fail-open on the spawn gate).

_ROUTER_GATE_COMMAND = (
    "/venv/bin/python -m omnigent.inner.hook_scripts.codex_router_hook "
    "route-subagent --bridge-dir /b --harness codex-native"
)


async def test_router_hooks_are_trusted_via_batchwrite() -> None:
    """Untrusted routing hooks are trusted with their currentHash."""
    client = _FakeCodexClient(
        hooks=[
            _hook("gate", _ROUTER_GATE_COMMAND, "untrusted", "sha256:gate"),
        ]
    )
    assert await trust_codex_router_hooks(client.request, cwd=_CWD) == []

    writes = _batchwrite_calls(client)
    assert len(writes) == 1
    edit = writes[0].params["edits"][0]
    assert edit["keyPath"] == "hooks.state"
    assert edit["value"] == {
        "gate": {"trusted_hash": "sha256:gate"},
    }
    assert writes[0].params["reloadUserConfig"] is True


async def test_router_hook_trust_never_touches_user_or_policy_hooks() -> None:
    """Only the routing hooks are trusted by the routing pass."""
    client = _FakeCodexClient(
        hooks=[
            _hook("gate", _ROUTER_GATE_COMMAND, "untrusted", "sha256:gate"),
            _hook("policy", _OUR_COMMAND, "untrusted", "sha256:policy"),
            _hook("theirs", _USER_COMMAND, "untrusted", "sha256:theirs"),
        ]
    )
    await trust_codex_router_hooks(client.request, cwd=_CWD)
    assert _batchwrite_calls(client)[0].params["edits"][0]["value"] == {
        "gate": {"trusted_hash": "sha256:gate"}
    }


async def test_router_hook_trust_reports_still_untrusted_without_raising() -> None:
    """A routing-trust failure is reported, never raised (policy must survive)."""
    client = _FakeCodexClient(
        hooks=[_hook("gate", _ROUTER_GATE_COMMAND, "untrusted")], flip_on_trust=False
    )
    assert await trust_codex_router_hooks(client.request, cwd=_CWD) == ["gate"]
    assert len(_batchwrite_calls(client)) == 1


async def test_router_hook_trust_noop_without_routing_hooks() -> None:
    """No routing hooks registered → no write, no failure."""
    client = _FakeCodexClient(hooks=[_hook("policy", _OUR_COMMAND, "trusted")])
    assert await trust_codex_router_hooks(client.request, cwd=_CWD) == []
    assert _batchwrite_calls(client) == []


async def test_already_trusted_router_hooks_skip_batchwrite() -> None:
    """Routing hooks already trusted issue no config write."""
    client = _FakeCodexClient(hooks=[_hook("gate", _ROUTER_GATE_COMMAND, "trusted")])
    assert await trust_codex_router_hooks(client.request, cwd=_CWD) == []
    assert _batchwrite_calls(client) == []


async def test_trust_step_covers_router_hooks_when_routing_armed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The startup trust step trusts the routing hooks alongside the policy hook."""
    from omnigent.inner.codex_executor import CODEX_ROUTER_DIR_ENV_VAR

    client = _FakeCodexClient(
        hooks=[
            _hook("policy", _OUR_COMMAND, "untrusted", "sha256:policy"),
            _hook("gate", _ROUTER_GATE_COMMAND, "untrusted", "sha256:gate"),
        ]
    )

    async def _fake_connect(self: Any) -> None:
        return None

    async def _fake_close(self: Any) -> None:
        return None

    monkeypatch.setattr(
        "omnigent.codex_native_app_server.CodexAppServerClient.connect", _fake_connect
    )
    monkeypatch.setattr("omnigent.codex_native_app_server.CodexAppServerClient.close", _fake_close)
    monkeypatch.setattr(
        "omnigent.codex_native_app_server.CodexAppServerClient.request",
        lambda self, method, params: client.request(method, params),
    )

    server = _test_app_server(tmp_path, tmp_path / "codex-home", tmp_path / "bridge", Path(_CWD))
    server.env[CODEX_ROUTER_DIR_ENV_VAR] = str(tmp_path / "bridge")
    server.router_hooks_registered = True
    await server._trust_policy_hooks()

    trusted = {}
    for write in _batchwrite_calls(client):
        trusted.update(write.params["edits"][0]["value"])
    assert set(trusted) == {"policy", "gate"}


async def test_trust_all_codex_hooks_trusts_user_hooks() -> None:
    """
    The runner-owned trust-all pass covers user hooks, not just Omnigent's.

    Codex ignores ``--dangerously-bypass-hook-trust`` for the startup
    review screen on a persistent ``resume``; persisting trust for the
    merged user hooks is what keeps that screen from stranding a resumed
    web session, so this pass must reach hooks the policy trust never does.
    """
    client = _FakeCodexClient(
        hooks=[
            _hook("ours", _OUR_COMMAND, "untrusted", "sha256:ours"),
            _hook("theirs", _USER_COMMAND, "untrusted", "sha256:theirs"),
        ]
    )

    assert await trust_all_codex_hooks(client.request, cwd=_CWD) == []

    writes = _batchwrite_calls(client)
    assert len(writes) == 1
    written = writes[0].params["edits"][0]["value"]
    assert written == {
        "ours": {"trusted_hash": "sha256:ours"},
        "theirs": {"trusted_hash": "sha256:theirs"},
    }


async def test_trust_all_codex_hooks_reports_still_untrusted_without_raising() -> None:
    """
    A hash that never flips is returned, not raised.

    The trust-all pass is a UX fix (suppress the review screen), not a
    security gate, so a persistent failure only risks the screen
    reappearing and must never block startup.
    """
    client = _FakeCodexClient(
        hooks=[_hook("theirs", _USER_COMMAND, "untrusted", "sha256:theirs")],
        flip_on_trust=False,
    )

    assert await trust_all_codex_hooks(client.request, cwd=_CWD) == ["theirs"]


async def test_trust_step_trusts_user_hooks_only_when_trust_all_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    ``trust_all_hooks`` gates whether the startup step trusts user hooks.

    Runner-owned sessions enable it so a resumed web session never faces
    the interactive review screen; interactive CLI sessions leave it off so
    a human at the terminal reviews their own new or changed hooks.
    """

    async def _fake_connect(self: Any) -> None:
        return None

    async def _fake_close(self: Any) -> None:
        return None

    monkeypatch.setattr(
        "omnigent.codex_native_app_server.CodexAppServerClient.connect", _fake_connect
    )
    monkeypatch.setattr("omnigent.codex_native_app_server.CodexAppServerClient.close", _fake_close)

    for trust_all, expected in ((True, {"policy", "theirs"}), (False, {"policy"})):
        client = _FakeCodexClient(
            hooks=[
                _hook("policy", _OUR_COMMAND, "untrusted", "sha256:policy"),
                _hook("theirs", _USER_COMMAND, "untrusted", "sha256:theirs"),
            ]
        )
        monkeypatch.setattr(
            "omnigent.codex_native_app_server.CodexAppServerClient.request",
            lambda self, method, params, _c=client: _c.request(method, params),
        )
        server = _test_app_server(
            tmp_path, tmp_path / "codex-home", tmp_path / "bridge", Path(_CWD)
        )
        server.trust_all_hooks = trust_all
        await server._trust_policy_hooks()

        trusted: dict[str, Any] = {}
        for write in _batchwrite_calls(client):
            trusted.update(write.params["edits"][0]["value"])
        assert set(trusted) == expected


async def test_policy_hook_command_runs_python_isolated() -> None:
    """The policy hook command passes ``-I`` before ``-m``.

    Same silent fail-open as the routing hooks: codex runs hooks with the
    session workspace as cwd, so without isolation a workspace containing an
    ``omnigent`` directory shadows the installed package and the policy gate
    dies on an import error codex never reports.
    """
    import shlex

    from omnigent.codex_native_app_server import _codex_policy_hook_command

    argv = shlex.split(_codex_policy_hook_command(Path("/b"), "/venv/bin/python"))
    assert argv[1:3] == ["-I", "-m"]


def test_codex_model_upgrade_target_reads_catalog_migration() -> None:
    """The runner records the exact old-to-new mapping Codex advertises."""
    from omnigent.codex_native_app_server import _codex_model_upgrade_target

    catalog = {
        "models": [
            {"slug": "gpt-5.4", "upgrade": {"model": "gpt-5.6-terra"}},
            {"slug": "current", "upgrade": None},
        ]
    }

    assert _codex_model_upgrade_target(catalog, "gpt-5.4") == "gpt-5.6-terra"
    assert _codex_model_upgrade_target(catalog, "current") is None
    assert _codex_model_upgrade_target(catalog, "missing") is None


def test_acknowledge_codex_model_migration_updates_private_config(tmp_path: Path) -> None:
    """Acknowledgement preserves user notices while suppressing one prompt."""
    from omnigent.codex_native_app_server import _acknowledge_codex_model_migration

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text("[notice]\nhide_rate_limit_model_nudge = true\n", encoding="utf-8")

    _acknowledge_codex_model_migration(codex_home, "gpt-5.4", "gpt-5.6-terra")

    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert config["notice"]["hide_rate_limit_model_nudge"] is True
    assert config["notice"]["model_migrations"] == {"gpt-5.4": "gpt-5.6-terra"}


def test_routed_spawn_note_appends_then_restores_the_user_base(tmp_path: Path) -> None:
    """The codex routed-spawn note rides ``developer_instructions``, reversibly.

    It must be additive to the user's own guidance on a fresh auto-harness
    launch and gone again on a resumed / pinned launch, which is what keeps a
    session that leaves auto-harness mode from carrying stale routing framing.
    """
    from omnigent.inner.hook_scripts.subagent_router import smart_routing_spawn_note

    note = smart_routing_spawn_note("codex-native")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text('developer_instructions = "Keep user guidance."\n', encoding="utf-8")

    _sync_codex_developer_instructions(codex_home, note)

    active = tomllib.loads(config_path.read_text(encoding="utf-8"))["developer_instructions"]
    assert active == f"Keep user guidance.\n\n{note}"
    assert "sys_session_create" in active
    # Codex takes bare MCP names plus a namespace, never a prefixed spelling.
    assert "mcp__omnigent__" not in active

    _sync_codex_developer_instructions(codex_home, None)

    resumed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert resumed["developer_instructions"] == "Keep user guidance."


@pytest.mark.parametrize(
    ("labels", "harness_override", "expected"),
    [
        ({"omnigent.routing.auto_harness": "1"}, None, True),
        # The sentinel survives until first-message routing resolves a harness.
        ({}, "auto", True),
        ({}, "codex-native", False),
        ({}, None, False),
    ],
    ids=["label", "sentinel", "pinned", "neither"],
)
async def test_codex_native_launch_config_reads_the_auto_harness_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    labels: dict[str, str],
    harness_override: str | None,
    expected: bool,
) -> None:
    """Only an auto-harness codex session gets the routed-spawn instructions."""
    import httpx

    from omnigent.runner.native.orchestration import _codex_native_launch_config
    from omnigent.runner.subagent_routing import AUTO_HARNESS_LABEL_KEY

    assert AUTO_HARNESS_LABEL_KEY == "omnigent.routing.auto_harness"
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:9999")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "workspace": str(tmp_path),
                "labels": labels,
                "harness_override": harness_override,
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://runner"
    ) as client:
        config = await _codex_native_launch_config(session_id="conv_abc", server_client=client)

    assert config.auto_harness is expected


async def test_probe_codex_model_options_uses_launch_config_and_marks_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The probe boots Codex with the session-launch materialization.

    Harness truth: the probe's rows are Codex's own ``model/list`` output
    under the same Databricks routing a session launch gets — provider
    overrides passed as ``-c`` args, ``DATABRICKS_HOST`` in env, a
    persistent probe home — reduced to a single default marker naming the
    launch-pinned model.
    """
    from omnigent import codex_native_app_server

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        codex_native_app_server,
        "resolve_native_codex_launch",
        lambda *, model: codex_native_app_server.NativeCodexLaunch(
            config_overrides=[],
            model=None,
            profile="oss",
        ),
    )
    monkeypatch.setattr(
        codex_native_app_server,
        "_databricks_launch_materialization",
        lambda *, model, profile: codex_native_app_server._DatabricksLaunchMaterialization(
            config_overrides=[
                'model="databricks-gpt-5-4"',
                'model_provider="omnigent_databricks"',
            ],
            model="databricks-gpt-5-4",
            host="https://ws.example",
        ),
    )
    monkeypatch.setattr(codex_native_app_server, "_clean_codex_env", lambda: {"PATH": "/bin"})

    captured: dict[str, object] = {}

    class _FakeProcess:
        pid = None
        returncode: int | None = None

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -1

        async def wait(self) -> int:
            self.returncode = 0 if self.returncode is None else self.returncode
            return self.returncode

    async def _fake_start(
        *,
        codex_path: str,
        listen_url: str,
        env: dict[str, str],
        cwd: Path,
        config_overrides: tuple[str, ...] = (),
    ) -> _FakeProcess:
        captured["codex_path"] = codex_path
        captured["env"] = dict(env)
        captured["cwd"] = cwd
        captured["config_overrides"] = list(config_overrides)
        return _FakeProcess()

    async def _fake_wait(process: object, port: int) -> None:
        del process, port

    class _FakeClient:
        def __init__(self, *, ws_url: str, client_name: str) -> None:
            assert client_name == "omnigent-codex-model-probe"

        async def connect(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            assert method == "model/list"
            return {
                "result": {
                    "data": [
                        {"id": "gpt-5.6-sol", "displayName": "Sol", "isDefault": True},
                        {"id": "gpt-5.4", "displayName": "gpt-5.4"},
                    ],
                    "nextCursor": None,
                }
            }

    monkeypatch.setattr(
        codex_native_app_server, "_start_codex_model_discovery_process", _fake_start
    )
    monkeypatch.setattr(codex_native_app_server, "_wait_for_discovery_listener", _fake_wait)
    monkeypatch.setattr(codex_native_app_server, "CodexAppServerClient", _FakeClient)

    rows = await codex_native_app_server.probe_codex_model_options(codex_path="/test/codex")

    # The launch-pinned model wins the (single) default marker, in either
    # spelling; Codex's own flag on sol is dropped.
    assert rows == [
        {"id": "gpt-5.6-sol", "displayName": "Sol"},
        {"id": "gpt-5.4", "displayName": "gpt-5.4", "isDefault": True},
    ]
    assert captured["config_overrides"] == [
        'model="databricks-gpt-5-4"',
        'model_provider="omnigent_databricks"',
    ]
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["DATABRICKS_HOST"] == "https://ws.example"
    # Persistent probe home under the omnigent cache, not a fresh temp dir.
    assert str(tmp_path / ".omnigent" / "cache" / "codex-model-probe") in env["CODEX_HOME"]
    assert Path(env["CODEX_HOME"]).is_dir()


async def test_probe_codex_model_options_probes_every_launch_shape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A non-Databricks launch still probes, with its own overrides verbatim.

    The plain Codex-login shape carries no ``DATABRICKS_HOST`` and no
    provider overrides beyond what the launch resolved (here the dismissal
    pin), and with no launch-pinned model Codex's own default marker
    stands.
    """
    from omnigent import codex_native_app_server

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        codex_native_app_server,
        "resolve_native_codex_launch",
        lambda *, model: codex_native_app_server.NativeCodexLaunch(
            config_overrides=['model_provider="openai"'],
            model=None,
            profile=None,
        ),
    )
    monkeypatch.setattr(codex_native_app_server, "_clean_codex_env", lambda: {"PATH": "/bin"})
    captured: dict[str, object] = {}

    class _FakeProcess:
        pid = None
        returncode: int | None = None

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -1

        async def wait(self) -> int:
            self.returncode = 0 if self.returncode is None else self.returncode
            return self.returncode

    async def _fake_start(
        *,
        codex_path: str,
        listen_url: str,
        env: dict[str, str],
        cwd: Path,
        config_overrides: tuple[str, ...] = (),
    ) -> _FakeProcess:
        captured["env"] = dict(env)
        captured["config_overrides"] = list(config_overrides)
        return _FakeProcess()

    async def _fake_wait(process: object, port: int) -> None:
        del process, port

    class _FakeClient:
        def __init__(self, *, ws_url: str, client_name: str) -> None:
            del ws_url, client_name

        async def connect(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            del method, params
            return {
                "result": {
                    "data": [{"id": "gpt-5.6-sol", "isDefault": True}, {"id": "gpt-5.5"}],
                    "nextCursor": None,
                }
            }

    monkeypatch.setattr(
        codex_native_app_server, "_start_codex_model_discovery_process", _fake_start
    )
    monkeypatch.setattr(codex_native_app_server, "_wait_for_discovery_listener", _fake_wait)
    monkeypatch.setattr(codex_native_app_server, "CodexAppServerClient", _FakeClient)

    rows = await codex_native_app_server.probe_codex_model_options(codex_path="/test/codex")

    assert rows == [{"id": "gpt-5.6-sol", "isDefault": True}, {"id": "gpt-5.5"}]
    assert captured["config_overrides"] == ['model_provider="openai"']
    env = captured["env"]
    assert isinstance(env, dict)
    assert "DATABRICKS_HOST" not in env


def test_resolve_databricks_codex_model_matches_servable_ids() -> None:
    """The codex launch model resolves against what the workspace serves.

    An unset model takes the newest servable id; a legacy ``databricks-``
    override resolves to the served ``system.ai.`` id for that same model; and a
    model the workspace does not serve passes through untouched (the gateway's
    error beats a silent substitution).
    """
    from types import SimpleNamespace
    from unittest.mock import patch

    from omnigent.codex_native_app_server import _resolve_databricks_codex_model

    servable = ("system.ai.gpt-5-6-sol", "system.ai.gpt-5-6-luna")
    with (
        patch(
            "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
            return_value=SimpleNamespace(token="tok"),
        ),
        patch(
            "omnigent.databricks_model_discovery.discover_databricks_codex_models",
            return_value=servable,
        ),
    ):
        assert (
            _resolve_databricks_codex_model("https://h.example.com", "prof", None)
            == "system.ai.gpt-5-6-sol"
        )
        assert (
            _resolve_databricks_codex_model(
                "https://h.example.com", "prof", "databricks-gpt-5-6-luna"
            )
            == "system.ai.gpt-5-6-luna"
        )
        assert (
            _resolve_databricks_codex_model("https://h.example.com", "prof", "databricks-gpt-9-9")
            == "databricks-gpt-9-9"
        )
