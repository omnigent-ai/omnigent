"""Tests for the runner-owned Pi bridge bearer refresh."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent import cli_auth
from omnigent.harnesses.pi_native import auth_command, auth_refresh, credentials
from omnigent.runner import _entry as runner_entry
from tests._helpers.polling import poll_until_async

_URL = "https://example.cloud.databricks.com/api/2.0/omnigent"


def _config(bridge_dir: Path) -> Path:
    path = bridge_dir / "config.json"
    headers = {"Authorization": "Bearer stale", "X-Omnigent-Runner-Tunnel-Token": "keep"}
    path.write_text(json.dumps({"authHeaders": headers}))
    return path


def _bearer(path: Path) -> str:
    return json.loads(path.read_text())["authHeaders"]["Authorization"]


async def _cancel(task: asyncio.Task[None]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_refresh_retries_failures_and_keeps_launch_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Failures keep the config and log the type once; a later mint is merged in."""
    path = _config(tmp_path)
    mints = iter([RuntimeError("secret"), RuntimeError("secret"), None, "fresh"])
    calls: list[dict[str, object]] = []

    def _make(url: str, **kwargs: object):
        calls.append({"url": url, **kwargs})
        result = next(mints)

        def _mint() -> str | None:
            if isinstance(result, Exception):
                assert _bearer(path) == "Bearer stale"
                raise result
            return result

        return _mint

    monkeypatch.setattr(runner_entry, "_make_auth_token_factory", _make)
    task = asyncio.create_task(
        auth_refresh.supervise_pi_auth_refresh(tmp_path, _URL, interval_s=0)
    )
    try:
        await poll_until_async(lambda: _bearer(path) == "Bearer fresh", timeout_s=5)
    finally:
        await _cancel(task)

    assert json.loads(path.read_text())["authHeaders"]["X-Omnigent-Runner-Tunnel-Token"] == "keep"
    assert calls == [{"url": _URL, "_allow_initial_token": False}] * 4
    assert [r.getMessage() for r in caplog.records] == [
        "Pi bridge auth refresh failed (RuntimeError)"
    ]


@pytest.mark.asyncio
async def test_default_interval_renews_cached_token_before_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner's real OIDC factory must renew before the bridge bearer lapses."""
    path = _config(tmp_path)
    now = 0.0
    monkeypatch.delenv("OMNIGENT_PROXY_BEARER_FILE", raising=False)
    monkeypatch.delenv("OMNIGENT_RUNNER_DELEGATED_AUTH", raising=False)
    monkeypatch.setattr(cli_auth, "_token_file_path", lambda: tmp_path / "tokens.json")
    monkeypatch.setattr(cli_auth, "time", SimpleNamespace(time=lambda: now))
    cli_auth.store_token(_URL, "stale", "test@example.com", expires_at=150)

    def _renew(url: str) -> str:
        cli_auth.store_token(url, "fresh", "test@example.com", expires_at=3600)
        return "fresh"

    async def _advance(interval_s: float) -> None:
        nonlocal now
        now += interval_s
        if _bearer(path) == "Bearer fresh":
            raise asyncio.CancelledError
        assert now < 150, "the bridge bearer expired before the next refresh"

    monkeypatch.setattr(cli_auth, "refresh_stored_token", _renew)
    monkeypatch.setattr(
        auth_refresh,
        "asyncio",
        SimpleNamespace(to_thread=asyncio.to_thread, wait_for=asyncio.wait_for, sleep=_advance),
    )
    with pytest.raises(asyncio.CancelledError):
        await auth_refresh.supervise_pi_auth_refresh(tmp_path, _URL)
    assert _bearer(path) == "Bearer fresh"


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
async def test_interrupted_mint_does_not_write_late_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    """A hung mint cannot overwrite the config after cancellation or recovery."""
    path = _config(tmp_path)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def _mint() -> str:
        if started.is_set():
            return "fresh"
        started.set()
        release.wait(timeout=10)
        finished.set()
        return "late"

    monkeypatch.setattr(runner_entry, "_make_auth_token_factory", lambda *_a, **_k: _mint)
    task = asyncio.create_task(
        auth_refresh.supervise_pi_auth_refresh(
            tmp_path, _URL, interval_s=0, timeout_s=0.1 if interruption == "timeout" else 30
        )
    )
    expected = "Bearer fresh" if interruption == "timeout" else "Bearer stale"
    try:
        await poll_until_async(started.is_set, timeout_s=5, interval_s=0.005)
        if interruption == "timeout":
            await poll_until_async(lambda: _bearer(path) == expected, timeout_s=5)
    finally:
        await _cancel(task)
        release.set()
        await poll_until_async(finished.is_set, timeout_s=5)
    assert _bearer(path) == expected


@pytest.mark.asyncio
async def test_slow_model_auth_is_warmed_shared_and_refreshed_outside_pi_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    if os.name != "posix" or shutil.which("node") is None:
        pytest.skip("the native Pi token wrapper requires POSIX and Node")
    monkeypatch.setattr(auth_command, "AUTH_TIMEOUT_MS", 100)
    token_file = tmp_path / "token"
    token_file.write_text("first-private-token")
    calls = tmp_path / "calls"
    script = (
        "import pathlib,time,sys; time.sleep(0.4); "
        "p=pathlib.Path(sys.argv[1]); "
        "p.write_text(p.read_text()+'x' if p.exists() else 'x'); "
        "print(pathlib.Path(sys.argv[2]).read_text())"
    )
    command = shlex.join([sys.executable, "-c", script, str(calls), str(token_file)])
    wrapped = auth_command.wrap_auth_command(
        tmp_path, "primary", command, refresh_interval_ms=900_000
    )
    auth_command.wrap_auth_command(tmp_path, "secondary", command, refresh_interval_ms=900_000)
    auth_dir = tmp_path / auth_command.AUTH_DIR_NAME
    await auth_command.refresh_model_auth(auth_dir)
    assert calls.read_text() == "x", "gateway surfaces must share one cached mint"

    def request() -> subprocess.CompletedProcess[str]:
        return subprocess.run(shlex.split(wrapped[1:]), capture_output=True, text=True, timeout=2)

    for _ in range(2):
        result = await asyncio.to_thread(request)
        assert result.returncode == 0
        assert result.stdout == "first-private-token"
        assert result.stderr == ""
    assert calls.read_text() == "x"
    cache = next(auth_dir.glob("token-*.json"))
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    assert all("private-token" not in path.read_text() for path in auth_dir.glob("*.result.json"))

    token_file.write_text("second-private-token")
    cached = json.loads(cache.read_text())
    cached["expiresAt"] = time.time() * 1000 + 100_000
    cached["cachedAt"] = cached["expiresAt"] - 900_000
    cache.write_text(json.dumps(cached))
    await auth_command.refresh_model_auth(auth_dir)
    assert calls.read_text() == "xx"
    assert (await asyncio.to_thread(request)).stdout == "second-private-token"

    cached = json.loads(cache.read_text())
    cached["expiresAt"] = time.time() * 1000 + 100_000
    cached["cachedAt"] = cached["expiresAt"] - 900_000
    cache.write_text(json.dumps(cached))
    token_file.unlink()
    await auth_command.refresh_model_auth(auth_dir)
    assert json.loads(cache.read_text()) == cached
    assert "exited 1" in caplog.text
    assert "private-token" not in caplog.text
    assert (await asyncio.to_thread(request)).stdout == "second-private-token"

    # An expired cache must never turn a failed mint into a successful request.
    cached["expiresAt"] = 0
    cache.write_text(json.dumps(cached))
    result = await asyncio.to_thread(request)
    assert result.returncode == 1
    assert result.stdout == ""

    async def fail_to_spawn(*args, **kwargs):
        raise OSError("private-token")

    monkeypatch.setattr(auth_command.asyncio, "create_subprocess_exec", fail_to_spawn)
    await auth_command.refresh_model_auth(auth_dir)
    assert "failed to start" in caplog.text
    assert "OSError" in caplog.text
    assert "private-token" not in caplog.text


@pytest.mark.asyncio
async def test_model_auth_refresh_ignores_removed_provider_configs(tmp_path: Path) -> None:
    if os.name != "posix" or shutil.which("node") is None:
        pytest.skip("the native Pi token wrapper requires POSIX and Node")
    calls = tmp_path / "mints"
    provider = credentials.PiProviderConfig(
        provider_id="primary",
        base_url="http://localhost/v1",
        api="openai-completions",
        model="mock-auth",
        api_key=f"!printf x >> {shlex.quote(str(calls))}; printf private-token",
        auth_header=True,
        auth_refresh_interval_ms=900_000,
    )
    rendered = provider.to_models_config()
    rendered["providers"]["secondary"] = dict(rendered["providers"]["primary"])
    credentials.write_pi_models_config(tmp_path, provider, rendered)
    auth_dir = tmp_path / auth_command.AUTH_DIR_NAME
    await auth_command.refresh_model_auth(auth_dir)
    assert calls.read_text() == "x"
    for cache in auth_dir.glob("token-*.json"):
        cache.unlink()

    # Relaunch the same session with a new command and caching disabled.
    credentials.write_pi_models_config(
        tmp_path, replace(provider, api_key="!printf current-token", auth_refresh_interval_ms=0)
    )
    await auth_command.refresh_model_auth(auth_dir)
    assert calls.read_text() == "x", "a removed provider must never mint again"
    assert not list(auth_dir.glob("token-*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("refresh_exit_code", [0, 7])
async def test_model_auth_refresh_preserves_request_failure_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refresh_exit_code: int
) -> None:
    if os.name != "posix" or shutil.which("node") is None:
        pytest.skip("the native Pi token wrapper requires POSIX and Node")
    monkeypatch.setattr(auth_command, "AUTH_TIMEOUT_MS", 500)
    started, release = tmp_path / "started", tmp_path / "release"
    script = (
        "import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); "
        "\nwhile not pathlib.Path(sys.argv[2]).exists(): time.sleep(0.01)\n"
        f"print('private-token'); sys.exit({refresh_exit_code})"
    )
    wrapped = auth_command.wrap_auth_command(
        tmp_path,
        "primary",
        shlex.join([sys.executable, "-c", script, str(started), str(release)]),
        refresh_interval_ms=900_000,
    )
    result = await asyncio.to_thread(
        subprocess.run,
        shlex.split(wrapped[1:]),
        capture_output=True,
        text=True,
        timeout=5,
        env={**os.environ, "OMNIGENT_PI_NATIVE_PID": "123"},
    )
    assert result.returncode == 1
    auth_dir = tmp_path / auth_command.AUTH_DIR_NAME
    result_path = next(auth_dir.glob("*.result.json"))
    request_failure = result_path.read_text()
    assert json.loads(request_failure)["kind"] == "timeout"
    started.unlink()
    task = asyncio.create_task(auth_command.refresh_model_auth(auth_dir))
    try:
        await poll_until_async(started.exists, timeout_s=5)
        assert result_path.read_text() == request_failure
        release.touch()
        await asyncio.wait_for(task, timeout=5)
        assert result_path.read_text() == request_failure
    finally:
        if not task.done():
            await _cancel(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
async def test_interrupted_model_refresh_reaps_helper_without_publishing_token(
    tmp_path: Path,
    interruption: str,
) -> None:
    if os.name != "posix" or shutil.which("node") is None:
        pytest.skip("the native Pi token wrapper requires POSIX and Node")
    from omnigent.inner._proc import process_alive

    pid_file = tmp_path / "pid"
    script = (
        "import os,pathlib,sys,time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
        "time.sleep(30); print('late-private-token')"
    )
    auth_command.wrap_auth_command(
        tmp_path,
        "primary",
        shlex.join([sys.executable, "-c", script, str(pid_file)]),
        refresh_interval_ms=900_000,
        refresh_timeout_ms=1_000 if interruption == "timeout" else 20_000,
    )
    auth_dir = tmp_path / auth_command.AUTH_DIR_NAME
    task = asyncio.create_task(auth_command.refresh_model_auth(auth_dir))
    try:
        await poll_until_async(lambda: pid_file.exists() and pid_file.read_text(), timeout_s=5)
        if interruption == "timeout":
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
    finally:
        if not task.done():
            await _cancel(task)
    pid = int(pid_file.read_text())
    await poll_until_async(lambda: not process_alive(pid), timeout_s=5)
    assert not list(auth_dir.glob("token-*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining_s", [-10, 120, 3_600])
async def test_model_auth_cache_respects_jwt_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remaining_s: int
) -> None:
    if os.name != "posix" or shutil.which("node") is None:
        pytest.skip("the native Pi token wrapper requires POSIX and Node")
    expires_at = time.time() + remaining_s
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expires_at}).encode()).decode()
    token = f"header.{payload.rstrip('=')}.signature"
    calls = tmp_path / "mints"
    wrapped = auth_command.wrap_auth_command(
        tmp_path,
        "primary",
        f"printf x >> {shlex.quote(str(calls))}; printf %s {shlex.quote(token)}",
        refresh_interval_ms=900_000,
    )
    auth_dir = tmp_path / auth_command.AUTH_DIR_NAME
    await auth_command.refresh_model_auth(auth_dir)
    cache = json.loads(next(auth_dir.glob("token-*.json")).read_text())
    assert cache["expiresAt"] <= expires_at * 1000
    result = await asyncio.to_thread(
        subprocess.run, shlex.split(wrapped[1:]), capture_output=True, text=True, timeout=2
    )
    assert result.returncode == 0
    assert result.stdout == token
    assert calls.read_text() == ("xx" if remaining_s < 0 else "x")
    if remaining_s == 120:
        monkeypatch.setattr(auth_command, "time", SimpleNamespace(time=lambda: expires_at - 80))
        await auth_command.refresh_model_auth(auth_dir)
        assert calls.read_text() == "x", "short-lived JWTs must not mint on every poll"
        monkeypatch.setattr(auth_command, "time", SimpleNamespace(time=lambda: expires_at - 40))
        await auth_command.refresh_model_auth(auth_dir)
        assert calls.read_text() == "xx", "refresh before the effective lifetime ends"
