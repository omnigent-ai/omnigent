"""E2E regression test for the multipart-create host binding.

Multipart ``POST /v1/sessions`` (agent bundle + ``metadata`` JSON part)
accepts ``host_id``/``workspace`` in ``SessionCreateMetadata``, and the
schema documents ``host_id`` as triggering the host launch flow. The
session created from the bundle must therefore bind to the given
external host and get a runner launched on it, exactly like the JSON
form (``{"agent_id", "host_id", "workspace"}``) does.

Regression guard: the multipart path used to silently drop
``metadata.host_id`` — the session was created with ``host_id: null``
and ``runner_id: null``, no runner was ever launched on the host, and
only ``workspace`` from the same metadata part survived.

Runs against the mock LLM server — no real credentials needed::

    .venv/bin/python -m pytest tests/e2e/test_multipart_create_host_binding.py -v
"""

from __future__ import annotations

import json
import signal
import subprocess
import time
from pathlib import Path

import httpx

from tests.e2e.conftest import POLL_INTERVAL_S, build_agent_bundle
from tests.e2e.test_host_e2e import (
    _spawn_host_daemon,
    _wait_for_host_online,
    _write_smoke_agent_yaml,
)


def _stop_daemon(proc: subprocess.Popen[bytes]) -> None:
    """Terminate a spawned host daemon, escalating to SIGKILL.

    :param proc: The daemon subprocess handle.
    """
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def test_multipart_create_binds_host_and_launches_runner(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """
    Multipart create with ``metadata.host_id`` binds the session to the
    host and launches its runner.

    Journey (the reported user path):

    1. Connect a host to the server and wait until it is online.
    2. ``POST /v1/sessions`` as multipart — an agent bundle plus a
       ``metadata`` JSON part carrying ``host_id`` and ``workspace``.
    3. The created session must carry the given ``host_id`` and a
       ``runner_id`` (the launch flow ran), with the runner reporting
       online — the same outcome the JSON create form produces.

    Before the fix the session stayed ``host_id: null`` /
    ``runner_id: null`` forever while ``workspace`` was persisted, so no
    first message could ever be delivered.
    """
    daemon = _spawn_host_daemon(
        tmp_path=tmp_path,
        live_server=live_server,
        mock_llm_server_url=mock_llm_server_url,
    )
    try:
        _wait_for_host_online(http_client, daemon.host_id, timeout=30.0)

        workspace = tmp_path / "ws"
        workspace.mkdir()
        bundle = build_agent_bundle(_write_smoke_agent_yaml(tmp_path))
        resp = http_client.post(
            "/v1/sessions",
            data={
                "metadata": json.dumps({"host_id": daemon.host_id, "workspace": str(workspace)})
            },
            files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
            timeout=120.0,
        )
        assert resp.status_code == 201, f"multipart create failed: {resp.status_code} {resp.text}"
        session_id = resp.json()["session_id"]

        # The launch may complete synchronously with the create (the JSON
        # path blocks on the host's launch ack) or bind shortly after;
        # poll the session snapshot until both bindings appear.
        deadline = time.monotonic() + 60.0
        snapshot: dict[str, object] = {}
        while time.monotonic() < deadline:
            session_resp = http_client.get(f"/v1/sessions/{session_id}")
            session_resp.raise_for_status()
            snapshot = session_resp.json()
            if snapshot.get("host_id") and snapshot.get("runner_id"):
                break
            time.sleep(POLL_INTERVAL_S)

        # The metadata part was parsed (workspace persisted), so a missing
        # host binding is precisely the dropped-host_id bug.
        assert snapshot.get("workspace") == str(workspace), (
            f"workspace from the metadata part was not persisted: {snapshot}"
        )
        assert snapshot.get("host_id") == daemon.host_id, (
            "multipart POST /v1/sessions dropped metadata.host_id: session "
            f"has host_id={snapshot.get('host_id')!r} (expected "
            f"{daemon.host_id!r}) and runner_id={snapshot.get('runner_id')!r} "
            "— the bundled session never binds to the host"
        )
        runner_id = snapshot.get("runner_id")
        assert runner_id, (
            f"multipart create bound the host but never launched a runner: runner_id={runner_id!r}"
        )

        # The launch flow must have actually started a runner on the host,
        # not just written the binding: the runner comes online.
        online_deadline = time.monotonic() + 60.0
        runner_online = False
        while time.monotonic() < online_deadline:
            status_resp = http_client.get(f"/v1/runners/{runner_id}/status")
            if status_resp.status_code == 200 and status_resp.json().get("online") is True:
                runner_online = True
                break
            time.sleep(POLL_INTERVAL_S)
        assert runner_online, (
            f"runner {runner_id!r} bound by the multipart create never came "
            "online — the host launch flow did not run"
        )
    finally:
        _stop_daemon(daemon.proc)
