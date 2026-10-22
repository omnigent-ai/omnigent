"""Unit tests for :class:`HarnessProcessManager` model-change respawn.

The harness model is a fixed process env var (``HARNESS_<H>_MODEL``), baked
in at spawn time. So a later turn requesting a different model — e.g. after
the user runs ``/model`` — must respawn the subprocess; otherwise the cached
process keeps serving the old model and ``/model`` silently has no effect.
These tests mock the subprocess-spawn boundary (``_spawn_entry`` /
``_close_entry``) so they exercise the respawn *decision* in ``get_client``
without launching real runner subprocesses.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from omnigent.runner.identity import RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR
from omnigent.runtime.harnesses.process_manager import (
    HarnessProcessManager,
    _acp_startup_config,
    _build_harness_spawn_env,
    _HarnessEndpoint,
    _model_env_key,
    _SubprocessEntry,
)


class _AliveProc:
    """Subprocess stand-in that reports as still running (``returncode`` None)."""

    returncode = None


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["claude-sdk", "acp"])
async def test_get_client_respawns_only_when_model_changes(
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
) -> None:
    """``get_client`` respawns iff a concrete different model is requested.

    Drives a single conversation through a sequence of model requests and
    asserts the spawn count tracks exactly the model *transitions* (same
    model → cache hit, no spawn; changed model → respawn; no model env →
    keep the running process). A failure means either ``/model`` wouldn't
    take effect (missing respawn) or every turn needlessly respawns
    (over-eager respawn that would churn the harness + drop its warm state).

    :param monkeypatch: Pytest monkeypatch fixture used to mock the
        subprocess-spawn boundary.
    """
    pm = HarnessProcessManager()
    # Bypass start(): these tests mock the spawn boundary, so no instance
    # dir / orphan sweep / real subprocess is needed.
    pm._started = True

    spawns: list[str | None] = []
    closes: list[str | None] = []

    async def _fake_spawn(conv: str, harness: str, env: dict[str, str] | None) -> _SubprocessEntry:
        """Record the spawned model and return a live fake entry."""
        model = (env or {}).get(_model_env_key(harness))
        spawns.append(model)
        return _SubprocessEntry(
            process=_AliveProc(),  # type: ignore[arg-type]  # stand-in process
            client=httpx.AsyncClient(),
            endpoint=_HarnessEndpoint(socket_path=Path("/tmp/fake.sock")),
            harness=harness,
            model=model,
        )

    async def _fake_close(entry: _SubprocessEntry) -> None:
        """Record the closed entry's model and release its client."""
        closes.append(entry.model)
        await entry.client.aclose()

    monkeypatch.setattr(pm, "_spawn_entry", _fake_spawn)
    monkeypatch.setattr(pm, "_close_entry", _fake_close)

    conv = "conv_x"
    key = _model_env_key(harness)  # HARNESS_CLAUDE_SDK_MODEL

    await pm.get_client(conv, harness, env={key: "claude-opus-4-6"})  # spawn opus
    await pm.get_client(conv, harness, env={key: "claude-opus-4-6"})  # same → cache hit
    await pm.get_client(conv, harness, env={key: "claude-sonnet-4-6"})  # changed → respawn
    await pm.get_client(conv, harness, env=None)  # no model env → keep running process
    await pm.get_client(conv, harness, env={key: "claude-opus-4-6"})  # changed back → respawn

    # Exactly three spawns, tracking the model transitions opus→sonnet→opus.
    # If the respawn-on-change were missing this would be ["claude-opus-4-6"]
    # (everything served by the first cached process).
    assert spawns == ["claude-opus-4-6", "claude-sonnet-4-6", "claude-opus-4-6"], spawns
    # Each respawn closed the prior process first (opus, then sonnet); the
    # cache-hit and the env=None turn close nothing.
    assert closes == ["claude-opus-4-6", "claude-sonnet-4-6"], closes

    final = pm._entries.get(conv)
    if final is not None:
        await final.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["qwen", "acp"])
async def test_live_acp_model_change_keeps_process(
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
) -> None:
    """ACP applies model changes through session config without respawning."""
    pm = HarnessProcessManager()
    pm._started = True
    spawns: list[str | None] = []

    async def _fake_spawn(conv: str, harness: str, env: dict[str, str] | None) -> _SubprocessEntry:
        model = (env or {}).get(_model_env_key(harness))
        spawns.append(model)
        return _SubprocessEntry(
            process=_AliveProc(),  # type: ignore[arg-type]
            client=httpx.AsyncClient(),
            endpoint=_HarnessEndpoint(socket_path=Path("/tmp/fake-qwen.sock")),
            harness=harness,
            model=model,
            acp_config=_acp_startup_config(env) if harness == "acp" else None,
        )

    monkeypatch.setattr(pm, "_spawn_entry", _fake_spawn)
    key = _model_env_key(harness)

    env = {"HARNESS_ACP_MODEL_LIST": "model-a,model-b"} if harness == "acp" else {}
    await pm.get_client("conv_acp", harness, env={**env, key: "model-a"})
    await pm.get_client("conv_acp", harness, env={**env, key: "model-b"})

    assert spawns == ["model-a"]
    entry = pm._entries.get("conv_acp")
    if entry is not None:
        await entry.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "changed_value"),
    [
        ("HARNESS_ACP_COMMAND", "another-agent --acp"),
        ("HARNESS_ACP_MODEL_LIST", "model-a,model-c"),
        ("HARNESS_ACP_DEFAULT_MODEL", "model-b"),
        ("HARNESS_ACP_ENV_UNSET", "OPENAI_API_KEY"),
        ("HARNESS_ACP_MODEL_LIST", None),
    ],
)
async def test_acp_startup_config_change_restarts_process(
    monkeypatch: pytest.MonkeyPatch, key: str, changed_value: str | None
) -> None:
    """Commands, curation, defaults, and credential exclusions cannot change in a cached child."""
    pm = HarnessProcessManager()
    pm._started = True

    async def fake_spawn(conv: str, harness: str, env: dict[str, str] | None) -> _SubprocessEntry:
        return _SubprocessEntry(
            process=_AliveProc(),
            client=httpx.AsyncClient(),
            endpoint=_HarnessEndpoint(socket_path=Path("/tmp/fake-acp.sock")),
            harness=harness,
            model=(env or {}).get(_model_env_key(harness)),
            acp_config=_acp_startup_config(env),
        )

    async def fake_close(entry: _SubprocessEntry) -> None:
        await entry.client.aclose()

    monkeypatch.setattr(pm, "_spawn_entry", fake_spawn)
    monkeypatch.setattr(pm, "_close_entry", fake_close)
    env = {
        "HARNESS_ACP_COMMAND": "agent --acp",
        "HARNESS_ACP_MODEL_LIST": "model-a,model-b",
        "HARNESS_ACP_DEFAULT_MODEL": "model-a",
        "HARNESS_ACP_MODEL": "model-a",
    }
    first = await pm.get_client("conv_acp", "acp", env=env)
    assert await pm.get_client("conv_acp", "any") is first
    assert await pm.get_client("conv_acp", "acp", env=env) is first
    changed_env = dict(env)
    if changed_value is None:
        changed_env.pop(key)
    else:
        changed_env[key] = changed_value
    second = await pm.get_client("conv_acp", "acp", env=changed_env)
    try:
        assert second is not first
        assert first.is_closed
        assert await pm.get_client("conv_acp", "acp", env=changed_env) is second
    finally:
        await second.aclose()


def test_build_harness_spawn_env_strips_binding_token_with_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner tunnel binding token never reaches the harness env.

    The runner process carries the binding token in its own
    ``os.environ`` (it reuses the token for request auth), so the merged
    spawn env would inherit it unless explicitly stripped. This is the
    token leak: a token visible to the harness lets the agent
    payload impersonate the runner against the control-plane tunnel.

    Asserts the token is gone while AP's own env and the caller's
    per-spec overrides both survive.

    :param monkeypatch: Pytest monkeypatch fixture used to seed the
        binding token (and a benign var) into ``os.environ``.
    """
    monkeypatch.setenv(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR, "bug-binding-token-secret")
    monkeypatch.setenv("PATH_MARKER_FOR_TEST", "marker-value")
    key = _model_env_key("claude-sdk")

    env = _build_harness_spawn_env({key: "claude-opus-4-6"})

    assert RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR not in env
    assert "bug-binding-token-secret" not in env.values()
    assert env[key] == "claude-opus-4-6"  # caller override preserved
    assert env["PATH_MARKER_FOR_TEST"] == "marker-value"  # AP env inherited


def test_build_harness_spawn_env_strips_binding_token_without_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no-overrides path also strips the token (not a bare inherit).

    The previous implementation returned ``None`` (full inherit) when no
    overrides were passed — the common case — which re-leaked the token.
    This pins the explicit-dict-with-strip behavior for that path.

    :param monkeypatch: Pytest monkeypatch fixture used to seed the
        binding token into ``os.environ``.
    """
    monkeypatch.setenv(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR, "bug-binding-token-secret")
    monkeypatch.setenv("PATH_MARKER_FOR_TEST", "marker-value")

    env = _build_harness_spawn_env(None)

    assert RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR not in env
    assert "bug-binding-token-secret" not in env.values()  # not leaked under another key
    assert env["PATH_MARKER_FOR_TEST"] == "marker-value"


@pytest.mark.parametrize("with_overrides", [False, True])
def test_build_harness_spawn_env_keeps_desktop_session_in_runner(
    monkeypatch: pytest.MonkeyPatch, with_overrides: bool
) -> None:
    session_env = {
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "XDG_RUNTIME_DIR": "/run/user/1000",
    }
    monkeypatch.setattr(os, "environ", {**session_env, "XDG_CONFIG_HOME": "/home/test/.config"})
    overrides = {**session_env, "HARNESS_CODEX_GATEWAY_AUTH_COMMAND": "printf %s test-key"}

    env = _build_harness_spawn_env(overrides if with_overrides else None)

    if with_overrides:
        assert {name: env[name] for name in session_env} == session_env
    else:
        assert session_env.keys().isdisjoint(env)
    assert env["XDG_CONFIG_HOME"] == "/home/test/.config"
    assert {name: os.environ[name] for name in session_env} == session_env
    assert {name: overrides[name] for name in session_env} == session_env
    if with_overrides:
        assert env["HARNESS_CODEX_GATEWAY_AUTH_COMMAND"] == "printf %s test-key"
