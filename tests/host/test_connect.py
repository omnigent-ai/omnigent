"""Tests for the host connect process (launch/stop handling)."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidStatus, InvalidURI
from websockets.http11 import Response

from omnigent.host import HOST_FATAL_EXIT_CODE
from omnigent.host.connect import (
    HostConnectError,
    HostProcess,
    HostRetryableConnectionError,
    _build_runner_env,
    _RunnerHandle,
    run_host_process,
)
from omnigent.host.frames import (
    HARNESS_NOT_CONFIGURED_ERROR_CODE,
    HostConnectionErrorFrame,
    HostCreateDirFrame,
    HostCreateDirResultFrame,
    HostDetectCredentialsFrame,
    HostDetectCredentialsResultFrame,
    HostHarnessReadinessFrame,
    HostHelloFrame,
    HostImportLocalByIdFrame,
    HostImportLocalFrame,
    HostInstallHarnessFrame,
    HostInstallHarnessResultFrame,
    HostLaunchRunnerFrame,
    HostLaunchRunnerResultFrame,
    HostListDirFrame,
    HostListDirResultFrame,
    HostModelOptionsFrame,
    HostModelOptionsResultFrame,
    HostRunnerExitedFrame,
    HostRunnerStatusFrame,
    HostRunnerStatusResultFrame,
    HostStatFrame,
    HostStatResultFrame,
    HostStopRunnerFrame,
    HostStopRunnerResultFrame,
    HostStoreSecretFrame,
    HostStoreSecretResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.host.identity import HostIdentity
from omnigent.host.runner_zygote import ZygoteUnavailable
from omnigent.runner.identity import (
    RUNNER_DELEGATED_AUTH_ENV_VAR,
    RUNNER_ID_ENV_VAR,
    RUNNER_INITIAL_AUTH_TOKEN_ENV_VAR,
    RUNNER_LAUNCH_HARNESS_ENV_VAR,
    RUNNER_PARENT_PID_ENV_VAR,
    RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR,
    RUNNER_WORKSPACE_ENV_VAR,
    token_bound_runner_id,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _isolated_model_catalog_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Point the shared model-catalog store at a per-test directory.

    The model-options lanes read and write the on-disk catalog store; a
    test must never touch (or be poisoned by) the developer's real
    ``~/.omnigent`` cache. Only the store's directory seam is redirected —
    ``OMNIGENT_DATA_DIR`` itself stays untouched so log-path tests keep
    seeing the real default layout.
    """
    store_dir = tmp_path_factory.mktemp("model_catalog_store")
    monkeypatch.setattr("omnigent.model_catalog_store._data_dir", lambda: store_dir)


@pytest.fixture(autouse=True)
def _no_real_zygote(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep these tests from forking a real runner zygote.

    ``HostProcess.run()`` prewarms the zygote at daemon start, so without
    this opt-out every test that drives ``run()`` would spawn a real
    ``python -m omnigent.runner._zygote`` (a 1-2s import of the runner
    graph). Zygote behavior itself is exercised via ``_FakeZygote`` here
    and with real ``ZygoteManager`` instances in ``test_runner_zygote.py``
    (which this construction-time gate does not affect).
    """
    from omnigent.runner._zygote import ZYGOTE_ENABLED_ENV_VAR

    monkeypatch.setenv(ZYGOTE_ENABLED_ENV_VAR, "0")


async def test_handle_model_options_serves_the_claude_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The launch picker is the harness-probed catalog, resolved on the host.

    The probe's rows pass through with the harness's own default marked,
    the endpoint's routable set rides along, and the second request is
    served from the fingerprint store — the harness is probed once.
    """
    from omnigent import claude_native

    config = claude_native.ClaudeNativeUcodeConfig(
        env={"ANTHROPIC_BASE_URL": "https://gw.example"},
        routable_models=(
            "system.ai.claude-sonnet-5",
            "system.ai.claude-sonnet-5[1m]",
        ),
    )
    monkeypatch.setattr(
        claude_native,
        "resolve_native_claude_config",
        lambda *, spec, refresh_models=True: config,
    )
    probe_calls: list[int] = []

    async def _fake_probe(_config: object) -> claude_native.ClaudeModelProbe:
        probe_calls.append(1)
        return claude_native.ClaudeModelProbe(
            alias_rows=[
                {
                    "id": "sonnet",
                    "model": "system.ai.claude-sonnet-5",
                    "displayName": "Sonnet 5",
                }
            ],
            default_model="system.ai.claude-sonnet-5",
            default_label="Sonnet 5",
        )

    monkeypatch.setattr(claude_native, "probe_claude_model_options", _fake_probe)
    host = _make_host_process()

    first = await host._handle_model_options(
        HostModelOptionsFrame(request_id="req_1", harness="claude-native"),
    )
    second = await host._handle_model_options(
        HostModelOptionsFrame(request_id="req_2", harness="claude-native"),
    )

    assert first == HostModelOptionsResultFrame(
        request_id="req_1",
        status="ok",
        models=[
            {
                "id": "sonnet",
                "model": "system.ai.claude-sonnet-5",
                "displayName": "Sonnet 5",
                "isDefault": True,
            }
        ],
        routable_models=[
            "system.ai.claude-sonnet-5",
            "system.ai.claude-sonnet-5[1m]",
        ],
    )
    assert second.models == first.models
    assert probe_calls == [1]
    _cleanup_host(host)


async def test_handle_model_options_claude_probe_failure_is_an_honest_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Claude catalog means an empty answer that says why.

    There is no configured/static fallback lane left: a probe that cannot
    run yields an honest empty listing with the reason, never invented
    rows.
    """
    from omnigent import claude_native

    monkeypatch.setattr(
        claude_native,
        "resolve_native_claude_config",
        lambda *, spec, refresh_models=True: None,
    )

    async def _failed_probe(_config: object) -> None:
        return None

    monkeypatch.setattr(claude_native, "probe_claude_model_options", _failed_probe)
    host = _make_host_process()

    result = await host._handle_model_options(
        HostModelOptionsFrame(request_id="req_models", harness="claude-native"),
    )

    assert result == HostModelOptionsResultFrame(
        request_id="req_models",
        status="ok",
        models=[],
        error="the claude model probe failed — see the host log",
    )
    _cleanup_host(host)


async def test_handle_model_options_uses_host_pi_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pi's launch picker uses only models configured through Omnigent."""
    from omnigent import pi_native_credentials

    monkeypatch.setattr(
        pi_native_credentials,
        "pi_native_model_options",
        lambda: [
            {
                "id": "omnigent-openai/system.ai.gpt-5-6-sol",
                "model": "omnigent-openai/system.ai.gpt-5-6-sol",
                "displayName": "omnigent-openai/GPT 5.6 Sol",
            }
        ],
    )
    host = _make_host_process()

    result = await host._handle_model_options(
        HostModelOptionsFrame(request_id="req_pi_models", harness="pi-native"),
    )

    assert result == HostModelOptionsResultFrame(
        request_id="req_pi_models",
        status="ok",
        models=[
            {
                "id": "omnigent-openai/system.ai.gpt-5-6-sol",
                "model": "omnigent-openai/system.ai.gpt-5-6-sol",
                "displayName": "omnigent-openai/GPT 5.6 Sol",
            }
        ],
    )


@pytest.mark.parametrize("failure", ["raises", "resolves_nothing"])
async def test_handle_model_options_codex_probe_failure_is_an_honest_empty(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """No Codex catalog means an empty answer that says why.

    There is no curated/provider fallback lane left: whether the catalog
    machinery raises or resolves nothing, the picker gets an honest empty
    listing with the reason, never invented rows.
    """
    from omnigent import codex_native_app_server

    async def _no_catalog(**_kwargs: object) -> list[dict[str, object]] | None:
        if failure == "raises":
            raise RuntimeError("codex probe unavailable")
        return None

    monkeypatch.setattr(codex_native_app_server, "codex_launch_catalog", _no_catalog)
    host = _make_host_process()

    result = await host._handle_model_options(
        HostModelOptionsFrame(request_id="req_models", harness="codex-native"),
    )

    assert result == HostModelOptionsResultFrame(
        request_id="req_models",
        status="ok",
        models=[],
        error="the codex model probe failed — see the host log",
    )
    _cleanup_host(host)


async def test_handle_model_options_rejects_unsupported_harness() -> None:
    """Only launch paths with host-resolved model catalogs are accepted."""
    host = _make_host_process()

    result = await host._handle_model_options(
        HostModelOptionsFrame(request_id="req_models", harness="cursor-native"),
    )

    assert result == HostModelOptionsResultFrame(
        request_id="req_models",
        status="failed",
        error="model options are unsupported for harness 'cursor-native'",
    )


async def test_handle_model_options_reports_the_endpoints_wider_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generations no picker row names are still launchable, so they ship too."""
    from omnigent import claude_native

    monkeypatch.setattr(
        claude_native,
        "resolve_native_claude_config",
        lambda *, spec, refresh_models=True: claude_native.ClaudeNativeUcodeConfig(
            env={"ANTHROPIC_DEFAULT_OPUS_MODEL": "system.ai.claude-opus-5"},
            model="system.ai.claude-opus-5",
            routable_models=("system.ai.claude-opus-5", "system.ai.claude-opus-4-8"),
        ),
    )

    async def _fake_probe(_config: object) -> claude_native.ClaudeModelProbe:
        return claude_native.ClaudeModelProbe(
            alias_rows=[{"id": "opus", "model": "system.ai.claude-opus-5"}],
            default_model="system.ai.claude-opus-5",
            default_label="Opus",
        )

    monkeypatch.setattr(claude_native, "probe_claude_model_options", _fake_probe)
    host = _make_host_process()

    result = await host._handle_model_options(
        HostModelOptionsFrame(request_id="req_models", harness="claude-native"),
    )

    assert result.routable_models == [
        "system.ai.claude-opus-5",
        "system.ai.claude-opus-4-8",
    ]
    _cleanup_host(host)


def _make_host_process() -> HostProcess:
    """Create a HostProcess with a test identity.

    :returns: A :class:`HostProcess` for testing.
    """
    identity = HostIdentity(
        host_id="host_test_connect",
        name="test-laptop",
    )
    return HostProcess(
        identity=identity,
        server_url="http://localhost:8000",
    )


def _cleanup_host(host: HostProcess) -> None:
    """Terminate a test host's spawned runners and exit watchers.

    ``_cleanup_runners`` pops every handle before terminating, so the
    watcher tasks see an intentional stop and never report; cancelling
    them afterwards just avoids "task was destroyed but pending"
    warnings at loop teardown.

    :param host: The host process under test.
    """
    host._cleanup_runners()
    for task in host._watcher_tasks:
        task.cancel()


async def test_handle_launch_spawns_subprocess(
    tmp_path: Path,
) -> None:
    """
    Verify that _handle_launch spawns a subprocess with the correct
    binding token and workspace, and returns status='launched'.

    If the result status is not 'launched', the subprocess spawn
    failed or the result frame construction is wrong.
    """
    host = _make_host_process()
    host._auth_token_factory = lambda: "host-bootstrap-bearer"
    host._auth_token_factory_resolved = True
    workspace = tmp_path / "project"
    workspace.mkdir()

    frame = HostLaunchRunnerFrame(
        request_id="req_001",
        binding_token="test_token_abc",
        workspace=str(workspace),
    )

    spawned_env: dict[str, str] = {}
    spawned_kwargs: dict[str, object] = {}

    original_popen = subprocess.Popen

    def _fake_popen(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        """Capture the env vars and spawn a no-op process.

        :param args: Command args (ignored — we spawn a real sleep).
        :param kwargs: Popen kwargs including env and stdin.
        :returns: A real subprocess handle.
        """
        env = kwargs.get("env", {})
        spawned_env.update(env)
        spawned_kwargs.update(kwargs)
        # Spawn a real process that sleeps briefly so poll() returns None.
        return original_popen(
            ["sleep", "10"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    with patch("omnigent.host.connect.subprocess.Popen", side_effect=_fake_popen):
        result = await host._handle_launch(frame)

    assert isinstance(result, HostLaunchRunnerResultFrame)
    # Status should be 'launched' since the process started.
    assert result.status == "launched", (
        f"Expected 'launched', got {result.status!r}. Error: {result.error}"
    )
    assert result.request_id == "req_001"
    expected_runner_id = token_bound_runner_id("test_token_abc")
    # runner_id should be derived from the binding token.
    assert result.runner_id == expected_runner_id, (
        "runner_id should be token_bound_runner_id(binding_token)"
    )

    # Verify env vars passed to the subprocess.
    assert spawned_env.get("RUNNER_SERVER_URL") == "http://localhost:8000"
    assert spawned_env.get("OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN") == "test_token_abc"
    assert spawned_env.get(RUNNER_INITIAL_AUTH_TOKEN_ENV_VAR) == "host-bootstrap-bearer"
    assert spawned_env.get("OMNIGENT_RUNNER_WORKSPACE") == str(workspace)

    # Runners must get a clean /dev/null stdin, not the daemon's inherited fd:
    # a long-lived (e.g. nohup'd) daemon can end up with a closed/recycled
    # stdin, and an inherited bad fd crashes the runner at interpreter startup
    # ("init_sys_streams: Bad file descriptor") so it never connects. If this
    # regresses, long-lived daemons intermittently fail to start sessions.
    assert spawned_kwargs.get("stdin") == subprocess.DEVNULL, (
        "runner subprocess must be spawned with stdin=subprocess.DEVNULL"
    )

    # Runners must start in the session workspace, not the daemon's inherited
    # cwd: a daemon launched from a directory that was later deleted (temp
    # checkout, removed worktree) makes every Path.cwd() in the runner raise
    # FileNotFoundError, and native terminals then fail to start.
    assert spawned_kwargs.get("cwd") == str(workspace), (
        "runner subprocess must be spawned with cwd=<session workspace>"
    )

    # Clean up the spawned sleep process (and its exit watcher).
    _cleanup_host(host)


async def test_handle_launch_fails_for_bad_workspace() -> None:
    """
    Verify that _handle_launch returns status='failed' when the
    workspace path does not exist.

    If it returns 'launched', the path validation is missing and
    the runner would start in a nonexistent directory.
    """
    host = _make_host_process()
    frame = HostLaunchRunnerFrame(
        request_id="req_002",
        binding_token="token_xyz",
        workspace="/nonexistent/path/that/does/not/exist",
    )

    result = await host._handle_launch(frame)

    assert isinstance(result, HostLaunchRunnerResultFrame)
    assert result.status == "failed", "Should fail for nonexistent workspace"
    assert "does not exist" in (result.error or ""), (
        f"Error should mention path doesn't exist, got: {result.error!r}"
    )
    assert result.error_code == "workspace_missing", (
        f"Should carry workspace_missing error_code, got: {result.error_code!r}"
    )
    assert result.runner_id is None


async def test_handle_launch_refuses_unconfigured_harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Verify _handle_launch refuses to spawn when the frame's harness is
    not configured, with the structured error_code and a message that
    names the harness, the host, and the `omni setup` fix.

    If this regresses, an unconfigured launch spawns a runner whose
    first turn dies inside the executor — the exact dead-session UX
    this check exists to prevent — and the server's 412 mapping (keyed
    on error_code) never fires.
    """
    host = _make_host_process()
    workspace = tmp_path / "project"
    workspace.mkdir()
    # Patch the symbol connect.py imported, with the real function's
    # signature; the workspace exists so ONLY the harness check can fail.
    monkeypatch.setattr(
        "omnigent.host.connect.harness_is_configured",
        lambda harness: False,
    )

    frame = HostLaunchRunnerFrame(
        request_id="req_unconfigured",
        binding_token="token_abc",
        workspace=str(workspace),
        harness="codex",
    )
    result = await host._handle_launch(frame)

    assert isinstance(result, HostLaunchRunnerResultFrame)
    assert result.status == "failed"
    # The structured code is what the server's 412 mapping keys on.
    assert result.error_code == HARNESS_NOT_CONFIGURED_ERROR_CODE
    # The message is shown verbatim to the user — it must name the
    # harness, the host, and the remediation command.
    assert "'codex'" in (result.error or "")
    assert "test-laptop" in (result.error or "")
    assert "omni setup" in (result.error or "")
    assert result.runner_id is None
    # No runner subprocess may exist after a refusal.
    assert host._runners == {}


async def test_handle_launch_native_cursor_message_points_at_cursor_installer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A native-Cursor refusal must name the ``cursor-agent`` installer and
    login, not ``omni setup`` — which only configures the SDK ``cursor``
    harness and never installs the ``cursor-agent`` CLI ``omni cursor`` boots.

    Here ``harness_setup_hint`` is the real function (only the readiness check
    is forced False), so this exercises the connect.py → hint wiring end to end.
    """
    host = _make_host_process()
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setattr(
        "omnigent.host.connect.harness_is_configured",
        lambda harness: False,
    )

    frame = HostLaunchRunnerFrame(
        request_id="req_cursor_native",
        binding_token="token_abc",
        workspace=str(workspace),
        harness="cursor-native",
    )
    result = await host._handle_launch(frame)

    assert result.status == "failed"
    assert result.error_code == HARNESS_NOT_CONFIGURED_ERROR_CODE
    message = result.error or ""
    assert "'cursor-native'" in message
    assert "test-laptop" in message
    assert "cursor.com/install" in message
    assert "cursor-agent login" in message
    assert "omni setup" not in message
    assert host._runners == {}


async def test_handle_launch_configured_harness_proceeds_to_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Verify a configured harness passes the check and the launch
    proceeds to the normal spawn path.

    If this fails with status='failed', the readiness check is
    refusing configured harnesses — every launch on the host would
    break, not just unconfigured ones.
    """
    host = _make_host_process()
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setattr(
        "omnigent.host.connect.harness_is_configured",
        lambda harness: True,
    )

    original_popen = subprocess.Popen

    def _fake_popen(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        """Spawn a no-op process so poll() returns None.

        :param args: Command args (ignored — we spawn a real sleep).
        :param kwargs: Popen kwargs (ignored).
        :returns: A real subprocess handle.
        """
        return original_popen(
            ["sleep", "10"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    frame = HostLaunchRunnerFrame(
        request_id="req_configured",
        binding_token="token_def",
        workspace=str(workspace),
        harness="claude-sdk",
    )
    with patch("omnigent.host.connect.subprocess.Popen", side_effect=_fake_popen):
        result = await host._handle_launch(frame)

    assert result.status == "launched", (
        f"Configured harness must launch, got {result.status!r}: {result.error}"
    )
    assert result.error_code is None

    # Clean up the spawned sleep process (and its exit watcher).
    _cleanup_host(host)


async def test_handle_launch_without_harness_skips_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Verify a launch frame with harness=None (an older server) never
    consults the readiness check — fail open across version skew.

    If the check ran anyway, upgrading hosts before servers would
    break launches for any session whose harness the host considers
    unconfigured, even though the server never asked for the check.
    """
    host = _make_host_process()
    workspace = tmp_path / "project"
    workspace.mkdir()

    def _must_not_be_called(harness: str) -> bool:
        """Fail the test if the readiness check runs for harness=None.

        :param harness: The harness the production code passed.
        :returns: Never returns.
        """
        raise AssertionError("harness_is_configured must not be called when frame.harness is None")

    monkeypatch.setattr(
        "omnigent.host.connect.harness_is_configured",
        _must_not_be_called,
    )

    original_popen = subprocess.Popen

    def _fake_popen(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        """Spawn a no-op process so poll() returns None.

        :param args: Command args (ignored — we spawn a real sleep).
        :param kwargs: Popen kwargs (ignored).
        :returns: A real subprocess handle.
        """
        return original_popen(
            ["sleep", "10"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    frame = HostLaunchRunnerFrame(
        request_id="req_no_harness",
        binding_token="token_ghi",
        workspace=str(workspace),
    )
    with patch("omnigent.host.connect.subprocess.Popen", side_effect=_fake_popen):
        result = await host._handle_launch(frame)

    assert result.status == "launched"

    # Clean up the spawned sleep process (and its exit watcher).
    _cleanup_host(host)


async def test_handle_launch_prints_exact_runner_log_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The launch banner names the exact runner log file, not just the dir.

    A foreground host's terminal shows the lifecycle line, but the runner's
    real output — the agent turn, tracebacks — lands only in the per-runner
    log file. The user needs that precise path to tail it, so the launch
    print must include it. We repoint ``Path.home`` so the log lands under
    tmp (no write to the developer's real ``~/.omnigent``).
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / ".omnigent"))
    host = _make_host_process()
    workspace = tmp_path / "project"
    workspace.mkdir()

    frame = HostLaunchRunnerFrame(
        request_id="req_log",
        binding_token="tok_log",
        workspace=str(workspace),
        session_id="conv_log",
    )

    original_popen = subprocess.Popen

    def _fake_popen(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        """Spawn a harmless sleep so poll() reports the runner as alive.

        :param args: Command args (ignored — a real sleep is spawned).
        :param kwargs: Popen kwargs (ignored).
        :returns: A real subprocess handle.
        """
        return original_popen(
            ["sleep", "10"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    with patch("omnigent.host.connect.subprocess.Popen", side_effect=_fake_popen):
        result = await host._handle_launch(frame)

    assert result.status == "launched", result.error
    # Exactly one runner-*.log was created under the runner log dir.
    runner_log_dir = tmp_path / ".omnigent" / "logs" / "runner"
    log_files = list(runner_log_dir.glob("runner-*.log"))
    assert len(log_files) == 1
    out = capsys.readouterr().out
    assert "↑ Runner started:" in out
    # The exact file path is printed, home-collapsed to ``~`` for readability.
    assert f"log: ~/.omnigent/logs/runner/{log_files[0].name}" in out
    assert "session: conv_log" in out

    _cleanup_host(host)


class _FakeTunnel:
    """In-memory stand-in for the host's WebSocket tunnel connection.

    Captures frames sent by the host process so tests can assert on
    outbound reports; ``recv`` immediately raises so ``_serve_frames``
    exits after its post-hello work.

    :param sent: Encoded frames sent via ``send``, in order.
    """

    def __init__(self) -> None:
        """Initialize with an empty sent-frame log."""
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        """Record an outbound frame.

        :param data: Encoded frame text.
        """
        self.sent.append(data)

    async def recv(self) -> str:
        """Simulate an immediate disconnect after hello."""
        raise ConnectionError("test disconnect")


class _ConnectionErrorTunnel:
    """Tunnel that returns one server connection error after host.hello."""

    def __init__(self, frame: HostConnectionErrorFrame) -> None:
        self.frame = frame
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        return encode_host_frame(self.frame)


class _RecordingWS:
    """Fake tunnel that records frames the readiness loop sends.

    The readiness loop (:meth:`HostProcess._harness_readiness_loop`) only ever
    calls ``send``; ``first_send`` lets a test await the first update frame
    deterministically instead of sleeping for a fixed interval.
    """

    def __init__(self) -> None:
        """Initialize the frame log and first-send signal."""
        self.sent: list[str] = []
        self.first_send = asyncio.Event()

    async def send(self, data: str) -> None:
        """Record an outbound frame and signal the first send.

        :param data: Encoded frame text.
        """
        self.sent.append(data)
        self.first_send.set()


async def _cancel(task: asyncio.Task[None]) -> None:
    """Cancel *task* and await its unwinding, swallowing the cancellation."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def test_unavailable_harness_quick_probe_is_ttl_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """The quick probe resolves each unavailable harness once per TTL.

    The readiness loop wakes every 5s; re-probing every unavailable
    harness with a full shutil.which PATH scan on each wakeup burns a
    sustained CPU core on hosts where each PATH stat is expensive. Within
    the TTL the cached verdict serves the cadence; expiry re-probes so a
    new install still surfaces.
    """
    from omnigent.host import connect as connect_mod

    calls: list[str] = []
    monkeypatch.setattr(connect_mod, "harness_is_configured", lambda h: calls.append(h) or False)
    connect_mod._invalidate_quick_probe_cache()
    try:
        previous = {"goose-native": False, "qwen-native": False}

        # Burst of quick probes well inside the TTL.
        for _ in range(12):
            assert connect_mod._unavailable_harness_became_ready(previous) is False
        # Each harness resolved exactly ONCE — the cadence served from cache.
        assert sorted(calls) == ["goose-native", "qwen-native"]

        # A harness that becomes configured is seen once the verdict expires.
        monkeypatch.setattr(connect_mod, "harness_is_configured", lambda h: h == "goose-native")
        monkeypatch.setattr(connect_mod, "HARNESS_READINESS_QUICK_PROBE_CACHE_TTL_S", 0.0)
        assert connect_mod._unavailable_harness_became_ready(previous) is True
    finally:
        connect_mod._invalidate_quick_probe_cache()
        monkeypatch.undo()


def test_quick_probe_cache_survives_configured_flip_within_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Within the TTL a cached miss is authoritative — an external install
    surfaces on the next expiry, not the next 5s tick."""
    from omnigent.host import connect as connect_mod

    state = {"configured": False}
    monkeypatch.setattr(connect_mod, "harness_is_configured", lambda _h: state["configured"])
    connect_mod._invalidate_quick_probe_cache()
    try:
        assert connect_mod._unavailable_harness_became_ready({"goose-native": False}) is False
        state["configured"] = True
        # Still inside the TTL: cached miss wins.
        assert connect_mod._unavailable_harness_became_ready({"goose-native": False}) is False
        # Expiry re-probes and sees the install.
        monkeypatch.setattr(connect_mod, "HARNESS_READINESS_QUICK_PROBE_CACHE_TTL_S", 0.0)
        assert connect_mod._unavailable_harness_became_ready({"goose-native": False}) is True
    finally:
        connect_mod._invalidate_quick_probe_cache()


async def test_live_host_refreshes_harness_readiness_without_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A setup completed after connect pushes a replacement readiness frame.

    The refresh now runs in :meth:`HostProcess._harness_readiness_loop`, off the
    receive loop, so a slow probe can never stall the tunnel keepalive.
    """
    readiness = iter(({"pi": True},))
    monkeypatch.setattr("omnigent.host.connect.gateway_inference_map", lambda: {"codex": True})
    monkeypatch.setattr(
        "omnigent.host.connect.configured_harness_map",
        lambda: next(readiness),
    )
    monkeypatch.setattr(
        "omnigent.host.connect.harness_is_configured",
        lambda harness: harness == "pi",
    )
    monkeypatch.setattr(
        "omnigent.host.connect.HARNESS_READINESS_REFRESH_INTERVAL_S",
        0.01,
    )
    host = _make_host_process()
    host._configured_harnesses = {"pi": False}
    host._gateway_inference = {"codex": True}
    ws = _RecordingWS()

    task = asyncio.create_task(host._harness_readiness_loop(ws))
    try:
        await asyncio.wait_for(ws.first_send.wait(), timeout=2.0)
    finally:
        await _cancel(task)

    assert len(ws.sent) == 1
    refresh = decode_host_frame(ws.sent[0])
    assert isinstance(refresh, HostHarnessReadinessFrame)
    assert refresh.configured_harnesses == {"pi": True}
    _cleanup_host(host)


async def test_live_host_full_refresh_detects_auth_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full-refresh fallback catches readiness changes beyond binary installs."""
    readiness = iter(({"codex": True},))
    monkeypatch.setattr("omnigent.host.connect.gateway_inference_map", lambda: {"codex": True})
    monkeypatch.setattr(
        "omnigent.host.connect.configured_harness_map",
        lambda: next(readiness),
    )
    monkeypatch.setattr(
        "omnigent.host.connect.HARNESS_READINESS_FULL_REFRESH_INTERVAL_S",
        0.01,
    )
    host = _make_host_process()
    host._configured_harnesses = {"codex": "needs-auth"}
    host._gateway_inference = {"codex": True}
    ws = _RecordingWS()

    task = asyncio.create_task(host._harness_readiness_loop(ws))
    try:
        await asyncio.wait_for(ws.first_send.wait(), timeout=2.0)
    finally:
        await _cancel(task)

    assert len(ws.sent) == 1
    refresh = decode_host_frame(ws.sent[0])
    assert isinstance(refresh, HostHarnessReadinessFrame)
    assert refresh.configured_harnesses == {"codex": True}
    _cleanup_host(host)


async def test_live_host_does_not_repeat_unchanged_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A periodic full refresh sends nothing when the readiness map is unchanged."""
    calls = {"n": 0}

    def _unchanged_map() -> dict[str, str]:
        calls["n"] += 1
        return {"codex": "needs-auth"}

    monkeypatch.setattr("omnigent.host.connect.configured_harness_map", _unchanged_map)
    monkeypatch.setattr("omnigent.host.connect.gateway_inference_map", lambda: {"codex": True})
    monkeypatch.setattr(
        "omnigent.host.connect.HARNESS_READINESS_FULL_REFRESH_INTERVAL_S",
        0.01,
    )
    host = _make_host_process()
    host._configured_harnesses = {"codex": "needs-auth"}
    host._gateway_inference = {"codex": True}
    ws = _RecordingWS()

    task = asyncio.create_task(host._harness_readiness_loop(ws))
    try:
        # Let at least two full refreshes recompute-and-compare before stopping.
        for _ in range(400):
            if calls["n"] >= 2:
                break
            await asyncio.sleep(0.005)
    finally:
        await _cancel(task)

    assert calls["n"] >= 2
    assert ws.sent == []
    _cleanup_host(host)


async def test_live_host_repushes_when_only_gateway_inference_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gateway-inference flip alone must reach the server, readiness unchanged."""
    gateway = iter(({"codex": False}, {"codex": True}))
    monkeypatch.setattr(
        "omnigent.host.connect.configured_harness_map",
        lambda: {"codex": True},
    )
    monkeypatch.setattr(
        "omnigent.host.connect.gateway_inference_map",
        lambda: next(gateway, {"codex": True}),
    )
    monkeypatch.setattr(
        "omnigent.host.connect.HARNESS_READINESS_FULL_REFRESH_INTERVAL_S",
        0.01,
    )
    host = _make_host_process()
    host._configured_harnesses = {"codex": True}
    host._gateway_inference = {"codex": False}
    ws = _RecordingWS()

    task = asyncio.create_task(host._harness_readiness_loop(ws))
    try:
        await asyncio.wait_for(ws.first_send.wait(), timeout=2.0)
    finally:
        await _cancel(task)

    assert len(ws.sent) == 1
    refresh = decode_host_frame(ws.sent[0])
    assert isinstance(refresh, HostHarnessReadinessFrame)
    assert refresh.configured_harnesses == {"codex": True}
    assert refresh.gateway_inference == {"codex": True}
    _cleanup_host(host)


async def test_handle_launch_immediate_exit_reports_exit_code_and_log_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An immediate runner death fails the launch with the actual cause.

    Before this, the daemon answered only "runner exited immediately
    with code N" — the real error (a traceback, a missing module)
    stayed in a log file on the host. The launch failure must now
    carry the exit code, the log path, and the log tail so the server
    can surface the cause to the user verbatim.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / ".omnigent"))
    host = _make_host_process()
    workspace = tmp_path / "project"
    workspace.mkdir()

    original_popen = subprocess.Popen

    def _fake_popen(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        """Spawn a process that writes to the real captured log and dies.

        Passes through the production-opened log handles so the error
        output lands exactly where the daemon will read the tail from,
        and waits for exit so ``poll()`` reports the death immediately.

        :param args: Command args (ignored — a failing sh is spawned).
        :param kwargs: Popen kwargs from production, including the log
            file handles.
        :returns: A finished subprocess handle with returncode 7.
        """
        proc = original_popen(
            ["sh", "-c", "echo 'RuntimeError: boom-traceback' >&2; exit 7"],
            stdin=subprocess.DEVNULL,
            stdout=kwargs["stdout"],
            stderr=kwargs["stderr"],
        )
        proc.wait()
        return proc

    frame = HostLaunchRunnerFrame(
        request_id="req_dead",
        binding_token="tok_dead",
        workspace=str(workspace),
    )
    with patch("omnigent.host.connect.subprocess.Popen", side_effect=_fake_popen):
        result = await host._handle_launch(frame)

    assert result.status == "failed"
    error = result.error or ""
    # The exit code identifies the failure class without log-reading.
    assert "code 7" in error
    # The log path lets the user fetch the full log on the host.
    assert "~/.omnigent/logs/runner/runner-" in error
    # The tail carries the actual cause — the whole point of the report.
    assert "RuntimeError: boom-traceback" in error


async def test_watch_runner_reports_unexpected_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner that dies after launch is reported via host.runner_exited.

    This is the launch-succeeded-then-crashed path (auth rejection, bad
    env, import error) — the daemon already told the server "launched",
    so only the watcher can carry the cause. Without the report, the
    client polls its full timeout and the user is sent to a log
    directory on the host.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr("omnigent.host.connect._RUNNER_WATCH_INTERVAL_S", 0.01)
    host = _make_host_process()
    tunnel = _FakeTunnel()
    host._ws = tunnel  # type: ignore[assignment] — duck-typed send
    workspace = tmp_path / "project"
    workspace.mkdir()

    original_popen = subprocess.Popen

    def _fake_popen(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        """Spawn a runner that lives briefly, logs a cause, then dies.

        The initial sleep keeps ``poll()`` None at launch time so the
        launch itself succeeds; the process then writes the cause to
        the production log handles and exits 3.

        :param args: Command args (ignored).
        :param kwargs: Popen kwargs from production, including the log
            file handles.
        :returns: A live subprocess handle.
        """
        return original_popen(
            ["sh", "-c", "echo 'tunnel rejected: crash-cause' >&2; sleep 0.2; exit 3"],
            stdin=subprocess.DEVNULL,
            stdout=kwargs["stdout"],
            stderr=kwargs["stderr"],
        )

    frame = HostLaunchRunnerFrame(
        request_id="req_watch",
        binding_token="tok_watch",
        workspace=str(workspace),
    )
    with patch("omnigent.host.connect.subprocess.Popen", side_effect=_fake_popen):
        result = await host._handle_launch(frame)
    assert result.status == "launched", result.error

    # Wait for the watcher to observe the death and send its report
    # (bounded — a hang means the watcher never fired).
    await asyncio.wait_for(asyncio.gather(*host._watcher_tasks), timeout=5.0)

    # Exactly one report; a second would double-record server-side.
    assert len(tunnel.sent) == 1
    report = decode_host_frame(tunnel.sent[0])
    assert isinstance(report, HostRunnerExitedFrame)
    assert report.runner_id == token_bound_runner_id("tok_watch")
    # The report carries the exit code and the log tail with the cause.
    assert "code 3" in report.error
    assert "tunnel rejected: crash-cause" in report.error


async def test_watch_runner_silent_on_intentional_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host.stop_runner termination is NOT reported as a crash.

    ``_handle_stop`` pops the handle before terminating; the watcher
    must read that as intentional and send nothing. A false report
    here would attach a scary "runner process exited" error to every
    cleanly stopped session.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr("omnigent.host.connect._RUNNER_WATCH_INTERVAL_S", 0.01)
    host = _make_host_process()
    tunnel = _FakeTunnel()
    host._ws = tunnel  # type: ignore[assignment] — duck-typed send
    workspace = tmp_path / "project"
    workspace.mkdir()

    original_popen = subprocess.Popen

    def _fake_popen(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        """Spawn a long-lived stand-in runner.

        :param args: Command args (ignored).
        :param kwargs: Popen kwargs (ignored beyond stdio defaults).
        :returns: A live subprocess handle.
        """
        return original_popen(
            ["sleep", "60"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    launch = HostLaunchRunnerFrame(
        request_id="req_stop",
        binding_token="tok_stop",
        workspace=str(workspace),
    )
    with patch("omnigent.host.connect.subprocess.Popen", side_effect=_fake_popen):
        result = await host._handle_launch(launch)
    assert result.status == "launched", result.error

    runner_id = token_bound_runner_id("tok_stop")
    stop_result = await host._handle_stop(
        HostStopRunnerFrame(request_id="req_stop_2", runner_id=runner_id)
    )
    assert stop_result.status == "stopped"

    # Let the watcher observe the (intentional) death and finish.
    await asyncio.wait_for(asyncio.gather(*host._watcher_tasks), timeout=5.0)

    # No runner_exited report and nothing parked for a reconnect —
    # either would mark a clean stop as a crash.
    assert tunnel.sent == []
    assert host._unreported_exits == {}


async def test_watch_runner_silent_on_clean_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner that exits cleanly (code 0) is NOT reported as a crash.

    The runner-level idle reaper shuts an inactive runner down with a graceful
    (exit-code-0) self-exit while it is still tracked in ``self._runners``
    (unlike a ``host.stop_runner``, which pops the handle first). The watcher
    must treat a zero exit code as a clean shutdown and send nothing — a false
    ``host.runner_exited`` here would attach a scary "runner process exited"
    error to a session the user only has to message to reactivate. A non-zero
    exit is still a crash and is covered by
    ``test_watch_runner_reports_unexpected_exit``.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr("omnigent.host.connect._RUNNER_WATCH_INTERVAL_S", 0.01)
    host = _make_host_process()
    tunnel = _FakeTunnel()
    host._ws = tunnel  # type: ignore[assignment] — duck-typed send
    workspace = tmp_path / "project"
    workspace.mkdir()

    original_popen = subprocess.Popen

    def _fake_popen(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        """Spawn a runner that lives briefly, then exits 0 (graceful).

        The initial sleep keeps ``poll()`` None at launch time so the launch
        succeeds; the process then exits cleanly, mimicking an idle-reaper
        shutdown while the handle is still tracked.

        :param args: Command args (ignored).
        :param kwargs: Popen kwargs from production, including log handles.
        :returns: A live subprocess handle.
        """
        return original_popen(
            ["sh", "-c", "sleep 0.2; exit 0"],
            stdin=subprocess.DEVNULL,
            stdout=kwargs["stdout"],
            stderr=kwargs["stderr"],
        )

    frame = HostLaunchRunnerFrame(
        request_id="req_clean",
        binding_token="tok_clean",
        workspace=str(workspace),
    )
    with patch("omnigent.host.connect.subprocess.Popen", side_effect=_fake_popen):
        result = await host._handle_launch(frame)
    assert result.status == "launched", result.error

    # Let the watcher observe the clean exit and finish.
    await asyncio.wait_for(asyncio.gather(*host._watcher_tasks), timeout=5.0)

    # A clean (code 0) exit is graceful, not a crash: no report, nothing parked.
    assert tunnel.sent == []
    assert host._unreported_exits == {}


async def test_unreported_exit_flushes_after_reconnect(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An exit report that raced a disconnect is sent after the next hello.

    A runner can die while the host tunnel is down (server restart,
    network blip). The report parks in ``_unreported_exits`` and must
    flush right after the reconnect hello — otherwise the death is
    never reported and the waiting client polls to its timeout.
    """
    host = _make_host_process()
    host._unreported_exits["runner_parked"] = "runner process exited with code 1"
    tunnel = _FakeTunnel()

    # _serve_frames sends hello, flushes parked reports, then hits the
    # fake recv's immediate disconnect.
    with pytest.raises(ConnectionError, match="test disconnect"):
        await host._serve_frames(tunnel)  # type: ignore[arg-type] — duck-typed ws

    assert len(tunnel.sent) == 2
    hello = decode_host_frame(tunnel.sent[0])
    assert isinstance(hello, HostHelloFrame)
    report = decode_host_frame(tunnel.sent[1])
    assert isinstance(report, HostRunnerExitedFrame)
    assert report.runner_id == "runner_parked"
    assert report.error == "runner process exited with code 1"
    # The queue drained — a retained entry would re-send on every
    # reconnect forever.
    assert host._unreported_exits == {}


async def test_capability_probe_timeout_does_not_block_connection(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A hung startup probe degrades to unknown metadata after its deadline."""
    host = _make_host_process()
    never = asyncio.Event()

    async def _blocked(*, startup: bool) -> None:
        del startup
        await never.wait()

    monkeypatch.setattr(host, "_probe_configured_harnesses", _blocked)
    monkeypatch.setattr(host, "_probe_gateway_inference", _blocked)
    monkeypatch.setattr("omnigent.host.connect._HOST_CAPABILITY_INIT_TIMEOUT_S", 0.01)

    await host._initialize_capabilities()

    assert host._configured_harnesses is None
    assert host._gateway_inference is None
    assert "capability discovery timed out" in capsys.readouterr().err


async def test_capability_probe_failure_does_not_block_registration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Optional startup discovery fails visibly but hello still reaches the server."""

    def _denied() -> dict[str, bool]:
        raise PermissionError(13, "Permission denied", "/usr/local/bin/codex")

    monkeypatch.setattr("omnigent.host.connect.configured_harness_map", _denied)
    monkeypatch.setattr(
        "omnigent.host.connect.gateway_inference_map",
        lambda: {"codex": False},
    )
    host = _make_host_process()
    await host._initialize_capabilities()
    tunnel = _FakeTunnel()

    with pytest.raises(ConnectionError, match="test disconnect"):
        await host._serve_frames(tunnel)  # type: ignore[arg-type]

    hello = decode_host_frame(tunnel.sent[0])
    assert isinstance(hello, HostHelloFrame)
    assert hello.configured_harnesses is None
    assert hello.gateway_inference == {"codex": False}
    assert "Permission denied: '/usr/local/bin/codex'" in capsys.readouterr().err


async def test_hello_advertises_installed_version() -> None:
    """The ``host.hello`` frame reports the omnigent version, not a placeholder.

    A hard-coded placeholder would make every host look like the same
    stale build in the server's version popover. The hello must carry the
    shared resolved version this host is actually running.
    """
    from omnigent.version import VERSION

    host = _make_host_process()
    tunnel = _FakeTunnel()

    with pytest.raises(ConnectionError, match="test disconnect"):
        await host._serve_frames(tunnel)  # type: ignore[arg-type] — duck-typed ws

    hello = decode_host_frame(tunnel.sent[0])
    assert isinstance(hello, HostHelloFrame)
    assert hello.version == VERSION
    # Guard against the old hard-coded literal creeping back.
    assert hello.version != "0.1.0"


async def test_handle_stop_terminates_process(tmp_path: Path) -> None:
    """
    Verify that _handle_stop terminates a tracked runner and
    returns status='stopped'.

    If the process isn't terminated, the runner keeps consuming
    resources after the server requested a stop.
    """
    host = _make_host_process()
    proc = subprocess.Popen(
        ["sleep", "60"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    host._runners["runner_aaa"] = _RunnerHandle(proc=proc, log_path=tmp_path / "runner-a.log")

    frame = HostStopRunnerFrame(
        request_id="req_003",
        runner_id="runner_aaa",
    )
    result = await host._handle_stop(frame)

    assert isinstance(result, HostStopRunnerResultFrame)
    assert result.status == "stopped"
    # Process should be terminated.
    assert proc.poll() is not None, "Runner process should be terminated after stop"
    # Runner should be removed from tracking.
    assert "runner_aaa" not in host._runners


async def test_handle_stop_unknown_runner() -> None:
    """
    Verify that _handle_stop returns status='failed' for an
    unknown runner_id.

    If it returns 'stopped', the stop is silently succeeding for
    a runner that doesn't exist, which could mask bugs.
    """
    host = _make_host_process()
    frame = HostStopRunnerFrame(
        request_id="req_004",
        runner_id="runner_nonexistent",
    )
    result = await host._handle_stop(frame)

    assert isinstance(result, HostStopRunnerResultFrame)
    assert result.status == "failed"
    assert "unknown runner" in (result.error or "")


async def test_handle_runner_status_alive_for_running_process(tmp_path: Path) -> None:
    """
    Verify ``_handle_runner_status`` reports ``alive`` for a tracked
    runner whose process is still running.

    This is the still-booting / still-serving case: the server must wait
    for this runner's tunnel rather than relaunch a healthy process.
    """
    host = _make_host_process()
    proc = subprocess.Popen(
        ["sleep", "60"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        host._runners["runner_live"] = _RunnerHandle(
            proc=proc, log_path=tmp_path / "runner-live.log"
        )
        result = await host._handle_runner_status(
            HostRunnerStatusFrame(request_id="req_rs", runner_id="runner_live")
        )
        assert isinstance(result, HostRunnerStatusResultFrame)
        assert result.request_id == "req_rs"
        assert result.status == "alive"
    finally:
        proc.terminate()
        proc.wait()


async def test_handle_runner_status_dead_for_exited_process(tmp_path: Path) -> None:
    """
    Verify ``_handle_runner_status`` reports ``dead`` for a tracked
    runner whose process has exited.

    A tracked-but-exited runner will never connect its tunnel, so the
    server must relaunch immediately instead of burning the connect
    grace on it.
    """
    host = _make_host_process()
    proc = subprocess.Popen(
        ["true"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.wait()  # ensure the process has exited before we query
    host._runners["runner_gone"] = _RunnerHandle(proc=proc, log_path=tmp_path / "runner-gone.log")
    result = await host._handle_runner_status(
        HostRunnerStatusFrame(request_id="req_rs", runner_id="runner_gone")
    )
    assert isinstance(result, HostRunnerStatusResultFrame)
    assert result.status == "dead"


async def test_handle_runner_status_unknown_for_untracked_runner() -> None:
    """
    Verify ``_handle_runner_status`` reports ``unknown`` for a runner
    this host has no record of.

    Covers a runner that was stopped (``_handle_stop`` popped it) and a
    fresh post-restart host that never spawned it — the exact
    host-restart case that used to strand the server on a full connect
    grace. Both must read ``unknown`` so the server relaunches at once.
    """
    host = _make_host_process()
    result = await host._handle_runner_status(
        HostRunnerStatusFrame(request_id="req_rs", runner_id="runner_never_seen")
    )
    assert isinstance(result, HostRunnerStatusResultFrame)
    assert result.status == "unknown"


def test_alive_runner_ids_cleans_dead(tmp_path: Path) -> None:
    """
    Verify that _alive_runner_ids removes dead processes and returns
    only alive ones.

    If dead runners persist, reconnect reconciliation would report
    runners that are no longer running.
    """
    host = _make_host_process()

    alive_proc = subprocess.Popen(
        ["sleep", "60"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    dead_proc = subprocess.Popen(
        ["true"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    dead_proc.wait()

    host._runners["runner_alive"] = _RunnerHandle(
        proc=alive_proc, log_path=tmp_path / "runner-alive.log"
    )
    host._runners["runner_dead"] = _RunnerHandle(
        proc=dead_proc, log_path=tmp_path / "runner-dead.log"
    )

    alive_ids = host._alive_runner_ids()

    assert "runner_alive" in alive_ids
    assert "runner_dead" not in alive_ids, "Dead runner should be cleaned up"
    assert "runner_dead" not in host._runners, "Dead runner should be removed from tracking dict"

    alive_proc.terminate()
    alive_proc.wait()


def test_cleanup_runners_terminates_all(tmp_path: Path) -> None:
    """
    Verify that _cleanup_runners terminates every tracked runner.

    This is the graceful shutdown path (Ctrl-C / finally block).
    If any runner survives, the host leaves orphaned processes.
    """
    host = _make_host_process()
    procs = []
    for name in ("runner_a", "runner_b", "runner_c"):
        proc = subprocess.Popen(
            ["sleep", "60"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        host._runners[name] = _RunnerHandle(proc=proc, log_path=tmp_path / f"{name}.log")
        procs.append(proc)

    host._cleanup_runners()

    # All processes should be terminated.
    for proc in procs:
        assert proc.poll() is not None, f"Runner pid={proc.pid} should be dead after cleanup"
    # Tracking dict should be empty.
    assert host._runners == {}


def test_reap_orphans_reaps_orphaned_children(tmp_path: Path) -> None:
    """Regression for #1782: orphaned children are reaped, not leaked.

    In production the leak is a harness tool subprocess (node/chromium/tmux)
    whose runner parent died: it is orphaned and reparented to the host
    (PID 1 in a container, or a subreaper). Once reparented it is a *direct*
    child of the host that nothing ``wait()``s — so it lingers as a
    ``<defunct>`` zombie forever. This test models that end state directly by
    forking a child the host does not track and never waits: ``os.fork`` is
    portable (no ``PR_SET_CHILD_SUBREAPER``, which macOS lacks), and a
    reparented orphan is indistinguishable from a plain unwaited child to the
    reaper. ``_reap_orphans_once`` must drain it.
    """
    import errno
    import os

    host = _make_host_process()

    # Fork a bare child (NOT a tracked runner, NOT wrapped in Popen) that
    # exits immediately — the faithful model of an orphan reparented to us.
    pid = os.fork()
    if pid == 0:  # pragma: no cover — child leg never returns to pytest
        os._exit(0)

    # Let it exit and become a zombie parented to this process.
    deadline = time.monotonic() + 5.0
    reaped_total = 0
    while time.monotonic() < deadline:
        reaped_total += host._reap_orphans_once()
        if reaped_total >= 1:
            break
        time.sleep(0.05)

    assert reaped_total >= 1, "orphaned child was not reaped (zombie leak — #1782)"
    # It is truly reaped: a direct waitpid now raises ECHILD (no such child).
    with pytest.raises(OSError) as exc_info:
        os.waitpid(pid, 0)
    assert exc_info.value.errno == errno.ECHILD
    # A second sweep with no orphans left is a clean no-op.
    assert host._reap_orphans_once() == 0


def test_reap_orphans_never_steals_tracked_runner_exit_code(tmp_path: Path) -> None:
    """The reaper must not consume a tracked runner's exit status (#1782).

    A naive ``waitpid(-1)`` reaper would reap a just-exited tracked runner
    behind ``Popen``'s back, making ``_watch_runner``'s ``poll()`` report a
    bogus exit 0 for a crash. ``_reap_orphans_once`` peeks with ``WNOWAIT``
    and skips tracked pids, so the runner's real exit code survives for the
    ``host.runner_exited`` report.
    """
    host = _make_host_process()

    # A tracked runner that exits non-zero (a "crash").
    runner = subprocess.Popen(["python3", "-c", "import sys; sys.exit(42)"])
    host._runners["runner_crash"] = _RunnerHandle(
        proc=runner, log_path=tmp_path / "runner-crash.log"
    )
    # Wait until the OS reports it as exited (zombie), WITHOUT Popen.wait().
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and runner.poll() is None:
        # poll() would itself reap; instead peek via the reaper repeatedly.
        host._reap_orphans_once()
        time.sleep(0.05)

    # The reaper ran while the crashed runner was reapable; it must NOT have
    # stolen it. Popen must still see the true exit code.
    assert runner.poll() == 42, "reaper corrupted the tracked runner's exit code"

    runner.wait()


def test_reaper_does_not_steal_host_owned_subprocess_exit_code(tmp_path: Path) -> None:
    """The reaper must not reap a host-owned subprocess's child (#1782).

    Regression for the Polly-flagged race: the host also spawns *direct*
    children that are not tracked runners — the ``git`` commands in
    :mod:`omnigent.host.git_worktree`, run via ``subprocess.run`` under
    ``asyncio.to_thread`` from the worktree handlers. If the 2s reaper sweep
    fires after such a git child has exited but before ``subprocess``'s own
    ``wait()`` collects it, a blind reaper would ``waitpid`` it — and CPython
    then swallows the resulting ``ECHILD`` and reports ``returncode == 0``,
    silently turning a *failed* ``git worktree`` op into a success.

    ``_host_subprocess_op`` pauses the reaper for exactly that window. Here a
    ``sh -c 'exit 42'`` stands in for a failing git command: while the op is
    marked in flight, ``_reap_orphans_once`` must be a no-op and must NOT
    consume the child, so the owner still reads the true exit code 42.
    """
    host = _make_host_process()

    # A host-owned subprocess (git stand-in) that FAILS with a distinctive
    # code. NOT a tracked runner — indistinguishable from an orphan to a naive
    # reaper.
    proc = subprocess.Popen(["sh", "-c", "exit 42"])
    # Let it exit so it is reapable (the dangerous window subprocess.run has
    # between the child exiting and its internal wait()).
    time.sleep(0.3)

    with host._host_subprocess_op():
        # Every sweep during the op must be a no-op — the reaper is paused.
        for _ in range(5):
            assert host._reap_orphans_once() == 0, "reaper ran during a host-owned op"
            time.sleep(0.02)

    # The owner still collects its child's TRUE exit code — not corrupted to 0.
    assert proc.poll() == 42, "reaper stole the host-owned subprocess's exit code (#1782)"
    proc.wait()


def test_host_subprocess_op_guard_is_reentrant_and_balanced(tmp_path: Path) -> None:
    """``_host_subprocess_op`` nests correctly and always rebalances (#1782).

    The reaper resumes only when the counter returns to 0, and the decrement
    must survive an exception in the guarded body (``finally``), or a raising
    worktree op would wedge the reaper off permanently.
    """
    host = _make_host_process()
    assert host._owned_subprocess_ops == 0

    with host._host_subprocess_op():
        assert host._owned_subprocess_ops == 1
        with host._host_subprocess_op():  # re-entrant
            assert host._owned_subprocess_ops == 2
        assert host._owned_subprocess_ops == 1
    assert host._owned_subprocess_ops == 0

    # An exception inside the guarded body must still rebalance the counter.
    with pytest.raises(RuntimeError):
        with host._host_subprocess_op():
            assert host._owned_subprocess_ops == 1
            raise RuntimeError("worktree op blew up")
    assert host._owned_subprocess_ops == 0, "guard leaked a ref on exception — reaper wedged"


def test_reap_orphans_is_noop_without_wnohang(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows (no ``os.WNOHANG``) the reaper is a clean no-op, not a crash.

    Windows has no child reparenting to a subreaper and no
    ``waitpid(-1, WNOHANG)``; touching ``os.WNOHANG`` there raises
    ``AttributeError``. The final drain runs unguarded in ``run``'s
    ``finally``, so an unguarded sweep would crash shutdown. Simulate the
    platform by deleting ``os.WNOHANG`` and assert a quiet ``0``.
    """
    import os

    monkeypatch.delattr(os, "WNOHANG", raising=False)

    host = _make_host_process()
    assert host._reap_orphans_once() == 0


def test_install_child_subreaper_is_safe_to_call() -> None:
    """``_install_child_subreaper`` never raises and reports a bool.

    ``True`` on Linux where ``prctl`` set the bit; ``False`` on non-Linux or
    when ``prctl`` is unavailable — both are acceptable, non-fatal outcomes.
    """
    import sys

    from omnigent.host.connect import _install_child_subreaper

    result = _install_child_subreaper()
    assert isinstance(result, bool)
    if sys.platform != "linux":
        assert result is False


def test_host_spawned_runner_has_parent_pid_env(
    tmp_path: Path,
) -> None:
    """
    Verify that runners spawned by the host have
    OMNIGENT_RUNNER_PARENT_PID set to the host's PID.

    The runner's parent-PID watchdog uses this to auto-exit when
    the host dies. If the env var is missing or wrong, runners
    would become orphans on hard kill.
    """
    host = _make_host_process()
    workspace = tmp_path / "project"
    workspace.mkdir()

    frame = HostLaunchRunnerFrame(
        request_id="req_pid",
        binding_token="token_for_pid_test",
        workspace=str(workspace),
    )

    import os

    spawned_env: dict[str, str] = {}
    original_popen = subprocess.Popen

    def _capture_env(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        """Capture env vars from the subprocess spawn.

        :param args: Command args.
        :param kwargs: Popen kwargs including env.
        :returns: A real subprocess.
        """
        env = kwargs.get("env", {})
        spawned_env.update(env)
        return original_popen(
            ["sleep", "10"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    with patch("omnigent.host.connect.subprocess.Popen", side_effect=_capture_env):
        import asyncio

        result = asyncio.run(host._handle_launch(frame))

    assert result.status == "launched"
    # RUNNER_PARENT_PID should be the host's own PID.
    parent_pid = spawned_env.get("OMNIGENT_RUNNER_PARENT_PID")
    assert parent_pid == str(os.getpid()), (
        f"Expected RUNNER_PARENT_PID={os.getpid()}, got {parent_pid}. "
        "Without this, the runner watchdog can't detect host death."
    )

    # Clean up.
    _cleanup_host(host)


# ── host.stat handler ───────────────────────────────────


def test_handle_stat_returns_directory_for_existing_dir(
    tmp_path: Path,
) -> None:
    """
    Verify that ``_handle_stat`` reports ``exists: true,
    type: "directory"`` for an existing directory and returns its
    realpath as ``canonical_path``.

    These three fields drive the server-side validation contract
    (path exists, is a directory, canonical_path is what gets
    stored). If any one is wrong, every host-launched session
    would either be rejected or land in the wrong directory.
    """
    host = _make_host_process()
    target = tmp_path / "project"
    target.mkdir()

    result = host._handle_stat(HostStatFrame(request_id="r_dir", path=str(target)))

    assert isinstance(result, HostStatResultFrame)
    assert result.status == "ok"
    assert result.exists is True
    assert result.type == "directory"
    # Compare via os.path.realpath because tmp_path on macOS goes
    # through /var → /private/var symlinks; the design says the
    # stored canonical_path is the realpath.
    import os

    assert result.canonical_path == os.path.realpath(target)
    assert result.error is None


def test_handle_stat_returns_file_for_existing_file(
    tmp_path: Path,
) -> None:
    """
    Verify that ``_handle_stat`` reports ``type: "file"`` for a
    regular file. The validator rejects non-directories at session
    create — without ``type``, it would happily store a file path
    as the workspace and the runner would fail on ``os.chdir``.
    """
    host = _make_host_process()
    target = tmp_path / "README.md"
    target.write_text("hi")

    result = host._handle_stat(HostStatFrame(request_id="r_file", path=str(target)))

    assert result.exists is True
    assert result.type == "file"


def test_handle_stat_follows_symlink_to_directory(
    tmp_path: Path,
) -> None:
    """
    Verify symlinks are followed: symlink to a directory returns
    ``type: "directory"`` and ``canonical_path`` is the target's
    realpath (not the symlink itself).

    This is the load-bearing case for the "symlinks cannot smuggle
    a workspace out of the agent's boundary" guarantee. If the
    canonical_path were the symlink path (not the target), a
    ``cwd: ~/foo`` boundary check would pass for ``~/foo/link →
    /etc``, and the runner would end up in /etc.
    """
    host = _make_host_process()
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real_dir)

    result = host._handle_stat(HostStatFrame(request_id="r_sym", path=str(link)))

    assert result.exists is True
    assert result.type == "directory"
    import os

    assert result.canonical_path == os.path.realpath(real_dir)


def test_handle_stat_dangling_symlink_returns_not_exists(
    tmp_path: Path,
) -> None:
    """
    Verify a symlink pointing at a non-existent target returns
    ``exists: false``.

    Without this collapse, the runner would later fail on chdir
    with a confusing error and no useful surface for the user.
    The design defines this as part of the "exists/not exists"
    contract — see HostStatResultFrame docstring.
    """
    host = _make_host_process()
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "does_not_exist")

    result = host._handle_stat(HostStatFrame(request_id="r_dangling", path=str(dangling)))

    assert result.status == "ok"
    assert result.exists is False
    assert result.canonical_path is None


def test_handle_stat_missing_path_returns_not_exists(
    tmp_path: Path,
) -> None:
    """
    Verify a missing path returns ``exists: false`` (not status
    "failed"). The design treats non-existence as a normal answer,
    not an error — so the route can return a 400 with a clean
    "doesn't exist" message instead of a 500.
    """
    host = _make_host_process()
    missing = tmp_path / "does_not_exist"

    result = host._handle_stat(HostStatFrame(request_id="r_missing", path=str(missing)))

    assert result.status == "ok"
    assert result.exists is False
    assert result.canonical_path is None
    assert result.error is None


def test_handle_stat_permission_denied_returns_not_exists(
    tmp_path: Path,
) -> None:
    """
    Verify a path the host process can't access (EACCES) collapses
    to ``exists: false`` rather than surfacing the EACCES.

    Per the design: v1 collapses ENOENT and EACCES into
    a single answer for simplicity. If we ever distinguish them,
    the wire shape changes (a ``readable`` field gets added);
    this test pins the v1 contract and would fail loud on a
    silent regression.
    """
    host = _make_host_process()
    locked = tmp_path / "locked"
    locked.mkdir()
    # Set permissions to 0 — even the owner cannot stat children.
    # We then ask for a child path to force EACCES rather than
    # ENOENT (the directory itself is still stat-able by its owner).
    import stat as stat_mod

    locked.chmod(0)
    try:
        child = locked / "child"
        result = host._handle_stat(HostStatFrame(request_id="r_eacces", path=str(child)))
        # Many filesystems / kernels return EACCES from stat
        # on the child of a 0-permission dir; some ultra-permissive
        # setups (e.g. macOS as root, certain CI sandboxes) may
        # short-circuit and return ENOENT. Both collapse to
        # exists:false per the design — so we accept either route.
        assert result.status == "ok"
        assert result.exists is False
    finally:
        # Restore so tmp_path cleanup doesn't fail.
        locked.chmod(stat_mod.S_IRWXU)


def test_handle_stat_expands_tilde(tmp_path: Path, monkeypatch) -> None:
    """
    Verify that ``~`` in the input path is expanded to the host
    process owner's home directory.

    The host (not the server) is the source of truth for ``~`` —
    designs/SESSION_WORKSPACE_SELECTION.md. If the host
    handler skipped expansion, agent specs with ``cwd: ~/foo``
    would never resolve and validation would fail on every host.
    """
    # Point HOME at our tmp_path so ~ resolves predictably.
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "subdir"
    target.mkdir()

    host = _make_host_process()
    result = host._handle_stat(HostStatFrame(request_id="r_tilde", path="~/subdir"))

    import os

    assert result.exists is True
    assert result.type == "directory"
    assert result.canonical_path == os.path.realpath(target)


def test_build_runner_env_allowlists_host_env_and_strips_secrets() -> None:
    """
    A spawned runner inherits only allowlisted host env vars — process
    essentials pass through, the host owner's NON-HARNESS secrets do
    not — plus the runner wiring vars. Harness credentials
    (HARNESS_CREDENTIAL_ENV_VARS) are the deliberate exception: the
    host owner provisions those precisely so runners can use them.
    Guards against unrelated host secrets (cloud creds, workspace
    tokens) leaking into every runner subprocess.
    """
    base = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/alice",
        "LANG": "en_US.UTF-8",
        "LC_CTYPE": "UTF-8",
        "DATABRICKS_CONFIG_PROFILE": "ambient",
        "DATABRICKS_CONFIG_FILE": "/tmp/databrickscfg",
        "DATABRICKS_AUTH_STORAGE": "plaintext",
        "ANTHROPIC_API_KEY": "sk-harness",
        "IS_SANDBOX": "1",
        "DATABRICKS_TOKEN": "dapi-secret",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "SOME_RANDOM_VAR": "x",
        "OMNIGENT_CLAUDE_SDK_NO_SANDBOX": "1",
        "KUBECONFIG": "/home/alice/.kube/config",
        "SSH_AUTH_SOCK": "/private/tmp/com.apple.launchd.7Qk/Listeners",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
        "OMNIGENT_DATABRICKS_EXTRA_HEADERS": '{"x-databricks-route-hint": "instance-abc"}',
        "OMNIGENT_LOG_LEVEL": "DEBUG",
        "OMNIGENT_LOG_TO_STDERR": "1",
        "OMNIGENT_LOG_TTY_FD": "9",
    }

    env = _build_runner_env(
        base,
        server_url="http://server",
        runner_id="runner_abc",
        binding_token="tok",
        workspace="/ws",
        parent_pid=42,
        initial_auth_token="host-bootstrap-bearer",
    )

    # Process essentials + the locale family pass through.
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/alice"
    assert env["LANG"] == "en_US.UTF-8"
    assert env["LC_CTYPE"] == "UTF-8"
    # Databricks config selectors are allowlisted ambient passthrough —
    # the ambient value reaches the runner unmodified (no flag override).
    assert env["DATABRICKS_CONFIG_PROFILE"] == "ambient"
    assert env["DATABRICKS_CONFIG_FILE"] == "/tmp/databrickscfg"
    # The token-storage backend selector forwards too — without it the runner
    # falls back to the ~/.databrickscfg default and can read a different token
    # store than the host/daemon, failing to mint a token (runner tunnel 401).
    assert env["DATABRICKS_AUTH_STORAGE"] == "plaintext"
    # Harness credentials forward — they exist FOR the runner's
    # harnesses (laptop: exported keys; managed sandbox: the
    # deployment's injected provider secrets).
    assert env["ANTHROPIC_API_KEY"] == "sk-harness"
    # The sandbox-image environment descriptor forwards: Claude Code
    # needs it to allow --dangerously-skip-permissions under root in
    # sandbox containers. Only the baked host image ever sets it.
    assert env["IS_SANDBOX"] == "1"
    # The claude-sdk sandbox bypass flag forwards — it is read inside the
    # harness, so a bare ``OMNIGENT_CLAUDE_SDK_NO_SANDBOX=1 omnigent run …``
    # must reach the runner without also forcing
    # ``OMNIGENT_RUNNER_ENV_PASSTHROUGH=OMNIGENT_CLAUDE_SDK_NO_SANDBOX``.
    assert env["OMNIGENT_CLAUDE_SDK_NO_SANDBOX"] == "1"
    # KUBECONFIG is a filesystem path (not a secret) — kubectl, helm, k9s
    # need it to resolve the user's cluster contexts and namespaces.
    assert env["KUBECONFIG"] == "/home/alice/.kube/config"
    # SSH_AUTH_SOCK is a socket path on the same footing. Dropping it leaves
    # every runner-spawned context without ssh-agent auth, so git-over-SSH and
    # SSH-cert tooling fail with "dial unix: missing address".
    assert env["SSH_AUTH_SOCK"] == "/private/tmp/com.apple.launchd.7Qk/Listeners"
    # CLAUDE_CODE_SKIP_BEDROCK_AUTH disables AWS SigV4 auth for LiteLLM
    # proxies — a non-secret boolean, same rationale as CLAUDE_CODE_USE_BEDROCK.
    assert env["CLAUDE_CODE_SKIP_BEDROCK_AUTH"] == "1"
    # Opaque request-routing headers forward host→runner so the runner's tunnel
    # and server callbacks reach the same server instance the host registered on
    # (without the operator also listing it in OMNIGENT_RUNNER_ENV_PASSTHROUGH).
    assert (
        env["OMNIGENT_DATABRICKS_EXTRA_HEADERS"] == '{"x-databricks-route-hint": "instance-abc"}'
    )
    # Process logging controls forward so host-spawned runners honor --debug
    # and --log-to-stderr.
    assert env["OMNIGENT_LOG_LEVEL"] == "DEBUG"
    assert env["OMNIGENT_LOG_TO_STDERR"] == "1"
    assert env["OMNIGENT_LOG_TTY_FD"] == "9"
    # Non-harness secrets are stripped — the point of the allowlist.
    assert "DATABRICKS_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    # Non-allowlisted vars are dropped (allowlist, not denylist).
    assert "SOME_RANDOM_VAR" not in env
    # Runner wiring is layered on.
    assert env["RUNNER_SERVER_URL"] == "http://server"
    assert env[RUNNER_ID_ENV_VAR] == "runner_abc"
    assert env[RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR] == "tok"
    assert env[RUNNER_DELEGATED_AUTH_ENV_VAR] == "1"
    assert env[RUNNER_INITIAL_AUTH_TOKEN_ENV_VAR] == "host-bootstrap-bearer"
    assert env[RUNNER_WORKSPACE_ENV_VAR] == "/ws"
    assert env[RUNNER_PARENT_PID_ENV_VAR] == "42"


def test_build_runner_env_forwards_harness_credentials_and_endpoints() -> None:
    """
    Every var in HARNESS_CREDENTIAL_ENV_VARS forwards when present —
    keys AND endpoint wiring (base URLs travel with their credentials
    or gateway setups break in confusing ways). Absent vars are simply
    not set rather than defaulted.
    """
    from omnigent.host.connect import HARNESS_CREDENTIAL_ENV_VARS

    base = {
        "PATH": "/usr/bin",
        "HOME": "/root",
        "ANTHROPIC_API_KEY": "sk-a",
        "ANTHROPIC_BASE_URL": "https://gateway.example.com/anthropic",
        "ANTHROPIC_MODEL": "gateway-served-claude",
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-sub",
        "CODEX_ACCESS_TOKEN": "codex-workspace-token",
        "OPENAI_API_KEY": "sk-o",
        "OPENAI_BASE_URL": "https://gateway.example.com/openai",
        "GEMINI_API_KEY": "g-key",
        "AWS_BEARER_TOKEN_BEDROCK": "absk-fwd",
        "ANTHROPIC_BEDROCK_BASE_URL": "https://bedrock-runtime.us-east-1.amazonaws.com",
        # An unrelated secret must never ride the credential set.
        "MY_UNRELATED_SECRET": "leak-me-not",
    }

    env = _build_runner_env(
        base,
        server_url="http://server",
        runner_id="runner_abc",
        binding_token="tok",
        workspace="/ws",
        parent_pid=42,
    )

    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        # The gateway model pin travels with the key/endpoint — without it a
        # LiteLLM-style gateway session launches Claude Code model-less and
        # the gateway rejects Claude Code's own default model.
        "ANTHROPIC_MODEL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CODEX_ACCESS_TOKEN",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "GEMINI_API_KEY",
        "AWS_BEARER_TOKEN_BEDROCK",
        "ANTHROPIC_BEDROCK_BASE_URL",
    ):
        # Pins each conventional name into the default set — dropping
        # one breaks that harness's credentials on managed sandboxes.
        assert name in HARNESS_CREDENTIAL_ENV_VARS
        assert env[name] == base[name]
    # ANTHROPIC_AUTH_TOKEN wasn't in base: present in the default set
    # but never invented into the env.
    assert "ANTHROPIC_AUTH_TOKEN" in HARNESS_CREDENTIAL_ENV_VARS
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    # A secret not on the credential set / allowlist is not forwarded.
    assert "MY_UNRELATED_SECRET" not in env


def test_build_runner_env_forwards_omnigent_prefixed_harness_credentials() -> None:
    """Prefixed harness credential aliases forward without creating raw names."""
    from omnigent.host.connect import HARNESS_CREDENTIAL_ENV_VARS

    base = {
        "PATH": "/usr/bin",
        "HOME": "/root",
        "OMNIGENT_ANTHROPIC_API_KEY": "sk-prefixed",
    }

    env = _build_runner_env(
        base,
        server_url="http://server",
        runner_id="runner_abc",
        binding_token="tok",
        workspace="/ws",
        parent_pid=42,
    )

    assert "OMNIGENT_ANTHROPIC_API_KEY" in HARNESS_CREDENTIAL_ENV_VARS
    assert env["OMNIGENT_ANTHROPIC_API_KEY"] == "sk-prefixed"
    assert "ANTHROPIC_API_KEY" not in env


def test_build_runner_env_passthrough_extends_forwarded_set() -> None:
    """
    OMNIGENT_RUNNER_ENV_PASSTHROUGH names EXTRA vars to forward (for
    `providers:`-config `env:` refs and custom gateway wiring) without
    opening the allowlist to anything unnamed.
    """
    base = {
        "PATH": "/usr/bin",
        "HOME": "/root",
        "OMNIGENT_RUNNER_ENV_PASSTHROUGH": "MY_GATEWAY_TOKEN, MY_GATEWAY_URL",
        "MY_GATEWAY_TOKEN": "tok-123",
        "MY_GATEWAY_URL": "https://llm.internal.example.com",
        "UNLISTED_SECRET": "nope",
    }

    env = _build_runner_env(
        base,
        server_url="http://server",
        runner_id="runner_abc",
        binding_token="tok",
        workspace="/ws",
        parent_pid=42,
    )

    # Named extras forward (whitespace around commas tolerated).
    assert env["MY_GATEWAY_TOKEN"] == "tok-123"
    assert env["MY_GATEWAY_URL"] == "https://llm.internal.example.com"
    # Anything unnamed stays behind the allowlist.
    assert "UNLISTED_SECRET" not in env


def test_dispatch_trace_context_reaches_runner_but_not_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The dispatch-blessed caller trace context must reach runner (and thus
    harness) subprocesses via the OMNIGENT_OTEL_ allowlist prefix, but a
    long-lived host daemon must NOT inherit it: the daemon outlives the
    dispatch and is reused, so a stale caller context stuck to it would
    funnel every later run into the first caller's dead trace.
    """
    from omnigent.cli import _build_host_daemon_env
    from omnigent.runtime.telemetry import (
        DISPATCH_TRACEPARENT_ENV_VAR,
        DISPATCH_TRACESTATE_ENV_VAR,
    )

    traceparent = "00-9fb2e1cf8fbe9c5ecb7742f04c351500-662a3348b2576ccf-01"

    runner_env = _build_runner_env(
        {
            "PATH": "/usr/bin:/bin",
            DISPATCH_TRACEPARENT_ENV_VAR: traceparent,
            DISPATCH_TRACESTATE_ENV_VAR: "vendor=abc",
        },
        server_url="http://server",
        runner_id="runner_abc",
        binding_token="tok",
        workspace="/ws",
        parent_pid=42,
    )
    assert runner_env[DISPATCH_TRACEPARENT_ENV_VAR] == traceparent
    assert runner_env[DISPATCH_TRACESTATE_ENV_VAR] == "vendor=abc"

    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv(DISPATCH_TRACEPARENT_ENV_VAR, traceparent)
    monkeypatch.setenv(DISPATCH_TRACESTATE_ENV_VAR, "vendor=abc")
    for server_url in (None, "https://example.databricksapps.com"):
        daemon_env = _build_host_daemon_env(server_url=server_url)
        assert DISPATCH_TRACEPARENT_ENV_VAR not in daemon_env
        assert DISPATCH_TRACESTATE_ENV_VAR not in daemon_env


def test_build_runner_env_passthrough_survives_remote_daemon_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OMNIGENT_RUNNER_ENV_PASSTHROUGH forwards a named var through BOTH hops.

    In ``--server`` mode the env crosses two strips: CLI→daemon
    (``_build_host_daemon_env``) then daemon→runner (``_build_runner_env``). The
    control var must survive the first hop or the second never sees the names it
    lists. A named var travels via the ``DATABRICKS_`` prefix on the first hop and
    the passthrough on the second, so it must reach the runner; an unnamed secret
    must not.
    """
    from omnigent.cli import _build_host_daemon_env

    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("OMNIGENT_RUNNER_ENV_PASSTHROUGH", "DATABRICKS_LINEAR_API_KEY")
    monkeypatch.setenv("DATABRICKS_LINEAR_API_KEY", "lin-secret")
    monkeypatch.setenv("DATABRICKS_UNNAMED", "should-not-forward")

    server = "https://example.databricksapps.com"
    daemon_env = _build_host_daemon_env(server_url=server)
    # The first hop must keep the control var (regression guard for the remote no-op).
    assert daemon_env["OMNIGENT_RUNNER_ENV_PASSTHROUGH"] == "DATABRICKS_LINEAR_API_KEY"

    runner_env = _build_runner_env(
        daemon_env,
        server_url=server,
        runner_id="runner_abc",
        binding_token="tok",
        workspace="/ws",
        parent_pid=42,
    )

    # The named var reaches the runner; an unnamed one does not.
    assert runner_env["DATABRICKS_LINEAR_API_KEY"] == "lin-secret"
    assert "DATABRICKS_UNNAMED" not in runner_env


def test_build_runner_env_preserves_ambient_databricks_profile() -> None:
    """
    Ambient Databricks profile/config-file selectors reach host runners.

    Databricks SDK resolution honors ``DATABRICKS_CONFIG_PROFILE`` and
    ``DATABRICKS_CONFIG_FILE``. If the host strips either selector, a
    native Codex runner can fail to resolve a profile that works in the
    user's shell.
    """
    env = _build_runner_env(
        {
            "PATH": "/usr/bin:/bin",
            "DATABRICKS_CONFIG_PROFILE": "oss",
            "DATABRICKS_CONFIG_FILE": "/tmp/databrickscfg",
        },
        server_url="http://server",
        runner_id="runner_abc",
        binding_token="tok",
        workspace="/ws",
        parent_pid=42,
    )

    assert env["DATABRICKS_CONFIG_PROFILE"] == "oss"
    assert env["DATABRICKS_CONFIG_FILE"] == "/tmp/databrickscfg"


def test_build_runner_env_propagates_data_dir_paths_not_db_uri() -> None:
    """
    The runtime data/config-dir PATH vars propagate to runners so the whole
    local chain agrees on where config + data live, but the DB URI (which may
    embed a password) does not.

    Regression guard: ``OMNIGENT_CONFIG_HOME`` was absent from the
    allowlist, so the daemon/runner used ``~/.omnigent`` while a CLI run
    under an isolated config home read the local-server pidfile elsewhere —
    discovery then timed out (the e2e ``OMNIGENT_CONFIG_HOME`` isolation
    case). A failure of the first two asserts means that regression is back;
    a failure of the third means a DB secret can now leak into a (possibly
    hosted) runner.
    """
    base = {
        "PATH": "/usr/bin:/bin",
        "OMNIGENT_CONFIG_HOME": "/tmp/iso-home",
        "OMNIGENT_DATA_DIR": "/tmp/iso-data",
        "OMNIGENT_DATABASE_URI": "postgresql://user:pw@host/db",
    }

    env = _build_runner_env(
        base,
        server_url="http://server",
        runner_id="runner_abc",
        binding_token="tok",
        workspace="/ws",
        parent_pid=42,
    )

    # Path vars propagate — they're how the runner finds the same config/data
    # dir the CLI + daemon + local server use.
    assert env["OMNIGENT_CONFIG_HOME"] == "/tmp/iso-home"
    assert env["OMNIGENT_DATA_DIR"] == "/tmp/iso-data"
    # The DB URI is NOT propagated — it may carry credentials and a runner
    # (hosted or local) has no business holding the server's DB connection.
    assert "OMNIGENT_DATABASE_URI" not in env


def test_build_runner_env_propagates_disable_keyring() -> None:
    """``OMNIGENT_DISABLE_KEYRING`` propagates so the runner resolves
    ``keychain:`` secret refs against the SAME backend the CLI configured.

    Regression guard: with the flag set, ``configure harnesses`` stores pasted
    API keys via the FILE backend (``secrets.json``) and writes
    ``keychain:<name>`` refs. If the flag didn't reach the runner it would fall
    back to the OS keyring and fail with "no stored secret named …" for a key
    the CLI just saved — the headless / file-backend deploy case (and the exact
    failure hit while dogfooding the first-run flow).
    """
    base = {"PATH": "/usr/bin:/bin", "OMNIGENT_DISABLE_KEYRING": "1"}
    env = _build_runner_env(
        base,
        server_url="http://server",
        runner_id="runner_abc",
        binding_token="tok",
        workspace="/ws",
        parent_pid=42,
    )
    assert env["OMNIGENT_DISABLE_KEYRING"] == "1"


# ── host.list_dir handler ───────────────────────────────


def test_handle_list_dir_returns_sorted_entries(tmp_path: Path) -> None:
    """
    Verify ``_handle_list_dir`` returns entries sorted by name with
    correct types and sizes.

    Sorted order matters for the Web UI tree (stable rendering)
    and for cursor-based pagination across pages.
    """
    host = _make_host_process()
    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("hello")
    (tmp_path / "build").mkdir()

    result = host._handle_list_dir(HostListDirFrame(request_id="r1", path=str(tmp_path)))

    assert isinstance(result, HostListDirResultFrame)
    assert result.status == "ok"
    assert result.error is None
    names = [e.name for e in result.entries]
    # Sorted by name — order pinned so cursor pagination is stable.
    assert names == ["README.md", "build", "src"]

    # Type classification.
    by_name = {e.name: e for e in result.entries}
    assert by_name["src"].type == "directory"
    assert by_name["build"].type == "directory"
    assert by_name["README.md"].type == "file"

    # Size only set for files.
    assert by_name["README.md"].bytes == 5  # len("hello")
    assert by_name["src"].bytes is None
    assert by_name["build"].bytes is None


def test_handle_list_dir_missing_path_returns_error(tmp_path: Path) -> None:
    """
    Verify a missing path returns ``status: "ok"`` with an error
    message rather than ``status: "failed"``.

    The design treats missing as a normal answer (so the route
    layer maps it to a 404 with the descriptive error). If we
    surfaced ``status: "failed"`` instead, every missing-directory
    browse would 500.
    """
    host = _make_host_process()
    missing = tmp_path / "does_not_exist"

    result = host._handle_list_dir(HostListDirFrame(request_id="r2", path=str(missing)))

    assert result.status == "ok"
    assert result.error == "path does not exist"
    assert result.entries == []


def test_handle_list_dir_on_file_returns_error(tmp_path: Path) -> None:
    """
    Verify list_dir on a regular file returns "not a directory"
    rather than crashing.

    Without this guard, a user clicking on a file in the picker
    would get a 500 instead of a clear "not a directory" message.
    """
    host = _make_host_process()
    target = tmp_path / "file.txt"
    target.write_text("hi")

    result = host._handle_list_dir(HostListDirFrame(request_id="r3", path=str(target)))

    assert result.status == "ok"
    assert "not a directory" in (result.error or "")
    assert result.entries == []


def test_handle_list_dir_follows_symlink_to_directory(tmp_path: Path) -> None:
    """
    Verify list_dir on a symlinked directory follows the link.

    Same posture as host.stat: symlinks are followed, type
    classification reflects the target. Without this, the picker
    would silently fail to enter symlinked directories.
    """
    host = _make_host_process()
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    (real_dir / "child.txt").write_text("data")
    link = tmp_path / "link"
    link.symlink_to(real_dir)

    result = host._handle_list_dir(HostListDirFrame(request_id="r4", path=str(link)))

    assert result.status == "ok"
    assert len(result.entries) == 1
    assert result.entries[0].name == "child.txt"


def test_handle_list_dir_skips_dangling_symlink_per_entry(
    tmp_path: Path,
) -> None:
    """
    Verify per-entry ``OSError`` (e.g. dangling symlink) is silently
    skipped rather than failing the whole listing.

    Matches the runner's ``list_dir`` posture: a single broken
    symlink in a large directory shouldn't prevent the user from
    seeing the rest. Without the per-entry try/except in the
    handler, a dangling symlink in a project's tree would 500
    every browse of that directory.
    """
    host = _make_host_process()
    good_file = tmp_path / "good.txt"
    good_file.write_text("ok")
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "missing_target")

    result = host._handle_list_dir(HostListDirFrame(request_id="r5", path=str(tmp_path)))

    assert result.status == "ok"
    names = [e.name for e in result.entries]
    # Good entry survived; dangling was silently skipped.
    assert "good.txt" in names
    assert "dangling" not in names


def test_handle_list_dir_expands_tilde(tmp_path: Path, monkeypatch) -> None:
    """
    Verify ``~`` in the input path is expanded against the host
    process owner's home.

    The host owns ``~`` resolution; the server passes tildes
    through unchanged (designs/SESSION_WORKSPACE_SELECTION.md).
    Without expansion, ``~/projects`` would become a
    literal subdir named ``~`` and fail with ENOENT.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "x.txt").write_text("data")

    host = _make_host_process()
    result = host._handle_list_dir(HostListDirFrame(request_id="r6", path="~/subdir"))

    assert result.status == "ok"
    assert len(result.entries) == 1
    assert result.entries[0].name == "x.txt"


def test_handle_list_dir_pagination_after_cursor(tmp_path: Path) -> None:
    """
    Verify that ``after`` cursor returns entries strictly after
    the cursor.

    Cursors are entry paths so they survive concurrent directory
    mutations between calls. Without this, the Web UI's "next
    page" button would silently return the same page or skip
    entries.
    """
    host = _make_host_process()
    for name in ("a.txt", "b.txt", "c.txt", "d.txt", "e.txt"):
        (tmp_path / name).write_text("x")

    result = host._handle_list_dir(
        HostListDirFrame(
            request_id="r7",
            path=str(tmp_path),
            limit=2,
            after=str(tmp_path / "b.txt"),
        )
    )

    assert result.status == "ok"
    names = [e.name for e in result.entries]
    # After "b.txt", next two are "c.txt" and "d.txt".
    assert names == ["c.txt", "d.txt"]
    assert result.has_more is True  # "e.txt" still remains


def test_handle_list_dir_pagination_before_cursor_returns_previous_page(
    tmp_path: Path,
) -> None:
    """
    Verify that ``before`` returns the page immediately before the cursor.

    Backward pagination should take the tail of the bounded slice, not the
    first entries in the directory. Otherwise a request for the page before
    "e.txt" returns "a.txt", "b.txt" instead of "c.txt", "d.txt".
    """
    host = _make_host_process()
    for name in ("a.txt", "b.txt", "c.txt", "d.txt", "e.txt"):
        (tmp_path / name).write_text("x")

    middle = host._handle_list_dir(
        HostListDirFrame(
            request_id="r_before_middle",
            path=str(tmp_path),
            limit=2,
            before=str(tmp_path / "e.txt"),
        )
    )

    assert middle.status == "ok"
    assert [e.name for e in middle.entries] == ["c.txt", "d.txt"]
    assert middle.has_more is True

    first = host._handle_list_dir(
        HostListDirFrame(
            request_id="r_before_first",
            path=str(tmp_path),
            limit=2,
            before=str(tmp_path / "c.txt"),
        )
    )

    assert first.status == "ok"
    assert [e.name for e in first.entries] == ["a.txt", "b.txt"]
    assert first.has_more is False


def test_handle_list_dir_pagination_last_page_has_more_false(
    tmp_path: Path,
) -> None:
    """
    Verify ``has_more=False`` on the last page.

    The Web UI uses ``has_more`` to decide whether to render the
    "next page" button. If we always returned ``True``, the user
    would click forever past the end of the listing.
    """
    host = _make_host_process()
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("x")

    result = host._handle_list_dir(HostListDirFrame(request_id="r8", path=str(tmp_path), limit=10))

    assert result.status == "ok"
    assert len(result.entries) == 2
    assert result.has_more is False


# ── host.create_dir handler ─────────────────────────────


def test_handle_create_dir_creates_directory(tmp_path: Path) -> None:
    """
    Verify ``_handle_create_dir`` makes the directory and returns its
    absolute path.

    This is the picker's "New folder" happy path — the returned path
    is what the picker navigates into afterward.
    """
    host = _make_host_process()
    target = tmp_path / "new-app"

    result = host._handle_create_dir(HostCreateDirFrame(request_id="m1", path=str(target)))

    assert isinstance(result, HostCreateDirResultFrame)
    assert result.status == "ok"
    assert result.error is None
    assert result.path == str(target)
    assert target.is_dir()


def test_handle_create_dir_creates_missing_parents(tmp_path: Path) -> None:
    """
    Verify missing parent directories are created (``os.makedirs``).

    Lets the picker accept a nested name like ``a/b/c`` in one go
    rather than forcing the user to create each level.
    """
    host = _make_host_process()
    target = tmp_path / "a" / "b" / "c"

    result = host._handle_create_dir(HostCreateDirFrame(request_id="m2", path=str(target)))

    assert result.status == "ok"
    assert target.is_dir()


def test_handle_create_dir_existing_returns_error_not_failed(tmp_path: Path) -> None:
    """
    Verify creating an existing directory returns ``status: "ok"`` with
    an "already exists" error rather than ``status: "failed"``.

    The route maps a non-empty ``error`` to a 409 so the picker shows
    "directory already exists" inline; surfacing ``failed`` would 502
    instead.
    """
    host = _make_host_process()
    existing = tmp_path / "dup"
    existing.mkdir()

    result = host._handle_create_dir(HostCreateDirFrame(request_id="m3", path=str(existing)))

    assert result.status == "ok"
    assert result.error == "directory already exists"
    assert result.path is None


def test_handle_create_dir_leaf_is_file_reports_file_not_directory(tmp_path: Path) -> None:
    """
    Verify a regular file at the target path reports a file, not a
    directory.

    ``os.makedirs`` raises ``FileExistsError`` for both an existing
    directory and an existing file; the handler must distinguish them
    so the picker doesn't mislabel "a file is in the way" as
    "directory already exists".
    """
    host = _make_host_process()
    a_file = tmp_path / "taken"
    a_file.write_text("hi")

    result = host._handle_create_dir(HostCreateDirFrame(request_id="m3b", path=str(a_file)))

    assert result.status == "ok"
    assert result.error == "a file already exists at that path"
    assert result.path is None


def test_handle_create_dir_parent_is_file_returns_error(tmp_path: Path) -> None:
    """
    Verify creating under a path whose parent is a regular file returns
    a clean error rather than crashing.
    """
    host = _make_host_process()
    a_file = tmp_path / "file.txt"
    a_file.write_text("hi")
    target = a_file / "child"

    result = host._handle_create_dir(HostCreateDirFrame(request_id="m4", path=str(target)))

    assert result.status == "ok"
    assert "not a directory" in (result.error or "")
    assert result.path is None


def test_handle_create_dir_expands_tilde(tmp_path: Path, monkeypatch) -> None:
    """
    Verify ``~`` expands against the host process owner's home.

    The host owns ``~`` resolution; without expansion ``~/scratch``
    would become a literal ``~`` subdir of the process cwd.
    """
    monkeypatch.setenv("HOME", str(tmp_path))

    host = _make_host_process()
    result = host._handle_create_dir(HostCreateDirFrame(request_id="m5", path="~/scratch"))

    assert result.status == "ok"
    assert (tmp_path / "scratch").is_dir()
    assert result.path == str(tmp_path / "scratch")


# ── host.install_harness handler ────────────────────────


def test_handle_install_harness_success_returns_refreshed_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A successful install returns ``ok`` and the recomputed readiness map.

    The server flips the UI badge off this map, so the handler must run the
    installer and then re-probe readiness, returning the fresh result.
    """
    import omnigent.host.connect as connect

    # Not yet installed, so the handler runs the installer.
    monkeypatch.setattr(connect, "harness_cli_installed", lambda key: False)
    monkeypatch.setattr(connect, "try_install_harness_cli", lambda key: (True, None))
    monkeypatch.setattr(
        connect,
        "configured_harness_map",
        lambda: {"claude-native": True, "codex-native": "needs-auth"},
    )

    host = _make_host_process()
    result = host._handle_install_harness(
        HostInstallHarnessFrame(request_id="i1", harness="claude")
    )

    assert isinstance(result, HostInstallHarnessResultFrame)
    assert result.status == "ok"
    assert result.error is None
    assert result.configured_harnesses == {"claude-native": True, "codex-native": "needs-auth"}


def test_handle_install_harness_already_installed_skips_installer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An already-installed harness returns fresh readiness without running npm.

    ``npm install -g`` re-resolves over the network and can take minutes even
    when the binary is already present, so a re-request must short-circuit —
    otherwise a user clicking Install on an installed harness waits pointlessly
    (and can hit the request timeout).
    """
    import omnigent.host.connect as connect

    monkeypatch.setattr(connect, "harness_cli_installed", lambda key: True)

    def _must_not_install(key: str) -> tuple[bool, str | None]:
        raise AssertionError("installer ran despite the harness already being installed")

    monkeypatch.setattr(connect, "try_install_harness_cli", _must_not_install)
    monkeypatch.setattr(connect, "configured_harness_map", lambda: {"opencode-native": True})

    host = _make_host_process()
    result = host._handle_install_harness(
        HostInstallHarnessFrame(request_id="i0", harness="opencode")
    )

    assert result.status == "ok"
    assert result.configured_harnesses == {"opencode-native": True}


def test_handle_install_harness_failure_surfaces_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A failed install returns ``failed`` with the installer's reason and no
    readiness map (the server keeps its prior view).
    """
    import omnigent.host.connect as connect

    monkeypatch.setattr(connect, "harness_cli_installed", lambda key: False)
    monkeypatch.setattr(
        connect,
        "try_install_harness_cli",
        lambda key: (False, "npm is not available on the host"),
    )

    host = _make_host_process()
    result = host._handle_install_harness(
        HostInstallHarnessFrame(request_id="i2", harness="codex")
    )

    assert result.status == "failed"
    assert result.error == "npm is not available on the host"
    assert result.configured_harnesses is None


def test_handle_install_harness_rejects_non_allowlisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A non-UI-installable harness is refused without invoking the installer.

    Defence in depth: even if a stray frame reaches the daemon, a harness
    whose installer is a ``curl | bash`` (e.g. hermes) must never run.
    """
    import omnigent.host.connect as connect

    def _must_not_install(key: str) -> tuple[bool, str | None]:
        raise AssertionError("installer reached for a non-allowlisted harness")

    monkeypatch.setattr(connect, "try_install_harness_cli", _must_not_install)

    host = _make_host_process()
    result = host._handle_install_harness(
        HostInstallHarnessFrame(request_id="i3", harness="hermes")
    )

    assert result.status == "failed"
    assert result.error is not None and "hermes" in result.error
    assert result.configured_harnesses is None


def test_handle_store_secret_key_writes_and_returns_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key store-secret request calls the core and returns fresh readiness.

    The handler is a thin wrapper: resolve family, call the non-interactive
    core, then recompute readiness so the server flips the badge (yellow →
    green) without a reconnect.
    """
    import omnigent.host.connect as connect
    from omnigent.onboarding.harness_auth import StoreCredentialResult

    calls: list[dict[str, object]] = []

    def _store(**kwargs: object) -> StoreCredentialResult:
        calls.append(kwargs)
        return StoreCredentialResult(True, "anthropic", None)

    monkeypatch.setattr(connect, "store_harness_credential", _store)
    monkeypatch.setattr(connect, "configured_harness_map", lambda: {"claude-native": True})

    host = _make_host_process()
    result = host._handle_store_secret(
        HostStoreSecretFrame(
            request_id="c1",
            harness="claude",
            kind="key",
            secret_value="sk-ant-x",
            default_model="claude-sonnet-4-6",
        )
    )
    assert isinstance(result, HostStoreSecretResultFrame)
    assert result.status == "ok"
    assert result.configured_harnesses == {"claude-native": True}
    # The core was called with the resolved family + the frame's fields.
    assert calls == [
        {
            "family": "anthropic",
            "kind": "key",
            "secret": "sk-ant-x",
            "base_url": None,
            "default_model": "claude-sonnet-4-6",
            "wire_api": None,
        }
    ]


def test_handle_store_secret_pi_maps_to_anthropic_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pi (which consumes both families) writes to its preferred anthropic family."""
    import omnigent.host.connect as connect
    from omnigent.onboarding.harness_auth import StoreCredentialResult

    seen: dict[str, object] = {}

    def _store(**kwargs: object) -> StoreCredentialResult:
        seen.update(kwargs)
        return StoreCredentialResult(True, "anthropic", None)

    monkeypatch.setattr(connect, "store_harness_credential", _store)
    monkeypatch.setattr(connect, "configured_harness_map", lambda: {"pi": True})

    host = _make_host_process()
    result = host._handle_store_secret(
        HostStoreSecretFrame(request_id="c2", harness="pi", kind="key", secret_value="sk-x")
    )
    assert result.status == "ok"
    assert seen["family"] == "anthropic"


def test_handle_store_secret_adopt_calls_adopt_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An adopt request references an env var via the adopt core."""
    import omnigent.host.connect as connect
    from omnigent.onboarding.harness_auth import DetectedCredential, StoreCredentialResult

    seen: dict[str, object] = {}

    def _adopt(**kwargs: object) -> StoreCredentialResult:
        seen.update(kwargs)
        return StoreCredentialResult(True, "openai", None)

    monkeypatch.setattr(connect, "adopt_env_credential", _adopt)
    monkeypatch.setattr(
        connect,
        "detect_adoptable_credentials",
        lambda: [
            DetectedCredential(family="openai", source="$OPENAI_API_KEY", env_var="OPENAI_API_KEY")
        ],
    )
    monkeypatch.setattr(connect, "configured_harness_map", lambda: {"codex-native": True})

    host = _make_host_process()
    result = host._handle_store_secret(
        HostStoreSecretFrame(
            request_id="c3", harness="codex", kind="adopt", env_var="OPENAI_API_KEY"
        )
    )
    assert result.status == "ok"
    assert seen == {"family": "openai", "env_var": "OPENAI_API_KEY"}


def test_handle_store_secret_pi_adopt_uses_detected_family_not_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adopting an OpenAI env var for pi writes it under the OPENAI family.

    pi consumes both anthropic + openai, so the UI can offer $OPENAI_API_KEY as
    adoptable for pi. The harness-derived family for pi is anthropic; adopting
    under it would write an anthropic provider pointing at an OpenAI key
    (mis-routed, fails at run time). The daemon must use the env var's OWN
    detected family instead.
    """
    import omnigent.host.connect as connect
    from omnigent.onboarding.harness_auth import DetectedCredential, StoreCredentialResult

    seen: dict[str, object] = {}

    def _adopt(**kwargs: object) -> StoreCredentialResult:
        seen.update(kwargs)
        return StoreCredentialResult(True, "openai", None)

    monkeypatch.setattr(connect, "adopt_env_credential", _adopt)
    monkeypatch.setattr(
        connect,
        "detect_adoptable_credentials",
        lambda: [
            DetectedCredential(family="openai", source="$OPENAI_API_KEY", env_var="OPENAI_API_KEY")
        ],
    )
    monkeypatch.setattr(connect, "configured_harness_map", lambda: {"pi": True})

    host = _make_host_process()
    result = host._handle_store_secret(
        HostStoreSecretFrame(request_id="c4", harness="pi", kind="adopt", env_var="OPENAI_API_KEY")
    )
    assert result.status == "ok"
    # openai (the env var's family), NOT anthropic (pi's harness-derived default).
    assert seen == {"family": "openai", "env_var": "OPENAI_API_KEY"}


def test_handle_store_secret_adopt_refuses_undetected_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adopt refuses an env var the host didn't detect, without writing.

    The server is owner-authz'd but doesn't otherwise validate env_var, so the
    handler must enforce the adopt boundary: only a var present in
    detect_adoptable_credentials() may be adopted. Otherwise a caller could name
    an arbitrary set env var (a DB password, an unrelated secret) and have it
    persisted as a provider credential and sent to the vendor endpoint as auth.
    """
    import omnigent.host.connect as connect
    from omnigent.onboarding.harness_auth import DetectedCredential

    def _must_not_write(**kwargs: object) -> object:
        raise AssertionError("adopt core reached for an undetected env var")

    monkeypatch.setattr(connect, "adopt_env_credential", _must_not_write)
    # Detect surfaces only ANTHROPIC_API_KEY; the request names a different var.
    monkeypatch.setattr(
        connect,
        "detect_adoptable_credentials",
        lambda: [
            DetectedCredential(
                family="anthropic", source="$ANTHROPIC_API_KEY", env_var="ANTHROPIC_API_KEY"
            )
        ],
    )

    host = _make_host_process()
    result = host._handle_store_secret(
        HostStoreSecretFrame(
            request_id="c6", harness="pi", kind="adopt", env_var="DATABASE_PASSWORD"
        )
    )
    assert result.status == "failed"
    assert result.error is not None and "DATABASE_PASSWORD" in result.error
    assert result.configured_harnesses is None


def test_handle_store_secret_rejects_non_ui_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    """A harness outside the UI-auth families is refused without a write."""
    import omnigent.host.connect as connect

    def _must_not_write(**kwargs: object) -> object:
        raise AssertionError("credential core reached for a non-UI-auth harness")

    monkeypatch.setattr(connect, "store_harness_credential", _must_not_write)
    host = _make_host_process()
    result = host._handle_store_secret(
        HostStoreSecretFrame(request_id="c4", harness="cursor", kind="key", secret_value="x")
    )
    assert result.status == "failed"
    assert result.error is not None and "cursor" in result.error


def test_handle_store_secret_surfaces_core_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A core write failure surfaces its non-secret reason as ``failed``."""
    import omnigent.host.connect as connect
    from omnigent.onboarding.harness_auth import StoreCredentialResult

    monkeypatch.setattr(
        connect,
        "store_harness_credential",
        lambda **k: StoreCredentialResult(False, None, "a gateway requires a base_url"),
    )
    host = _make_host_process()
    result = host._handle_store_secret(
        HostStoreSecretFrame(request_id="c5", harness="codex", kind="gateway", secret_value="x")
    )
    assert result.status == "failed"
    assert result.error == "a gateway requires a base_url"
    assert result.configured_harnesses is None


def test_handle_detect_credentials_returns_non_secret_descriptors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The detect handler returns the core's descriptors as plain dicts."""
    import omnigent.host.connect as connect
    from omnigent.onboarding.harness_auth import DetectedCredential

    monkeypatch.setattr(
        connect,
        "detect_adoptable_credentials",
        lambda: [DetectedCredential("anthropic", "$ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")],
    )
    host = _make_host_process()
    result = host._handle_detect_credentials(HostDetectCredentialsFrame(request_id="d1"))
    assert isinstance(result, HostDetectCredentialsResultFrame)
    assert result.credentials == [
        {"family": "anthropic", "source": "$ANTHROPIC_API_KEY", "env_var": "ANTHROPIC_API_KEY"}
    ]


# --- Fail-loud on permanent tunnel failures ----------------------------
#
# Before the fix, HostProcess.run() caught every connection exception and
# reconnected forever, so an auth/authorization/outdated-server failure
# looked like a silent hang. These tests drive the public run() /
# run_host_process entry points with a stubbed websockets connect whose
# handshake (__aenter__) raises the upgrade rejection, and assert that
# permanent failures fail loud while transient ones still reconnect.


class _HandshakeFailingConnect:
    """Async-CM stand-in for ``websockets.asyncio.client.connect`` whose
    handshake raises a preset exception.

    Models the real library: ``connect(...)`` returns an async context
    manager and the upgrade rejection surfaces from ``__aenter__``.

    :param exc: Exception to raise from ``__aenter__``, e.g. an
        :class:`~websockets.exceptions.InvalidStatus` carrying a 403.
    """

    def __init__(self, exc: BaseException) -> None:
        """Store the exception the handshake will raise.

        :param exc: Exception raised on ``__aenter__``.
        """
        self._exc = exc

    async def __aenter__(self) -> object:
        """Raise the preset handshake exception.

        :returns: Never returns — always raises.
        :raises BaseException: The exception passed at construction.
        """
        raise self._exc

    async def __aexit__(self, *exc_info: object) -> bool:
        """No-op async-CM exit.

        :param exc_info: Standard ``__aexit__`` triple (unused).
        :returns: ``False`` so any exception propagates.
        """
        return False


class _DroppedTunnel:
    """Fake accepted tunnel that drops after *frames_before_drop* frames.

    Lets ``_serve_frames`` send the ``host.hello`` frame (proving the
    upgrade was accepted), serves the scripted number of inbound frames
    (undecodable junk the dispatcher ignores), then fails ``recv()`` like
    an abruptly closed connection, returning control to the reconnect
    loop.

    :param frames_before_drop: Inbound frames to deliver before dropping;
        ``0`` (the default) models an accepted-but-silent connection.
    """

    def __init__(self, frames_before_drop: int = 0) -> None:
        """Store the inbound-frame script."""
        self._frames_left = frames_before_drop

    async def send(self, data: str | bytes) -> None:
        """Accept any outbound frame (the ``host.hello``) silently.

        :param data: Encoded frame payload (ignored).
        :returns: None.
        """

    async def recv(self) -> str:
        """Serve scripted frames, then drop."""
        if self._frames_left > 0:
            self._frames_left -= 1
            return "not-a-decodable-frame"
        raise ConnectionClosedError(None, None)


class _AcceptingConnect:
    """Async-CM stand-in for a *successful* WS upgrade.

    ``__aenter__`` hands back a :class:`_DroppedTunnel`, so the host
    marks the connection authenticated and then loses it after the
    scripted number of inbound frames — the minimal "connected once"
    event, silent by default.

    :param frames_before_drop: Passed through to :class:`_DroppedTunnel`.
    """

    def __init__(self, frames_before_drop: int = 0) -> None:
        """Store the tunnel's inbound-frame script.

        :param frames_before_drop: Frames the tunnel serves before dropping.
        """
        self._frames_before_drop = frames_before_drop

    async def __aenter__(self) -> _DroppedTunnel:
        """Complete the handshake with a tunnel that later drops.

        :returns: A :class:`_DroppedTunnel`.
        """
        return _DroppedTunnel(self._frames_before_drop)

    async def __aexit__(self, *exc_info: object) -> bool:
        """No-op async-CM exit.

        :param exc_info: Standard ``__aexit__`` triple (unused).
        :returns: ``False`` so any exception propagates.
        """
        return False


class _ConnectSpy:
    """Stub for ``websockets.asyncio.client.connect`` that records calls
    and scripts each handshake with the next queued entry.

    An exception entry fails that handshake; a ``None`` entry accepts it
    with a tunnel that drops on first ``recv()`` (so the reconnect loop
    regains control); an int entry accepts it with a tunnel that serves
    that many inbound frames before dropping. The last queued entry
    repeats for any further calls, so a single fatal exception covers
    the "fails on first attempt" case and a ``[transient,
    CancelledError]`` pair covers "retried once, then stop".

    :param exceptions: Per-call handshake script, e.g.
        ``[None, InvalidStatus(resp_503), asyncio.CancelledError()]``.
    """

    def __init__(self, exceptions: list[BaseException | int | None]) -> None:
        """Initialize the spy with a handshake script.

        :param exceptions: Exception to raise (``None`` or an int frame
            count to accept) on each successive call; the final entry
            repeats.
        """
        self._exceptions = exceptions
        self.call_count = 0
        self.calls: list[dict[str, object]] = []

    def __call__(self, url: str, **kwargs: object) -> _HandshakeFailingConnect | _AcceptingConnect:
        """Return an async-CM scripting the handshake for this call.

        :param url: Tunnel URL passed by production (ignored).
        :param kwargs: Connect kwargs passed by production (recorded).
        :returns: A context manager whose ``__aenter__`` raises the
            queued exception, or completes the handshake for a ``None``
            entry.
        """
        self.calls.append(kwargs)
        exc = self._exceptions[min(self.call_count, len(self._exceptions) - 1)]
        self.call_count += 1
        if exc is None or isinstance(exc, int):
            return _AcceptingConnect(exc or 0)
        return _HandshakeFailingConnect(exc)


def _invalid_status(status_code: int, body: bytes = b"") -> InvalidStatus:
    """Build a real :class:`InvalidStatus` for a rejected WS upgrade.

    Matches what ``websockets`` raises when the server answers the
    upgrade with a non-101 status.

    :param status_code: HTTP status on the upgrade response, e.g.
        ``403``.
    :param body: Optional response body the server sent with the refusal
        (what ``_refuse_upgrade`` emits), so a test can assert it is
        surfaced to the operator.
    :returns: An ``InvalidStatus`` whose ``response.status_code`` is
        *status_code*.
    """
    return InvalidStatus(Response(status_code, "", Headers(), body))


def _patch_connect(monkeypatch: pytest.MonkeyPatch, spy: _ConnectSpy) -> None:
    """Stub the WS connect and silence the auth-token factory.

    Patches ``websockets.asyncio.client.connect`` (the module attribute
    production resolves at call time) with *spy*, and forces
    ``_make_auth_token_factory`` to return ``None`` so no real Databricks
    credentials/network are touched.

    :param monkeypatch: The pytest monkeypatch fixture.
    :param spy: Connect stub to install.
    :returns: None.
    """
    import websockets.asyncio.client as ws_client

    import omnigent.runner._entry as entry_mod

    monkeypatch.setattr(entry_mod, "_make_auth_token_factory", lambda *, server_url=None: None)
    monkeypatch.setattr(ws_client, "connect", spy)


def _host(
    server_url: str = "https://app.example.databricks.com",
) -> HostProcess:
    """Build a HostProcess for the fail-loud tests.

    :param server_url: Server URL the host connects to, e.g.
        ``"https://app.example.databricks.com"``.
    :returns: A configured :class:`HostProcess`.
    """
    identity = HostIdentity(host_id="host_test_connect", name="test-laptop")
    return HostProcess(identity, server_url)


def test_build_connect_headers_adds_org_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recorded ?o= selector rides the tunnel handshake.

    The WS upgrade must name the workspace via ``X-Databricks-Org-Id`` or it
    routes to the account. The header rides alongside the Origin sentinel,
    independent of the bearer/managed-token branch.

    :param monkeypatch: The pytest monkeypatch fixture.
    :returns: None.
    """
    import omnigent.runner._entry as entry_mod

    # No managed token + no real Databricks creds: isolate the bearer
    # branch so only the routing header is under test.
    monkeypatch.delenv("OMNIGENT_HOST_TOKEN", raising=False)
    monkeypatch.setattr(entry_mod, "_make_auth_token_factory", lambda *, server_url=None: None)
    monkeypatch.setattr(
        "omnigent.cli_auth.load_databricks_org_id", lambda _url: "2850744067564480"
    )

    headers = _host("https://acme.databricks.com/api/2.0/omnigent")._build_connect_headers()

    assert headers["X-Databricks-Org-Id"] == "2850744067564480"


def test_build_connect_headers_retains_auth_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host reconnects and runner launches share one warm auth factory."""
    import omnigent.runner._entry as entry_mod

    factory_builds: list[str | None] = []
    token_calls: list[int] = []

    def _factory() -> str:
        token_calls.append(1)
        return "warm-host-token"

    def _make_factory(*, server_url: str | None = None) -> object:
        factory_builds.append(server_url)
        return _factory

    monkeypatch.delenv("OMNIGENT_HOST_TOKEN", raising=False)
    monkeypatch.setattr(entry_mod, "_make_auth_token_factory", _make_factory)

    host = _host("https://app.example.databricksapps.com")
    first = host._build_connect_headers()
    second = host._build_connect_headers()
    launch_token = host._current_auth_token(initialize=False)

    assert first["Authorization"] == "Bearer warm-host-token"
    assert second["Authorization"] == "Bearer warm-host-token"
    assert launch_token == "warm-host-token"
    assert factory_builds == ["https://app.example.databricksapps.com"]
    assert token_calls == [1, 1, 1]


def test_build_connect_headers_slice_key_only_on_workspace_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The slice key rides the handshake only on a host-sharded mount.

    A host-sharded server (``/api/2.0/omnigent``) routes through the
    sharding layer, so the host pins its tunnel with the slice key (its host_id).
    An unsharded / local server has no sharding layer, so the key is omitted to
    keep that server's logs clean.

    :param monkeypatch: The pytest monkeypatch fixture.
    :returns: None.
    """
    import omnigent.runner._entry as entry_mod

    # Isolate the slice-key branch from the bearer/managed-token paths.
    monkeypatch.delenv("OMNIGENT_HOST_TOKEN", raising=False)
    monkeypatch.setattr(entry_mod, "_make_auth_token_factory", lambda *, server_url=None: None)

    workspace = _host("https://acme.databricks.com/api/2.0/omnigent")._build_connect_headers()
    assert workspace["X-Databricks-Omnigent-Slice-Key"] == "host_test_connect"

    self_hosted = _host("http://127.0.0.1:6767")._build_connect_headers()
    assert "X-Databricks-Omnigent-Slice-Key" not in self_hosted


def test_build_runner_env_carries_host_id() -> None:
    """The runner is told its host id so its tunnel co-locates with the host's.

    The env var carries the host_id whenever one is present, regardless of the
    server — the runner forwards it to ``databricks_request_headers``, which
    emits the routing header only on a host-sharded deployment (see
    ``test_cli_auth.test_databricks_request_headers_slice_key``). With no
    host_id (a CLI-local runner), the env var is omitted.
    """
    from omnigent.runner.identity import RUNNER_SLICE_KEY_ENV_VAR

    def _env(*, server_url: str, host_id: str | None) -> dict[str, str]:
        return _build_runner_env(
            {},
            server_url=server_url,
            runner_id="runner_abc",
            binding_token="tok",
            workspace="/ws",
            parent_pid=123,
            host_id=host_id,
        )

    # Set from host_id on any server (gating lives in the header builder).
    for server_url in (
        "https://acme.databricks.com/api/2.0/omnigent",
        "http://127.0.0.1:6767",
    ):
        assert _env(server_url=server_url, host_id="host_x")[RUNNER_SLICE_KEY_ENV_VAR] == "host_x"

    # No host (CLI-local runner) → omitted.
    assert RUNNER_SLICE_KEY_ENV_VAR not in _env(
        server_url="https://acme.databricks.com/api/2.0/omnigent", host_id=None
    )


def test_build_runner_env_carries_launch_harness() -> None:
    """The runner is told its session's harness so it can prewarm at boot.

    claude-native runners use the stamp to start ambient provider detection
    (a ~0.6-0.9s ``claude auth status`` subprocess on macOS) during boot
    instead of inside terminal auto-create. With no harness (an older server
    that doesn't resolve one), the env var is omitted.
    """

    def _env(*, harness: str | None) -> dict[str, str]:
        return _build_runner_env(
            {},
            server_url="http://127.0.0.1:6767",
            runner_id="runner_abc",
            binding_token="tok",
            workspace="/ws",
            parent_pid=123,
            harness=harness,
        )

    assert _env(harness="claude-native")[RUNNER_LAUNCH_HARNESS_ENV_VAR] == "claude-native"
    assert RUNNER_LAUNCH_HARNESS_ENV_VAR not in _env(harness=None)


async def test_run_retries_on_login_redirect(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A login-redirect upgrade failure retries with a warning.

    The Databricks Apps OAuth proxy bounces an unauthenticated ``wss://``
    upgrade to an ``https://.../authorize`` URL; ``websockets`` then raises
    ``InvalidURI`` because the redirect scheme isn't ws/wss. This can
    happen transiently during server restarts, so the host must retry
    with backoff rather than dying. The warning still surfaces the single
    ``omnigent login`` remediation so the operator can act if the cause
    is persistent.
    """
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    spy = _ConnectSpy(
        [
            InvalidURI("https://w/oidc/authorize", "scheme isn't ws or wss"),
            asyncio.CancelledError(),
        ]
    )
    _patch_connect(monkeypatch, spy)
    host = _host()

    with caplog.at_level(logging.WARNING, logger="omnigent.host.connect"):
        await host.run()

    # 2 = redirect attempt + cancel attempt → it genuinely reconnected.
    assert spy.call_count == 2
    # The warning surfaces the login-page cause and the credentials hint —
    # the single remediation message recommending `omnigent login <url>`.
    assert any("login page" in r.message for r in caplog.records)
    assert any(
        "omnigent login https://app.example.databricks.com" in r.message for r in caplog.records
    )


async def test_login_redirect_prints_warning_to_terminal(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The first login redirect of a streak warns on stderr, not just the log.

    ``_logger.warning`` goes to the CLI log file, so before this fix a
    not-logged-in ``omnigent host`` sat completely silent on the terminal
    while retrying (the user's only signal was Ctrl-C and reading the log).
    The terminal warning must name the cause and the copy-pasteable
    ``omnigent login <url>`` remedy.
    """
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    spy = _ConnectSpy(
        [
            InvalidURI("https://w/oidc/authorize", "scheme isn't ws or wss"),
            asyncio.CancelledError(),
        ]
    )
    _patch_connect(monkeypatch, spy)
    host = _host()

    await host.run()

    err = capsys.readouterr().err
    # The cause reached the terminal — if missing, the silent-hang
    # regression is back (warning only in the log file).
    assert "login page" in err
    # The exact remedy command, URL included, so the user can copy-paste.
    assert "omnigent login https://app.example.databricks.com" in err


async def test_fresh_host_fails_loud_after_persistent_login_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent login redirects on a never-connected host fail loud.

    A host that has never completed an upgrade and keeps getting bounced
    to the login page is unauthenticated, not watching a server restart —
    it must raise HostConnectError (→ exit 1 with the fix printed) instead
    of retrying forever with the terminal silent (the silent-hang regression
    could resurface once the redirect was made retryable).
    """
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    # A single queued redirect repeats forever — the streak only ends
    # because the host gives up.
    spy = _ConnectSpy([InvalidURI("https://w/oidc/authorize", "scheme isn't ws or wss")])
    _patch_connect(monkeypatch, spy)
    host = _host()

    with pytest.raises(HostConnectError) as excinfo:
        await host.run()

    message = str(excinfo.value)
    # The fatal message identifies the auth cause and the exact remedy.
    assert "login page" in message
    assert "omnigent login https://app.example.databricks.com" in message
    # 3 = _LOGIN_REDIRECT_FATAL_ATTEMPTS: enough retries to absorb a
    # one-off proxy blip, then fail. 1 would mean the blip
    # tolerance regressed; more (or no raise at all) would mean the
    # fail-loud threshold is broken and the host loops silently again.
    assert spy.call_count == 3


async def test_login_redirect_streak_resets_on_other_transient_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only CONSECUTIVE login redirects count toward the fatal threshold.

    A messy server restart can interleave login redirects with other
    transient failures (5xx bounces). Those must reset the streak —
    otherwise a fresh host riding out a restart would die from redirects
    accumulated across unrelated errors instead of three in a row.
    """
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    redirect = InvalidURI("https://w/oidc/authorize", "scheme isn't ws or wss")
    # Two redirects, then a 503 (must reset the streak), then redirects
    # repeating forever until the host gives up.
    spy = _ConnectSpy([redirect, redirect, _invalid_status(503), redirect])
    _patch_connect(monkeypatch, spy)
    host = _host()

    with pytest.raises(HostConnectError):
        await host.run()

    # 6 = 2 redirects + the streak-resetting 503 + 3 consecutive
    # redirects that hit the fatal threshold. 5 would mean the 503 did
    # NOT reset the streak (the pre-restart redirects were counted),
    # killing fresh hosts during messy restarts.
    assert spy.call_count == 6


async def test_connected_host_retries_login_redirects_indefinitely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Login redirects after a successful connect never turn fatal.

    A host that already authenticated and then gets login redirects is
    watching a server restart behind the Apps OAuth proxy —
    killing it would drop its live runners. It must keep retrying past
    the fresh-host fatal threshold.
    """
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    redirect = InvalidURI("https://w/oidc/authorize", "scheme isn't ws or wss")
    # Accepted upgrade first (None), then MORE redirects than the
    # fresh-host fatal threshold of 3, then a cancel to end the test.
    spy = _ConnectSpy([None, redirect, redirect, redirect, redirect, asyncio.CancelledError()])
    _patch_connect(monkeypatch, spy)
    host = _host()

    # Returns normally: if the post-connect redirects were misclassified
    # as fatal this would raise HostConnectError after the 3rd redirect
    # (call_count 4) instead.
    await host.run()

    # 6 = accepted connect + 4 retried redirects + the ending cancel.
    # Fewer means the host died mid-streak (restart killed a live host).
    assert spy.call_count == 6


@pytest.mark.parametrize("status", [401, 403])
async def test_connected_host_retries_auth_rejection_indefinitely(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """401/403 after a successful connect never turns fatal.

    A host that already completed an upgrade proved its credentials and
    authorization are valid. A later 401/403 is almost always a dropped
    VPN whose corporate proxy answers the upgrade before it reaches the
    server — killing the host would drop its live runners and force a
    manual ``omnigent host`` restart once the VPN reconnects. It must keep
    retrying past the fresh-host fatal threshold instead.
    """
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    rejection = _invalid_status(status)
    # Accepted upgrade first (None), then several rejections (more than the
    # fresh-host login-redirect fatal threshold of 3 to prove there is no
    # cap), then a cancel to end the test.
    spy = _ConnectSpy([None, rejection, rejection, rejection, rejection, asyncio.CancelledError()])
    _patch_connect(monkeypatch, spy)
    host = _host()

    # Returns normally: if the post-connect rejections were still treated as
    # fatal this would raise HostConnectError on the first one (call_count 2).
    await host.run()

    # 6 = accepted connect + 4 retried rejections + the ending cancel.
    assert spy.call_count == 6


@pytest.mark.parametrize("status", [401, 403])
async def test_connected_host_auth_rejection_prints_notice_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: int,
) -> None:
    """The first auth rejection of an outage warns on stderr, exactly once.

    ``_logger.warning`` goes to the CLI log file, so a foreground
    ``omnigent host`` would sit silent while it retried a dropped VPN. The
    terminal notice must name the cause and mention VPN/network, and must
    print only once per outage (not on every retry) so it doesn't spam
    stderr while connectivity is down.
    """
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    rejection = _invalid_status(status)
    spy = _ConnectSpy([None, rejection, rejection, rejection, asyncio.CancelledError()])
    _patch_connect(monkeypatch, spy)
    host = _host()

    await host.run()

    err = capsys.readouterr().err
    # The cause reached the terminal, naming the transient network hint.
    assert f"HTTP {status}" in err
    assert "VPN" in err
    # Printed once for the whole outage, not once per retry — three
    # consecutive rejections must yield a single notice line.
    assert err.count(f"HTTP {status}") == 1


@pytest.mark.parametrize("status", [401, 403])
async def test_fresh_host_retries_auth_rejection_before_failing_loud(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    """A never-connected host retries auth errors before failing loud.

    Databricks OAuth can refresh after daemon start but before a later
    tunnel upgrade. The host gets a few attempts, then still raises a
    clear credential failure instead of looping forever.
    """
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    spy = _ConnectSpy([_invalid_status(status)])
    _patch_connect(monkeypatch, spy)
    host = _host()

    with pytest.raises(HostConnectError):
        await host.run()

    assert spy.call_count == 3


@pytest.mark.parametrize(
    "status,expected",
    [
        (404, "permanent"),
    ],
)
async def test_run_fails_loud_on_permanent_4xx(
    monkeypatch: pytest.MonkeyPatch, status: int, expected: str
) -> None:
    """A non-auth permanent 4xx upgrade rejection fails loud immediately.

    A wrong or old server cannot recover through reconnecting, so run()
    must raise HostConnectError rather than backing off.
    """
    spy = _ConnectSpy([_invalid_status(status)])
    _patch_connect(monkeypatch, spy)
    host = _host()

    with pytest.raises(HostConnectError) as excinfo:
        await host.run()

    # Message identifies the specific permanent failure.
    assert expected in str(excinfo.value)
    assert spy.call_count == 1


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_rejection_suggests_omnigent_login(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """401/403 rejections point the user at ``omnigent login``.

    Both statuses are auth failures the user can resolve by logging in to
    an accounts/OIDC-mode server (a Databricks profile token may
    authenticate at the proxy yet be rejected by the server itself). The
    fatal message must name the exact remedy command, including the
    server URL, so the user can copy-paste it.
    """
    server_url = "https://app.example.databricks.com"
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    spy = _ConnectSpy([_invalid_status(status)])
    _patch_connect(monkeypatch, spy)
    host = _host(server_url=server_url)

    with pytest.raises(HostConnectError) as excinfo:
        await host.run()

    # The exact, copy-pasteable command — URL included — must be present,
    # not just the bare word "login".
    assert f"omnigent login {server_url}" in str(excinfo.value)


async def test_non_auth_permanent_4xx_omits_login_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-auth permanent 4xx (404) does NOT suggest ``omnigent login``.

    The login remedy is specific to credential/authorization failures;
    surfacing it on a 404 ("wrong URL / route missing") would misdirect
    the user. This pins the hint to the 401/403 paths only.
    """
    spy = _ConnectSpy([_invalid_status(404)])
    _patch_connect(monkeypatch, spy)
    host = _host()

    with pytest.raises(HostConnectError) as excinfo:
        await host.run()

    message = str(excinfo.value)
    # Confirm we got the actual 404 fatal message (not some unrelated
    # error that happens to lack "login") before asserting the absence.
    assert "HTTP 404" in message
    assert "omnigent login" not in message


async def test_409_rejection_names_the_self_service_reset_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 409 refusal points at `omnigent host reset-id`, not just an admin.

    The 409 means the machine's host id is owned by another identity (e.g.
    an M2M service principal that won credential resolution earlier). The
    user must get a copy-pasteable self-service recovery command instead of
    being told only to find an administrator.
    """
    spy = _ConnectSpy([_invalid_status(409)])
    _patch_connect(monkeypatch, spy)
    host = _host()

    with pytest.raises(HostConnectError) as excinfo:
        await host.run()

    message = str(excinfo.value)
    assert "HTTP 409" in message
    assert "host reset-id" in message


@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_run_reconnects_on_transient_upgrade_failure(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """Transient upgrade failures reconnect instead of failing loud.

    Retryable 4xx (408/429) and any 5xx are transient (server bounce,
    rate limit) and must stay on the reconnect path. A clean
    ``CancelledError`` on the second attempt ends the loop so the test
    terminates; with backoff zeroed the retry is immediate.
    """
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    spy = _ConnectSpy([_invalid_status(status), asyncio.CancelledError()])
    _patch_connect(monkeypatch, spy)
    host = _host()

    # Returns normally (no HostConnectError): the transient failure was
    # retried, and the 2nd-attempt CancelledError broke the loop. If the
    # transient status had been treated as fatal, this would raise
    # HostConnectError instead and call_count would be 1.
    await host.run()

    # 2 = transient attempt + cancel attempt → it genuinely reconnected.
    assert spy.call_count == 2


async def test_reconnect_uses_shorter_handshake_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only reconnects use the shorter open timeout; cold startup stays tolerant."""
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    monkeypatch.setattr("omnigent.host.connect.configured_harness_map", dict)
    monkeypatch.setattr("omnigent.host.connect.gateway_inference_map", dict)
    spy = _ConnectSpy([None, asyncio.CancelledError()])
    _patch_connect(monkeypatch, spy)
    host = _host()

    await host.run()

    assert [call["open_timeout"] for call in spy.calls] == [10.0, 3.0]


async def test_host_records_accepted_connection_and_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The host reports lifecycle metrics only after an accepted upgrade."""
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    monkeypatch.setattr("omnigent.host.connect.configured_harness_map", dict)
    monkeypatch.setattr("omnigent.host.connect.gateway_inference_map", dict)
    spy = _ConnectSpy([None, asyncio.CancelledError()])
    _patch_connect(monkeypatch, spy)
    connected: list[tuple[str, bool]] = []
    disconnected: list[tuple[str, BaseException | None]] = []
    monkeypatch.setattr(
        "omnigent.host.connect.record_websocket_connected",
        lambda kind, *, reconnect: connected.append((kind, reconnect)),
    )
    monkeypatch.setattr(
        "omnigent.host.connect.record_websocket_disconnected",
        lambda kind, error, **_kwargs: disconnected.append((kind, error)),
    )

    await _host().run()

    assert connected == [("host", False)]
    assert len(disconnected) == 1
    assert disconnected[0][0] == "host"
    assert isinstance(disconnected[0][1], ConnectionClosedError)


def _refused_exc() -> ConnectionRefusedError:
    """A single-stack connection-refused, as asyncio raises it.

    :returns: ``ConnectionRefusedError`` with the platform's errno set.
    """
    return ConnectionRefusedError(errno.ECONNREFUSED, "Connect call failed ('127.0.0.1', 5566)")


def _dual_stack_refused_exc() -> OSError:
    """The combined dual-stack refusal asyncio raises for ``localhost``.

    ``create_connection`` folds the per-address failures into one
    ``OSError`` whose errno is lost — only the per-address ``[Errno N]``
    texts survive in the message.

    :returns: ``OSError`` shaped like asyncio's combined failure.
    """
    e = errno.ECONNREFUSED
    return OSError(
        f"Multiple exceptions: [Errno {e}] Connect call failed ('::1', 5566, 0, 0), "
        f"[Errno {e}] Connect call failed ('127.0.0.1', 5566)"
    )


@pytest.mark.parametrize(
    ("refused_exc", "server_url"),
    [
        (_refused_exc(), "http://127.0.0.1:5566"),
        (_dual_stack_refused_exc(), "http://localhost:5566"),
    ],
    ids=["single-stack", "dual-stack-combined"],
)
async def test_loopback_host_exits_after_sustained_connection_refused(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    refused_exc: OSError,
    server_url: str,
) -> None:
    """Persistent connection-refused against a loopback server exits the host.

    A dead local server never comes back on its own, yet the reconnect
    loop retried forever at the backoff cap — zombie hosts looped for
    days against dead loopback ports. After a bounded refused streak the
    host must log one clear ERROR and exit via the permanent-failure
    path. Dual-stack refusals arrive as asyncio's combined ``OSError``
    (errno lost) and must count exactly the same.
    """
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    monkeypatch.setattr("omnigent.host.connect._LOOPBACK_REFUSED_FATAL_ATTEMPTS", 3)
    spy = _ConnectSpy([refused_exc])
    _patch_connect(monkeypatch, spy)
    host = _host(server_url)

    with caplog.at_level(logging.ERROR, logger="omnigent.host.connect"):
        with pytest.raises(HostConnectError) as excinfo:
            await host.run()

    # Exactly the threshold — no premature death, no infinite loop.
    assert spy.call_count == 3
    # The fatal message names the dead address (printed by run_host_process).
    assert server_url in str(excinfo.value)
    # ONE clear ERROR explaining the exit, not one per retry.
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert server_url in errors[0].message
    assert "no server is listening" in errors[0].message


async def test_remote_host_retries_connection_refused_past_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connection-refused against a REMOTE server never turns fatal.

    A refusal from a remote endpoint can be an outage or a rolling
    deploy — it recovers, so the host must keep retrying past the
    loopback fatal threshold. Only loopback refusals prove the server
    is gone for good.
    """
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    monkeypatch.setattr("omnigent.host.connect._LOOPBACK_REFUSED_FATAL_ATTEMPTS", 3)
    refused = _refused_exc()
    # More refusals than the threshold, then a cancel to end the test.
    spy = _ConnectSpy([refused, refused, refused, refused, asyncio.CancelledError()])
    _patch_connect(monkeypatch, spy)
    host = _host()

    # Returns normally: a fatal misfire would raise HostConnectError on
    # the 3rd refusal (call_count 3) instead of reaching the cancel.
    await host.run()

    # 5 = 4 retried refusals (past the threshold of 3) + the ending cancel.
    assert spy.call_count == 5


async def test_accepted_connect_resets_loopback_refused_streak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful connect resets the loopback refused streak.

    Only CONSECUTIVE refusals may kill the host: a streak interrupted by
    a real connection (server came back, then died again) must start
    counting from zero, not inherit pre-recovery refusals.
    """
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    monkeypatch.setattr("omnigent.host.connect._LOOPBACK_REFUSED_FATAL_ATTEMPTS", 3)
    # The accepted connect sends a real hello; skip its slow CLI probes —
    # only the streak accounting is under test here.
    monkeypatch.setattr("omnigent.host.connect.configured_harness_map", dict)
    refused = _refused_exc()
    # 2 refusals (one short of fatal), an accepted upgrade (its tunnel
    # drops on first recv), then refusals repeating until fatal.
    spy = _ConnectSpy([refused, refused, None, refused])
    _patch_connect(monkeypatch, spy)
    host = _host("http://localhost:5566")

    with pytest.raises(HostConnectError):
        await host.run()

    # 6 = 2 refusals + accepted connect + 3 FRESH refusals to reach the
    # threshold. 5 would mean the accepted connect did not reset the
    # streak (pre-recovery refusals were counted toward the exit).
    assert spy.call_count == 6


def test_run_host_process_exits_nonzero_on_fatal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """run_host_process surfaces a fatal tunnel failure as its own exit code.

    The CLI entry point must print the cause + fix and exit non-zero, not
    hang silently. The code is the dedicated fatal one rather than a bare
    1, so an in-sandbox supervisor can tell "this can never succeed" from
    "this crashed" and stand down instead of restarting a bad credential
    forever. Driven through the real ``asyncio.run`` path, so this is a
    sync test.
    """
    _patch_connect(monkeypatch, _ConnectSpy([_invalid_status(403)]))

    with pytest.raises(SystemExit) as excinfo:
        run_host_process(
            server_url="https://app.example.databricks.com",
            config_path=tmp_path / "config.yaml",
        )

    # Non-zero exit so callers/CI see the failure, and distinguishable
    # from a crash so supervision doesn't retry it.
    assert excinfo.value.code == HOST_FATAL_EXIT_CODE
    assert HOST_FATAL_EXIT_CODE != 1
    err = capsys.readouterr().err
    # The actionable message reached stderr (banner + the 403 cause).
    assert "Could not connect" in err
    assert "HTTP 403" in err


async def test_run_host_process_invalid_host_id_exits_actionably(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed configured identity is a user error, not a CLI crash."""
    monkeypatch.setenv("OMNIGENT_HOST_ID", "not-a-uuid")
    monkeypatch.setenv("OMNIGENT_HOST_NAME", "managed-test")

    with pytest.raises(SystemExit) as excinfo:
        run_host_process(
            server_url="https://app.example.databricks.com",
            config_path=tmp_path / "config.yaml",
        )

    assert excinfo.value.code == HOST_FATAL_EXIT_CODE
    err = capsys.readouterr().err
    assert "Could not start host" in err
    assert "OMNIGENT_HOST_ID" in err
    assert "not-a-uuid" in err


def test_run_host_process_mirrors_structured_logs_on_foreground_tty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Foreground ``omnigent host`` mirrors logs like ``omnigent server``."""
    monkeypatch.delenv("OMNIGENT_LOG_TO_STDERR", raising=False)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    _patch_connect(monkeypatch, _ConnectSpy([asyncio.CancelledError()]))

    with patch(
        "omnigent.host.connect.configure_process_logging",
        return_value=tmp_path / "host.log",
    ) as configure_logging:
        run_host_process(
            server_url="https://app.example.databricks.com",
            config_path=tmp_path / "config.yaml",
        )

    configure_logging.assert_called_once_with("host", log_to_stderr=True)


def test_run_host_process_announces_session_log_dir_on_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``omnigent host`` names the session-log dir on start (was silent).

    The reported regression: ``omnigent host`` ran completely silently, so a
    quiet/stuck host gave no hint where to look. The startup banner now points
    at the per-session runner log dir up front; the exact ``runner-*.log`` is
    printed later when each runner launches. We stub the tunnel to a clean
    cancel so ``run()`` returns immediately, and repoint ``Path.home`` so the
    advertised dir resolves under tmp.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / ".omnigent"))
    # A single CancelledError ends the connect loop cleanly (no fatal exit),
    # so run_host_process returns after printing the startup banner.
    _patch_connect(monkeypatch, _ConnectSpy([asyncio.CancelledError()]))

    run_host_process(
        server_url="https://app.example.databricks.com",
        config_path=tmp_path / "config.yaml",
    )

    out = capsys.readouterr().out
    assert "Session logs: ~/.omnigent/logs/runner/" in out
    assert "This host's log: ~/.omnigent/logs/host/host-" in out


async def test_run_sweeps_orphaned_native_bridge_dirs_on_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host startup reclaims native bridge dirs orphaned by a crashed runner.

    A runner that dies uncleanly (SIGKILL / crash / host restart mid-run)
    never runs its explicit-delete cleanup, and if no new runner ever
    launches on the machine the runner-side startup sweep never fires
    either — so ``~/.omnigent`` grows without bound. The host daemon's own
    (re)start is the reliable moment to reap: ``run()`` must invoke the
    cross-harness bridge-dir sweep before entering the connect loop.
    """
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    _patch_connect(monkeypatch, _ConnectSpy([asyncio.CancelledError()]))
    sweeps: list[int] = []
    monkeypatch.setattr(
        "omnigent.native_bridge_common.reap_orphaned_native_bridge_dirs",
        lambda: sweeps.append(1) or 3,
    )
    host = _host()

    await host.run()

    assert sweeps == [1]


async def test_run_survives_a_failing_native_bridge_dir_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising bridge-dir sweep must not abort host startup.

    The sweep is best-effort housekeeping; a broken bridge module or an
    unreadable bridge root must never prevent the host from registering.
    """
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    _patch_connect(monkeypatch, _ConnectSpy([asyncio.CancelledError()]))

    def _boom() -> int:
        raise OSError("bridge root unreadable")

    monkeypatch.setattr(
        "omnigent.native_bridge_common.reap_orphaned_native_bridge_dirs",
        _boom,
    )
    host = _host()

    # Startup completes (run returns via the clean cancel) despite the raise.
    await host.run()


async def test_launch_cancelled_midspawn_does_not_leak_untracked_runner(
    tmp_path: Path,
) -> None:
    """Cancelling a launch mid-spawn tears the runner down instead of leaking it.

    ``_handle_launch`` registers the handle in ``_runners`` only after the spawn
    thread returns, so a cancellation in that window used to leave a live runner
    that was never watched, stopped, or reaped (and whose exit status the zygote
    would retain forever). The spawn is shielded and the abandoned process is
    terminated.
    """
    host = _make_host_process()
    host._auth_token_factory = lambda: "host-bootstrap-bearer"
    host._auth_token_factory_resolved = True
    workspace = tmp_path / "project"
    workspace.mkdir()

    original_popen = subprocess.Popen
    spawned: list[subprocess.Popen[bytes]] = []
    spawn_started = threading.Event()
    release_spawn = threading.Event()

    def _slow_popen(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        """Spawn a long-lived process, signalling once it exists.

        :param args: Command args (ignored — a real sleep stands in).
        :param kwargs: Popen kwargs (ignored).
        :returns: A real subprocess handle.
        """
        proc = original_popen(
            ["sleep", "60"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        spawned.append(proc)
        spawn_started.set()
        # Stay inside the shielded spawn until the test has issued its cancel.
        # Returning here immediately let a loaded machine finish the launch
        # before the cancel landed, so the cancel missed the leak window.
        release_spawn.wait(10.0)
        return proc

    frame = HostLaunchRunnerFrame(
        request_id="req_cancel",
        binding_token="tok_cancel",
        workspace=str(workspace),
    )

    with patch("omnigent.host.connect.subprocess.Popen", side_effect=_slow_popen):
        task = asyncio.create_task(host._handle_launch(frame))
        # Cancel only once the spawn thread has actually created the process,
        # so we exercise the real leak window rather than a pre-spawn cancel.
        await asyncio.to_thread(spawn_started.wait, 10.0)
        task.cancel()
        release_spawn.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert spawned, "the spawn thread should have created a process"
    # Never registered (that is the leak window) ...
    assert not host._runners
    # ... but also not left running: the shield's done-callback terminates it.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and spawned[0].poll() is None:
        await asyncio.sleep(0.05)
    assert spawned[0].poll() is not None, "abandoned runner was leaked, still alive"


async def test_handle_model_options_serves_codex_probe_rows_and_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Databricks-routed Codex request is answered by the harness probe.

    The probe rows pass through verbatim with their ids as the routable
    set, and the second request is served from the fingerprint cache —
    the harness is booted once.
    """
    from omnigent import codex_native_app_server

    monkeypatch.setattr(
        codex_native_app_server,
        "resolve_native_codex_launch",
        lambda *, model: codex_native_app_server.NativeCodexLaunch(
            config_overrides=[],
            model="databricks-gpt-5-4",
            profile="oss",
        ),
    )
    probe_calls: list[int] = []

    async def _fake_probe(**_kwargs: object) -> list[dict[str, object]]:
        probe_calls.append(1)
        return [
            {"id": "gpt-5.6-sol", "displayName": "GPT-5.6-Sol"},
            {"id": "gpt-5.4", "displayName": "gpt-5.4", "isDefault": True},
        ]

    monkeypatch.setattr(codex_native_app_server, "probe_codex_model_options", _fake_probe)
    host = _make_host_process()

    first = await host._handle_model_options(
        HostModelOptionsFrame(request_id="req_1", harness="codex-native"),
    )
    second = await host._handle_model_options(
        HostModelOptionsFrame(request_id="req_2", harness="codex-native"),
    )

    assert first == HostModelOptionsResultFrame(
        request_id="req_1",
        status="ok",
        models=[
            {"id": "gpt-5.6-sol", "displayName": "GPT-5.6-Sol"},
            {"id": "gpt-5.4", "displayName": "gpt-5.4", "isDefault": True},
        ],
        routable_models=["gpt-5.6-sol", "gpt-5.4"],
    )
    assert second.models == first.models
    assert probe_calls == [1]
    _cleanup_host(host)


async def test_handle_model_options_serves_claude_sdk_endpoint_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SDK-mode Claude is a pass-through client, so the endpoint listing is
    the harness truth — served in the exact wire spelling the SDK sends."""
    from omnigent.model_catalog import ModelEntry, ModelListing

    def _fake_listing(spec: object, harness: str) -> ModelListing:
        assert harness == "claude-sdk"
        return ModelListing(
            source="gateway",
            verified=True,
            models=(
                ModelEntry(id="databricks-claude-sonnet-5", family="claude"),
                ModelEntry(id="databricks-claude-opus-4-8", family="claude"),
            ),
            note="test catalog",
        )

    monkeypatch.setattr("omnigent.model_catalog.list_models_for_worker", _fake_listing)
    host = _make_host_process()

    result = await host._handle_model_options(
        HostModelOptionsFrame(request_id="req_sdk", harness="claude-sdk"),
    )

    assert result == HostModelOptionsResultFrame(
        request_id="req_sdk",
        status="ok",
        models=[
            {"id": "databricks-claude-sonnet-5", "displayName": "databricks-claude-sonnet-5"},
            {"id": "databricks-claude-opus-4-8", "displayName": "databricks-claude-opus-4-8"},
        ],
        routable_models=["databricks-claude-sonnet-5", "databricks-claude-opus-4-8"],
    )
    _cleanup_host(host)


async def test_handle_model_options_claude_sdk_rides_the_probe_when_endpoints_list_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subscription SDK launch serves the claude CLI's probed rows.

    Subscription providers list nothing endpoint-side (the curated
    stand-ins are gone), and the SDK drives the claude CLI — so the CLI's
    probed listing is the truth for this lane too.
    """
    from omnigent.host.connect import ModelOptionsResult
    from omnigent.model_catalog import ModelListing

    def _fake_listing(spec: object, harness: str) -> ModelListing:
        assert harness == "claude-sdk"
        return ModelListing(
            source="static",
            verified=False,
            models=(),
            note="the claude CLI login exposes no model-listing API before launch",
        )

    monkeypatch.setattr("omnigent.model_catalog.list_models_for_worker", _fake_listing)
    host = _make_host_process()

    async def _fake_probed() -> ModelOptionsResult:
        return ModelOptionsResult(
            models=[{"id": "sonnet", "model": "claude-sonnet-5", "displayName": "Sonnet 5"}],
            routable_models=[],
        )

    monkeypatch.setattr(host, "_probed_claude_model_options", _fake_probed)

    result = await host._handle_model_options(
        HostModelOptionsFrame(request_id="req_sdk_sub", harness="claude-sdk"),
    )

    assert result == HostModelOptionsResultFrame(
        request_id="req_sdk_sub",
        status="ok",
        models=[{"id": "sonnet", "model": "claude-sonnet-5", "displayName": "Sonnet 5"}],
        routable_models=[],
    )
    _cleanup_host(host)


async def test_model_options_frame_replies_off_the_receive_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow probe must not stall the tunnel receive loop.

    ``_handle_raw_message`` returns while the probe is still blocked; the
    reply frame arrives from the dispatched task once the probe finishes.
    """
    from omnigent import codex_native_app_server
    from omnigent.host.frames import encode_host_frame

    monkeypatch.setattr(
        codex_native_app_server,
        "resolve_native_codex_launch",
        lambda *, model: codex_native_app_server.NativeCodexLaunch(
            config_overrides=[],
            model=None,
            profile="oss",
        ),
    )
    release_probe = asyncio.Event()

    async def _slow_probe(**_kwargs: object) -> list[dict[str, object]]:
        await release_probe.wait()
        return [{"id": "gpt-5.6-sol", "displayName": "GPT-5.6-Sol"}]

    monkeypatch.setattr(codex_native_app_server, "probe_codex_model_options", _slow_probe)
    host = _make_host_process()
    ws = _RecordingWS()
    raw = encode_host_frame(HostModelOptionsFrame(request_id="req_slow", harness="codex-native"))

    # Starting the frame task returns immediately — the receive loop is free
    # while the probe blocks; the reply arrives from the frame's own task.
    host._start_frame_task(ws, raw)  # type: ignore[arg-type] — duck-typed ws
    await asyncio.sleep(0.05)
    assert ws.sent == []

    release_probe.set()
    await asyncio.wait_for(ws.first_send.wait(), timeout=2.0)
    reply = decode_host_frame(ws.sent[0])
    assert isinstance(reply, HostModelOptionsResultFrame)
    assert reply.request_id == "req_slow"
    assert reply.status == "ok"
    assert reply.models == [{"id": "gpt-5.6-sol", "displayName": "GPT-5.6-Sol"}]
    _cleanup_host(host)


async def test_dns_errno_failure_is_not_a_recycle(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Windows errno 11001 (getaddrinfo failed) is not close code 1001.

    The old substring classifier matched "1001" inside "11001", so a
    sustained DNS outage (VPN down) classified every failure as an explicit
    recycle and reconnected at the 0.5s prompt cadence indefinitely — 2
    attempts/second, 122k attempts overnight in the field report.
    """
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_CAP_S", 0.0)
    dns_failure = OSError(11001, "getaddrinfo failed")
    spy = _ConnectSpy([dns_failure, dns_failure, dns_failure, asyncio.CancelledError()])
    _patch_connect(monkeypatch, spy)
    host = _host()

    with caplog.at_level(logging.WARNING, logger="omnigent.host.connect"):
        await host.run()

    assert spy.call_count == 4
    reconnects = [
        record.message for record in caplog.records if "Reconnecting in" in record.message
    ]
    assert len(reconnects) == 3
    assert not any("(recycle" in r for r in reconnects), (
        "a DNS resolution failure must take the backoff ladder, not the prompt recycle cadence"
    )


@pytest.mark.parametrize("port", [502, 1001, 1012])
async def test_endpoint_port_is_not_a_recycle(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    port: int,
) -> None:
    """Standalone numbers in transport errors are not protocol status codes."""
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    failure = OSError(f"Connect call failed ('203.0.113.1', {port})")
    spy = _ConnectSpy([failure, asyncio.CancelledError()])
    _patch_connect(monkeypatch, spy)

    with caplog.at_level(logging.WARNING, logger="omnigent.host.connect"):
        await _host().run()

    reconnects = [
        record.message for record in caplog.records if "Reconnecting in" in record.message
    ]
    assert len(reconnects) == 1
    assert "(recycle" not in reconnects[0]


async def test_sustained_recycle_failures_fall_back_to_backoff(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A sustained run of recycle-classified failures is an outage, not a cycle.

    The prompt cadence exists for a brief ingress recycle; when the same
    classification fires attempt after attempt without a connection ever
    establishing (proxy 502 while the server is down), the daemon must back
    off instead of hammering the endpoint twice a second forever.
    """
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_CAP_S", 0.0)
    monkeypatch.setattr("omnigent.host.connect._RECYCLE_PROMPT_MAX_STREAK", 3)
    # Raised AT CONNECT TIME (never accepted): an ingress-classified outage.
    rejected = _invalid_status(502)
    spy = _ConnectSpy([rejected] * 6 + [asyncio.CancelledError()])
    _patch_connect(monkeypatch, spy)
    host = _host()

    with caplog.at_level(logging.WARNING, logger="omnigent.host.connect"):
        await host.run()

    assert spy.call_count == 7
    reconnects = [
        record.message for record in caplog.records if "Reconnecting in" in record.message
    ]
    assert len(reconnects) == 6
    prompt = [i for i, r in enumerate(reconnects) if "(recycle" in r]
    # The first _RECYCLE_PROMPT_MAX_STREAK attempts are prompt; every attempt
    # after that takes the backoff ladder.
    assert prompt == [0, 1, 2], f"prompt cadence must stop after the streak cap: {prompt}"


async def test_silent_connect_streak_escalates_and_slows_reconnects(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Repeated accepted-but-silent connections escalate to slow backoff."""
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_CAP_S", 0.0)
    monkeypatch.setattr("omnigent.host.connect._SILENT_CONNECT_ESCALATE_ATTEMPTS", 3)
    monkeypatch.setattr("omnigent.host.connect.configured_harness_map", dict)
    monkeypatch.setattr("omnigent.host.connect.gateway_inference_map", dict)
    spy = _ConnectSpy([None, None, None, None, None, asyncio.CancelledError()])
    _patch_connect(monkeypatch, spy)
    host = _host()

    with caplog.at_level(logging.WARNING, logger="omnigent.host.connect"):
        await host.run()

    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "3 consecutive connections but never responded" in errors[0].message
    assert capsys.readouterr().err.count("never responded") == 1
    reconnects = [
        record.message for record in caplog.records if "Reconnecting in" in record.message
    ]
    assert len(reconnects) == 5
    assert "(recycle — prompt reconnect)" in reconnects[1]
    assert "(recycle — prompt reconnect)" not in reconnects[-1]


async def test_inbound_frame_resets_silent_connect_streak(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Any inbound frame resets consecutive silent-connection accounting."""
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_CAP_S", 0.0)
    monkeypatch.setattr("omnigent.host.connect._SILENT_CONNECT_ESCALATE_ATTEMPTS", 3)
    monkeypatch.setattr("omnigent.host.connect.configured_harness_map", dict)
    monkeypatch.setattr("omnigent.host.connect.gateway_inference_map", dict)
    spy = _ConnectSpy([None, None, 1, None, None, asyncio.CancelledError()])
    _patch_connect(monkeypatch, spy)
    host = _host()

    with caplog.at_level(logging.WARNING, logger="omnigent.host.connect"):
        await host.run()

    assert host._silent_connect_streak == 2
    assert not [record for record in caplog.records if record.levelno == logging.ERROR]


async def test_connection_error_frame_fails_loudly_on_live_receive_path() -> None:
    """A non-retryable server error escapes the live receive loop."""
    host = _host()
    tunnel = _ConnectionErrorTunnel(
        HostConnectionErrorFrame(
            stage="registration",
            error="database unavailable",
            retryable=False,
        )
    )

    with pytest.raises(HostConnectError, match="registration: database unavailable"):
        await host._serve_frames(tunnel)  # type: ignore[arg-type]

    assert len(tunnel.sent) == 1
    assert isinstance(decode_host_frame(tunnel.sent[0]), HostHelloFrame)
    assert host._frame_tasks == set()


async def test_retryable_connection_error_escapes_live_receive_path() -> None:
    """A retryable server error reaches the reconnect loop's exception class."""
    host = _host()
    tunnel = _ConnectionErrorTunnel(
        HostConnectionErrorFrame(
            stage="registration",
            error="database restarting",
            retryable=True,
        )
    )

    with pytest.raises(HostRetryableConnectionError, match="registration: database restarting"):
        await host._serve_frames(tunnel)  # type: ignore[arg-type]

    assert host._frame_tasks == set()


async def test_capabilities_are_initialized_once_across_reconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconnect handshakes reuse startup metadata instead of rerunning probes."""
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)
    calls = {"readiness": 0, "gateway": 0}

    def _readiness() -> dict[str, bool]:
        calls["readiness"] += 1
        return {"pi": True}

    def _gateway() -> dict[str, bool]:
        calls["gateway"] += 1
        return {"codex": True}

    monkeypatch.setattr("omnigent.host.connect.configured_harness_map", _readiness)
    monkeypatch.setattr("omnigent.host.connect.gateway_inference_map", _gateway)
    spy = _ConnectSpy([None, None, asyncio.CancelledError()])
    _patch_connect(monkeypatch, spy)
    host = _host()

    await host.run()

    assert spy.call_count == 3
    assert calls == {"readiness": 1, "gateway": 1}


class _FakeZygote:
    """Scripted ZygoteManager stand-in for the spawn-fallback tests.

    :param fail_at: ``"start"`` or ``"fork"`` — which call raises
        :class:`ZygoteUnavailable`.
    :param running: What ``is_running()`` reports after the failure.
    """

    def __init__(self, *, fail_at: str, running: bool) -> None:
        """Store the failure script.

        :param fail_at: Which call raises.
        :param running: Post-failure ``is_running()`` answer.
        """
        self._fail_at = fail_at
        self._running = running
        self.start_calls = 0
        self.stop_calls = 0

    @property
    def pid(self) -> int:
        """A fixed fake zygote pid."""
        return 4242

    def is_running(self) -> bool:
        """Report the scripted liveness."""
        return self._running

    def start(self) -> None:
        """Raise when scripted to fail at start."""
        self.start_calls += 1
        if self._fail_at == "start":
            raise ZygoteUnavailable("scripted start failure")

    def fork_runner(self, env: dict[str, str], log_path: str, workspace: str) -> object:
        """Always raise — the fork channel is scripted broken."""
        del env, log_path, workspace
        raise ZygoteUnavailable("scripted fork failure")

    def stop(self) -> None:
        """Record the reap."""
        self.stop_calls += 1


class _FakeSpawnedProc:
    """Minimal Popen stand-in for the direct-spawn fallback."""

    pid = 31337

    def poll(self) -> int | None:
        """Report the fake runner still running."""
        return None


def _spawn_with_fake_zygote(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    zygote: _FakeZygote,
) -> tuple[HostProcess, list[list[str]]]:
    """Drive ``_spawn_runner_proc`` with *zygote* and a stubbed direct Popen.

    :param monkeypatch: The pytest monkeypatch fixture.
    :param tmp_path: Holds the fake runner log file.
    :param zygote: The scripted zygote installed on the host.
    :returns: The host and the argv of every direct ``Popen`` call.
    """
    host = _make_host_process()
    host._zygote = zygote  # type: ignore[assignment]
    host._zygote_disabled = False
    popen_argvs: list[list[str]] = []
    log_path = tmp_path / "runner.log"

    def _fake_open_log(
        destination: str,
        *,
        root: object = None,
        prefix: str | None = None,
    ) -> tuple[Path, object]:
        del destination, root, prefix
        return log_path, open(log_path, "ab", buffering=0)

    def _fake_popen(argv: list[str], **kwargs: object) -> _FakeSpawnedProc:
        del kwargs
        popen_argvs.append(list(argv))
        return _FakeSpawnedProc()

    monkeypatch.setattr("omnigent.host.connect.open_process_log_file", _fake_open_log)
    monkeypatch.setattr("omnigent.host.connect.subprocess.Popen", _fake_popen)
    host._spawn_runner_proc({}, "slug", tmp_path)
    return host, popen_argvs


def test_dead_zygote_is_reaped_and_respawnable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A zygote that died mid-life must not disable forking forever.

    Its forked runners self-terminate via their orphan watchdogs the
    moment it dies, so nothing depends on the dead instance — reap it
    and leave the zygote enabled so the next launch's ``start()``
    respawns a fresh one, instead of losing copy-on-write forking for
    the rest of the daemon's life.
    """
    zygote = _FakeZygote(fail_at="fork", running=False)

    host, popen_argvs = _spawn_with_fake_zygote(monkeypatch, tmp_path, zygote)

    assert host._zygote_disabled is False
    assert zygote.stop_calls == 1
    # This launch still succeeded, via the direct-spawn fallback.
    assert len(popen_argvs) == 1


def test_alive_zygote_with_broken_channel_is_disabled_not_stopped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An alive zygote with a broken channel is disabled but never stopped.

    Stopping it would orphan the healthy runners already forked from it
    (their parent-death watchdogs would self-terminate live sessions).
    """
    zygote = _FakeZygote(fail_at="fork", running=True)

    host, popen_argvs = _spawn_with_fake_zygote(monkeypatch, tmp_path, zygote)

    assert host._zygote_disabled is True
    assert zygote.stop_calls == 0
    assert len(popen_argvs) == 1


def test_zygote_start_failure_disables_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A zygote that cannot start is disabled — retrying every launch is waste."""
    zygote = _FakeZygote(fail_at="start", running=False)

    host, popen_argvs = _spawn_with_fake_zygote(monkeypatch, tmp_path, zygote)

    assert host._zygote_disabled is True
    assert zygote.stop_calls == 0
    assert len(popen_argvs) == 1


async def test_run_prestarts_zygote_before_first_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run() warms the zygote so the first launch skips its cold import.

    The zygote's one-time import of the runner graph (~1-2s) otherwise lands
    inside the first session launch of the daemon's life, extending that
    session's "Starting up…" window.
    """
    host = _make_host_process()
    zygote = _FakeZygote(fail_at="none", running=True)
    host._zygote = zygote  # type: ignore[assignment]
    host._zygote_disabled = False

    async def _fake_connect(self_: HostProcess) -> None:
        # Hold the "connection" open until the prestart thread has run, then
        # shut the daemon down.
        for _ in range(500):
            if zygote.start_calls:
                break
            await asyncio.sleep(0.01)
        raise KeyboardInterrupt

    monkeypatch.setattr(HostProcess, "_connect_and_serve", _fake_connect)
    # Keep run()'s shutdown sweep from reaping unrelated pytest children.
    monkeypatch.setattr(HostProcess, "_reap_orphans_once", lambda self_: 0)

    await host.run()

    assert zygote.start_calls == 1
    # run()'s finally still reaps the prewarmed zygote on shutdown.
    assert zygote.stop_calls == 1


class _CollectingWs:
    """Duck-typed tunnel connection capturing every frame sent on it."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        """Record one outbound frame."""
        self.sent.append(data)


async def _drain_frame_tasks(host: HostProcess) -> None:
    """Await every in-flight frame task (handler failures are contained)."""
    await asyncio.gather(*list(host._frame_tasks))


async def test_slow_frame_does_not_head_of_line_block(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A slow handler must not delay the frames queued behind it.

    The receive loop used to await each frame inline, so a create's own
    background ``host.model_options`` CLI exec (measured 650-794ms) parked
    the launch frame sent ~20ms later behind it on every session create —
    0.3-1.3s of added "Starting up…" latency. Frames now run on their own
    tasks: a stat arriving behind a parked handler completes immediately.
    """
    host = _make_host_process()
    ws = _CollectingWs()
    slow_started = asyncio.Event()
    release_slow = asyncio.Event()

    async def _parked_model_options(
        self_: HostProcess, frame: HostModelOptionsFrame
    ) -> HostModelOptionsResultFrame:
        slow_started.set()
        await release_slow.wait()
        return HostModelOptionsResultFrame(request_id=frame.request_id, status="ok")

    monkeypatch.setattr(HostProcess, "_handle_model_options", _parked_model_options)

    host._start_frame_task(
        ws,  # type: ignore[arg-type] — duck-typed ws
        encode_host_frame(HostModelOptionsFrame(request_id="req_slow", harness="claude-native")),
    )
    await slow_started.wait()
    host._start_frame_task(
        ws,  # type: ignore[arg-type] — duck-typed ws
        encode_host_frame(HostStatFrame(request_id="req_stat", path=str(tmp_path))),
    )

    # The stat result lands while the model-options handler is still parked.
    for _ in range(500):
        if ws.sent:
            break
        await asyncio.sleep(0.01)
    assert ws.sent, "stat frame never answered while a slow frame was in flight"
    assert '"req_stat"' in ws.sent[0]

    release_slow.set()
    await _drain_frame_tasks(host)
    assert any('"req_slow"' in frame for frame in ws.sent)


async def test_stop_frame_never_overtakes_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner lifecycle frames keep their arrival order under concurrency.

    A session DELETE racing a slow create sends a stop while the launch is
    mid-flight; if the stop ran first it would no-op on an unknown runner
    and leave the launched runner orphaned. The lifecycle lock serializes
    launch/stop in arrival order while other frames stay concurrent.
    """
    host = _make_host_process()
    ws = _CollectingWs()
    order: list[str] = []
    release_launch = asyncio.Event()

    async def _parked_launch(
        self_: HostProcess, frame: HostLaunchRunnerFrame
    ) -> HostLaunchRunnerResultFrame:
        order.append("launch:start")
        await release_launch.wait()
        order.append("launch:end")
        return HostLaunchRunnerResultFrame(
            request_id=frame.request_id, status="launched", runner_id="runner_x"
        )

    async def _recording_stop(
        self_: HostProcess, frame: HostStopRunnerFrame
    ) -> HostStopRunnerResultFrame:
        order.append("stop")
        return HostStopRunnerResultFrame(request_id=frame.request_id, status="stopped")

    monkeypatch.setattr(HostProcess, "_handle_launch", _parked_launch)
    monkeypatch.setattr(HostProcess, "_handle_stop", _recording_stop)

    host._start_frame_task(
        ws,  # type: ignore[arg-type] — duck-typed ws
        encode_host_frame(
            HostLaunchRunnerFrame(request_id="rq1", binding_token="tok", workspace="/w")
        ),
    )
    host._start_frame_task(
        ws,  # type: ignore[arg-type] — duck-typed ws
        encode_host_frame(HostStopRunnerFrame(request_id="rq2", runner_id="runner_x")),
    )
    await asyncio.sleep(0.05)
    # The stop is parked behind the lifecycle lock, not run concurrently.
    assert order == ["launch:start"]

    release_launch.set()
    await _drain_frame_tasks(host)
    assert order == ["launch:start", "launch:end", "stop"]


async def test_frame_handler_failure_is_contained(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A crashing handler is logged; the tunnel and later frames survive.

    Pre-concurrency, a handler exception propagated out of the receive loop
    and tore the tunnel down (a reconnect for one bad request).
    """
    host = _make_host_process()
    ws = _CollectingWs()

    def _boom(self_: HostProcess, frame: HostStatFrame) -> HostStatResultFrame:
        raise RuntimeError("scripted stat failure")

    monkeypatch.setattr(HostProcess, "_handle_stat", _boom)
    with caplog.at_level(logging.ERROR, logger="omnigent.host.connect"):
        host._start_frame_task(
            ws,  # type: ignore[arg-type] — duck-typed ws
            encode_host_frame(HostStatFrame(request_id="req_boom", path=str(tmp_path))),
        )
        await _drain_frame_tasks(host)

    assert ws.sent == []
    assert any("host frame handler failed" in record.message for record in caplog.records)

    # A later frame on the same connection is still handled.
    host._start_frame_task(
        ws,  # type: ignore[arg-type] — duck-typed ws
        encode_host_frame(HostListDirFrame(request_id="req_ls", path=str(tmp_path))),
    )
    await _drain_frame_tasks(host)
    assert ws.sent and '"req_ls"' in ws.sent[0]


def test_direct_spawn_keeps_the_workspace_off_sys_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The runner is spawned with ``-P`` so its workspace cannot shadow omnigent.

    The runner's cwd is the session workspace, and ``python -m`` would otherwise
    prepend it to ``sys.path``, so a session opened *in an omnigent checkout*
    imports that checkout instead of the installed package, and a long-lived
    daemon plus a mid-flight ``git pull`` then skews the two.
    """
    zygote = _FakeZygote(fail_at="start", running=False)

    _host, popen_argvs = _spawn_with_fake_zygote(monkeypatch, tmp_path, zygote)

    assert popen_argvs[0][1:] == ["-P", "-m", "omnigent.runner._entry"]


async def test_on_resume_from_suspend_aborts_live_tunnel() -> None:
    """A detected wake aborts the live tunnel and flags a prompt reconnect.

    ``_on_resume_from_suspend`` runs synchronously on the event loop; with a
    live ``self._ws`` it must abort the transport — so ``_serve_frames``'
    blocked ``recv`` raises at once instead of waiting out the ~90s keepalive —
    and set the woke flag that ``run`` reads to skip the reconnect backoff.

    :returns: None.
    """
    host = _make_host_process()

    class _Transport:
        """asyncio-transport stub that records an ``abort()``."""

        def __init__(self) -> None:
            self.aborted = False

        def abort(self) -> None:
            """Record the forced close.

            :returns: None.
            """
            self.aborted = True

    transport = _Transport()
    host._ws = SimpleNamespace(transport=transport)  # type: ignore[assignment]

    host._on_resume_from_suspend(3600.0)

    assert transport.aborted is True
    assert host._woke_from_suspend is True


async def test_on_resume_from_suspend_noop_without_live_tunnel() -> None:
    """With no live tunnel the resume hook does nothing and sets no flag.

    A wake during a reconnect backoff has no socket to abort; the flag must
    stay clear so an unrelated later drop is not spuriously reclassified as a
    prompt reconnect.

    :returns: None.
    """
    host = _make_host_process()
    assert host._ws is None

    host._on_resume_from_suspend(3600.0)

    assert host._woke_from_suspend is False


async def test_run_reconnects_promptly_after_suspend(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After a wake, run() aborts the dead tunnel and reconnects at once.

    Drives the real reconnect loop against a *loopback* server, where an
    abrupt drop is NOT auto-classified as a benign ingress recycle — so a
    prompt reconnect here is attributable to the suspend-resume path, not the
    recycle heuristic. A fake watcher fires a resume once the tunnel is live;
    the real ``_on_resume_from_suspend`` aborts it, and the loop must reconnect
    (not hang or exit) and attribute the drop to the resume.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param caplog: Log capture fixture.
    :returns: None.
    """
    monkeypatch.setattr("omnigent.host.connect._RECONNECT_BASE_S", 0.0)

    class _BlockingTunnel:
        """Accepted tunnel whose ``recv()`` blocks until the transport aborts."""

        def __init__(self) -> None:
            self._dead = asyncio.Event()
            self.transport = SimpleNamespace(abort=self._dead.set)

        async def send(self, data: str | bytes) -> None:
            """Accept the ``host.hello`` frame silently.

            :param data: Encoded frame payload (ignored).
            :returns: None.
            """
            del data

        async def recv(self) -> str:
            """Block until aborted, then fail like a dropped connection.

            :returns: Never returns a frame.
            :raises ConnectionClosedError: Once the transport is aborted.
            """
            await self._dead.wait()
            raise ConnectionClosedError(None, None)

    class _BlockingConnect:
        """Async-CM for a successful upgrade to a blocking tunnel."""

        def __init__(self, tunnel: _BlockingTunnel) -> None:
            self._tunnel = tunnel

        async def __aenter__(self) -> _BlockingTunnel:
            """Complete the handshake with the blocking tunnel.

            :returns: The blocking tunnel.
            """
            return self._tunnel

        async def __aexit__(self, *exc_info: object) -> bool:
            """No-op async-CM exit.

            :param exc_info: Standard ``__aexit__`` triple (unused).
            :returns: ``False`` so the disconnect propagates.
            """
            del exc_info
            return False

    tunnel = _BlockingTunnel()
    connect_calls = {"n": 0}

    def _connect(url: str, **kwargs: object) -> object:
        """Serve one live tunnel, then stop the loop.

        :param url: Tunnel URL (ignored).
        :param kwargs: Connect kwargs (ignored).
        :returns: A blocking-tunnel CM on the first call; a CM that cancels
            the loop thereafter.
        """
        del url, kwargs
        connect_calls["n"] += 1
        if connect_calls["n"] == 1:
            return _BlockingConnect(tunnel)
        return _HandshakeFailingConnect(asyncio.CancelledError())

    host = _make_host_process()  # loopback server_url (http://localhost:8000)

    async def _fake_watch(on_resume: object, **_kwargs: object) -> None:
        """Simulate a wake once the first tunnel is live, then idle.

        :param on_resume: The host's real resume hook.
        :param _kwargs: Ignored watcher tuning args.
        :returns: None.
        """
        # Wait until _serve_frames has set self._ws so there is a live socket
        # to abort (otherwise the resume hook would be a no-op).
        while host._ws is None:
            await asyncio.sleep(0)
        on_resume(3600.0)  # type: ignore[operator]
        await asyncio.Event().wait()

    import websockets.asyncio.client as ws_client

    import omnigent.runner._entry as entry_mod

    monkeypatch.setattr(entry_mod, "_make_auth_token_factory", lambda *, server_url=None: None)
    monkeypatch.setattr(ws_client, "connect", _connect)
    monkeypatch.setattr("omnigent.host.connect.watch_for_resume", _fake_watch)

    with caplog.at_level(logging.WARNING, logger="omnigent.host.connect"):
        await host.run()

    # Two connects: the initial live tunnel + the prompt reconnect after wake.
    assert connect_calls["n"] == 2
    # The disconnect was attributed to the resume, and the flag was consumed.
    assert any("resumed from suspend" in r.message for r in caplog.records)
    assert host._woke_from_suspend is False


def test_post_connect_auth_rejection_escalates_without_going_fatal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A 401/403 AFTER the host has connected retries forever (never fatal),
    but a sustained streak escalates the operator message from a transient-
    network hint to a re-auth prompt that names ``omnigent login`` — so a
    permanently-rejected credential surfaces instead of looping silently.
    """
    from omnigent.host.connect import _AUTH_REJECT_ESCALATE_ATTEMPTS

    host = _make_host_process()
    host._ever_connected = True

    # First rejection: retryable (None), framed as a transient network blip.
    assert host._classify_http_status(403) is None
    first = capsys.readouterr().err
    assert "network dropped" in first
    assert "omnigent login" not in first

    # Streak climbs toward — but not to — the escalation threshold: stays quiet
    # so a brief VPN outage never raises a false re-auth alarm.
    for _ in range(2, _AUTH_REJECT_ESCALATE_ATTEMPTS):
        assert host._classify_http_status(403) is None
    assert "omnigent login" not in capsys.readouterr().err

    # Crossing the threshold escalates — names the real remedy — and is STILL
    # retryable (no fatal error, so a recoverable daemon is never killed).
    assert host._classify_http_status(403) is None
    escalated = capsys.readouterr().err
    assert "omnigent login http://localhost:8000" in escalated
    assert "no longer a transient network blip" in escalated
    assert host._auth_retry_streak == _AUTH_REJECT_ESCALATE_ATTEMPTS


async def test_launch_harness_probe_runs_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The readiness probe must not run inline on the daemon's event loop.

    It shells out to ``<cli> --version``, so inline a hung CLI would stall the
    keepalive pong the server counts as liveness.
    """
    host = _make_host_process()
    workspace = tmp_path / "project"
    workspace.mkdir()
    loop_thread_id = threading.get_ident()
    probe_thread_ids: list[int] = []

    def _record_thread(harness: str) -> bool:
        probe_thread_ids.append(threading.get_ident())
        return False

    monkeypatch.setattr("omnigent.host.connect.harness_is_configured", _record_thread)

    result = await host._handle_launch(
        HostLaunchRunnerFrame(
            request_id="req_probe_thread",
            binding_token="token_abc",
            workspace=str(workspace),
            harness="codex",
        )
    )

    assert result.error_code == HARNESS_NOT_CONFIGURED_ERROR_CODE
    assert probe_thread_ids, "the harness probe should still run"
    assert loop_thread_id not in probe_thread_ids, (
        "harness readiness probe ran on the event loop thread"
    )


async def test_fatal_upgrade_error_surfaces_server_refusal_body() -> None:
    """A refusal that carries a body (what ``_refuse_upgrade`` sends, e.g. the
    server's malformed-host-id 400) is surfaced verbatim — the client passes the
    server's own reason through instead of guessing from the status code.
    """
    host = _make_host_process()
    server_reason = (
        "Invalid host id 'superagent-databricks-host': host ids must be UUIDs. "
        "Set OMNIGENT_HOST_ID to a UUID (or unset it to have one generated) and reconnect."
    )
    err = host._fatal_upgrade_error(_invalid_status(400, server_reason.encode()))
    assert err is not None  # 400 is a permanent refusal, not retried
    assert server_reason in str(err)
    assert "HTTP 400" in str(err)


async def test_fatal_upgrade_error_bodyless_4xx_falls_back_to_generic() -> None:
    """A permanent 4xx with no body (a bare pre-accept close carries none) still
    yields an actionable, generic message rather than an empty one.
    """
    host = _make_host_process()
    err = host._fatal_upgrade_error(_invalid_status(400))
    assert err is not None
    assert "HTTP 400" in str(err)
    assert "the server rejected the host tunnel request" in str(err)


async def test_handle_launch_supersedes_previous_runner_for_same_session(
    tmp_path: Path,
) -> None:
    """A relaunch for a session tears down that session's previous runner.

    The server rotates the binding token on every relaunch, so without
    this the host accumulates one live runner per attempt — each with a
    transcript forwarder tailing the same session (the #5182 leak).
    """
    host = _make_host_process()
    host._auth_token_factory = lambda: "host-bootstrap-bearer"
    host._auth_token_factory_resolved = True
    workspace = tmp_path / "project"
    workspace.mkdir()

    original_popen = subprocess.Popen

    def _fake_popen(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        return original_popen(
            ["sleep", "30"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    with patch("omnigent.host.connect.subprocess.Popen", side_effect=_fake_popen):
        first = await host._handle_launch(
            HostLaunchRunnerFrame(
                request_id="req_a",
                binding_token="tok_a",
                workspace=str(workspace),
                session_id="conv_super",
            )
        )
        first_handle = host._runners[first.runner_id]
        second = await host._handle_launch(
            HostLaunchRunnerFrame(
                request_id="req_b",
                binding_token="tok_b",
                workspace=str(workspace),
                session_id="conv_super",
            )
        )

    assert first.status == "launched" and second.status == "launched"
    # The superseded runner is untracked at once; its termination runs
    # detached, so poll-wait briefly for the process to go down.
    assert first.runner_id not in host._runners
    for _ in range(70):
        if first_handle.proc.poll() is not None:
            break
        await asyncio.sleep(0.1)
    assert first_handle.proc.poll() is not None, (
        "the previous runner for the session must be terminated on relaunch"
    )
    assert second.runner_id in host._runners
    _cleanup_host(host)


async def test_handle_launch_leaves_other_sessions_runners_alone(
    tmp_path: Path,
) -> None:
    """Supersession is scoped to the frame's session: no cross-session kills."""
    host = _make_host_process()
    host._auth_token_factory = lambda: "host-bootstrap-bearer"
    host._auth_token_factory_resolved = True
    workspace = tmp_path / "project"
    workspace.mkdir()

    original_popen = subprocess.Popen

    def _fake_popen(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        return original_popen(
            ["sleep", "30"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    with patch("omnigent.host.connect.subprocess.Popen", side_effect=_fake_popen):
        first = await host._handle_launch(
            HostLaunchRunnerFrame(
                request_id="req_a",
                binding_token="tok_a2",
                workspace=str(workspace),
                session_id="conv_one",
            )
        )
        second = await host._handle_launch(
            HostLaunchRunnerFrame(
                request_id="req_b",
                binding_token="tok_b2",
                workspace=str(workspace),
                session_id="conv_two",
            )
        )

    assert first.runner_id in host._runners
    assert second.runner_id in host._runners
    assert host._runners[first.runner_id].proc.poll() is None
    _cleanup_host(host)


async def test_handle_launch_spawn_failure_preserves_previous_runner(
    tmp_path: Path,
) -> None:
    """A failed relaunch spawn must not have killed the session's runner.

    Supersession runs only after the replacement is alive: trading a
    working runner for a spawn failure would leave the session with
    nothing (worse than the leak this fixes).
    """
    host = _make_host_process()
    host._auth_token_factory = lambda: "host-bootstrap-bearer"
    host._auth_token_factory_resolved = True
    workspace = tmp_path / "project"
    workspace.mkdir()

    original_popen = subprocess.Popen
    calls = {"n": 0}

    def _popen_second_fails(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("fork failed")
        return original_popen(
            ["sleep", "30"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    with patch("omnigent.host.connect.subprocess.Popen", side_effect=_popen_second_fails):
        first = await host._handle_launch(
            HostLaunchRunnerFrame(
                request_id="req_a",
                binding_token="tok_keep",
                workspace=str(workspace),
                session_id="conv_keep",
            )
        )
        second = await host._handle_launch(
            HostLaunchRunnerFrame(
                request_id="req_b",
                binding_token="tok_fail",
                workspace=str(workspace),
                session_id="conv_keep",
            )
        )

    assert second.status == "failed"
    assert first.runner_id in host._runners, (
        "a failed replacement spawn must leave the previous runner tracked"
    )
    assert host._runners[first.runner_id].proc.poll() is None, (
        "a failed replacement spawn must leave the previous runner alive"
    )
    _cleanup_host(host)


async def test_supersede_stop_does_not_block_the_launch(
    tmp_path: Path,
) -> None:
    """A SIGTERM-ignoring old runner can't head-of-line block the relaunch.

    The termination round (5s grace, then SIGKILL) runs detached; the
    launch handler must return while the stubborn process is still up.
    """
    host = _make_host_process()
    host._auth_token_factory = lambda: "host-bootstrap-bearer"
    host._auth_token_factory_resolved = True
    workspace = tmp_path / "project"
    workspace.mkdir()

    original_popen = subprocess.Popen
    calls = {"n": 0}

    def _popen_first_stubborn(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        calls["n"] += 1
        if calls["n"] == 1:
            # Ignores SIGTERM: only the detached round's SIGKILL ends it.
            return original_popen(
                ["bash", "-c", 'trap "" TERM; sleep 30'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return original_popen(
            ["sleep", "30"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    with patch("omnigent.host.connect.subprocess.Popen", side_effect=_popen_first_stubborn):
        first = await host._handle_launch(
            HostLaunchRunnerFrame(
                request_id="req_a",
                binding_token="tok_stub",
                workspace=str(workspace),
                session_id="conv_stub",
            )
        )
        first_handle = host._runners[first.runner_id]
        started = time.monotonic()
        second = await host._handle_launch(
            HostLaunchRunnerFrame(
                request_id="req_b",
                binding_token="tok_new",
                workspace=str(workspace),
                session_id="conv_stub",
            )
        )
        elapsed = time.monotonic() - started

    assert second.status == "launched"
    assert elapsed < 3.0, (
        f"the launch must not wait out the old runner's termination grace (took {elapsed:.1f}s)"
    )
    # The stubborn process survives SIGTERM but the detached round's
    # SIGKILL takes it down within its 5s grace + margin.
    for _ in range(80):
        if first_handle.proc.poll() is not None:
            break
        await asyncio.sleep(0.1)
    assert first_handle.proc.poll() is not None, (
        "the detached stop must eventually SIGKILL a SIGTERM-ignoring runner"
    )
    _cleanup_host(host)


async def test_handle_import_local_all_streams_a_frame_per_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``source="all"`` streams one session frame each (tagged), then a done frame."""
    from omnigent.host.frames import (
        HostImportLocalDoneFrame,
        HostImportLocalSessionFrame,
        decode_host_frame,
    )

    host = _make_host_process()

    # The cross-harness selector already merged/ranked; the handler just loads.
    def _fake_across(*, limit: int) -> list[tuple[str, str]]:
        return [("claude", "c1"), ("codex", "x1")]

    def _fake_load(source: str, session_id: str) -> SimpleNamespace:
        item = SimpleNamespace(
            type="message",
            response_id="r1",
            data=SimpleNamespace(model_dump=lambda **_kw: {"role": "user"}),
        )
        return SimpleNamespace(
            external_session_id=session_id,
            workspace="/repo",
            items=[item],
            title=f"{source} title",
            source=source,
        )

    monkeypatch.setattr(
        "omnigent.session_import.local.list_recent_sessions_across_harnesses", _fake_across
    )
    monkeypatch.setattr("omnigent.session_import.local.load_local_session", _fake_load)

    sent: list[str] = []

    class _FakeWs:
        async def send(self, text: str) -> None:
            sent.append(text)

    await host._handle_import_local(
        _FakeWs(),  # type: ignore[arg-type]
        HostImportLocalFrame(request_id="req_all", source="all", limit=5),
    )

    frames = [decode_host_frame(text) for text in sent]
    session_frames = [f for f in frames if isinstance(f, HostImportLocalSessionFrame)]
    done_frames = [f for f in frames if isinstance(f, HostImportLocalDoneFrame)]

    # One frame per session, each tagged with its own source + title + the total.
    assert {
        (f.session.external_session_id, f.session.source, f.session.title) for f in session_frames
    } == {("c1", "claude", "claude title"), ("x1", "codex", "codex title")}
    assert all(f.total == 2 for f in session_frames)
    assert len(done_frames) == 1 and done_frames[0].status == "ok"


async def test_handle_import_local_exact_id_does_not_list_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exact harness/id request loads that transcript without enumeration."""
    from omnigent.host.frames import HostImportLocalDoneFrame, HostImportLocalSessionFrame

    host = _make_host_process()

    def _unexpected_list(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("exact import must not enumerate local sessions")

    def _fake_load(source: str, session_id: str) -> SimpleNamespace:
        assert (source, session_id) == ("codex", "session-exact")
        item = SimpleNamespace(
            type="message",
            response_id="r1",
            data=SimpleNamespace(model_dump=lambda **_kw: {"role": "user"}),
        )
        return SimpleNamespace(
            external_session_id=session_id,
            workspace="/repo",
            items=[item],
            title="Exact session",
            source=source,
        )

    monkeypatch.setattr(
        "omnigent.session_import.local.list_recent_sessions_across_harnesses", _unexpected_list
    )
    monkeypatch.setattr(
        "omnigent.session_import.local.list_recent_local_session_ids", _unexpected_list
    )
    monkeypatch.setattr("omnigent.session_import.local.load_local_session", _fake_load)

    sent: list[str] = []

    class _FakeWs:
        async def send(self, text: str) -> None:
            sent.append(text)

    await host._handle_import_local(
        _FakeWs(),  # type: ignore[arg-type]
        HostImportLocalByIdFrame(
            request_id="req_exact",
            source="codex",
            session_id="session-exact",
        ),
    )

    frames = [decode_host_frame(text) for text in sent]
    session_frames = [f for f in frames if isinstance(f, HostImportLocalSessionFrame)]
    done_frames = [f for f in frames if isinstance(f, HostImportLocalDoneFrame)]
    assert [f.session.external_session_id for f in session_frames] == ["session-exact"]
    assert session_frames[0].total == 1
    assert len(done_frames) == 1 and done_frames[0].status == "ok"


async def test_handle_import_local_reports_unreadable_sessions_as_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session that fails to load sends no frame but is counted on the done frame."""
    from omnigent.host.frames import (
        HostImportLocalDoneFrame,
        HostImportLocalSessionFrame,
        decode_host_frame,
    )

    host = _make_host_process()

    def _fake_across(*, limit: int) -> list[tuple[str, str]]:
        return [("claude", "good"), ("claude", "corrupt")]

    def _fake_load(source: str, session_id: str) -> SimpleNamespace:
        if session_id == "corrupt":
            raise ValueError("truncated transcript")
        item = SimpleNamespace(
            type="message",
            response_id="r1",
            data=SimpleNamespace(model_dump=lambda **_kw: {"role": "user"}),
        )
        return SimpleNamespace(
            external_session_id=session_id,
            workspace="/repo",
            items=[item],
            title="ok",
            source=source,
        )

    monkeypatch.setattr(
        "omnigent.session_import.local.list_recent_sessions_across_harnesses", _fake_across
    )
    monkeypatch.setattr("omnigent.session_import.local.load_local_session", _fake_load)

    sent: list[str] = []

    class _FakeWs:
        async def send(self, text: str) -> None:
            sent.append(text)

    await host._handle_import_local(
        _FakeWs(),  # type: ignore[arg-type]
        HostImportLocalFrame(request_id="req_fail", source="all", limit=5),
    )

    frames = [decode_host_frame(text) for text in sent]
    session_frames = [f for f in frames if isinstance(f, HostImportLocalSessionFrame)]
    done_frames = [f for f in frames if isinstance(f, HostImportLocalDoneFrame)]

    # Only the readable session got a frame; the corrupt one is counted, not sent.
    assert [f.session.external_session_id for f in session_frames] == ["good"]
    assert len(done_frames) == 1
    assert done_frames[0].status == "ok" and done_frames[0].failed == 1
