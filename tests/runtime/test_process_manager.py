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

from pathlib import Path

import httpx
import pytest

from omnigent.runner.identity import RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR
from omnigent.runtime.harnesses.process_manager import (
    HarnessProcessManager,
    _build_harness_spawn_env,
    _HarnessEndpoint,
    _model_env_key,
    _SubprocessEntry,
)


class _AliveProc:
    """Subprocess stand-in that reports as still running (``returncode`` None)."""

    returncode = None


@pytest.mark.asyncio
async def test_get_client_isolates_per_env_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_client`` spawns one subprocess per unique spawn-env fingerprint.

    Conversations with the same harness AND the same env share a subprocess.
    Different envs (including different models) get separate subprocesses so
    per-spec isolation is preserved — the model and other env vars are baked
    into the executor at construction time and cannot be changed per-turn.

    :param monkeypatch: Pytest monkeypatch fixture used to mock the
        subprocess-spawn boundary.
    """
    pm = HarnessProcessManager()
    pm._started = True

    spawns: list[str | None] = []

    async def _fake_spawn(conv: str, harness: str, env: dict[str, str] | None) -> _SubprocessEntry:
        model = (env or {}).get(_model_env_key(harness))
        spawns.append(model)
        return _SubprocessEntry(
            process=_AliveProc(),  # type: ignore[arg-type]
            client=httpx.AsyncClient(),
            endpoint=_HarnessEndpoint(socket_path=Path("/tmp/fake.sock")),
            harness=harness,
        )

    async def _fake_close(entry: _SubprocessEntry) -> None:
        await entry.client.aclose()

    monkeypatch.setattr(pm, "_spawn_entry", _fake_spawn)
    monkeypatch.setattr(pm, "_close_entry", _fake_close)

    conv, harness = "conv_x", "claude-sdk"
    key = _model_env_key(harness)

    await pm.get_client(conv, harness, env={key: "claude-opus-4-6"})
    await pm.get_client(conv, harness, env={key: "claude-opus-4-6"})  # same env → reuse
    await pm.get_client("conv_y", harness, env={key: "claude-opus-4-6"})  # same env → reuse
    await pm.get_client(
        "conv_z", harness, env={key: "claude-sonnet-4-6"}
    )  # diff model → new spawn

    # Two spawns: one per unique env fingerprint.
    assert spawns == ["claude-opus-4-6", "claude-sonnet-4-6"], spawns

    for entry in pm._entries.values():
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
