"""E2E regression: a session whose agent bundle blob is missing from the
artifact store (while the agent row survives) answers a client-safe 409, not
an unhandled 500, for both agent-contents reads and runner launches.

Runs against a real ``omnigent server`` subprocess over the mock LLM::

    .venv/bin/python -m pytest tests/e2e/test_session_missing_bundle_e2e.py -v
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import httpx
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.server_runner import ServerRunner, server_runner
from tests._helpers.session import bundle_files, post_session_bundle

# CI shells can carry an egress proxy; every call targets 127.0.0.1.
_http = httpx.Client(trust_env=False)


@pytest.fixture(scope="module", autouse=True)
def _close_http_client():
    yield
    _http.close()


_HOST_ONLINE_TIMEOUT_S = 30.0
_POLL_S = 0.5
_MISSING_BUNDLE_CODE = "agent_bundle_missing"


def _create_session_with_scoped_agent(base_url: str) -> tuple[str, str]:
    """Create a session bound to a fresh session-scoped agent with a bundle.

    Uploads a minimal ``openai-agents`` agent via the multipart
    ``POST /v1/sessions`` create path — exactly how a launched session is
    registered — so the agent has a real ``bundle_location`` whose blob the
    later removal targets.
    """
    yaml_text = "\n".join(
        [
            "name: missing-bundle-fixture",
            "description: Minimal agent whose bundle blob is later removed from the store.",
            "executor:",
            "  harness: openai-agents",
            "  model: gpt-5.4",
            "prompt: |",
            "  You are a terse smoke-test assistant.",
            "",
        ]
    )
    bundle_bytes = bundle_files({"missing-bundle-fixture.yaml": yaml_text.encode()})
    create = post_session_bundle(
        _http.post,
        f"{base_url}/v1/sessions",
        bundle_bytes,
        filename="missing-bundle-fixture.tar.gz",
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        timeout=30.0,
    )
    create.raise_for_status()
    body = create.json()
    return str(body["session_id"]), str(body["agent_id"])


def _lose_agent_bundle(stack: ServerRunner, agent_id: str) -> str:
    """Remove the agent's bundle blob (and its extracted cache) while keeping
    the agent row.

    Applies the loss through the real store API: read the surviving row's
    ``bundle_location`` and delete that blob from the artifact store. Also
    drops the on-disk ``AgentCache`` entry, which the server keeps under
    ``<artifact_location>/.cache/<agent_id>`` (``cli.py``), so a replacement
    instance cannot serve the spec from a warm disk cache.
    """
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore

    agent = SqlAlchemyAgentStore(stack.database_uri).get(agent_id)
    assert agent is not None and agent.bundle_location is not None
    location = agent.bundle_location

    artifacts = LocalArtifactStore(str(stack.artifact_location))
    assert artifacts.exists(location), f"bundle {location!r} should exist before removal"
    artifacts.delete(location)
    assert not artifacts.exists(location)
    shutil.rmtree(stack.artifact_location / ".cache" / agent_id, ignore_errors=True)

    # The row the dereference starts from must still be present.
    assert SqlAlchemyAgentStore(stack.database_uri).get(agent_id) is not None
    return location


def _wait_for_online_host(base_url: str, timeout: float = _HOST_ONLINE_TIMEOUT_S) -> str:
    """Poll ``GET /v1/hosts`` until a host reports online; return its id."""
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        resp = _http.get(f"{base_url}/v1/hosts", timeout=5.0)
        last = f"{resp.status_code} {resp.text}"
        if resp.status_code == 200:
            for host in resp.json().get("hosts", []):
                if host.get("status") == "online":
                    return str(host["host_id"])
        time.sleep(_POLL_S)
    raise AssertionError(f"no host came online within {timeout}s: {last}")


def test_agent_contents_missing_bundle_returns_client_error(tmp_path: Path) -> None:
    """``GET /agent/contents`` must not 500 when the bundle blob is gone.

    The route reads the artifact store directly, so the lost blob flips the
    same server process from a 200 (bundle present) to the reported failure.
    """
    with server_runner(tmp_path) as stack:
        session_id, agent_id = _create_session_with_scoped_agent(stack.base_url)

        before = _http.get(
            f"{stack.base_url}/v1/sessions/{session_id}/agent/contents", timeout=10.0
        )
        assert before.status_code == 200, (
            f"bundle should serve before removal, got {before.status_code} {before.text!r}"
        )

        _lose_agent_bundle(stack, agent_id)

        after = _http.get(
            f"{stack.base_url}/v1/sessions/{session_id}/agent/contents", timeout=10.0
        )
        assert after.status_code == 409, (
            "a missing agent bundle must surface as a client-safe 409, not an "
            f"unhandled 500; got {after.status_code} {after.text!r}"
        )
        assert after.json()["error"]["code"] == _MISSING_BUNDLE_CODE, after.text


def test_launch_runner_missing_bundle_returns_client_error(tmp_path: Path) -> None:
    """``POST /v1/hosts/{id}/runners`` must not 500 when the bundle blob is gone.

    Launch resolves the agent spec server-side through ``AgentCache.load``,
    which serves a warm cache in the creating process; a replacement instance
    (restarted server + lost artifact dir) resolves cold and dereferences the
    missing blob — the reported resume failure.
    """
    with server_runner(tmp_path) as stack:
        session_id, agent_id = _create_session_with_scoped_agent(stack.base_url)
        _lose_agent_bundle(stack, agent_id)

        # Replacement instance: fresh process clears the in-memory spec cache.
        stack.restart_server()
        stack.start_host()
        host_id = _wait_for_online_host(stack.base_url)

        launch = _http.post(
            f"{stack.base_url}/v1/hosts/{host_id}/runners",
            json={"session_id": session_id, "workspace": str(stack.workspace)},
            timeout=60.0,
        )
        assert launch.status_code == 409, (
            "launching a runner for a session whose agent bundle is missing must "
            f"surface as a client-safe 409, not an unhandled 500; got "
            f"{launch.status_code} {launch.text!r}"
        )
        assert launch.json()["error"]["code"] == _MISSING_BUNDLE_CODE, launch.text
