"""Tests for omnigent.chat — omnigent chat CLI logic."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import click
import httpx
import pytest
from omnigent_client import OmnigentError as ClientOmnigentError
from omnigent_client import QueryResult

import omnigent.chat as chat_module
from omnigent.chat import (
    _DEFAULT_AD_HOC_MODEL,
    _SERVER_READY_BACKOFF_POLL_SECONDS,
    _SERVER_READY_FAST_POLL_WINDOW_SECONDS,
    _SERVER_READY_INITIAL_POLL_SECONDS,
    ChatOverrides,
    _apply_overrides_to_raw,
    _chat_via_daemon,
    _cleanup_materialized_override_bundle,
    _DaemonChatSession,
    _default_cli_model,
    _extract_agent_name,
    _is_url,
    _materialize_override_bundle,
    _persisted_turn_text,
    _prepare_chat_session_via_daemon,
    _query_sessions_once,
    _raise_server_failed,
    _remote_headers,
    _spec_used_families,
    _start_local_server,
    _validate_agent_spec,
    _wait_for_remote_runner,
    _wait_for_server,
    run_chat,
)
from omnigent.cli import _build_resume_parts
from omnigent.inner.databricks_executor import DatabricksCredentials
from omnigent.spec import load as load_spec
from omnigent.spec import validate as validate_spec

# ── _is_url ──────────────────────────────────────────


def test_is_url_http() -> None:
    """HTTP URLs are detected."""
    assert _is_url("http://localhost:8000") is True


def test_is_url_https() -> None:
    """HTTPS URLs are detected."""
    assert _is_url("https://my-server.example.com") is True


def test_is_url_path() -> None:
    """Filesystem paths are not URLs."""
    assert _is_url("./my-agent/") is False


def test_is_url_relative() -> None:
    """Relative paths are not URLs."""
    assert _is_url("tests/resources/examples/archer") is False


def test_is_url_absolute() -> None:
    """Absolute paths are not URLs."""
    assert _is_url("/home/user/my-agent") is False


def test_redirect_native_resume_routes_kiro_wrapper(monkeypatch: pytest.MonkeyPatch) -> None:
    """A kiro-native wrapper session redirects to ``run_kiro_native``."""
    monkeypatch.setattr(
        chat_module,
        "_wrapper_label_for_conversation",
        lambda *, base_url, conversation_id: "kiro-native-ui",
    )
    captured: dict[str, object] = {}

    def _capture(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("omnigent.kiro_native.run_kiro_native", _capture)

    redirected = chat_module._redirect_native_resume_if_needed(
        base_url="https://example.com",
        conversation_id="conv_kiro",
        auto_open_conversation=True,
    )

    assert redirected is True
    assert captured == {
        "server": "https://example.com",
        "session_id": "conv_kiro",
        "kiro_args": (),
        "auto_open_conversation": True,
    }


# ── _extract_agent_name ──────────────────────────────


def test_extract_name_from_config(tmp_path: Path) -> None:
    """Reads agent name from config.yaml."""
    agent_dir = tmp_path / "test-agent"
    agent_dir.mkdir()
    (agent_dir / "config.yaml").write_text(
        "spec_version: 1\nname: my-cool-agent\nexecutor:\n  config:\n    harness: openai-agents\n"
    )
    assert _extract_agent_name(agent_dir) == "my-cool-agent"


def test_extract_name_falls_back_to_dirname(tmp_path: Path) -> None:
    """Falls back to directory name when config has no name."""
    agent_dir = tmp_path / "fallback-agent"
    agent_dir.mkdir()
    (agent_dir / "config.yaml").write_text("spec_version: 1\n")
    assert _extract_agent_name(agent_dir) == "fallback-agent"


def test_extract_name_no_config(tmp_path: Path) -> None:
    """Falls back to directory name when no config.yaml exists."""
    agent_dir = tmp_path / "no-config"
    agent_dir.mkdir()
    assert _extract_agent_name(agent_dir) == "no-config"


# ── _validate_agent_spec ─────────────────────────────


def _write_archer_config(agent_dir: Path, *, api_key: str) -> None:
    """
    Write a minimal valid agent config.yaml.

    Mirrors the structure of ``examples/archer/config.yaml``
    (the spec requires ``llm.connection.api_key``, not a bare
    ``llm.api_key``) so the env-expansion path the test wants to
    exercise is the same one a real spec would hit.

    :param agent_dir: Directory to write into; must already exist.
    :param api_key: Value for ``llm.connection.api_key``. Use a literal
        string for the happy path or ``"${SOME_UNSET_VAR}"`` to trigger
        the env-expansion error path.
    """
    (agent_dir / "config.yaml").write_text(
        "spec_version: 1\n"
        "name: test-agent\n"
        "instructions: hi\n"
        "executor:\n"
        "  config:\n"
        "    harness: openai-agents\n"
        "llm:\n"
        "  model: openai/gpt-4o\n"
        "  connection:\n"
        f"    api_key: {api_key}\n"
    )


def test_validate_agent_spec_unresolved_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Unresolved ``${VAR}`` references in config.yaml surface as a
    ``ClickException`` so the user sees the real error inline rather
    than the generic "Server failed to start" message.
    """
    monkeypatch.delenv("AP_TEST_MISSING_KEY", raising=False)
    agent_dir = tmp_path / "broken-env"
    agent_dir.mkdir()
    _write_archer_config(agent_dir, api_key="${AP_TEST_MISSING_KEY}")

    with pytest.raises(click.ClickException) as excinfo:
        _validate_agent_spec(agent_dir)

    # Asserting on the variable name (not just "ClickException raised")
    # proves the underlying OmnigentError message reached the user
    # — that's the entire point of the pre-validation step.
    assert "AP_TEST_MISSING_KEY" in excinfo.value.message


def test_validate_agent_spec_missing_config(tmp_path: Path) -> None:
    """
    A directory with no ``config.yaml`` raises ``ClickException``
    (load() raises ``FileNotFoundError`` which the helper converts).
    """
    agent_dir = tmp_path / "no-config"
    agent_dir.mkdir()

    with pytest.raises(click.ClickException) as excinfo:
        _validate_agent_spec(agent_dir)

    # Confirms the FileNotFoundError branch of the except clause fired
    # (not the OmnigentError branch) — both must convert.
    assert "config.yaml" in excinfo.value.message


def test_validate_agent_spec_valid_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A well-formed spec with all env vars resolved returns ``None``
    and does not raise. Guards against the helper accidentally
    rejecting valid specs.
    """
    monkeypatch.setenv("AP_TEST_PRESENT_KEY", "sk-fake-test-value")
    agent_dir = tmp_path / "ok-agent"
    agent_dir.mkdir()
    _write_archer_config(agent_dir, api_key="${AP_TEST_PRESENT_KEY}")

    assert _validate_agent_spec(agent_dir) is None


def test_wait_for_server_uses_fast_poll_before_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Local chat startup should probe aggressively during the initial
    ready window, then back off slightly after that.

    The first sleep proves the helper no longer burns a fixed 500 ms
    before noticing a ready server; the later sleep proves the backoff
    still engages on slower cold starts instead of busy-spinning.
    """

    class _Resp:
        """Minimal response stub exposing ``status_code``."""

        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    server = SimpleNamespace(
        proc=SimpleNamespace(poll=lambda: None),
        runner_id=None,
        log_path=Path("/tmp/server.log"),
    )
    monotonic_values = iter(
        [0.0, 0.0, 0.2, 0.2, 1.2, 1.2, 1.25, 1.25],
    )
    sleep_calls: list[float] = []
    http_calls = {"count": 0}

    def _fake_monotonic() -> float:
        """Return scripted times covering fast-then-backoff phases."""
        return next(monotonic_values)

    def _fake_sleep(seconds: float) -> None:
        """Record each poll interval the helper chooses."""
        sleep_calls.append(seconds)

    def _fake_get(url: str, timeout: float) -> _Resp:
        """Fail twice, then report ready on the third probe."""
        del url, timeout
        http_calls["count"] += 1
        if http_calls["count"] < 3:
            raise __import__("httpx").ConnectError("not ready")
        return _Resp(200)

    monkeypatch.setattr("omnigent.chat.time.monotonic", _fake_monotonic)
    monkeypatch.setattr("omnigent.chat.time.sleep", _fake_sleep)
    monkeypatch.setattr("omnigent.chat.httpx.get", _fake_get)

    _wait_for_server(8123, server, timeout=5.0)

    assert sleep_calls == [
        _SERVER_READY_INITIAL_POLL_SECONDS,
        _SERVER_READY_BACKOFF_POLL_SECONDS,
    ], (
        "Expected fast polling before the backoff window and a slower "
        "poll interval after it; different values would change local "
        "chat startup responsiveness."
    )
    assert _SERVER_READY_INITIAL_POLL_SECONDS < _SERVER_READY_BACKOFF_POLL_SECONDS
    assert _SERVER_READY_FAST_POLL_WINDOW_SECONDS == 1.0


def test_raise_server_failed_truncates_log_to_tail(tmp_path: Path) -> None:
    """
    The ClickException includes the tail of the server log inline so
    CI failures (which can't tail the file by hand) carry the
    traceback in stderr.

    Writes strictly more than ``_SERVER_LOG_TAIL_LINES`` lines so the
    truncation path is exercised: the head must be dropped, the tail
    must be preserved.
    """
    from omnigent.chat import _SERVER_LOG_TAIL_LINES

    log = tmp_path / "server.log"
    head_lines = [f"banner-line-{i}" for i in range(_SERVER_LOG_TAIL_LINES + 30)]
    tail_lines = [
        "ERROR: spec parse failed at line 3",
        "Traceback (most recent call last):",
        "  File omnigent/server/app.py, line 42, in create_app",
        "RuntimeError: missing required field 'agent'",
    ]
    log.write_text("\n".join(head_lines + tail_lines) + "\n")
    server = SimpleNamespace(
        proc=SimpleNamespace(args=["python", "-m", "omnigent", "server"]),
        log_path=log,
    )

    with pytest.raises(click.ClickException) as exc:
        _raise_server_failed(server)

    msg = exc.value.message
    # Each tail line is the actual cause; they must appear.
    for line in tail_lines:
        assert line in msg, f"missing tail line {line!r} in:\n{msg}"
    # The earliest head line must NOT appear -- proves we dropped the
    # head. (The 30 extra head lines ensure banner-line-0 is well
    # outside the tail window.)
    assert "banner-line-0" not in msg, (
        f"truncation didn't drop the head; banner-line-0 leaked into message:\n{msg}"
    )
    # The cmd display and log path are still in the message.
    assert "python -m omnigent server" in msg
    assert str(log) in msg


def test_raise_server_failed_handles_unreadable_log(tmp_path: Path) -> None:
    """
    If the log file is missing or unreadable, the exception still
    raises with a clear note rather than crashing with OSError.
    """
    missing = tmp_path / "does-not-exist.log"
    server = SimpleNamespace(
        proc=SimpleNamespace(args=["python", "-m", "omnigent", "server"]),
        log_path=missing,
    )

    with pytest.raises(click.ClickException) as exc:
        _raise_server_failed(server)

    msg = exc.value.message
    assert "could not read log file" in msg
    # Path and cmd display still surface so the user can investigate.
    assert str(missing) in msg
    assert "python -m omnigent server" in msg


def test_wait_for_server_waits_for_runner_tunnel_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Local chat startup waits until the runner's WS tunnel is online.

    Without this, the server can respond before the runner finishes
    reconnecting to ``/v1/runners/{id}/tunnel`` and the first prompt
    races into ``WSTunnelTransport`` while the runner is still offline.
    """

    class _Resp:
        """Minimal response stub exposing status and JSON body."""

        def __init__(self, status_code: int, body: dict[str, bool] | None = None) -> None:
            self.status_code = status_code
            self._body = body

        def json(self) -> dict[str, bool]:
            """Return the scripted response body."""
            if self._body is None:
                raise AssertionError("json() called on a response without a body")
            return self._body

    server = SimpleNamespace(
        proc=SimpleNamespace(poll=lambda: None),
        runner_id="runner_wait_test",
        log_path=Path("/tmp/server.log"),
    )
    monotonic_values = iter([0.0, 0.0, 0.2, 0.2, 0.3])
    sleep_calls: list[float] = []
    status_bodies = iter([{"online": False}, {"online": True}])
    requested_urls: list[str] = []

    def _fake_monotonic() -> float:
        """Return scripted times for one retry."""
        return next(monotonic_values)

    def _fake_sleep(seconds: float) -> None:
        """Record the poll interval chosen while runner is offline."""
        sleep_calls.append(seconds)

    def _fake_get(url: str, timeout: float) -> _Resp:
        """Report server readiness immediately but runner online later."""
        del timeout
        requested_urls.append(url)
        if url.endswith("/health"):
            return _Resp(200)
        if url.endswith("/v1/runners/runner_wait_test/status"):
            return _Resp(200, next(status_bodies))
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr("omnigent.chat.time.monotonic", _fake_monotonic)
    monkeypatch.setattr("omnigent.chat.time.sleep", _fake_sleep)
    monkeypatch.setattr("omnigent.chat.httpx.get", _fake_get)

    _wait_for_server(8123, server, timeout=5.0)

    assert sleep_calls == [_SERVER_READY_INITIAL_POLL_SECONDS]
    assert requested_urls == [
        "http://127.0.0.1:8123/health",
        "http://127.0.0.1:8123/v1/runners/runner_wait_test/status",
        "http://127.0.0.1:8123/health",
        "http://127.0.0.1:8123/v1/runners/runner_wait_test/status",
    ]


def test_start_local_server_spawns_runner_as_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Local server startup spawns the runner as a sibling subprocess.

    What this proves: ``_start_local_server`` spawns both the server
    (via ``subprocess.Popen``) and the runner (via
    ``_start_cli_runner_process``). The server receives a tunnel token
    via ``OMNIGENT_RUNNER_TUNNEL_TOKEN`` so it accepts exactly the
    sibling runner's tunnel. The runner is NOT a child of the server.
    """
    from omnigent.cli import _CliRunnerProcess

    class _Proc:
        """Minimal subprocess handle returned by the patched Popen."""

        def __init__(
            self,
            args: list[str],
            env: dict[str, str],
            stdout: object,
            stderr: object,
        ) -> None:
            self.args = args
            self.env = env
            self.stdout = stdout
            self.stderr = stderr

        def poll(self) -> None:
            """Report the subprocess as still running."""

    server_popen_calls: list[_Proc] = []

    def _fake_popen(
        args: list[str],
        *,
        env: dict[str, str],
        stdout: object,
        stderr: object,
        start_new_session: bool,
    ) -> _Proc:
        """Record the server subprocess command."""
        assert start_new_session is True
        proc = _Proc(args=args, env=env, stdout=stdout, stderr=stderr)
        server_popen_calls.append(proc)
        return proc

    runner_proc = _Proc(args=[], env={}, stdout=None, stderr=None)
    runner_calls: list[dict[str, object]] = []

    def _fake_start_runner(**kwargs: object) -> _CliRunnerProcess:
        """Record the runner spawn arguments."""
        runner_calls.append(kwargs)
        return _CliRunnerProcess(
            proc=runner_proc,
            runner_id=str(kwargs.get("runner_id", "")),
            tunnel_token=str(kwargs.get("tunnel_token", "")),
        )

    # Import before patching ``subprocess.Popen``. The runner package imports
    # MCP modules with ``subprocess.Popen[...]`` annotations, and patching the
    # process-global module first makes those imports fail in isolated runs.
    import omnigent.runner.identity  # noqa: F401

    monkeypatch.setattr("omnigent.chat.subprocess.Popen", _fake_popen)
    monkeypatch.setattr("omnigent.chat._omnigent_log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(
        "omnigent.chat.load_spec",
        lambda _path: SimpleNamespace(executor=SimpleNamespace(profile=None)),
    )
    monkeypatch.setattr(
        "omnigent.cli._start_cli_runner_process",
        _fake_start_runner,
    )
    server = _start_local_server(tmp_path, 8765, ephemeral=True)

    # Server subprocess was spawned.
    assert len(server_popen_calls) == 1
    assert server_popen_calls[0].args[2:6] == [
        "omnigent.cli",
        "server",
        "--host",
        "127.0.0.1",
    ]
    assert server_popen_calls[0].args[-2:] == ["--agent", str(tmp_path)]
    # Server receives the tunnel token, not RUNNER_ID_ENV_VAR.
    assert "OMNIGENT_RUNNER_TUNNEL_TOKEN" in server_popen_calls[0].env

    # Runner was spawned as a sibling via _start_cli_runner_process.
    assert len(runner_calls) == 1
    assert runner_calls[0]["server_url"] == "http://127.0.0.1:8765"
    assert (
        runner_calls[0]["tunnel_token"]
        == server_popen_calls[0].env["OMNIGENT_RUNNER_TUNNEL_TOKEN"]
    )
    assert runner_calls[0]["isolate_session"] is True

    # LocalServer exposes both runner_id and runner_proc.
    assert server.runner_id is not None
    assert server.runner_proc is runner_proc
    assert server.log_path.parent == tmp_path / "logs" / "server"
    assert server.log_path.name.startswith("server-")
    assert server.log_path.suffix == ".log"


def test_wait_for_remote_runner_uses_status_endpoint_and_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remote ``--server`` waits until the laptop runner tunnel is online."""

    class _Resp:
        """Minimal response stub exposing status and JSON body."""

        def __init__(self, body: dict[str, bool]) -> None:
            self.status_code = 200
            self._body = body

        def json(self) -> dict[str, bool]:
            """Return the scripted response body."""
            return self._body

    proc = SimpleNamespace(poll=lambda: None, returncode=None)
    headers = {"Authorization": "Bearer tok-test"}
    status_bodies = iter([{"online": False}, {"online": True}])
    requested: list[tuple[str, dict[str, str]]] = []
    monotonic_values = iter([0.0, 0.0, 0.2, 0.2, 0.3])
    sleep_calls: list[float] = []

    def _fake_monotonic() -> float:
        """Return scripted times for one retry."""
        return next(monotonic_values)

    def _fake_sleep(seconds: float) -> None:
        """Record the chosen poll interval."""
        sleep_calls.append(seconds)

    def _fake_get(url: str, *, headers: dict[str, str], timeout: float) -> _Resp:
        """Return offline once, then online."""
        del timeout
        requested.append((url, headers))
        return _Resp(next(status_bodies))

    monkeypatch.setattr("omnigent.chat.time.monotonic", _fake_monotonic)
    monkeypatch.setattr("omnigent.chat.time.sleep", _fake_sleep)
    monkeypatch.setattr("omnigent.chat.httpx.get", _fake_get)

    _wait_for_remote_runner(
        "https://example.databricksapps.com",
        "runner_remote_test",
        headers,
        proc,
        timeout=5.0,
    )

    assert requested == [
        (
            "https://example.databricksapps.com/v1/runners/runner_remote_test/status",
            headers,
        ),
        (
            "https://example.databricksapps.com/v1/runners/runner_remote_test/status",
            headers,
        ),
    ]
    assert sleep_calls == [_SERVER_READY_INITIAL_POLL_SECONDS]


def test_wait_for_remote_runner_fails_loud_on_auth_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 401/403 status probe reports auth failure instead of timing out.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """

    class _Resp:
        """Minimal response stub exposing status and JSON body."""

        status_code = 401

        def json(self) -> dict[str, bool]:
            """Return an offline body.

            :returns: Offline runner status.
            """
            return {"online": False}

    proc = SimpleNamespace(poll=lambda: None, returncode=None)

    def _fake_get(url: str, *, headers: dict[str, str], timeout: float) -> _Resp:
        """Return an auth rejection for the status endpoint.

        :param url: Status endpoint URL.
        :param headers: Auth headers passed by the caller.
        :param timeout: Per-request timeout.
        :returns: A 401 response.
        """
        del url, headers, timeout
        return _Resp()

    monkeypatch.setattr("omnigent.chat.httpx.get", _fake_get)

    with pytest.raises(click.ClickException, match="status check was rejected \\(401\\)"):
        _wait_for_remote_runner(
            "https://example.databricksapps.com",
            "runner_remote_test",
            {"Authorization": "Bearer tok-test"},
            proc,
            timeout=5.0,
        )


def test_wait_for_remote_runner_timeout_surfaces_log_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Timeout failures point the user at the runner log path.

    The exception message includes the captured log location so
    the user knows where to look, but — by design — does NOT
    dump log lines inline. Surfacing the tail made the error
    overwhelming; the file is one ``cat`` away.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Pytest tmp dir fixture.
    :returns: None.
    """
    log_path = tmp_path / "runner.log"
    # The file CONTAINS a recognizable error line; we then assert
    # below that this line does not bleed into the user-facing
    # error message (path-only policy).
    log_path.write_text(
        "INFO: connecting tunnel to https://example.databricksapps.com\n"
        "ERROR: tunnel rejected (HTTP 401)\n"
    )

    class _Resp:
        """Status-endpoint stub that reports the runner offline.

        Mirrors the response shape the real server returns when
        the runner has not yet sent its hello frame.
        """

        status_code = 200

        def json(self) -> dict[str, bool]:
            """Return an offline body.

            :returns: Offline runner status.
            """
            return {"online": False}

    proc = SimpleNamespace(poll=lambda: None, returncode=None)
    monotonic_values = iter([0.0, 0.0, 0.05, 10.0])

    def _fake_monotonic() -> float:
        """Advance scripted time past the 5s timeout on the third call.

        :returns: The next scripted monotonic value.
        """
        return next(monotonic_values)

    monkeypatch.setattr("omnigent.chat.time.monotonic", _fake_monotonic)
    monkeypatch.setattr("omnigent.chat.time.sleep", lambda _s: None)
    monkeypatch.setattr(
        "omnigent.chat.httpx.get",
        lambda *_a, **_k: _Resp(),
    )

    with pytest.raises(click.ClickException) as exc_info:
        _wait_for_remote_runner(
            "https://example.databricksapps.com",
            "runner_remote_test",
            {"Authorization": "Bearer tok-test"},
            proc,
            timeout=5.0,
            log_path=log_path,
        )
    message = exc_info.value.message
    # Path is named so the user can open it themselves.
    assert str(log_path) in message
    assert "Runner log:" in message
    # The timeout summary line is still present (regression
    # check that the format helper did not eat it).
    assert "did not register" in message
    # Path-only policy: log CONTENT must not appear in the error.
    assert "ERROR: tunnel rejected" not in message
    assert "INFO: connecting tunnel" not in message


def test_wait_for_remote_runner_early_exit_surfaces_log_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Runner-died-during-startup failures also surface the log path.

    The runner subprocess may crash before the server ever sees
    it (bad config, missing env var, import failure). The early-
    exit branch of the poll loop must point at the log file the
    same way the timeout branch does — and must not flood the
    error with the captured traceback.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Pytest tmp dir fixture.
    :returns: None.
    """
    log_path = tmp_path / "runner.log"
    log_path.write_text(
        "Traceback (most recent call last):\n"
        '  File "runner.py", line 1, in <module>\n'
        "ModuleNotFoundError: No module named 'foo'\n"
    )

    proc = SimpleNamespace(poll=lambda: 1, returncode=1)
    monkeypatch.setattr("omnigent.chat.time.monotonic", lambda: 0.0)
    monkeypatch.setattr("omnigent.chat.time.sleep", lambda _s: None)

    def _fake_get(*_a, **_k):
        """Status probe never invoked because the runner is dead.

        :raises AssertionError: If the poll loop reaches httpx.
        """
        raise AssertionError("should not reach httpx when runner already exited")

    monkeypatch.setattr("omnigent.chat.httpx.get", _fake_get)

    with pytest.raises(click.ClickException) as exc_info:
        _wait_for_remote_runner(
            "https://example.databricksapps.com",
            "runner_remote_test",
            {"Authorization": "Bearer tok-test"},
            proc,
            timeout=5.0,
            log_path=log_path,
        )
    message = exc_info.value.message
    assert "exited early with code 1" in message
    assert str(log_path) in message
    # Path-only policy: traceback content must not leak in.
    assert "ModuleNotFoundError" not in message
    assert "Traceback" not in message


def test_chat_remote_prompt_uses_one_shot_not_repl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prompt mode sends one SDK query and does not open the REPL."""
    calls: dict[str, object] = {}

    def _fake_one_shot(
        *,
        base_url: str,
        agent_name: str,
        tool_handler: object | None,
        prompt: str,
        runner_id: str | None = None,
        session_bundle: bytes | None = None,
        session_bundle_filename: str = "agent.tar.gz",
        resume_conversation_id: str | None = None,
        auto_open_conversation: bool = False,
    ) -> None:
        """Record one-shot query inputs."""
        del (
            session_bundle,
            session_bundle_filename,
            resume_conversation_id,
            auto_open_conversation,
        )
        calls["one_shot"] = (
            base_url,
            agent_name,
            tool_handler,
            prompt,
            runner_id,
        )

    def _fake_repl(*_args: object, **_kwargs: object) -> None:
        """Fail if prompt mode opens the interactive REPL."""
        raise AssertionError("prompt mode must use one-shot query, not REPL")

    monkeypatch.setattr(chat_module, "_run_one_shot", _fake_one_shot)
    monkeypatch.setattr(chat_module, "_run_repl", _fake_repl)

    chat_module._chat_with_server(
        "https://example.databricksapps.com/",
        None,
        initial_message="say hi",
        agent_name="hello",
        runner_id="runner_local_test",
    )

    assert calls["one_shot"] == (
        "https://example.databricksapps.com",
        "hello",
        None,
        "say hi",
        "runner_local_test",
    )


def test_run_prompt_local_dispatches_headless_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local ``-p`` calls the headless local helper with persistence flags."""
    captured: dict[str, object] = {}

    def _fake_headless(
        agent_path: str,
        tool_handler: object | None,
        *,
        overrides: chat_module.ChatOverrides,
        prompt: str,
        ephemeral: bool = False,
    ) -> None:
        """Record local headless dispatch inputs."""
        captured["agent_path"] = agent_path
        captured["tool_handler"] = tool_handler
        captured["overrides"] = overrides
        captured["prompt"] = prompt
        captured["ephemeral"] = ephemeral

    monkeypatch.setattr(chat_module, "_run_local_headless_prompt", _fake_headless)

    chat_module.run_prompt(
        "tests/resources/examples/hello_world.yaml",
        None,
        prompt="hello",
        ephemeral=True,
    )

    assert captured["agent_path"] == "tests/resources/examples/hello_world.yaml"
    assert captured["tool_handler"] is None
    overrides = captured["overrides"]
    assert isinstance(overrides, chat_module.ChatOverrides)
    # No --harness/--model/--system-prompt were passed, so the headless
    # helper must receive an empty override set (nothing baked into the spec).
    assert overrides.has_any is False
    assert captured["prompt"] == "hello"
    assert captured["ephemeral"] is True


def test_canonicalize_local_agent_path_promotes_root_config_yaml(tmp_path: Path) -> None:
    """A directory agent's root ``config.yaml`` resolves to its bundle root.

    This is the shape users naturally type for bundles like
    ``examples/polly/config.yaml``. If the helper returns the file instead
    of the parent, the bundler treats it as a standalone YAML and drops
    sibling ``agents/`` / ``skills/`` directories.
    """
    agent_dir = tmp_path / "bundle"
    agent_dir.mkdir()
    config_yaml = agent_dir / "config.yaml"
    config_yaml.write_text("spec_version: 1\nname: bundle\nprompt: hi\n")
    standalone_yaml = tmp_path / "agent.yaml"
    standalone_yaml.write_text("name: single\nprompt: hi\n")

    assert chat_module._canonicalize_local_agent_path(config_yaml) == agent_dir
    assert chat_module._canonicalize_local_agent_path(standalone_yaml) == standalone_yaml
    assert chat_module._canonicalize_local_agent_path(agent_dir) == agent_dir


def test_run_chat_with_server_url_routes_through_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_chat`` with a non-URL target ensures the backend and goes daemon.

    A local agent path + ``--server`` must resolve the backend via
    ``_ensure_backend`` (which ensures the connect daemon) and hand off to
    ``_chat_via_daemon`` — never the removed CLI-spawned-runner path.
    """
    agent_yaml = tmp_path / "hello.yaml"
    agent_yaml.write_text("name: hello\nprompt: Say hi.\n")
    calls: dict[str, object] = {}

    def _fake_ensure_backend(server: str | None) -> str:
        calls["ensure_backend"] = server
        return "https://example.databricksapps.com"

    def _fake_via_daemon(
        agent_path: str, base_url: str, tool_handler: object, **kwargs: object
    ) -> None:
        calls["via_daemon"] = {"agent_path": agent_path, "base_url": base_url, **kwargs}

    monkeypatch.setattr("omnigent.cli._ensure_backend", _fake_ensure_backend)
    monkeypatch.setattr(chat_module, "_chat_via_daemon", _fake_via_daemon)

    run_chat(
        target=str(agent_yaml),
        client_tools=None,
        server_url="https://example.databricksapps.com",
        prompt="say hi",
    )

    assert calls["ensure_backend"] == "https://example.databricksapps.com"
    via = calls["via_daemon"]
    assert isinstance(via, dict)
    assert via["base_url"] == "https://example.databricksapps.com"
    assert via["agent_path"] == str(agent_yaml)
    assert via["initial_message"] == "say hi"
    assert via["fork_session_id"] is None


def test_chat_via_daemon_uses_directory_bundle_for_root_config_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing ``bundle/config.yaml`` uploads and labels the whole bundle.

    Regression guard for directory agents such as ``examples/polly``: the
    daemon path must feed the parent directory into materialization, skill
    discovery, and bundling. If it feeds the file, sub-agents and skills are
    silently excluded from the uploaded session bundle.
    """
    agent_dir = tmp_path / "orchestrator"
    (agent_dir / "agents" / "worker").mkdir(parents=True)
    (agent_dir / "skills" / "investigate").mkdir(parents=True)
    config_yaml = agent_dir / "config.yaml"
    config_yaml.write_text(
        "spec_version: 1\n"
        "name: orchestrator\n"
        "prompt: orchestrate\n"
        "executor:\n"
        "  type: omnigent\n"
        "  config:\n"
        "    harness: claude-sdk\n"
    )
    (agent_dir / "agents" / "worker" / "config.yaml").write_text(
        "spec_version: 1\n"
        "name: worker\n"
        "prompt: work\n"
        "executor:\n"
        "  type: omnigent\n"
        "  config:\n"
        "    harness: codex-native\n"
    )
    (agent_dir / "skills" / "investigate" / "SKILL.md").write_text(
        "---\nname: investigate\ndescription: investigate things\n---\nBody\n"
    )
    captured: dict[str, object] = {}

    async def _fake_prepare(**kwargs: object) -> _DaemonChatSession:
        """Record daemon preparation inputs and return a prepared session."""
        captured["prepare"] = kwargs
        return _DaemonChatSession(session_id="conv_dir", runner_id="runner_dir")

    def _fake_bundle(path: Path) -> bytes:
        """Record the bundle source path and return fake tarball bytes."""
        captured["bundle_path"] = path
        return b"bundle-bytes"

    def _fake_chat_with_server(*_args: object, **kwargs: object) -> None:
        """Record REPL attachment inputs."""
        captured["chat"] = kwargs

    monkeypatch.setattr(chat_module, "_bundle_agent", _fake_bundle)
    monkeypatch.setattr(
        "omnigent.host.identity.load_or_create_host_identity",
        lambda: SimpleNamespace(host_id="host_x", name="x"),
    )
    monkeypatch.setattr(chat_module, "_resolve_resume_target", lambda **_k: None)
    monkeypatch.setattr(chat_module, "_prepare_chat_session_via_daemon", _fake_prepare)
    monkeypatch.setattr(chat_module, "_chat_with_server", _fake_chat_with_server)

    _chat_via_daemon(
        str(config_yaml),
        "https://example.databricksapps.com",
        None,
        overrides=ChatOverrides(),
    )

    assert captured["bundle_path"] == agent_dir
    chat = captured["chat"]
    assert isinstance(chat, dict)
    assert chat["agent_yaml"] == agent_dir
    assert chat["agent_name"] == "orchestrator"
    skills = chat["skills"]
    assert isinstance(skills, list)
    assert "investigate" in {skill.name for skill in skills}


def test_run_local_headless_prompt_uses_directory_bundle_for_root_config_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One-shot local prompt mode also preserves directory-agent siblings.

    This covers ``omnigent run bundle/config.yaml -p ...``. Without the
    canonicalization in the headless helper, interactive runs would upload the
    full bundle while one-shot runs would silently upload only ``config.yaml``.
    """
    agent_dir = tmp_path / "orchestrator"
    agent_dir.mkdir()
    config_yaml = agent_dir / "config.yaml"
    config_yaml.write_text(
        "spec_version: 1\n"
        "name: orchestrator\n"
        "prompt: orchestrate\n"
        "executor:\n"
        "  type: omnigent\n"
        "  config:\n"
        "    harness: claude-sdk\n"
    )
    captured: dict[str, object] = {}

    def _fake_bundle(path: Path) -> bytes:
        """Record the bundle source path and return fake tarball bytes."""
        captured["bundle_path"] = path
        return b"bundle-bytes"

    def _fake_start_local_server(
        spec_path: Path,
        port: int,
        *,
        ephemeral: bool = False,
    ) -> SimpleNamespace:
        """Record the local server spec path and return a fake server."""
        captured["server_spec_path"] = spec_path
        captured["server_ephemeral"] = ephemeral
        return SimpleNamespace(proc=None, runner_proc=None, runner_id="runner_headless")

    def _fake_run_headless_prompt(
        base_url: str,
        agent_name: str,
        tool_handler: object | None,
        *,
        prompt: str,
        runner_id: str | None = None,
        session_bundle: bytes | None = None,
    ) -> None:
        """Record one-shot prompt inputs instead of making an API call."""
        captured["headless"] = {
            "base_url": base_url,
            "agent_name": agent_name,
            "tool_handler": tool_handler,
            "prompt": prompt,
            "runner_id": runner_id,
            "session_bundle": session_bundle,
        }

    monkeypatch.setattr(chat_module, "_find_free_port", lambda: 34567)
    monkeypatch.setattr(chat_module, "_start_local_server", _fake_start_local_server)
    monkeypatch.setattr(chat_module, "_wait_for_server", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(chat_module, "_stop_local_server", lambda _server: None)
    monkeypatch.setattr(chat_module, "_bundle_agent", _fake_bundle)
    monkeypatch.setattr(chat_module, "_run_headless_prompt", _fake_run_headless_prompt)

    chat_module._run_local_headless_prompt(
        str(config_yaml),
        None,
        overrides=ChatOverrides(),
        prompt="say hi",
        ephemeral=True,
    )

    assert captured["server_spec_path"] == agent_dir
    assert captured["server_ephemeral"] is True
    assert captured["bundle_path"] == agent_dir
    headless = captured["headless"]
    assert isinstance(headless, dict)
    assert headless["agent_name"] == "orchestrator"
    assert headless["prompt"] == "say hi"
    assert headless["runner_id"] == "runner_headless"
    assert headless["session_bundle"] == b"bundle-bytes"


def test_chat_local_uses_directory_bundle_for_root_config_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local interactive mode preserves a directory agent's bundle root.

    This covers the ``_chat_local`` path used by local non-daemon REPL runs.
    If the helper passes ``bundle/config.yaml`` through unchanged, the local
    server and session bundle lose sibling directories such as ``agents/`` and
    ``skills/``.
    """
    agent_dir = tmp_path / "orchestrator"
    (agent_dir / "skills" / "investigate").mkdir(parents=True)
    config_yaml = agent_dir / "config.yaml"
    config_yaml.write_text(
        "spec_version: 1\n"
        "name: orchestrator\n"
        "prompt: orchestrate\n"
        "executor:\n"
        "  type: omnigent\n"
        "  config:\n"
        "    harness: claude-sdk\n"
    )
    (agent_dir / "skills" / "investigate" / "SKILL.md").write_text(
        "---\nname: investigate\ndescription: investigate things\n---\nBody\n"
    )
    captured: dict[str, object] = {}

    def _fake_start_local_server(
        spec_path: Path,
        port: int,
        *,
        ephemeral: bool = False,
    ) -> chat_module.LocalServer:
        """Record the local server spec path and return a fake handle."""
        captured["server_spec_path"] = spec_path
        captured["server_port"] = port
        captured["server_ephemeral"] = ephemeral
        return chat_module.LocalServer(
            proc=SimpleNamespace(),
            log_path=tmp_path / "server.log",
            runner_id="runner_local",
            runner_proc=None,
        )

    def _fake_bundle(path: Path) -> bytes:
        """Record the bundle source path and return fake tarball bytes."""
        captured["bundle_path"] = path
        return b"bundle-bytes"

    def _fake_chat_with_server(*args: object, **kwargs: object) -> None:
        """Record local REPL attachment inputs."""
        captured["chat_args"] = args
        captured["chat_kwargs"] = kwargs

    monkeypatch.setattr(chat_module, "_find_free_port", lambda: 45678)
    monkeypatch.setattr(chat_module, "_start_local_server", _fake_start_local_server)
    monkeypatch.setattr(chat_module, "_wait_for_server", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(chat_module, "_stop_local_server", lambda _server: None)
    monkeypatch.setattr(chat_module, "_resolve_resume_target", lambda **_kwargs: "conv_resume")
    monkeypatch.setattr(chat_module, "_bundle_agent", _fake_bundle)
    monkeypatch.setattr(chat_module, "_chat_with_server", _fake_chat_with_server)

    chat_module._chat_local(
        str(config_yaml),
        None,
        overrides=ChatOverrides(),
        initial_message="say hi",
        ephemeral=True,
        resume_latest=True,
    )

    assert captured["server_spec_path"] == agent_dir
    assert captured["server_port"] == 45678
    assert captured["server_ephemeral"] is True
    assert captured["bundle_path"] == agent_dir
    chat_args = captured["chat_args"]
    assert isinstance(chat_args, tuple)
    assert chat_args[0] == "http://127.0.0.1:45678"
    chat_kwargs = captured["chat_kwargs"]
    assert isinstance(chat_kwargs, dict)
    assert chat_kwargs["agent_yaml"] == agent_dir
    assert chat_kwargs["agent_name"] == "orchestrator"
    assert chat_kwargs["initial_message"] == "say hi"
    assert chat_kwargs["resume_conversation_id"] == "conv_resume"
    assert chat_kwargs["runner_id"] == "runner_local"
    assert chat_kwargs["session_bundle"] == b"bundle-bytes"
    skills = chat_kwargs["skills"]
    assert isinstance(skills, list)
    assert "investigate" in {skill.name for skill in skills}


def test_chat_via_daemon_hands_daemon_runner_to_chat_with_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_chat_via_daemon`` attaches the REPL to the daemon-prepared session.

    The daemon owns the runner: ``_chat_with_server`` must receive the
    daemon-spawned ``runner_id``, resume into the prepared session, and get
    ``runner_recover=None`` (no CLI-side restart).
    """
    agent_yaml = tmp_path / "hello.yaml"
    agent_yaml.write_text("name: hello\nprompt: Say hi.\n")
    captured: dict[str, object] = {}

    async def _fake_prepare(**kwargs: object) -> _DaemonChatSession:
        captured["prepare"] = kwargs
        return _DaemonChatSession(session_id="conv_daemon", runner_id="runner_daemon")

    def _fake_chat_with_server(
        server_url: str,
        tool_handler: object | None,
        *,
        runner_id: str | None = None,
        runner_recover: Callable[[], str] | None = None,
        resume_conversation_id: str | None = None,
        session_bundle: bytes | None = None,
        fork_session_id: str | None = None,
        **kwargs: object,
    ) -> None:
        captured["chat"] = {
            "server_url": server_url,
            "runner_id": runner_id,
            "runner_recover": runner_recover,
            "resume_conversation_id": resume_conversation_id,
            "session_bundle": session_bundle,
            "fork_session_id": fork_session_id,
        }

    monkeypatch.setattr(chat_module, "_bundle_agent", lambda _p: b"bundle-bytes")
    monkeypatch.setattr(
        "omnigent.host.identity.load_or_create_host_identity",
        lambda: SimpleNamespace(host_id="host_x", name="x"),
    )
    monkeypatch.setattr(chat_module, "_resolve_resume_target", lambda **_k: None)
    monkeypatch.setattr(chat_module, "_prepare_chat_session_via_daemon", _fake_prepare)
    monkeypatch.setattr(chat_module, "_chat_with_server", _fake_chat_with_server)

    _chat_via_daemon(
        str(agent_yaml),
        "https://example.databricksapps.com",
        None,
        overrides=ChatOverrides(),
        initial_message="say hi",
    )

    chat = captured["chat"]
    assert isinstance(chat, dict)
    # Daemon-owned runner + resume into the prepared session; no CLI recover.
    assert chat["runner_id"] == "runner_daemon"
    assert chat["resume_conversation_id"] == "conv_daemon"
    assert chat["runner_recover"] is None
    assert chat["fork_session_id"] is None
    # Bundle is still passed so the one-shot path takes its sessions branch.
    assert chat["session_bundle"] == b"bundle-bytes"
    # The prep was asked to create a fresh session (no resume/fork).
    prepare = captured["prepare"]
    assert isinstance(prepare, dict)
    assert prepare["resume_conversation_id"] is None
    assert prepare["fork_session_id"] is None
    assert prepare["host_id"] == "host_x"


class _FakeSessionsApi:
    """Minimal async sessions API recording create/fork for prep tests.

    :param captured: Dict the fake records ``create`` / ``fork`` calls into.
    """

    def __init__(self, captured: dict[str, object]) -> None:
        self._captured = captured

    async def create(self, bundle: bytes, *, filename: str, workspace: str) -> SimpleNamespace:
        """Record a session create and return a stub with a new id."""
        self._captured["create"] = {"workspace": workspace, "filename": filename}
        return SimpleNamespace(id="conv_created")

    async def fork(self, session_id: str) -> dict[str, str]:
        """Record a fork and return the new (fork) session id."""
        self._captured["fork"] = session_id
        return {"id": "conv_forked"}


class _FakeSdkClient:
    """Async-context-manager stand-in for ``OmnigentClient`` in prep tests.

    :param captured: Dict forwarded to the fake sessions API.
    """

    def __init__(self, captured: dict[str, object]) -> None:
        self.sessions = _FakeSessionsApi(captured)

    async def __aenter__(self) -> _FakeSdkClient:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _patch_daemon_launch(monkeypatch: pytest.MonkeyPatch, captured: dict[str, object]) -> None:
    """Stub the daemon-launch helpers + SDK client for prep tests.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param captured: Dict the stubs record their inputs into.
    """
    monkeypatch.setattr(
        "omnigent_client.OmnigentClient",
        lambda **_kw: _FakeSdkClient(captured),
    )

    async def _no_host_wait(client: object, host_id: str, *, timeout_s: float) -> None:
        return None

    async def _fake_launch(
        client: object, *, host_id: str, session_id: str, workspace: str
    ) -> str:
        captured["launch"] = {"host_id": host_id, "session_id": session_id, "workspace": workspace}
        return "runner_daemon"

    async def _no_runner_wait(client: object, runner_id: str, *, timeout_s: float) -> None:
        captured["wait_runner"] = runner_id

    async def _fake_bind(client: object, session_id: str, runner_id: str) -> None:
        captured["bind"] = {"session_id": session_id, "runner_id": runner_id}

    monkeypatch.setattr("omnigent.host.daemon_launch.wait_for_host_online", _no_host_wait)
    monkeypatch.setattr("omnigent.host.daemon_launch.launch_or_reuse_daemon_runner", _fake_launch)
    monkeypatch.setattr("omnigent.host.daemon_launch.wait_for_runner_online", _no_runner_wait)
    monkeypatch.setattr("omnigent.native_terminal.bind_session_runner", _fake_bind)


def test_prepare_chat_session_via_daemon_creates_fresh_and_launches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No resume/fork → create a fresh session, then launch the daemon runner."""
    captured: dict[str, object] = {}
    _patch_daemon_launch(monkeypatch, captured)

    prepared = asyncio.run(
        _prepare_chat_session_via_daemon(
            base_url="https://example.databricksapps.com",
            headers={},
            auth=None,
            host_id="host_x",
            bundle=b"bundle-bytes",
            resume_conversation_id=None,
            fork_session_id=None,
            workspace="/tmp/proj",
        )
    )

    assert "create" in captured  # a fresh session was created
    assert "fork" not in captured
    assert prepared.session_id == "conv_created"
    assert prepared.runner_id == "runner_daemon"
    # The runner is launched bound to the freshly-created session.
    assert captured["launch"] == {
        "host_id": "host_x",
        "session_id": "conv_created",
        "workspace": "/tmp/proj",
    }


def test_prepare_chat_session_via_daemon_resume_skips_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resume id is used as-is — no create — and the runner binds to it."""
    captured: dict[str, object] = {}
    _patch_daemon_launch(monkeypatch, captured)

    prepared = asyncio.run(
        _prepare_chat_session_via_daemon(
            base_url="https://example.databricksapps.com",
            headers={},
            auth=None,
            host_id="host_x",
            bundle=b"bundle-bytes",
            resume_conversation_id="conv_resume",
            fork_session_id=None,
            workspace="/tmp/proj",
        )
    )

    assert "create" not in captured  # resume must not create a new session
    assert prepared.session_id == "conv_resume"
    assert prepared.runner_id == "runner_daemon"
    launch = captured["launch"]
    assert isinstance(launch, dict)
    assert launch["session_id"] == "conv_resume"


def test_prepare_chat_session_via_daemon_binds_runner_to_clear_stopped_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume re-binds the runner via ``bind_session_runner`` (the PATCH chokepoint).

    ``launch_or_reuse_daemon_runner`` binds via the host-launch / online-reuse
    paths, neither of which clears the server-side ``omnigent.stopped`` marker
    — only the ``replace_runner_id`` PATCH (which ``bind_session_runner`` issues)
    does. So resuming a stopped session must call ``bind_session_runner`` with
    the launched runner id; otherwise the first turn is rejected until the
    session is un-stopped in the web UI. The marker-clearing itself is
    server-side (``replace_runner_id``); here we assert the client routes the
    bind through that chokepoint. If this fails (no ``bind`` captured / wrong
    id), the un-stop regressed.
    """
    captured: dict[str, object] = {}
    _patch_daemon_launch(monkeypatch, captured)

    asyncio.run(
        _prepare_chat_session_via_daemon(
            base_url="https://example.databricksapps.com",
            headers={},
            auth=None,
            host_id="host_x",
            bundle=b"bundle-bytes",
            resume_conversation_id="conv_resume",
            fork_session_id=None,
            workspace="/tmp/proj",
        )
    )

    # The launched runner is re-bound to the resumed session through the
    # PATCH chokepoint that clears omnigent.stopped — same pattern as
    # ``omnigent claude`` (claude_native.py's bind_session_runner call).
    assert captured["bind"] == {"session_id": "conv_resume", "runner_id": "runner_daemon"}


def test_prepare_chat_session_via_daemon_fork_wins_over_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fork id creates a child session and takes precedence over resume."""
    captured: dict[str, object] = {}
    _patch_daemon_launch(monkeypatch, captured)

    prepared = asyncio.run(
        _prepare_chat_session_via_daemon(
            base_url="https://example.databricksapps.com",
            headers={},
            auth=None,
            host_id="host_x",
            bundle=b"bundle-bytes",
            resume_conversation_id="conv_resume",
            fork_session_id="conv_parent",
            workspace="/tmp/proj",
        )
    )

    # Fork must not fall through to create; otherwise a user-requested fork
    # would silently start from a blank session instead of the parent.
    assert "create" not in captured
    # Fork takes precedence over resume and uses the requested parent id.
    assert captured["fork"] == "conv_parent"
    assert prepared.session_id == "conv_forked"
    assert prepared.runner_id == "runner_daemon"
    launch = captured["launch"]
    assert isinstance(launch, dict)
    # The daemon runner must bind to the forked child, not the parent or the
    # ignored resume id.
    assert launch["session_id"] == "conv_forked"


# ── OMNIGENT_MODEL env-var fallback ───────────────────
#
# These tests pin the env-var contract on the
# ``omnigent/cli.py`` → ``run_chat`` direct path. Without
# them, ``OMNIGENT_MODEL=foo`` was silently dropped on the
# ``omnigent`` console-script default Omnigent path because
# ``_apply_overrides_to_raw`` used the hardcoded
# ``_DEFAULT_AD_HOC_MODEL`` instead of the env-var-aware
# helper. See ``designs/RUN_OMNIGENT_REPL_PARITY.md``.


def test_default_cli_model_returns_hardcoded_default_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With ``OMNIGENT_MODEL`` unset, the helper returns the
    hardcoded ``_DEFAULT_AD_HOC_MODEL``.

    What this proves: the existing default behavior (the model
    that ships in the README example) is preserved when no env
    var is set. If this fails, users running
    ``omnigent run hello.yaml`` without setting the env var
    would suddenly land on a different model than they did
    before — silently breaking their workflows.
    """
    monkeypatch.delenv("OMNIGENT_MODEL", raising=False)
    assert _default_cli_model() == _DEFAULT_AD_HOC_MODEL


def test_default_cli_model_honors_omnigent_model_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With ``OMNIGENT_MODEL=foo`` set, the helper returns
    ``"foo"``.

    What this proves: the env-var override fires. If the helper
    returns ``_DEFAULT_AD_HOC_MODEL`` here, the env var was
    silently dropped — exactly the regression this gap closed.
    """
    monkeypatch.setenv("OMNIGENT_MODEL", "databricks-claude-sonnet-4-6")
    assert _default_cli_model() == "databricks-claude-sonnet-4-6"


def test_apply_overrides_uses_env_var_when_yaml_has_no_model_or_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A YAML that declares neither ``executor.model`` nor
    ``executor.harness``, processed with empty overrides and
    ``OMNIGENT_MODEL=foo`` set, lands with ``executor.model =
    "foo"``.

    What this proves: the env var traverses
    ``_apply_overrides_to_raw`` to the executor block. If this
    fails with the assertion showing ``databricks-gpt-5-4``
    (the hardcoded default), the helper isn't being called —
    line 756 of ``omnigent/chat.py`` reverted to the literal
    ``_DEFAULT_AD_HOC_MODEL`` and the env var is dropped again.
    """
    monkeypatch.setenv("OMNIGENT_MODEL", "databricks-claude-sonnet-4-6")
    raw: dict[str, object] = {"name": "ad_hoc", "prompt": "hi"}

    _apply_overrides_to_raw(raw, ChatOverrides())

    executor = raw["executor"]
    assert isinstance(executor, dict), (
        f"_apply_overrides_to_raw must always set ``executor`` to a dict; "
        f"got {executor!r}. If this is missing, the YAML mutation logic "
        f"regressed before the env-var fallback path was reached."
    )
    assert executor.get("model") == "databricks-claude-sonnet-4-6", (
        f"Expected env-var override 'databricks-claude-sonnet-4-6' to "
        f"land in executor.model; got {executor.get('model')!r}. If "
        f"this is 'databricks-gpt-5-4' (the hardcoded default), line "
        f"756 of omnigent/chat.py is back to the literal "
        f"_DEFAULT_AD_HOC_MODEL and OMNIGENT_MODEL is silently dropped "
        f"on the omnigent/cli.py → run_chat path."
    )


def test_apply_overrides_explicit_model_wins_over_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A ``--model`` override takes precedence over
    ``OMNIGENT_MODEL``.

    What this proves: the precedence chain is
    ``--model`` > ``executor.model`` in YAML > ``OMNIGENT_MODEL``
    > ``_DEFAULT_AD_HOC_MODEL``. If this fails, the env var is
    overriding an explicit CLI flag — surprising and broken.
    """
    monkeypatch.setenv("OMNIGENT_MODEL", "from-env")
    raw: dict[str, object] = {"name": "ad_hoc", "prompt": "hi"}

    _apply_overrides_to_raw(raw, ChatOverrides(model="from-flag"))

    executor = raw["executor"]
    assert isinstance(executor, dict)
    assert executor.get("model") == "from-flag", (
        f"--model override must win over OMNIGENT_MODEL. Got "
        f"{executor.get('model')!r}; if this is 'from-env' the "
        f"precedence chain inverted and explicit CLI args lost to "
        f"environment values — a surprising regression."
    )


def test_apply_overrides_canonicalizes_claude_harness_alias() -> None:
    """AP override materialization normalizes ``--harness claude``."""
    raw: dict[str, object] = {"name": "claude_agent", "prompt": "hi"}

    _apply_overrides_to_raw(raw, ChatOverrides(harness="claude"))

    executor = raw["executor"]
    assert isinstance(executor, dict)
    assert executor["harness"] == "claude-sdk"
    assert "model" not in executor


def test_apply_overrides_writes_nested_config_harness_for_spec_version_bundle() -> None:
    """
    ``--harness`` on a ``spec_version`` bundle lands in
    ``executor.config.harness`` — the ONLY harness location that
    format's parser reads.

    Regression guard for the polly no-op: ``omnigent run
    examples/polly --harness pi`` used to write the flat
    ``executor.harness`` key, which ``_parse_executor`` ignores for
    spec_version specs — the brain silently stayed on claude-sdk.
    """
    raw: dict[str, object] = {
        "spec_version": 1,
        "name": "polly",
        "prompt": "orchestrate",
        "executor": {
            "type": "omnigent",
            "context_window": 1000000,
            "config": {"harness": "claude-sdk", "profile": "my-profile"},
        },
    }

    _apply_overrides_to_raw(raw, ChatOverrides(harness="pi"))

    executor = raw["executor"]
    assert isinstance(executor, dict)
    config = executor["config"]
    assert isinstance(config, dict)
    assert config["harness"] == "pi", (
        f"Expected the override to replace executor.config.harness; got "
        f"{config.get('harness')!r}. If this is 'claude-sdk', the override "
        f"went to the flat executor.harness key the bundle parser ignores — "
        f"the silent no-op this fix removed."
    )
    # No dead flat key — the bundle parser would ignore it and a future
    # reader would be misled about which value wins.
    assert "harness" not in executor, (
        f"Flat executor.harness {executor.get('harness')!r} should not be "
        f"written for spec_version bundles."
    )
    # Sibling config keys survive the override.
    assert config["profile"] == "my-profile"
    # Declared harness suppresses the ad-hoc default-model fallback.
    assert "model" not in executor


def test_apply_overrides_flat_harness_creates_no_config_for_single_file_yaml() -> None:
    """
    Single-file omnigent YAMLs (no ``spec_version``) keep the flat
    ``executor.harness`` write and gain no ``config`` block.

    If a ``config`` key appears here, the spec-format detection in
    ``_apply_harness_override_to_executor`` misfired — the inner
    loader reads the flat key, and a stray ``config`` block would be
    dead weight in the materialized YAML.
    """
    raw: dict[str, object] = {"name": "codex_agent", "prompt": "hi"}

    _apply_overrides_to_raw(raw, ChatOverrides(harness="codex"))

    executor = raw["executor"]
    assert isinstance(executor, dict)
    assert executor["harness"] == "codex"
    assert "config" not in executor


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("claude", "claude-sdk"),
        ("openai-agents-sdk", "openai-agents"),
        ("pi", "pi"),
    ],
)
def test_apply_overrides_canonicalizes_alias_into_spec_version_config(
    monkeypatch: pytest.MonkeyPatch,
    alias: str,
    canonical: str,
) -> None:
    """
    Alias spellings canonicalize before landing in
    ``executor.config.harness``, so the materialized bundle always
    carries the canonical id the runtime registry dispatches on.

    ``openai-agents-sdk`` is the spelling the project docs use for
    the run examples; without the alias it fails ``--harness``
    validation outright.
    """
    # Ambient OpenAI creds would trigger env-auth baking for the
    # openai-agents case; deterministic tests must not depend on them.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    raw: dict[str, object] = {
        "spec_version": 1,
        "name": "bundle",
        "prompt": "hi",
        "executor": {"type": "omnigent", "config": {"harness": "claude-sdk"}},
    }

    _apply_overrides_to_raw(raw, ChatOverrides(harness=alias))

    executor = raw["executor"]
    assert isinstance(executor, dict)
    assert executor["config"]["harness"] == canonical, (
        f"--harness {alias!r} must canonicalize to {canonical!r} in the "
        f"materialized bundle; got {executor['config'].get('harness')!r}. "
        f"A raw alias here would fail OMNIGENT_HARNESSES validation or "
        f"miss the runtime dispatch registry."
    )


def test_apply_overrides_harness_and_model_together_for_spec_version_bundle() -> None:
    """
    ``--harness`` + ``--model`` on a bundle land in their respective
    parser-read locations: nested ``config.harness`` and flat
    ``executor.model``.

    This is the polly-on-GPT invocation shape: ``omnigent run
    examples/polly --harness openai-agents --model <gpt>``.
    """
    raw: dict[str, object] = {
        "spec_version": 1,
        "name": "polly",
        "prompt": "orchestrate",
        "executor": {"type": "omnigent", "config": {"harness": "claude-sdk"}},
    }

    _apply_overrides_to_raw(raw, ChatOverrides(harness="pi", model="databricks-claude-sonnet-4-6"))

    executor = raw["executor"]
    assert isinstance(executor, dict)
    assert executor["config"]["harness"] == "pi"
    # The bundle parser reads model from the FLAT executor.model key.
    assert executor["model"] == "databricks-claude-sonnet-4-6"


def test_apply_overrides_rejects_harness_for_non_omnigent_executor_type() -> None:
    """
    A spec_version bundle with a non-omnigent ``executor.type`` fails
    loud on ``--harness`` instead of silently no-opping.

    Those executor types have no ``config.harness``; writing one
    would recreate the ignored-override bug in a new spot.
    """
    raw: dict[str, object] = {
        "spec_version": 1,
        "name": "sdk_agent",
        "prompt": "hi",
        "executor": {"type": "claude_sdk", "model": "claude-opus-4-8"},
    }

    with pytest.raises(click.ClickException, match="claude_sdk"):
        _apply_overrides_to_raw(raw, ChatOverrides(harness="pi"))


def test_apply_overrides_skips_default_when_yaml_declares_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A YAML that declares ``executor.harness`` but no model
    must NOT have the env var injected — the harness picks its
    own default model and pairing it with the gpt-5-4 default
    breaks Databricks FM API calls.

    What this proves: the ``"harness" not in executor_block``
    guard is intact. If this fails (executor.model becomes
    "from-env"), a YAML like ``claude_code_agent.yaml`` that
    declares ``harness: claude-sdk`` would suddenly receive
    ``model: from-env`` from the env var, even though the
    harness expects to choose its own. The guard exists for
    exactly this case.
    """
    monkeypatch.setenv("OMNIGENT_MODEL", "from-env")
    raw: dict[str, object] = {
        "name": "claude_agent",
        "prompt": "hi",
        "executor": {"harness": "claude-sdk"},
    }

    _apply_overrides_to_raw(raw, ChatOverrides())

    executor = raw["executor"]
    assert isinstance(executor, dict)
    assert "model" not in executor, (
        f"executor.model should NOT be injected when harness is "
        f"declared, but got {executor.get('model')!r}. The "
        f"harness-absence guard was bypassed; a harness-only YAML "
        f"now gets paired with an env-var-driven model that the "
        f"harness's underlying API may reject."
    )


def test_materialize_override_bundle_bakes_env_var_into_yaml(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    End-to-end through ``_materialize_override_bundle``: write a
    real YAML file, set ``OMNIGENT_MODEL=foo``, materialize a
    rewritten bundle, read the result — ``executor.model``
    must be ``"foo"``.

    What this proves: the env var survives the
    ``mkdtemp`` → ``yaml.safe_load`` → ``_apply_overrides_to_raw``
    → ``yaml.safe_dump`` round-trip and lands as a real,
    on-disk override the omnigent server reads. If the
    written YAML has ``model: databricks-gpt-5-4``, the env-var
    fallback regressed somewhere in the materialization
    pipeline.
    """
    import yaml as _yaml

    monkeypatch.setenv("OMNIGENT_MODEL", "databricks-claude-sonnet-4-6")

    src = tmp_path / "ad_hoc.yaml"
    src.write_text("name: ad_hoc\nprompt: hi\n")

    materialized = _materialize_override_bundle(src, ChatOverrides())
    try:
        # The helper returns either the original path (if no
        # rewrite was needed) or a rewritten copy under a tmpdir.
        # The env-var fallback path requires a rewrite, so the
        # returned path must NOT equal the source.
        assert materialized != src, (
            "Expected _materialize_override_bundle to rewrite the YAML "
            "(env-var fallback needs the executor.model injection); "
            "got the source path back unchanged. The ``needs_fallback`` "
            "branch in _materialize_override_bundle didn't fire."
        )

        rewritten = _yaml.safe_load(materialized.read_text())
        assert rewritten["executor"]["model"] == "databricks-claude-sonnet-4-6", (
            f"Materialized YAML's executor.model is "
            f"{rewritten['executor'].get('model')!r}; expected the env-var "
            f"override 'databricks-claude-sonnet-4-6'. If this is "
            f"'databricks-gpt-5-4', the env-var read isn't surviving the "
            f"materialization round-trip."
        )
    finally:
        _cleanup_materialized_override_bundle(materialized)


def test_nested_config_harness_skips_ad_hoc_model_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A single-file spec that declares its harness under the bundle-style
    ``executor.config.harness`` (no flat ``harness:``, no ``model:``) must
    NOT trigger the ``_DEFAULT_AD_HOC_MODEL`` fallback — this is the polly
    shape (``examples/polly/config.yaml`` run as a file).

    Regression guard for the ``databricks-gpt-5-4`` injection: before
    ``_spec_declares_harness_or_model`` looked under ``config``, an unpinned
    polly loaded as a single file got force-fed the GPT ad-hoc default,
    which the claude-sdk harness can't speak. With the nested-harness check,
    ``_materialize_override_bundle`` returns the source unchanged (no rewrite,
    no injected model), letting normal provider resolution pick the model.
    The harness is ``claude-sdk`` (not OpenAI-compatible) and there are no
    overrides / ambient OpenAI creds, so the only path that could fire is the
    ad-hoc fallback.
    """
    monkeypatch.delenv("OMNIGENT_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    src = tmp_path / "config.yaml"
    src.write_text(
        "spec_version: 1\nname: nested-harness\nprompt: hi\n"
        "executor:\n  type: omnigent\n  config:\n    harness: claude-sdk\n"
    )

    materialized = _materialize_override_bundle(src, ChatOverrides())
    try:
        assert materialized == src, (
            "Expected _materialize_override_bundle to return the source "
            "unchanged for a nested executor.config.harness spec, but it "
            "rewrote the bundle — the ad-hoc-model fallback fired because "
            "_spec_declares_harness_or_model didn't recognize the nested "
            "harness. An unpinned polly would get databricks-gpt-5-4."
        )
    finally:
        if materialized != src:
            _cleanup_materialized_override_bundle(materialized)


def test_apply_overrides_skips_default_for_nested_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A nested ``executor.config.harness`` spec must NOT get the GPT
    ad-hoc model injected by ``_apply_overrides_to_raw``.

    Reproduces the Debbie breakage: ``_apply_overrides_to_raw`` used to
    run the ad-hoc fallback using a shallow ``"harness" not in
    executor_block`` check. Debbie declares its harness as
    ``executor.config.harness`` (claude-sdk), so the top-level
    ``executor`` block has no ``harness`` key and the GPT default
    (``databricks-gpt-5-4``) was force-fed onto the claude-sdk
    brain — which then sent ``anthropic/v1/messages`` to a GPT gateway
    endpoint and got a 400.

    What this proves: the fallback consults
    ``_spec_declares_harness_or_model`` (which recognizes the nested
    harness), so no ``executor.model`` is injected. If this fails
    (``executor.model`` becomes ``databricks-gpt-5-4``), the shallow guard
    regressed and the claude-sdk brain is mispaired with a GPT model.
    """
    # Even with the env-var default in play, the nested harness must
    # suppress the fallback entirely.
    monkeypatch.setenv("OMNIGENT_MODEL", "databricks-gpt-5-4")
    raw: dict[str, object] = {
        "spec_version": 1,
        "name": "debby",
        "prompt": "hi",
        "executor": {"type": "omnigent", "config": {"harness": "claude-sdk"}},
    }

    _apply_overrides_to_raw(raw, ChatOverrides())

    executor = raw["executor"]
    assert isinstance(executor, dict)
    assert "model" not in executor, (
        f"executor.model should NOT be injected for a nested "
        f"executor.config.harness spec, but got {executor.get('model')!r}. "
        f"The ad-hoc fallback fired despite a declared (nested) harness — "
        f"this is the Debby regression where the claude-sdk brain got "
        f"force-fed databricks-gpt-5-4."
    )


def test_materialize_directory_bundle_with_override_keeps_nested_harness_unpinned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A directory bundle with a nested claude-sdk harness, materialized for
    an executor-unrelated override, must come back WITHOUT a pinned GPT model.

    End-to-end reproduction of the Debby breakage through the real
    ``mkdtemp`` → ``copytree`` → ``yaml.safe_load`` →
    ``_apply_overrides_to_raw`` → ``yaml.safe_dump`` pipeline.
    ``--system-prompt`` forces materialization (any override does), and
    before the fix the rewritten ``config.yaml`` got
    ``executor.model: databricks-gpt-5-4`` baked in — which the claude-sdk
    harness then routed to the Anthropic gateway, producing the 400 the
    user saw.

    What this proves: the rewritten bundle preserves the nested harness
    and injects no model, so downstream provider/ucode resolution picks
    the correct ``databricks-claude-*`` model. If ``executor.model``
    appears (especially ``databricks-gpt-5-4``), the directory override
    path regressed.
    """
    import yaml as _yaml

    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    # The env-var default would be the injected value if the fallback
    # wrongly fired — set it to the exact bad model to make a regression
    # unmistakable.
    monkeypatch.setenv("OMNIGENT_MODEL", "databricks-gpt-5-4")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    agent_dir = tmp_path / "debby"
    agent_dir.mkdir()
    (agent_dir / "config.yaml").write_text(
        "spec_version: 1\n"
        "name: debby\n"
        "prompt: hi\n"
        "executor:\n"
        "  type: omnigent\n"
        "  config:\n"
        "    harness: claude-sdk\n"
    )

    materialized = _materialize_override_bundle(agent_dir, ChatOverrides(system_prompt="be terse"))
    try:
        assert materialized != agent_dir, (
            "Expected the directory bundle to be materialized because "
            "--system-prompt is an override; got the source dir back unchanged."
        )
        rewritten = _yaml.safe_load((materialized / "config.yaml").read_text())
        # The override that forced materialization landed.
        assert rewritten["prompt"] == "be terse", rewritten
        executor = rewritten["executor"]
        # The nested harness survives the round-trip unchanged.
        assert executor["config"]["harness"] == "claude-sdk", executor
        # The crux: no model is pinned, so the claude-sdk harness resolves a
        # Claude model via ucode/provider instead of the GPT ad-hoc default.
        assert "model" not in executor, (
            f"Materialized debby config pinned executor.model="
            f"{executor.get('model')!r}; expected none. A GPT model here "
            f"(databricks-gpt-5-4) is the regression that sent "
            f"anthropic/v1/messages to a GPT gateway endpoint."
        )
    finally:
        _cleanup_materialized_override_bundle(materialized)


@pytest.mark.parametrize("brain_harness", ["pi", "openai-agents"])
@pytest.mark.parametrize(
    ("bundle_name", "expected_workers"),
    [
        (
            "polly",
            {
                "claude_code": "claude-native",
                "codex": "codex-native",
                "pi": "pi",
            },
        ),
        ("debby", {"claude": "claude-sdk", "gpt": "codex"}),
    ],
)
def test_materialize_bundle_overrides_brain_harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    bundle_name: str,
    expected_workers: dict[str, str],
    brain_harness: str,
) -> None:
    """
    A REAL bundled orchestrator (polly / debby), materialized with
    ``--harness``, parses to a valid spec whose brain runs the requested
    harness and whose sub-agents keep their own declared harnesses.

    End-to-end through the production pipeline: ``copytree`` →
    ``_apply_overrides_to_raw`` → ``yaml.safe_dump`` → ``omnigent.spec.load``
    → ``validate``. This is the exact path ``omnigent run examples/polly
    --harness pi`` (or ``examples/debby``) takes before the bundle reaches
    a server.

    What this proves: (1) the override reaches ``executor.config.harness``
    where the bundle parser reads it — before the fix it landed on a flat
    key and the brain silently stayed claude-sdk; (2) the rewritten spec
    still validates (the harness is in OMNIGENT_HARNESSES); (3) the
    override never leaks into the sub-agents, which would break
    cross-vendor orchestration (polly's workers) and debby's claude-vs-gpt
    debate pairing.

    :param bundle_name: Packaged example bundle under
        ``omnigent.resources.examples``, e.g. ``"polly"``.
    :param expected_workers: Sub-agent name → declared harness mapping the
        override must leave untouched.
    :param brain_harness: The ``--harness`` value under test, e.g. ``"pi"``.
    """
    import importlib.resources

    # Isolate from the developer's omnigent config and ambient creds so
    # env-auth baking / model fallback can't make the result machine-dependent.
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OMNIGENT_MODEL", raising=False)

    bundle_dir = Path(
        str(importlib.resources.files("omnigent.resources.examples").joinpath(bundle_name))
    )

    materialized = _materialize_override_bundle(bundle_dir, ChatOverrides(harness=brain_harness))
    try:
        assert materialized != bundle_dir, (
            "Expected --harness to force a rewritten bundle copy; got the "
            "source dir back, meaning the override was dropped entirely."
        )

        spec = load_spec(materialized)
        assert spec.executor.config.get("harness") == brain_harness, (
            f"Parsed brain harness is {spec.executor.config.get('harness')!r}, "
            f"expected {brain_harness!r}. If this is 'claude-sdk', the "
            f"override was written to the flat executor.harness key the "
            f"spec_version parser ignores — the {bundle_name} --harness no-op."
        )
        # The bundle stays model-unpinned: the overridden harness resolves
        # its provider's default model, exactly like claude-sdk does today.
        assert spec.executor.model is None
        result = validate_spec(spec)
        assert result.valid, (
            f"Materialized {bundle_name} spec failed validation: "
            f"{[(e.path, e.message) for e in result.errors]}. The override "
            f"produced a bundle the server would reject at registration."
        )
        # Sub-agents keep their own declared harnesses — the brain override
        # must not cascade into them.
        worker_harnesses = {
            sub.name: sub.executor.config.get("harness") for sub in spec.sub_agents
        }
        assert worker_harnesses == expected_workers, (
            f"Sub-agent harnesses changed under a brain-only override: "
            f"{worker_harnesses}. The override must rewrite only the "
            f"top-level config.yaml, never agents/<name>/config.yaml."
        )
    finally:
        _cleanup_materialized_override_bundle(materialized)


def test_materialize_override_bundle_bakes_openai_env_auth_for_daemon_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    ``openai-agents`` prompt runs bake ambient OpenAI credentials into the spec.

    The daemon-owned runner intentionally does not inherit
    ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL``. This test proves the
    one-shot E2E path gets a self-contained bundle instead of a spec
    that only works when the runner sees the caller's shell env.
    """
    import yaml as _yaml

    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://gateway.example.com/openai/v1")

    src = tmp_path / "hello.yaml"
    src.write_text("name: hello\nprompt: hi\n")

    materialized = _materialize_override_bundle(
        src,
        ChatOverrides(harness="openai-agents", model="databricks-gpt-5-4-mini"),
    )

    try:
        rewritten = _yaml.safe_load(materialized.read_text())
        auth = rewritten["executor"]["auth"]
        assert auth == {
            "type": "api_key",
            "api_key": "sk-env-test",
            "base_url": "https://gateway.example.com/openai/v1",
        }, (
            f"Expected daemon-bound openai-agents run to carry explicit api_key auth; "
            f"got {auth!r}. Without this, the runner starts without provider "
            f"credentials and the one-shot stdout is empty."
        )
    finally:
        _cleanup_materialized_override_bundle(materialized)


def test_materialize_override_bundle_adds_openai_env_auth_for_directory_without_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Directory specs also materialize when only OpenAI env auth is missing.

    This covers the non-``--model`` / non-``--harness`` case: a local
    agent-image directory with ``executor.harness: openai-agents`` still
    needs a rewritten ``config.yaml`` because the daemon runner cannot
    read the CLI process's provider-secret env vars.
    """
    import yaml as _yaml

    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-dir-env-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://gateway.example.com/openai/v1")

    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "config.yaml").write_text(
        "spec_version: 1\n"
        "name: dir_agent\n"
        "instructions: hi\n"
        "executor:\n"
        "  harness: openai-agents\n"
        "  model: databricks-gpt-5-4-mini\n"
    )

    materialized = _materialize_override_bundle(agent_dir, ChatOverrides())

    try:
        assert materialized != agent_dir, (
            "Expected directory spec to be copied for auth materialization; "
            "returning the original directory leaves daemon runners dependent "
            "on stripped OPENAI_* environment variables."
        )
        rewritten = _yaml.safe_load((materialized / "config.yaml").read_text())
        assert rewritten["executor"]["auth"]["api_key"] == "sk-dir-env-test"
    finally:
        _cleanup_materialized_override_bundle(materialized)


def test_cleanup_materialized_override_bundle_removes_temp_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Cleaning a materialized OpenAI-auth spec removes its temp directory.

    :param monkeypatch: Pytest environment patch helper.
    :param tmp_path: Temporary source-spec directory.
    :returns: None.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-cleanup-test")
    src = tmp_path / "hello.yaml"
    src.write_text("name: hello\nprompt: hi\n")

    materialized = _materialize_override_bundle(
        src,
        ChatOverrides(harness="openai-agents", model="databricks-gpt-5-4-mini"),
    )
    tempdir = materialized.parent
    try:
        assert "sk-cleanup-test" in (tempdir / "hello.yaml").read_text()
    finally:
        _cleanup_materialized_override_bundle(materialized)

    assert not tempdir.exists()


def test_materialize_override_bundle_cleans_tempdir_when_directory_invalid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Failed directory materialization removes its temporary bundle copy.

    :param monkeypatch: Pytest patch helper used to pin the tempdir path.
    :param tmp_path: Temporary source-spec directory.
    :returns: None.
    """
    source = tmp_path / "bad-agent"
    source.mkdir()
    (source / "tool.py").write_text("print('copied')\n")
    tempdir = tmp_path / "materialized"

    def fake_mkdtemp(*, prefix: str) -> str:
        """
        Return a deterministic tempdir path for cleanup assertions.

        :param prefix: Requested tempdir prefix, e.g.
            ``"omnigent-override-"``.
        :returns: Filesystem path to the deterministic tempdir.
        """
        assert prefix == "omnigent-override-"
        tempdir.mkdir()
        return str(tempdir)

    monkeypatch.setattr(chat_module.tempfile, "mkdtemp", fake_mkdtemp)

    with pytest.raises(click.ClickException, match=r"directory has no config\.yaml"):
        _materialize_override_bundle(
            source,
            ChatOverrides(harness="openai-agents"),
        )

    assert not tempdir.exists()


def test_apply_overrides_keeps_explicit_openai_auth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Ambient OpenAI env auth never overwrites explicit YAML auth.

    Explicit ``executor.auth`` is the user's declared source of truth.
    If this test fails, a caller's shell env can silently reroute a spec
    that intentionally picked a different key or base URL.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-should-not-win")
    raw: dict[str, object] = {
        "name": "x",
        "prompt": "hi",
        "executor": {
            "harness": "openai-agents",
            "auth": {
                "type": "api_key",
                "api_key": "sk-explicit",
                "base_url": "https://explicit.example.com/v1",
            },
        },
    }

    _apply_overrides_to_raw(raw, ChatOverrides())

    executor = raw["executor"]
    assert isinstance(executor, dict)
    assert executor["auth"] == {
        "type": "api_key",
        "api_key": "sk-explicit",
        "base_url": "https://explicit.example.com/v1",
    }


# ── remote auth plumbing ──────────────────────────────────────────────────


def test_remote_headers_prefers_explicit_remote_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit remote bearer env var wins over ambient Databricks credentials."""
    monkeypatch.setenv("OMNIGENT_REMOTE_AUTH_TOKEN", "env-token")
    monkeypatch.setattr(
        chat_module,
        "_read_databrickscfg",
        lambda _profile: DatabricksCredentials(host="https://x", token="ambient-token"),
    )

    assert _remote_headers(server_url="https://srv.example.com") == {
        "Authorization": "Bearer env-token"
    }


def test_remote_headers_falls_back_to_ambient_databricks_creds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No env token + no stored login record → ambient Databricks credentials.

    Bottom of the resolution chain: with ``OMNIGENT_REMOTE_AUTH_TOKEN``
    unset, no stored OIDC token, and no stored Databricks Apps pointer
    record for the server, ``_remote_headers`` must fall back to
    ``_read_databrickscfg(None)`` (the SDK's ambient resolution — no
    profile is threaded anymore) and put its token in the bearer header.
    """
    monkeypatch.delenv("OMNIGENT_REMOTE_AUTH_TOKEN", raising=False)
    monkeypatch.setattr("omnigent.cli_auth.load_token", lambda _url: None)
    monkeypatch.setattr(chat_module, "_stored_databricks_record_token", lambda _url: None)
    read_calls: list[object] = []

    def _fake_read(profile: object) -> DatabricksCredentials:
        """Record the profile argument and return ambient creds."""
        read_calls.append(profile)
        return DatabricksCredentials(host="https://workspace", token="ambient-token")

    monkeypatch.setattr(chat_module, "_read_databrickscfg", _fake_read)

    headers = _remote_headers(server_url="https://srv.example.com")

    # The ambient token reached the Authorization header.
    assert headers == {"Authorization": "Bearer ambient-token"}
    # Ambient resolution: exactly one lookup, with no profile threaded
    # (None) — a non-None value here means profile plumbing came back.
    assert read_calls == [None]


def test_remote_headers_adds_org_id_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recorded ?o= selector rides every ad-hoc request.

    These probes (session info, agent pick, runner status, native
    forwarders) carry no httpx Auth, so the workspace-routing header must be
    added here or the request routes to the account. It accompanies
    whichever bearer the resolution chain produced.
    """
    monkeypatch.delenv("OMNIGENT_REMOTE_AUTH_TOKEN", raising=False)
    monkeypatch.setattr("omnigent.cli_auth.load_token", lambda _url: None)
    monkeypatch.setattr(chat_module, "_stored_databricks_record_token", lambda _url: "rec-tok")
    monkeypatch.setattr(
        "omnigent.cli_auth.load_databricks_org_id", lambda _url: "2850744067564480"
    )

    headers = _remote_headers(server_url="https://acme.databricks.com/api/2.0/omnigent")

    assert headers == {
        "Authorization": "Bearer rec-tok",
        "X-Databricks-Org-Id": "2850744067564480",
    }


def test_remote_headers_omits_org_when_no_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """No recorded ?o= selector → the request URL/headers carry no org header.

    Guards the "unchanged when nothing recorded" claim: a single-workspace
    or Databricks Apps server (no stored org id) must produce only the
    bearer, so the runtime replay never appends a routing header where none
    was recorded.
    """
    monkeypatch.delenv("OMNIGENT_REMOTE_AUTH_TOKEN", raising=False)
    monkeypatch.setattr("omnigent.cli_auth.load_token", lambda _url: None)
    monkeypatch.setattr(chat_module, "_stored_databricks_record_token", lambda _url: "rec-tok")
    monkeypatch.setattr("omnigent.cli_auth.load_databricks_org_id", lambda _url: None)

    headers = _remote_headers(server_url="https://single.databricks.com/api/2.0/omnigent")

    assert headers == {"Authorization": "Bearer rec-tok"}
    assert "X-Databricks-Org-Id" not in headers


def test_server_headers_do_not_encode_runner_affinity() -> None:
    """Runner affinity is persisted by PATCH /v1/sessions, not headers."""
    headers = chat_module._server_headers(runner_id="runner_local_test")

    assert headers == {}


def test_run_chat_remote_dispatches_without_profile_plumbing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote URL target dispatches to ``_chat_with_server`` with no profile.

    The ``--profile`` flag is gone: remote auth resolves from env token /
    stored login / ambient Databricks credentials inside the server-client
    helpers. ``run_chat`` must hand the remote path only its remaining
    kwargs — a stray ``profile`` kwarg here means the plumbing came back.
    """
    captured: dict[str, object] = {}

    def _fake_chat_with_server(
        server_url: str,
        tool_handler: object | None,
        **kwargs: object,
    ) -> None:
        """Record the remote dispatch inputs."""
        captured["server_url"] = server_url
        captured["tool_handler"] = tool_handler
        captured["kwargs"] = kwargs

    monkeypatch.setattr(chat_module, "_chat_with_server", _fake_chat_with_server)

    run_chat(
        target="https://example.databricksapps.com",
        client_tools=None,
        prompt="hello",
        resume_conversation_id="conv_123",
    )

    assert captured["server_url"] == "https://example.databricksapps.com"
    assert captured["tool_handler"] is None
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    # The user-facing inputs reach the server path intact.
    assert kwargs["initial_message"] == "hello"
    assert kwargs["resume_conversation_id"] == "conv_123"
    # No profile threading anywhere in the remote dispatch.
    assert "profile" not in kwargs, kwargs


def test_run_repl_auto_opens_conversation_when_session_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_run_repl`` opens the browser link when the REPL session id is known.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    opened: list[tuple[str, str, bool]] = []

    class _Client:
        """Async context manager stub for :class:`OmnigentClient`."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            """
            Accept client constructor args without opening a transport.

            :param args: Positional constructor args.
            :param kwargs: Keyword constructor args.
            :returns: None.
            """
            del args, kwargs

        async def __aenter__(self) -> object:
            """
            Enter the fake client context.

            :returns: The fake client object.
            """
            return self

        async def __aexit__(self, *args: object) -> None:
            """
            Exit the fake client context.

            :param args: Async context manager exit args.
            :returns: None.
            """
            del args

    async def _fake_run_repl(*args: object, **kwargs: object) -> str:
        """
        Simulate the SDK REPL discovering a conversation id on startup.

        :param args: Positional ``run_repl`` args.
        :param kwargs: Keyword ``run_repl`` args.
        :returns: Conversation id returned by the REPL.
        """
        del args
        on_session_start = kwargs["on_session_start"]
        assert callable(on_session_start)
        on_session_start("conv_abc")
        return "conv_abc"

    def _fake_open(
        *,
        base_url: str,
        conversation_id: str,
        enabled: bool,
        warn: Callable[[str], None] | None = None,
    ) -> None:
        """
        Capture the browser-open request.

        :param base_url: Omnigent server base URL.
        :param conversation_id: Conversation id passed to the opener.
        :param enabled: Whether auto-open was enabled.
        :param warn: Warning sink passed by production code.
        :returns: None.
        """
        del warn
        opened.append((base_url, conversation_id, enabled))

    monkeypatch.setattr(chat_module, "OmnigentClient", _Client)
    monkeypatch.setattr("omnigent.repl.run_repl", _fake_run_repl)
    monkeypatch.setattr(chat_module, "open_conversation_link_if_enabled", _fake_open)
    monkeypatch.setattr("omnigent.repl._tmux_pane.register_pane", lambda **kwargs: None)

    chat_module._run_repl(
        "http://127.0.0.1:8181",
        "hello",
        None,
        runner_id="runner_abc",
        session_bundle=b"bundle",
        auto_open_conversation=True,
    )

    assert opened == [("http://127.0.0.1:8181", "conv_abc", True)]


def test_run_chat_remote_still_rejects_model_harness_and_system_prompt() -> None:
    """Remote mode rejects local-only overrides like --model."""
    with pytest.raises(click.ClickException) as excinfo:
        run_chat(
            target="https://example.databricksapps.com",
            client_tools=None,
            model="databricks-gpt-5-4",
        )

    assert "--harness / --model / --system-prompt only apply to local" in excinfo.value.message


# ---------------------------------------------------------------------------
# _build_resume_parts (Click-based resume command builder)
# ---------------------------------------------------------------------------


def _make_run_context(**params: object) -> click.Context:
    """Build a Click context for the ``run`` command with the given params.

    Unspecified params fall through to the Click option defaults.

    :param params: Keyword arguments matching the ``run`` command's
        parameter names, e.g. ``target="agent.yaml"``,
        ``harness="claude-sdk"``.
    :returns: A Click context whose ``.params`` dict reflects the
        given overrides applied on top of the command's defaults.
    """
    from omnigent.cli import cli

    run_cmd = cli.commands["run"]  # type: ignore[attr-defined]
    # Start with the declared defaults, then overlay the caller's overrides.
    merged = {p.name: p.default for p in run_cmd.params}
    merged.update(params)
    ctx = click.Context(run_cmd, info_name="run", parent=click.Context(cli, info_name="omnigent"))
    ctx.params = merged
    return ctx


def test_build_resume_parts_preserves_flags() -> None:
    """Multiple non-default flags are preserved together, in declaration order."""
    ctx = _make_run_context(
        target="agent.yaml",
        harness="claude-sdk",
        model="gpt-5.4-mini",
        server="https://example.com",
    )
    with ctx:
        parts = _build_resume_parts()
    # Exact list: every non-default flag survives, ordered by the run
    # command's parameter declaration order (--harness, --model, --server).
    # A missing pair means _build_resume_parts dropped a live override;
    # an extra entry means a default leaked into the resume command.
    assert parts == [
        "omnigent",
        "run",
        "agent.yaml",
        "--harness",
        "claude-sdk",
        "--model",
        "gpt-5.4-mini",
        "--server",
        "https://example.com",
    ]


def test_build_resume_parts_strips_prompt() -> None:
    """One-shot -p/--prompt is excluded from resume parts."""
    ctx = _make_run_context(target="agent.yaml", prompt="hello world")
    with ctx:
        parts = _build_resume_parts()
    assert "--prompt" not in parts
    assert "hello world" not in parts
    assert "agent.yaml" in parts


def test_build_resume_parts_strips_fork() -> None:
    """--fork is excluded from resume parts."""
    ctx = _make_run_context(target="agent.yaml", fork_session_id="conv_old")
    with ctx:
        parts = _build_resume_parts()
    assert "--fork" not in parts
    assert "conv_old" not in parts


def test_build_resume_parts_strips_continue() -> None:
    """-c/--continue is excluded from resume parts."""
    ctx = _make_run_context(target="agent.yaml", resume_latest=True)
    with ctx:
        parts = _build_resume_parts()
    assert "--continue" not in parts


def test_build_resume_parts_strips_resume() -> None:
    """Existing --resume value is excluded (replaced by caller)."""
    ctx = _make_run_context(target="agent.yaml", resume="conv_old")
    with ctx:
        parts = _build_resume_parts()
    assert "--resume" not in parts
    assert "conv_old" not in parts


def test_build_resume_parts_includes_harness_and_model() -> None:
    """--harness and --model are preserved."""
    ctx = _make_run_context(target="agent.yaml", harness="claude-sdk", model="gpt-5.4-mini")
    with ctx:
        parts = _build_resume_parts()
    assert "--harness" in parts
    assert "claude-sdk" in parts
    assert "--model" in parts
    assert "gpt-5.4-mini" in parts


def test_build_resume_parts_omits_defaults() -> None:
    """Params left at their default value are omitted."""
    ctx = _make_run_context(target="agent.yaml")
    with ctx:
        parts = _build_resume_parts()
    # Only the command path + the target.
    assert parts == ["omnigent", "run", "agent.yaml"]


# ---------------------------------------------------------------------------
# _run_repl resume hint integration
# ---------------------------------------------------------------------------


def _stub_run_repl_deps(
    monkeypatch: pytest.MonkeyPatch,
    *,
    conversation_id: str | None,
    captured_kwargs: dict[str, object] | None = None,
) -> None:
    """Stub the heavy dependencies of ``_run_repl`` so it can run in tests.

    Replaces ``run_repl`` (the async REPL), ``OmnigentClient``
    (the HTTP client), and ``register_pane`` (tmux integration)
    with lightweight fakes.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param conversation_id: The value ``run_repl`` should return —
        ``None`` simulates an immediate exit with no conversation.
    :param captured_kwargs: When set, the fake ``run_repl`` stores
        its keyword arguments into this dict for inspection.
    """

    async def _fake_run_repl(*_args: object, **kwargs: object) -> str | None:
        if captured_kwargs is not None:
            captured_kwargs.update(kwargs)
        return conversation_id

    # run_repl is lazily imported inside _run_repl as
    # ``from omnigent.repl import run_repl``, which reads the
    # package attribute. Patch both the package and the source
    # module so the lazy import picks up our fake regardless of
    # which reference Python resolves.
    import omnigent.repl as _repl_pkg

    monkeypatch.setattr(_repl_pkg, "run_repl", _fake_run_repl)
    monkeypatch.setattr("omnigent.repl._repl.run_repl", _fake_run_repl)
    monkeypatch.setattr("omnigent.chat.OmnigentClient", _FakeClientCtx)
    monkeypatch.setattr("omnigent.chat._server_auth", lambda server_url=None: None)
    monkeypatch.setattr(
        "omnigent.repl._tmux_pane.register_pane",
        lambda **_kw: None,
    )


def test_run_repl_passes_resume_parts_to_run_repl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_run_repl threads resume_parts to run_repl."""
    captured: dict[str, object] = {}
    _stub_run_repl_deps(monkeypatch, conversation_id="conv_1", captured_kwargs=captured)
    parts = ["omnigent", "run", "agent.yaml", "--server", "https://example.com"]

    chat_module._run_repl(
        "http://127.0.0.1:9999",
        "hello_world",
        None,
        resume_parts=parts,
    )

    assert captured["resume_parts"] == parts


def test_run_repl_passes_ephemeral_to_run_repl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_run_repl threads ephemeral flag to run_repl."""
    captured: dict[str, object] = {}
    _stub_run_repl_deps(monkeypatch, conversation_id="conv_1", captured_kwargs=captured)

    chat_module._run_repl(
        "http://127.0.0.1:9999",
        "hello_world",
        None,
        ephemeral=True,
    )

    assert captured["ephemeral"] is True


def test_run_repl_ephemeral_defaults_to_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_run_repl passes ephemeral=False by default."""
    captured: dict[str, object] = {}
    _stub_run_repl_deps(monkeypatch, conversation_id="conv_1", captured_kwargs=captured)

    chat_module._run_repl(
        "http://127.0.0.1:9999",
        "hello_world",
        None,
    )

    assert captured["ephemeral"] is False


class _FakeSessionsForResume:
    """Minimal sessions namespace for resume lookup tests."""

    def __init__(self, rows: list[object]) -> None:
        self.rows = rows
        self.last_kwargs: dict[str, object] | None = None

    async def list(self, **kwargs: object) -> list[object]:
        self.last_kwargs = kwargs
        return self.rows


class _FakeResumeClient:
    """Minimal OmnigentClient-like object exposing sessions."""

    def __init__(self, rows: list[object]) -> None:
        self.sessions = _FakeSessionsForResume(rows)


@pytest.mark.asyncio
async def test_resolve_latest_conversation_id_async_scopes_by_agent_name() -> None:
    """``--continue`` finds session-scoped agents by YAML name, not template id."""
    row = SimpleNamespace(id="conv_session_scoped")
    client = _FakeResumeClient([row])

    resolved = await chat_module._resolve_latest_conversation_id_async(
        client=client,
        agent_name="duplicate_yaml_name",
    )

    assert resolved == "conv_session_scoped"
    assert client.sessions.last_kwargs == {
        "agent_name": "duplicate_yaml_name",
        "limit": 1,
        "order": "desc",
        "sort_by": "updated_at",
    }


@pytest.mark.asyncio
async def test_resolve_latest_conversation_id_async_returns_none_for_unknown_name() -> None:
    """No same-name session rows means there is nothing to continue."""
    client = _FakeResumeClient([])

    resolved = await chat_module._resolve_latest_conversation_id_async(
        client=client,
        agent_name="never_run",
    )

    assert resolved is None
    assert client.sessions.last_kwargs == {
        "agent_name": "never_run",
        "limit": 1,
        "order": "desc",
        "sort_by": "updated_at",
    }


# ---------------------------------------------------------------------------
# Helpers for _run_repl resume-hint tests
# ---------------------------------------------------------------------------


class _FakeClientCtx:
    """Minimal OmnigentClient stand-in that works as an async context manager.

    Yields itself from ``async with`` — no real connection is opened.
    """

    def __init__(self, **_kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _FakeClientCtx:
        return self

    async def __aexit__(self, *_args: object) -> None:
        pass


def _first_auth_header(auth: httpx.Auth, url: str) -> str | None:
    """
    Drive a single-yield ``httpx.Auth.auth_flow`` and return the
    ``Authorization`` header it set (without sending a response).

    :param auth: The auth instance under test.
    :param url: Request URL, e.g. ``"https://ex.databricks.com/v1/x"``.
    :returns: The Authorization header value, or ``None`` if unset.
    """
    flow = auth.auth_flow(httpx.Request("GET", url))
    request = next(flow)
    flow.close()
    return request.headers.get("Authorization")


def test_databricks_token_auth_resolves_sdk_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_DatabricksTokenAuth`` resolves Databricks SDK auth ONCE and reuses it
    across requests, instead of rebuilding ``Config`` + shelling out to the
    Databricks CLI per request.

    Regression guard for the per-request auth tax on the long-lived
    transcript-forwarder client (the web-UI reply-persist path): if the auth
    regresses to per-request resolution, ``resolve_calls`` jumps from 1 to
    the number of requests and this fails.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    import omnigent.inner.databricks_executor as dbx

    class _CountingConfig:
        """Config double whose authenticate() counts calls."""

        def __init__(self) -> None:
            self.authenticate_calls = 0

        def authenticate(self) -> dict[str, str]:
            self.authenticate_calls += 1
            return {"Authorization": "Bearer tok-xyz"}

    cfg = _CountingConfig()
    resolve_calls = {"n": 0}

    def _fake_resolve(
        profile: str | None = None, *, host: str | None = None
    ) -> tuple[object, str]:
        """Stand in for _resolve_databricks_auth; counts resolutions."""
        resolve_calls["n"] += 1
        # No stored pointer record (load_databricks_workspace_host → None)
        # means ambient SDK resolution: neither a profile nor a host is
        # threaded into the resolver anymore.
        assert profile is None and host is None, (profile, host)
        return dbx._DatabricksBearerAuth(cfg, profile_name=None), "https://ex.databricks.com"

    monkeypatch.setattr(dbx, "_resolve_databricks_auth", _fake_resolve)
    monkeypatch.delenv(chat_module._REMOTE_AUTH_TOKEN_ENV, raising=False)  # skip static path
    monkeypatch.setattr("omnigent.cli_auth.load_token", lambda _url: None)  # skip OIDC path
    # No Databricks Apps pointer record stored for this server → the auth
    # falls through to ambient SDK resolution rather than host-keyed lookup.
    monkeypatch.setattr("omnigent.cli_auth.load_databricks_workspace_host", lambda _url: None)

    auth = chat_module._DatabricksTokenAuth(server_url="https://ex.databricks.com")

    headers = [_first_auth_header(auth, "https://ex.databricks.com/v1/x") for _ in range(4)]

    # Every request gets the bearer from the reused SDK auth.
    assert headers == ["Bearer tok-xyz"] * 4
    # THE FIX: SDK auth resolved exactly once across 4 requests. A value > 1
    # means per-request resolution (the per-request Databricks CLI auth tax)
    # has regressed on the forwarder client.
    assert resolve_calls["n"] == 1, (
        f"_resolve_databricks_auth called {resolve_calls['n']}x; expected 1 "
        f"(resolve-once-and-cache)."
    )
    # authenticate() runs per request (4) — cheap in-memory SDK cache hits,
    # NOT CLI shell-outs. That's the behavior the fix preserves.
    assert cfg.authenticate_calls == 4


def test_databricks_token_auth_sets_org_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every SDK-client request carries the workspace-routing header.

    The header is set at the top of ``auth_flow`` — independent of which
    credential branch runs — so the workspace-routing signal rides even when
    a request carries no bearer.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.delenv(chat_module._REMOTE_AUTH_TOKEN_ENV, raising=False)
    monkeypatch.setattr("omnigent.cli_auth.load_token", lambda _url: None)
    monkeypatch.setattr(
        "omnigent.cli_auth.databricks_org_id_headers",
        lambda _url: {"X-Databricks-Org-Id": "2850744067564480"},
    )
    # Isolate from real Databricks SDK resolution: the bearer is irrelevant
    # here — only the routing header is under test.
    monkeypatch.setattr(chat_module._DatabricksTokenAuth, "_sdk_token", lambda self: None)

    auth = chat_module._DatabricksTokenAuth(
        server_url="https://acme.databricks.com/api/2.0/omnigent"
    )
    flow = auth.auth_flow(
        httpx.Request("GET", "https://acme.databricks.com/api/2.0/omnigent/v1/sessions")
    )
    request = next(flow)
    flow.close()

    assert request.headers["X-Databricks-Org-Id"] == "2850744067564480"


# ── _spec_used_families (startup-header creds line) ──────


def test_spec_used_families_multi_vendor_directory_agent(tmp_path) -> None:
    """A directory agent's families include its sub-agents' harnesses.

    Proves the data behind the startup header's per-family creds line: an
    orchestrator on ``claude-sdk`` (anthropic) with a sub-agent on
    ``codex-native`` (openai) surfaces BOTH families — so the header can
    say "Claude -> ... . Codex -> ...". A regression that stopped walking
    sub-agents (or lost the native-harness family mapping) would drop
    openai and the creds line would silently omit Codex.
    """
    root = tmp_path / "orchestrator"
    (root / "agents" / "worker").mkdir(parents=True)
    (root / "config.yaml").write_text(
        "spec_version: 1\n"
        "name: orchestrator\n"
        "prompt: orchestrate\n"
        "executor:\n"
        "  type: omnigent\n"
        "  config:\n"
        "    harness: claude-sdk\n"
        "tools:\n"
        "  agents:\n"
        "    - worker\n"
    )
    (root / "agents" / "worker" / "config.yaml").write_text(
        "spec_version: 1\n"
        "name: worker\n"
        "prompt: work\n"
        "executor:\n"
        "  type: omnigent\n"
        "  config:\n"
        "    harness: codex-native\n"
    )

    # Both the orchestrator (anthropic) and its sub-agent (openai) count,
    # whether the caller passes the config.yaml or the directory itself
    # (the latter is what `omnigent run <dir>` threads through).
    assert _spec_used_families(root / "config.yaml") == ["anthropic", "openai"]
    assert _spec_used_families(root) == ["anthropic", "openai"]


def test_spec_used_families_degrades_gracefully() -> None:
    """``_spec_used_families`` returns ``[]`` for the no-spec / standalone cases.

    Proves the best-effort guard: a ``None`` path (remote-URL target) and a
    standalone single-file agent (a path NOT named ``config.yaml`` — no
    sub-agent directory to parse) both yield an empty list, so the header
    simply omits the creds line rather than raising at REPL boot.
    """
    assert _spec_used_families(None) == []
    assert _spec_used_families(Path("examples/standalone-agent.yaml")) == []


# ── _await_accounts_first_run_setup (accounts first-run wait-and-continue) ──


def _info_response(payload: dict[str, object]) -> SimpleNamespace:
    """Build a stub httpx response whose ``.json()`` returns *payload*.

    :param payload: The ``/v1/info`` body, e.g.
        ``{"accounts_enabled": True, "needs_setup": True}``.
    :returns: An object exposing ``.json()`` like ``httpx.Response``.
    """
    return SimpleNamespace(json=lambda: payload)


def test_await_accounts_setup_noop_when_token_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CLI that already holds a token for the server does not probe/wait."""
    monkeypatch.setattr("omnigent.cli_auth.load_token", lambda _url: "existing-token")

    def _must_not_probe(*_a: object, **_k: object) -> object:
        raise AssertionError("must not call /v1/info when a token already exists")

    monkeypatch.setattr("omnigent.chat.httpx.get", _must_not_probe)

    chat_module._await_accounts_first_run_setup("http://127.0.0.1:8000")


def test_await_accounts_setup_noop_for_header_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the server is not in accounts mode there is no admin to wait for."""
    monkeypatch.setattr("omnigent.cli_auth.load_token", lambda _url: None)
    monkeypatch.setattr(
        "omnigent.chat.httpx.get",
        lambda _url, timeout=5.0: _info_response(
            {"accounts_enabled": False, "needs_setup": False}
        ),
    )

    def _must_not_sleep(_s: float) -> None:
        raise AssertionError("must not poll in header mode")

    monkeypatch.setattr("omnigent.chat.time.sleep", _must_not_sleep)

    chat_module._await_accounts_first_run_setup("http://127.0.0.1:8000")


def test_await_accounts_setup_waits_then_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accounts first-run: block until the admin-claim mints the CLI token.

    The token is absent on the initial check and the first poll, then
    appears (the operator created the admin in the browser, and
    ``/auth/setup`` minted the loopback token). The helper must then
    return so ``run`` continues into the REPL.
    """
    calls: dict[str, int] = {"n": 0}

    def _load(_url: str) -> str | None:
        calls["n"] += 1
        # None on the pre-check and the first poll; token on the second poll.
        return None if calls["n"] <= 2 else "minted-token"

    monkeypatch.setattr("omnigent.cli_auth.load_token", _load)
    monkeypatch.setattr(
        "omnigent.chat.httpx.get",
        lambda _url, timeout=5.0: _info_response({"accounts_enabled": True, "needs_setup": True}),
    )
    monkeypatch.setattr("omnigent.chat.time.sleep", lambda _s: None)

    chat_module._await_accounts_first_run_setup("http://127.0.0.1:8000")

    assert calls["n"] >= 3  # pre-check + at least two polls before the token landed


def test_await_accounts_setup_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the admin is never created, the wait fails loud (no hang/traceback)."""
    monkeypatch.setattr("omnigent.cli_auth.load_token", lambda _url: None)
    monkeypatch.setattr(
        "omnigent.chat.httpx.get",
        lambda _url, timeout=5.0: _info_response({"accounts_enabled": True, "needs_setup": True}),
    )
    monkeypatch.setattr("omnigent.chat.time.sleep", lambda _s: None)

    with pytest.raises(click.ClickException, match="Timed out"):
        chat_module._await_accounts_first_run_setup("http://127.0.0.1:8000", timeout_s=0.0)


def test_run_attach_errors_loud_when_host_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_attach`` fails loud — and never connects — when the session has no
    online runner (the host is offline). ``attach`` must never start one."""
    monkeypatch.setattr(
        chat_module,
        "_attach_session_info",
        lambda **_kw: chat_module._AttachSessionInfo(
            runner_online=False, agent_name="nessie", harness="codex"
        ),
    )
    connected = False

    def _must_not_connect(*_args: object, **_kwargs: object) -> None:
        """Fail the test if attach reaches the connect/REPL path with no runner."""
        nonlocal connected
        connected = True

    monkeypatch.setattr(chat_module, "_chat_with_server", _must_not_connect)

    with pytest.raises(click.ClickException, match="no online runner"):
        chat_module.run_attach(base_url="http://localhost:8000", conversation_id="conv_x")

    assert connected is False, "attach connected despite the host being offline"


def test_run_attach_connects_post_only_without_binding_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_attach`` connects post-only (``attach_only``) once the host runner is
    online — it never owns or binds a runner, so cross-user co-drive (where
    binding is owner-only) works and turns route to the host's runner."""
    monkeypatch.setattr(
        chat_module,
        "_attach_session_info",
        lambda **_kw: chat_module._AttachSessionInfo(
            runner_online=True, agent_name="nessie", harness="codex"
        ),
    )
    captured: dict[str, object] = {}

    def _capture(base_url: str, _tool_handler: object, **kwargs: object) -> None:
        """Record the _chat_with_server call without opening a REPL."""
        captured["base_url"] = base_url
        captured.update(kwargs)

    monkeypatch.setattr(chat_module, "_chat_with_server", _capture)

    chat_module.run_attach(
        base_url="http://localhost:8000/",
        conversation_id="conv_abc",
    )

    # Trailing slash stripped; the live conversation is resumed (not created).
    assert captured["base_url"] == "http://localhost:8000"
    assert captured["resume_conversation_id"] == "conv_abc"
    # Post-only co-drive: attach_only is set and NO runner is bound or owned
    # (binding is owner-only server-side; posting needs only edit access).
    assert captured["attach_only"] is True
    assert "runner_id" not in captured
    assert "runner_recover" not in captured
    # Session-honest banner inputs: the session's own agent name (so we skip the
    # server agent-picker + its "Agent: …" echo) and the host's harness.
    assert captured["agent_name"] == "nessie"
    assert captured["attach_harness"] == "codex"


def test_run_attach_uses_session_snapshot_runner_online(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attach fails loud when the session snapshot says the runner is offline.

    ``GET /v1/sessions/{id}`` is session-scoped: the caller has already been
    authorized for that session, so its ``runner_online`` field is the right
    liveness source for cross-user co-drive. The owner-scoped
    ``/v1/runners/{id}/status`` endpoint must not be needed.
    """

    class _Resp:
        def __init__(self, status_code: int, payload: dict[str, object]) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict[str, object]:
            return self._payload

    def _fake_get(url: str, *, headers: dict[str, str], timeout: float) -> _Resp:
        if url.endswith("/v1/sessions/conv_x"):
            return _Resp(
                200,
                {
                    "runner_id": "runner_z",
                    "runner_online": False,
                    "agent_name": "nessie",
                    "harness": "claude-sdk",
                },
            )
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr("omnigent.chat.httpx.get", _fake_get)
    connected = False

    def _must_not_connect(*_args: object, **_kwargs: object) -> None:
        """Fail the test if attach reaches the connect/REPL path with a dead runner."""
        nonlocal connected
        connected = True

    monkeypatch.setattr(chat_module, "_chat_with_server", _must_not_connect)

    with pytest.raises(click.ClickException, match="no online runner"):
        chat_module.run_attach(
            base_url="https://app.example.databricksapps.com",
            conversation_id="conv_x",
        )

    # Offline runner ⇒ loud failure with relaunch guidance, never a silent
    # optimistic attach.
    assert connected is False, (
        "attach connected despite the session snapshot reporting runner_online=false"
    )


def test_run_attach_does_not_probe_owner_scoped_runner_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Older snapshots without ``runner_online`` stay optimistic.

    The runner-status endpoint intentionally reports another user's runner as
    offline to prevent enumeration. Attach supports shared-session co-drive, so
    it must not fall back to that owner-scoped endpoint when the session
    snapshot lacks liveness; new servers put session-scoped liveness directly
    on ``GET /v1/sessions/{id}``.
    """

    class _Resp:
        def __init__(self, status_code: int, payload: dict[str, object]) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict[str, object]:
            return self._payload

    def _fake_get(url: str, *, headers: dict[str, str], timeout: float) -> _Resp:
        if url.endswith("/v1/sessions/conv_shared"):
            return _Resp(
                200,
                {"runner_id": "runner_alice", "agent_name": "nessie", "harness": "codex"},
            )
        raise AssertionError(f"owner-scoped runner status probe leaked through: {url}")

    monkeypatch.setattr("omnigent.chat.httpx.get", _fake_get)
    captured: dict[str, object] = {}

    def _capture(base_url: str, _tool_handler: object, **kwargs: object) -> None:
        """Record attach hand-off kwargs without opening a REPL."""
        captured["base_url"] = base_url
        captured.update(kwargs)

    monkeypatch.setattr(chat_module, "_chat_with_server", _capture)

    chat_module.run_attach(
        base_url="https://app.example.databricksapps.com",
        conversation_id="conv_shared",
    )

    assert captured["resume_conversation_id"] == "conv_shared"
    assert captured["attach_only"] is True


# ── _query_sessions_once reconcile + _persisted_turn_text ───────────
#
# Regression coverage for the headless ``-p`` bug: a transport-level
# runner disconnect publishes ``session.status: failed`` for the session
# even after the turn completed and persisted its response, and the
# no-replay SSE subscription can additionally miss the terminal
# ``response.completed`` event. Both leave ``SessionsChat.query``
# raising / returning empty while the assistant text is safely persisted
# server-side. ``_query_sessions_once`` must reconcile against the
# transcript instead of surfacing a spurious "turn failed".


def _item_user(text: str) -> dict[str, object]:
    """
    Build a persisted user-message item in the flat API shape.

    :param text: The user prompt text, e.g. ``"say hi"``.
    :returns: An item dict matching ``ConversationItem.to_api_dict()``
        for a user message.
    """
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }


def _item_assistant(text: str, *, status: str = "completed") -> dict[str, object]:
    """
    Build a persisted assistant-message item in the flat API shape.

    :param text: The assistant output text, e.g. ``"hi there"``.
    :param status: The item status, e.g. ``"completed"`` (default) or
        ``"incomplete"`` for a turn that errored mid-stream.
    :returns: An item dict matching ``ConversationItem.to_api_dict()``
        for an assistant message.
    """
    return {
        "id": "msg_assistant",
        "response_id": "resp_x",
        "type": "message",
        "status": status,
        "role": "assistant",
        "model": "test-model",
        "content": [{"type": "output_text", "text": text}],
    }


def _item_error(message: str) -> dict[str, object]:
    """
    Build a persisted terminal ``error`` item (harness start-failure shape).

    :param message: The error text, e.g.
        ``"inner executor error: Failed to start cursor-sdk agent: ..."``.
    :returns: An item dict matching the flat API shape for an error item.
    """
    return {
        "type": "error",
        "status": "completed",
        "source": "execution",
        "code": "RuntimeError",
        "message": message,
    }


class _FakeSessionsNamespace:
    """
    Minimal stand-in for ``client.sessions`` used by the reconcile tests.

    Only the methods ``_query_sessions_once`` / ``_persisted_turn_text``
    actually touch are implemented; each returns a real value (never a
    MagicMock) so an unexpected call surfaces loudly.

    :param items: The transcript ``list_items`` should return, e.g.
        ``[_item_user("hi"), _item_assistant("hello")]``.
    :param list_items_must_not_be_called: When ``True``, ``list_items``
        raises — used to prove the success path never reconciles.
    """

    def __init__(
        self,
        items: list[dict[str, object]],
        *,
        list_items_must_not_be_called: bool = False,
    ) -> None:
        self._items = items
        self._forbid_list_items = list_items_must_not_be_called
        self.list_items_calls = 0

    async def create(self, bundle: bytes, *, filename: str, workspace: str) -> SimpleNamespace:
        """Pretend to create a session; return an object with an ``id``."""
        return SimpleNamespace(id="conv_test")

    async def bind_runner(self, session_id: str, *, runner_id: str) -> SimpleNamespace:
        """Pretend to bind a runner; echo back the session id."""
        return SimpleNamespace(id=session_id)

    async def list_items(
        self, session_id: str, *, limit: int, order: str
    ) -> list[dict[str, object]]:
        """
        Return the controlled transcript honoring ``order``.

        Test fixtures author ``self._items`` chronologically (oldest
        first), matching how a reader thinks about a conversation.
        Production fetches ``order="desc"`` (newest first) so the
        reconcile window tracks the latest turn, so this mirrors the
        real server by reversing for ``desc``.

        :param session_id: Session id (unused; single-session stub).
        :param limit: Max items (unused; fixtures are small).
        :param order: ``"asc"`` (chronological) or ``"desc"`` (newest
            first).
        :returns: The transcript items in the requested order.
        """
        self.list_items_calls += 1
        if self._forbid_list_items:
            raise AssertionError(
                "list_items must NOT be read when the live turn returned "
                "text — reconcile is a failure-only fallback."
            )
        return list(reversed(self._items)) if order == "desc" else list(self._items)


class _FakeAPClient:
    """
    Real stub client exposing only the surface the reconcile path uses.

    :param items: Transcript returned by ``sessions.list_items``.
    :param list_items_must_not_be_called: Forwarded to the sessions
        namespace to assert the success path skips reconcile.
    """

    def __init__(
        self,
        items: list[dict[str, object]],
        *,
        list_items_must_not_be_called: bool = False,
    ) -> None:
        self.sessions = _FakeSessionsNamespace(
            items, list_items_must_not_be_called=list_items_must_not_be_called
        )
        self.files = SimpleNamespace(
            for_session=lambda _sid: SimpleNamespace(upload=None, get=None)
        )

    async def _fetch_agent_tools(
        self, agent_id: str, session_id: str | None = None
    ) -> list[dict[str, object]]:
        """No spec-declared tools — the reconcile tests pass no tool callables."""
        return []


def _fake_sessions_chat_cls(
    query_impl: Callable[[str], object],
    *,
    extra_turns: list[str] | None = None,
) -> type:
    """
    Build a ``SessionsChat`` replacement whose ``query`` is ``query_impl``.

    The constructor accepts (and ignores) the real keyword arguments
    ``_query_sessions_once`` passes, so only the ``query`` boundary —
    the spot where a spurious ``failed`` raises or a missed completion
    returns empty — is controlled by the test.

    :param query_impl: Async callable taking the prompt and returning a
        :class:`QueryResult` or raising, e.g. one that raises
        ``OmnigentError("turn failed")``.
    :param extra_turns: Optional list of text strings to return from
        successive ``await_turn()`` calls, simulating async orchestrator
        auto-wakes. When exhausted ``await_turn`` returns empty text and
        ``last_turn_saw_waiting`` returns ``False``.
    :returns: A class usable as a drop-in for ``SessionsChat``.
    """
    _extra = list(extra_turns or [])

    class _FakeSessionsChat:
        def __init__(self, **_kwargs: object) -> None:
            self._pending = list(_extra)

        @property
        def status(self) -> str:
            # Mirrors the real snapshot: "running" while sub-agents are pending
            # (the runner emits "waiting" → relay collapses to "running"),
            # "idle" when done.
            return "running" if self._pending else "idle"

        async def refresh(self) -> None:
            pass  # status is derived from _pending; no fetch needed.

        async def query(self, prompt: str) -> QueryResult:
            return await query_impl(prompt)  # type: ignore[return-value]

        async def await_turn(self, *, timeout: float | None = None) -> QueryResult:
            if self._pending:
                text = self._pending.pop(0)
                return QueryResult(text=text, files=[])
            return QueryResult(text="", files=[])

    return _FakeSessionsChat


async def _raise_turn_failed(_prompt: str) -> QueryResult:
    """Simulate the spurious transport ``failed`` raise."""
    raise ClientOmnigentError("turn failed")


async def _raise_genuine_failure(_prompt: str) -> QueryResult:
    """Simulate a real setup/auth failure that persists no output."""
    raise ClientOmnigentError("auth misconfigured")


async def _return_empty(_prompt: str) -> QueryResult:
    """Simulate a missed ``response.completed`` (no-replay race)."""
    return QueryResult(text="", files=[])


async def _return_text(_prompt: str) -> QueryResult:
    """Simulate a normal, fully-observed completion."""
    return QueryResult(text="direct answer", files=[])


async def _run_one_shot(
    client: _FakeAPClient,
    query_impl: Callable[[str], object],
    monkeypatch: pytest.MonkeyPatch,
) -> str | None:
    """
    Drive ``_query_sessions_once`` with a faked ``SessionsChat.query``.

    :param client: The fake Omnigent client supplying the transcript.
    :param query_impl: The async ``query`` behavior to install.
    :param monkeypatch: pytest monkeypatch fixture.
    :returns: Whatever ``_query_sessions_once`` returns.
    """
    # chat.py does ``from omnigent_client import SessionsChat`` inside
    # the function, so patch the attribute on the package (resolved at
    # call time), not a chat-module-local alias.
    monkeypatch.setattr("omnigent_client.SessionsChat", _fake_sessions_chat_cls(query_impl))
    return await _query_sessions_once(
        client=client,
        agent_name="hello_world",
        tool_handler=None,
        prompt="say hi",
        session_bundle=b"bundle-bytes",
        session_bundle_filename="agent.tar.gz",
        runner_id="runner_test",
    )


async def test_query_sessions_once_reconciles_persisted_text_on_failed_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spurious ``failed`` after a completed turn returns the persisted text.

    This is the exact reported bug: ``omnigent run -p`` printed
    "Error: turn failed" while the remote session held the response. If
    this fails, the ``-p`` path is again raising on a transport-induced
    ``session.status: failed`` instead of recovering the saved answer.
    """
    client = _FakeAPClient([_item_user("say hi"), _item_assistant("hi there")])
    result = await _run_one_shot(client, _raise_turn_failed, monkeypatch)
    assert result == "hi there"  # the persisted assistant text, not an error
    # Exactly one transcript read — the failure-only reconcile fallback.
    assert client.sessions.list_items_calls == 1


async def test_query_sessions_once_reconciles_persisted_text_on_empty_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missed ``response.completed`` (empty result) recovers the persisted text.

    If this fails, a turn whose completion event was lost to the
    no-replay subscribe race would print nothing even though the runner
    persisted the assistant message.
    """
    client = _FakeAPClient([_item_user("say hi"), _item_assistant("hello from runner")])
    result = await _run_one_shot(client, _return_empty, monkeypatch)
    assert result == "hello from runner"


async def test_query_sessions_once_reraises_when_no_persisted_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine failure with no persisted assistant output still surfaces.

    The transcript has only the user message — the turn produced
    nothing. If this fails, real auth/setup failures would be silently
    swallowed as empty output instead of raising.
    """
    client = _FakeAPClient([_item_user("say hi")])
    with pytest.raises(ClientOmnigentError, match="auth misconfigured"):
        await _run_one_shot(client, _raise_genuine_failure, monkeypatch)


async def test_query_sessions_once_surfaces_persisted_error_when_no_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn that produced no assistant text but persisted a terminal error
    (e.g. cursor's invalid-model start failure) surfaces that error instead of
    returning None. If this fails, headless ``-p`` renders a failed turn as a
    silent, exit-0 empty success.
    """
    client = _FakeAPClient(
        [
            _item_user("say hi"),
            _item_error("inner executor error: Failed to start cursor-sdk agent: bad model"),
        ]
    )
    with pytest.raises(ClientOmnigentError, match="Failed to start cursor-sdk agent"):
        await _run_one_shot(client, _return_empty, monkeypatch)


async def test_query_sessions_once_returns_none_when_no_text_and_no_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No assistant text and no persisted error item → None (the caller prints
    nothing). Guards against the error-surfacing path raising spuriously when a
    turn genuinely produced nothing and recorded no error.
    """
    client = _FakeAPClient([_item_user("unanswered")])
    result = await _run_one_shot(client, _return_empty, monkeypatch)
    assert result is None


async def test_query_sessions_once_returns_text_without_reconcile_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The normal completion path returns the live text and skips the transcript.

    If this fails (``list_items`` was called), the wrapper is reconciling
    on every turn, adding a needless round-trip to the happy path.
    """
    client = _FakeAPClient([], list_items_must_not_be_called=True)
    result = await _run_one_shot(client, _return_text, monkeypatch)
    assert result == "direct answer"
    assert client.sessions.list_items_calls == 0  # no reconcile on success


async def test_query_sessions_once_multi_turn_async_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extra auto-woken turns are collected and joined with the first turn's text.

    Simulates an async orchestrator (e.g. polly) that dispatches sub-agents
    in turn 1, then is auto-woken for turn 2 when they complete. The headless
    ``-p`` path must not exit after turn 1 — it must follow the session until
    idle and concatenate all turns.

    If this fails, multi-turn orchestrators like polly will always produce
    partial output (only turn 1's narration, never the final synthesis).
    """
    monkeypatch.setattr(
        "omnigent_client.SessionsChat",
        _fake_sessions_chat_cls(
            _return_text,
            extra_turns=["<!-- POLLY_REVIEW_START -->\n## Summary\nLooks good."],
        ),
    )
    client = _FakeAPClient([], list_items_must_not_be_called=True)
    result = await _query_sessions_once(
        client=client,
        agent_name="polly",
        tool_handler=None,
        prompt="review this PR",
        session_bundle=b"bundle-bytes",
        session_bundle_filename="agent.tar.gz",
        runner_id="runner_test",
    )
    assert result is not None
    assert "direct answer" in result
    assert "<!-- POLLY_REVIEW_START -->" in result
    assert "Looks good." in result


async def test_persisted_turn_text_anchors_on_last_user_message() -> None:
    """Only the current turn's assistant output is returned, not a prior turn's.

    A resumed session carries earlier assistant messages. If this fails,
    a ``-p`` turn that genuinely produced nothing would return stale
    prior-turn text as if it were this turn's answer.
    """
    client = _FakeAPClient(
        [
            _item_assistant("OLD prior-turn answer"),
            _item_user("new question"),
            _item_assistant("NEW answer"),
        ]
    )
    assert await _persisted_turn_text(client, "conv_test") == "NEW answer"


async def test_persisted_turn_text_none_when_no_assistant_after_user() -> None:
    """Returns None when the latest user message has no following assistant message.

    If this fails, an empty turn (no output persisted) would be treated
    as success and the spurious-failure error would be swallowed.
    """
    client = _FakeAPClient([_item_assistant("OLD"), _item_user("unanswered question")])
    assert await _persisted_turn_text(client, "conv_test") is None


async def test_persisted_turn_text_concatenates_this_turn_messages() -> None:
    """Multiple assistant messages within the current turn are concatenated in order.

    If this fails, multi-message turns would be truncated to the first
    (or last) assistant message.
    """
    client = _FakeAPClient(
        [
            _item_user("q"),
            _item_assistant("part one "),
            _item_assistant("part two"),
        ]
    )
    assert await _persisted_turn_text(client, "conv_test") == "part one part two"


async def test_persisted_turn_text_ignores_non_completed_assistant_item() -> None:
    """A non-``completed`` (mid-stream-errored) assistant item is not reconciled.

    Returning a partial item from a genuinely failed turn would swallow
    the real error. If this returns the partial text, the wrapper would
    mask a mid-stream failure as success.
    """
    client = _FakeAPClient(
        [_item_user("q"), _item_assistant("partial before crash", status="incomplete")]
    )
    assert await _persisted_turn_text(client, "conv_test") is None


async def test_query_sessions_once_reraises_on_failed_with_only_partial_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``failed`` turn that persisted only a non-``completed`` item still raises.

    This is the fail-loud guard: the transcript holds a partial
    assistant item (status ``incomplete``), so reconcile yields nothing
    and the genuine failure surfaces instead of printing a half-answer.
    """
    client = _FakeAPClient(
        [_item_user("say hi"), _item_assistant("half a reply", status="incomplete")]
    )
    with pytest.raises(ClientOmnigentError, match="turn failed"):
        await _run_one_shot(client, _raise_turn_failed, monkeypatch)


def test_spec_used_families_pi_brain_agent_contributes_pi_surface(tmp_path) -> None:
    """A pi-brain orchestrator surfaces ``pi`` alongside its sub-agents' families.

    Proves the data behind polly's startup-header creds line: the pi
    harness maps to no single model family (it consumes both), so it must
    contribute its own ``pi`` surface — the header then resolves that
    surface's effective credential. A regression that drops the pi branch
    leaves only the sub-agents' families and the header silently omits
    "Pi → …".
    """
    root = tmp_path / "polly-like"
    (root / "agents" / "worker").mkdir(parents=True)
    (root / "config.yaml").write_text(
        "spec_version: 1\n"
        "name: polly-like\n"
        "prompt: orchestrate\n"
        "executor:\n"
        "  type: omnigent\n"
        "  config:\n"
        "    harness: pi\n"
        "tools:\n"
        "  agents:\n"
        "    - worker\n"
    )
    (root / "agents" / "worker" / "config.yaml").write_text(
        "spec_version: 1\n"
        "name: worker\n"
        "prompt: work\n"
        "executor:\n"
        "  type: omnigent\n"
        "  config:\n"
        "    harness: codex-native\n"
    )

    # The pi brain contributes the pi surface; the codex-native sub-agent
    # contributes openai. Sorted, pi lands last — the header order.
    assert _spec_used_families(root) == ["openai", "pi"]


def test_env_auth_injection_skipped_when_global_auth_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ambient OPENAI_API_KEY must not be baked over configured auth.

    With a global ``auth:`` block (written by ``omnigent setup``), the
    user's configured Databricks routing is the explicit choice; baking
    the shell's env key into the materialized spec as ``executor.auth``
    would silently hijack it — the exact failure mode that produced
    empty openai-agents replies in the e2e REPL suite.
    """
    from omnigent.chat import _inject_openai_env_auth_if_needed

    config_home = tmp_path / "config"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(
        "auth:\n  type: databricks\n  profile: my-ws\n", encoding="utf-8"
    )
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-ambient-shell-key")

    raw: dict[str, object] = {"executor": {"harness": "openai-agents"}}
    _inject_openai_env_auth_if_needed(raw)

    # No auth baked: the global block remains the routing source and the
    # runner resolves it via OMNIGENT_CONFIG_HOME. A baked api_key here
    # means ambient env regained priority over configured credentials.
    assert "auth" not in raw["executor"]


def test_env_auth_injection_applies_when_nothing_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With NO configured credential, the env key is still baked.

    Daemon-owned runners don't inherit provider secrets, so for users
    whose only credential is the shell's OPENAI_API_KEY the bake is what
    keeps ``run --harness openai-agents`` working at all.
    """
    from omnigent.chat import _inject_openai_env_auth_if_needed

    config_home = tmp_path / "config-empty"
    config_home.mkdir()
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-ambient-shell-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    raw: dict[str, object] = {"executor": {"harness": "openai-agents"}}
    _inject_openai_env_auth_if_needed(raw)

    executor = raw["executor"]
    assert isinstance(executor, dict)
    # The bake carries the actual key value so the uploaded bundle is
    # self-contained for the secret-less runner.
    assert executor["auth"] == {"type": "api_key", "api_key": "sk-ambient-shell-key"}


def test_redirect_native_resume_handles_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cursor-native resume hands off to ``omnigent cursor`` (direct attach).

    Regression: without a cursor branch in ``_redirect_native_resume_if_needed``
    the resume fell through to the Omnigent REPL, which drove an Omnigent turn
    per message (persisting its own user item) *while* the cursor forwarder
    mirrored the same message from the cursor store — recording each user
    message twice. The redirect keeps the TUI the single source of turns.
    """
    from omnigent._wrapper_labels import CURSOR_NATIVE_WRAPPER_VALUE

    monkeypatch.setattr(
        chat_module,
        "_wrapper_label_for_conversation",
        lambda **_kw: CURSOR_NATIVE_WRAPPER_VALUE,
    )
    captured: dict[str, object] = {}

    def _fake_run_cursor_native(**kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("omnigent.cursor_native.run_cursor_native", _fake_run_cursor_native)

    handled = chat_module._redirect_native_resume_if_needed(
        base_url="https://example.com",
        conversation_id="conv_abc123",
        auto_open_conversation=True,
    )

    assert handled is True
    assert captured == {
        "server": "https://example.com",
        "session_id": "conv_abc123",
        "cursor_args": (),
        "auto_open_conversation": True,
    }


def test_cursor_native_resume_never_drives_an_omnigent_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resuming a cursor-native conversation must not enter the turn-driving REPL.

    This is the behavior that makes the user's message appear exactly once. The
    duplicate had two sources: (1) the Omnigent turn the REPL drives, which
    persists its own user item, and (2) the cursor forwarder mirroring the same
    message back from the cursor store. ``_chat_with_server`` must short-circuit
    on the wrapper redirect *before* either ``_run_repl`` or ``_run_one_shot``
    is reached, so source (1) never happens and only the forwarder records the
    turn.
    """
    from omnigent._wrapper_labels import CURSOR_NATIVE_WRAPPER_VALUE

    monkeypatch.setattr(
        chat_module,
        "_wrapper_label_for_conversation",
        lambda **_kw: CURSOR_NATIVE_WRAPPER_VALUE,
    )
    redirected: dict[str, object] = {}
    monkeypatch.setattr(
        "omnigent.cursor_native.run_cursor_native",
        lambda **kwargs: redirected.update(kwargs),
    )

    def _fail_repl(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("_run_repl drove an Omnigent turn for a cursor-native resume")

    def _fail_one_shot(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("_run_one_shot drove an Omnigent turn for a cursor-native resume")

    monkeypatch.setattr(chat_module, "_run_repl", _fail_repl)
    monkeypatch.setattr(chat_module, "_run_one_shot", _fail_one_shot)
    # _pick_agent runs only on the non-redirect path; tripping it also signals
    # the redirect failed to short-circuit.
    monkeypatch.setattr(
        chat_module,
        "_pick_agent",
        lambda *_a, **_k: pytest.fail("reached the non-redirect path"),
    )

    # Returns cleanly via the redirect; the AssertionError stubs above fire if
    # it ever falls through to a turn-driving path.
    chat_module._chat_with_server(
        "https://example.com",
        None,
        resume_conversation_id="conv_abc123",
    )

    assert redirected["session_id"] == "conv_abc123"
