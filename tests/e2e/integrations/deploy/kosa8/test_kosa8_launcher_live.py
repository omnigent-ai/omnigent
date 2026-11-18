"""Optional live test of the kosa8 launcher primitives against real sandboxes.

Drives the real :class:`~omnigent.onboarding.sandboxes.kosa8.Kosa8SandboxLauncher`
through what the managed-host and CLI-bootstrap flows rely on: provision from
the host image, run (exit codes, stderr, env passthrough, a command longer
than kosa8's 90 s buffered-exec limit), put, streaming, keep-alive, a sleep
and an in-place wake that keeps a background process, status, an idempotent
terminate, and a clear error for a rejected API key. Every sandbox it creates
is deleted, also when a check fails.

Opt-in because it creates billable sandboxes:

    OMNIGENT_E2E_KOSA8=1 KOSA8_API_KEY=... \
    .venv/bin/python -m pytest tests/e2e/integrations/deploy/kosa8/test_kosa8_launcher_live.py -v
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

import click
import pytest

from omnigent.onboarding.sandboxes.kosa8 import Kosa8SandboxLauncher

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("OMNIGENT_E2E_KOSA8") != "1",
        reason="set OMNIGENT_E2E_KOSA8=1 to create live kosa8 sandboxes",
    ),
    pytest.mark.timeout(1200),
]

_MARKER_ENV = "OMNIGENT_E2E_KOSA8_MARKER"


@pytest.fixture(scope="module")
def launcher() -> Iterator[Kosa8SandboxLauncher]:
    """A launcher that sets one marker variable on its sandboxes, preflighted."""
    os.environ[_MARKER_ENV] = f"marker-{int(time.time())}"
    kosa8_launcher = Kosa8SandboxLauncher(env=[_MARKER_ENV], labels={"purpose": "e2e"})
    kosa8_launcher.prepare()
    try:
        yield kosa8_launcher
    finally:
        os.environ.pop(_MARKER_ENV, None)


@pytest.fixture(scope="module")
def sandbox(launcher: Kosa8SandboxLauncher) -> Iterator[str]:
    """One provisioned sandbox shared by the module, deleted at the end."""
    sandbox_id = launcher.provision("e2e-launcher")
    try:
        yield sandbox_id
    finally:
        launcher.terminate(sandbox_id)


def test_host_image_is_ready(launcher: Kosa8SandboxLauncher, sandbox: str) -> None:
    """The sandbox boots the host image, so omnigent is already installed."""
    result = launcher.run(sandbox, "omnigent --version")
    assert "omnigent" in result.stdout.lower()


def test_run_forwards_env_exit_codes_and_stderr(
    launcher: Kosa8SandboxLauncher, sandbox: str
) -> None:
    """Commands see the passthrough env; exit codes and stderr come back intact."""
    marker = os.environ[_MARKER_ENV]
    assert launcher.run(sandbox, f'echo "${_MARKER_ENV}"').stdout.strip() == marker
    failed = launcher.run(sandbox, "echo oops >&2; exit 7", check=False)
    assert (failed.returncode, failed.stderr.strip()) == (7, "oops")
    with pytest.raises(click.ClickException, match="exit 7"):
        launcher.run(sandbox, "exit 7")


def test_run_outlasts_the_buffered_exec_limit(
    launcher: Kosa8SandboxLauncher, sandbox: str
) -> None:
    """A streamed command runs past 90 s, as installs and clones do."""
    result = launcher.run(sandbox, "sleep 100; echo finished")
    assert result.stdout.strip().endswith("finished")


def test_put_and_stream(launcher: Kosa8SandboxLauncher, sandbox: str, tmp_path: Path) -> None:
    """A shipped file arrives byte for byte; a streamed command yields its lines."""
    local = tmp_path / "payload.bin"
    local.write_bytes(os.urandom(64 * 1024))
    launcher.put(sandbox, local, "/tmp/payload.bin")
    size = launcher.run(sandbox, "wc -c < /tmp/payload.bin").stdout.strip()
    assert size == str(64 * 1024)

    process = launcher.stream_exec(sandbox, "for i in 1 2 3; do echo line$i; done")
    assert [line.strip() for line in process.lines] == ["line1", "line2", "line3"]
    assert process.wait() == 0


def test_keep_alive_and_status(launcher: Kosa8SandboxLauncher, sandbox: str) -> None:
    assert launcher.keep_alive(sandbox) is True
    assert launcher.is_running(sandbox) is True


def test_sleep_and_wake_keep_processes(launcher: Kosa8SandboxLauncher, sandbox: str) -> None:
    """A slept sandbox wakes in place with its background process still running."""
    launcher.run(sandbox, "setsid nohup sleep 3600 >/dev/null 2>&1 & echo $! > /tmp/bg.pid")
    launcher.stop(sandbox)
    assert launcher.is_running(sandbox) is False
    launcher.stop(sandbox)  # already asleep: a no-op
    launcher.resume(sandbox)
    assert launcher.is_running(sandbox) is True
    alive = launcher.run(sandbox, 'kill -0 "$(cat /tmp/bg.pid)"', check=False)
    assert alive.returncode == 0


def test_terminate_is_idempotent(launcher: Kosa8SandboxLauncher) -> None:
    sandbox_id = launcher.provision("e2e-terminate")
    launcher.terminate(sandbox_id)
    launcher.terminate(sandbox_id)
    assert launcher.is_running(sandbox_id) is False


def test_rejected_api_key_is_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KOSA8_API_KEY", "k8_invalid")
    with pytest.raises(click.ClickException, match="HTTP 401"):
        Kosa8SandboxLauncher().provision("e2e-bad-key")
