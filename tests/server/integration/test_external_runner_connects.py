"""Integration test: an external runner connects to a local server.

Verifies the ``run --server http://127.0.0.1:...`` scenario end-to-end
by spawning a real ``omnigent server`` subprocess (no ``--agent``),
then launching an external runner via ``_start_cli_runner_process``.
The test asserts the runner registers in the tunnel registry and the
server reports it as online via ``GET /v1/runners/{id}/status``.

This test exercises both fixes in the tunnel loopback bypass:

1. ``_start_cli_runner_process`` derives ``runner_id`` from
   ``token_bound_runner_id(binding_token)`` so the path and
   header agree.
2. The server's tunnel route skips the ``allowed_tunnel_tokens``
   allow-list for loopback clients so an external runner's token
   (not in the server's own allow-list) is accepted.

Without either fix, the runner gets close code 4004 ("runner
tunnel token is not authorized" or "runner_id does not match
tunnel token") and exits with code 1.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

import httpx
import pytest

from tests._helpers.live_server import HarnessCredentials, start_live_server


@pytest.fixture
def local_server(tmp_path: Path) -> tuple[str, int]:
    """
    Spawn a bare ``omnigent server`` (no ``--agent``) and yield
    ``(base_url, server_pid)``.

    Teardown sends SIGTERM with a 5s grace period.

    :param tmp_path: Pytest temporary directory for the DB, artifacts,
        and server log.
    :returns: ``(base_url, server_pid)`` — e.g.
        ``("http://localhost:51234", 12345)``.
    """
    db_path = tmp_path / "test.db"
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    log_path = tmp_path / "server.log"
    proc, base_url = start_live_server(
        creds=HarnessCredentials(harness="databricks", profile=None, llm_api_key=None),
        db_path=db_path,
        artifact_dir=artifact_dir,
        log_path=log_path,
    )
    try:
        yield base_url, proc.pid
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
            proc.wait(timeout=3)


def test_external_runner_connects_to_local_server(
    local_server: tuple[str, int],
) -> None:
    """
    An external runner launched via ``_start_cli_runner_process``
    registers with a local ``omnigent server`` and is reported
    online.

    This is the ``run --server http://127.0.0.1:...`` code path.
    Before the loopback bypass fix, the server rejected the runner
    with close code 4004 because (a) the runner_id didn't match the
    token-derived expectation and (b) the server's tunnel allow-list
    blocked the runner's binding token.

    :param local_server: ``(base_url, server_pid)`` from the
        ``local_server`` fixture.
    """
    base_url, _server_pid = local_server

    # Clear env vars that might leak from the test process and
    # confuse the runner's identity resolution.
    clean_env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in (
            "OMNIGENT_RUNNER_ID",
            "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN",
            "OMNIGENT_RUNNER_TUNNEL_TOKEN",
        )
    }
    saved = os.environ.copy()
    os.environ.clear()
    os.environ.update(clean_env)

    try:
        from omnigent.cli import _start_cli_runner_process, _stop_cli_runner_process

        runner = _start_cli_runner_process(
            server_url=base_url,
            capture_logs=False,
        )

        try:
            # Poll until the server reports the runner as online.
            # The runner needs to connect the WS tunnel and send its hello
            # frame; budget 60s (hard cap — the loop exits as soon as it
            # is online, so only starved CI workers ever use the tail).
            online = False
            status_url = f"{base_url}/v1/runners/{runner.runner_id}/status"
            deadline = time.monotonic() + 60.0
            while time.monotonic() < deadline:
                time.sleep(0.5)
                if runner.proc.poll() is not None:
                    pytest.fail(
                        f"Runner exited early with code {runner.proc.returncode}. "
                        f"If code 1, the tunnel was likely rejected (4004). "
                        f"runner_id={runner.runner_id}"
                    )
                try:
                    resp = httpx.get(status_url, timeout=2)
                    if resp.status_code == 200 and resp.json().get("online") is True:
                        online = True
                        break
                except httpx.HTTPError:
                    pass

            # Runner must be online. If not, the tunnel handshake
            # failed — either the runner_id / binding-token mismatch
            # (Bug 1) or the allow-list rejection (Bug 2).
            assert online, (
                f"Runner {runner.runner_id} did not come online within 60s. "
                f"Runner process alive: {runner.proc.poll() is None}"
            )
        finally:
            _stop_cli_runner_process(runner.proc, grace_timeout=3)
    finally:
        os.environ.clear()
        os.environ.update(saved)
