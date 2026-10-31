"""Exercise the deletion wait through the optional Kubernetes SDK's HTTP transport."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import click
import pytest

import omnigent.onboarding.sandboxes.kubernetes as k8s


@pytest.fixture
def sdk_api(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[k8s.KubernetesSandboxLauncher, SimpleNamespace]]:
    pytest.importorskip("kubernetes")
    state = SimpleNamespace(responses=[404], delay=0.0, requests=[], retry_after=None)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state.requests.append(self.path)
            status = state.responses[0]
            if len(state.responses) > 1:
                state.responses.pop(0)
            time.sleep(state.delay)
            body = json.dumps(
                {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "resume-job"}}
                if status == 200
                else {
                    "apiVersion": "v1",
                    "kind": "Status",
                    "code": status,
                    "message": "test response",
                }
            ).encode()
            with suppress(OSError):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if state.retry_after is not None:
                    self.send_header("Retry-After", state.retry_after)
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        "kubernetes.config.load_kube_config",
        lambda **kwargs: setattr(
            kwargs["client_configuration"], "host", f"http://127.0.0.1:{server.server_port}"
        ),
    )
    launcher = k8s.KubernetesSandboxLauncher(in_cluster=False, namespace="test")
    try:
        yield launcher, state
    finally:
        launcher._close_clients()
        server.shutdown()
        server.server_close()
        thread.join()


def test_deletion_wait_reads_jobs_through_the_real_sdk(
    sdk_api: tuple[k8s.KubernetesSandboxLauncher, SimpleNamespace],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher, state = sdk_api
    state.responses = [200, 200, 404]
    monkeypatch.setattr(k8s, "_RESUME_DELETE_POLL_S", 0.01)

    launcher._wait_for_job_deleted("resume-job")

    assert state.requests == ["/apis/batch/v1/namespaces/test/jobs/resume-job"] * 3
    assert launcher._api_client is None
    _, batch = launcher._load_clients()
    assert batch.api_client.configuration.retries is None


def test_deletion_wait_has_no_hidden_sdk_transport_retries(
    sdk_api: tuple[k8s.KubernetesSandboxLauncher, SimpleNamespace],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher, state = sdk_api
    state.responses = [200]
    state.delay = 0.75
    monkeypatch.setattr(k8s, "_RESUME_DELETE_TIMEOUT_S", 0.2)
    launcher._load_clients()

    with pytest.raises(click.ClickException, match=r"last check failed:.*Read timed out"):
        launcher._wait_for_job_deleted("resume-job")

    assert len(state.requests) == 1
    assert launcher._api_client is None


def test_deletion_wait_caps_retry_after_to_the_remaining_budget(
    sdk_api: tuple[k8s.KubernetesSandboxLauncher, SimpleNamespace],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher, state = sdk_api
    state.responses = [429, 404]
    state.retry_after = "60"
    monkeypatch.setattr(k8s, "_RESUME_DELETE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(k8s, "_RESUME_DELETE_POLL_S", 0.01)

    with pytest.raises(click.ClickException, match="last check failed"):
        launcher._wait_for_job_deleted("resume-job")

    assert len(state.requests) == 1
    assert launcher._api_client is None
