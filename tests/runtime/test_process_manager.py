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
    _build_harness_spawn_env,
    _cwd_env_key,
    _HarnessEndpoint,
    _model_env_key,
    _requested_cwd,
    _SubprocessEntry,
)


class _AliveProc:
    """Subprocess stand-in that reports as still running (``returncode`` None)."""

    returncode = None


@pytest.mark.asyncio
async def test_get_client_respawns_only_when_model_changes(
    monkeypatch: pytest.MonkeyPatch,
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

    conv, harness = "conv_x", "claude-sdk"
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
async def test_get_client_respawns_only_when_cwd_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_client`` respawns iff a different working directory is requested.

    The cwd is baked into the subprocess env at spawn (``HARNESS_<H>_CWD``),
    so after a session workspace change the next turn's spawn env carries a
    new cwd and must respawn — otherwise the cached process keeps running
    turns from the old directory. Same-cwd and no-cwd turns must keep the
    running process.

    :param monkeypatch: Pytest monkeypatch fixture used to mock the
        subprocess-spawn boundary.
    """
    pm = HarnessProcessManager()
    pm._started = True

    spawns: list[str | None] = []
    closes: list[str | None] = []

    async def _fake_spawn(conv: str, harness: str, env: dict[str, str] | None) -> _SubprocessEntry:
        cwd = (env or {}).get(_cwd_env_key(harness))
        spawns.append(cwd)
        return _SubprocessEntry(
            process=_AliveProc(),  # type: ignore[arg-type]  # stand-in process
            client=httpx.AsyncClient(),
            endpoint=_HarnessEndpoint(socket_path=Path("/tmp/fake-cwd.sock")),
            harness=harness,
            cwd=cwd,
        )

    async def _fake_close(entry: _SubprocessEntry) -> None:
        closes.append(entry.cwd)
        await entry.client.aclose()

    monkeypatch.setattr(pm, "_spawn_entry", _fake_spawn)
    monkeypatch.setattr(pm, "_close_entry", _fake_close)

    conv, harness = "conv_cwd", "claude-sdk"
    key = _cwd_env_key(harness)  # HARNESS_CLAUDE_SDK_CWD

    await pm.get_client(conv, harness, env={key: "/ws"})  # spawn at /ws
    await pm.get_client(conv, harness, env={key: "/ws"})  # same → cache hit
    await pm.get_client(conv, harness, env={key: "/ws/sub"})  # re-rooted → respawn
    await pm.get_client(conv, harness, env=None)  # no cwd env → keep running process

    assert spawns == ["/ws", "/ws/sub"], spawns
    assert closes == ["/ws"], closes

    final = pm._entries.get(conv)
    if final is not None:
        await final.client.aclose()


@pytest.mark.asyncio
async def test_acp_cli_cwd_change_respawns_via_the_shared_acp_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catalog ACP CLI harnesses respawn on a cwd change under ``HARNESS_ACP_CWD``.

    Rows like ``grok`` share the generic ACP wrap, whose spawn env carries
    the cwd as ``HARNESS_ACP_CWD`` rather than ``HARNESS_GROK_CWD``; keying
    the respawn check off the harness name alone would silently never fire
    for them and a re-rooted session would keep running in the old directory.

    :param monkeypatch: Pytest monkeypatch fixture used to mock the
        subprocess-spawn boundary.
    """
    pm = HarnessProcessManager()
    pm._started = True

    spawns: list[str | None] = []

    async def _fake_spawn(conv: str, harness: str, env: dict[str, str] | None) -> _SubprocessEntry:
        cwd = _requested_cwd(harness, env)
        spawns.append(cwd)
        return _SubprocessEntry(
            process=_AliveProc(),  # type: ignore[arg-type]  # stand-in process
            client=httpx.AsyncClient(),
            endpoint=_HarnessEndpoint(socket_path=Path("/tmp/fake-grok.sock")),
            harness=harness,
            cwd=cwd,
        )

    async def _fake_close(entry: _SubprocessEntry) -> None:
        await entry.client.aclose()

    monkeypatch.setattr(pm, "_spawn_entry", _fake_spawn)
    monkeypatch.setattr(pm, "_close_entry", _fake_close)

    await pm.get_client("conv_grok", "grok", env={"HARNESS_ACP_CWD": "/ws"})
    await pm.get_client("conv_grok", "grok", env={"HARNESS_ACP_CWD": "/ws"})  # cache hit
    await pm.get_client("conv_grok", "grok", env={"HARNESS_ACP_CWD": "/ws/sub"})  # respawn

    assert spawns == ["/ws", "/ws/sub"], spawns

    final = pm._entries.get("conv_grok")
    if final is not None:
        await final.client.aclose()


@pytest.mark.asyncio
async def test_qwen_model_change_keeps_live_acp_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Qwen applies model changes through ACP session config without respawning."""
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
        )

    monkeypatch.setattr(pm, "_spawn_entry", _fake_spawn)
    key = _model_env_key("qwen")

    await pm.get_client("conv_qwen", "qwen", env={key: "qwen-turbo"})
    await pm.get_client("conv_qwen", "qwen", env={key: "qwen-plus"})

    assert spawns == ["qwen-turbo"]
    entry = pm._entries.get("conv_qwen")
    if entry is not None:
        await entry.client.aclose()


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
