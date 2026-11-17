"""Execute the lint workflow's download and integrity gate against a local server."""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/lint.yml"
pytestmark = pytest.mark.posix_only


@pytest.mark.parametrize(
    ("statuses", "valid_checksum", "expected_requests", "installed"),
    [
        pytest.param([200], True, 1, True, id="healthy"),
        pytest.param([504, 200], True, 2, True, id="transient-gateway-timeout"),
        pytest.param([504], True, 4, False, id="persistent-gateway-timeout"),
        pytest.param([404], True, 1, False, id="missing-release"),
        pytest.param([200], False, 1, False, id="checksum-mismatch"),
    ],
)
def test_ktlint_download_preserves_integrity_and_bounds_retries(
    tmp_path: Path,
    statuses: list[int],
    valid_checksum: bool,
    expected_requests: int,
    installed: bool,
) -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    [step] = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if step.get("name") == "Install ktlint"
    ]
    payload = b"synthetic ktlint release\n"
    requests: list[str] = []

    class ReleaseHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status = statuses[min(len(requests), len(statuses) - 1)]
            requests.append(self.path)
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), ReleaseHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    download = tmp_path / "download"
    destination = tmp_path / "installed"
    release_url = "https://github.com/ktlint/ktlint/releases/download/${KTLINT_VERSION}/ktlint"
    script = step["run"].replace(release_url, f"http://127.0.0.1:{server.server_port}/ktlint")
    assert script != step["run"]
    script = script.replace("/tmp/ktlint", str(download))
    script = script.replace("/usr/local/bin/ktlint", str(destination))
    environment = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "KTLINT_VERSION": step["env"]["KTLINT_VERSION"],
        "KTLINT_SHA256": hashlib.sha256(payload).hexdigest() if valid_checksum else "0" * 64,
    }
    try:
        result = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", 'sudo() { "$@"; }\n' + script],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)

    assert requests == ["/ktlint"] * expected_requests
    assert (result.returncode == 0) is installed, result.stderr
    assert destination.exists() is installed
    if installed:
        assert destination.read_bytes() == payload
        assert os.access(destination, os.X_OK)
