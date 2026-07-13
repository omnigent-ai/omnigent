"""E2E test: zero-downtime agent update (mock LLM).

Verifies that an in-flight request on the old agent version completes
successfully, and a new request after the update uses the new version
(observable via changed instructions that affect the response content).

Usage::

    pytest tests/e2e/test_agent_update.py -v
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import tempfile
from pathlib import Path
from typing import Any

import httpx
import yaml

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    release_mock_gate,
    reset_mock_llm,
    send_user_message_to_session,
)
from tests.e2e.helpers import final_assistant_text

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEST_AGENT_DIR = _REPO_ROOT / "tests" / "resources" / "agents" / "compaction-test"
_TEST_AGENT_NAME = "compaction-test"

# Marker phrase injected into v2 instructions so we can verify
# the v2 response was produced by the updated spec.
_V2_MARKER = "ZEBRAFINCH"


def _upload_agent_with_id(
    client: httpx.Client,
    agent_dir: Path,
    mock_llm_server_url: str,
) -> dict[str, Any]:
    """
    Upload an agent bundle via multipart ``POST /v1/sessions`` and
    return the agent metadata (including ``id``) from the
    session-scoped agent endpoint.

    Patches the config to route through the mock LLM server.

    :param client: HTTP client pointed at the server.
    :param agent_dir: Path to the agent directory.
    :param mock_llm_server_url: Mock LLM server URL.
    :returns: The agent response JSON with ``id``, ``name``,
        ``version``, etc.
    """
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        with tarfile.open(tmp.name, "w:gz") as tar:
            for item in sorted(agent_dir.rglob("*")):
                if not item.is_file():
                    continue
                arcname = str(item.relative_to(agent_dir))
                if item.name == "config.yaml":
                    cfg = yaml.safe_load(item.read_text())
                    # Point at mock LLM
                    cfg.setdefault("executor", {})["auth"] = {
                        "type": "api_key",
                        "api_key": "mock-key",
                        "base_url": f"{mock_llm_server_url}/v1",
                    }
                    data = yaml.dump(cfg).encode()
                    info = tarfile.TarInfo(name=arcname)
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
                else:
                    tar.add(str(item), arcname=arcname)
        tmp_path = tmp.name
    try:
        with open(tmp_path, "rb") as f:
            metadata = json.dumps({})
            resp = client.post(
                "/v1/sessions",
                data={"metadata": metadata},
                files={
                    "bundle": (
                        "agent.tar.gz",
                        f,
                        "application/gzip",
                    ),
                },
            )
        resp.raise_for_status()
        session_id = resp.json()["session_id"]
        agent_resp = client.get(f"/v1/sessions/{session_id}/agent")
        agent_resp.raise_for_status()
        agent_data: dict[str, Any] = agent_resp.json()
        agent_data["_session_id"] = session_id
        return agent_data
    finally:
        os.unlink(tmp_path)


def _build_updated_bundle(
    agent_dir: Path,
    config_overrides: dict[str, Any],
    mock_llm_server_url: str,
) -> bytes:
    """
    Build a tarball from an agent directory with config.yaml
    fields overridden and mock LLM auth injected.

    :param agent_dir: Path to the original agent directory.
    :param config_overrides: Dict of fields to merge into
        config.yaml.
    :param mock_llm_server_url: Mock LLM server URL.
    :returns: Raw bytes of the ``.tar.gz`` bundle.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for item in Path(agent_dir).rglob("*"):
            if not item.is_file():
                continue
            arcname = str(item.relative_to(agent_dir))
            if item.name == "config.yaml" and item.parent == agent_dir:
                config = yaml.safe_load(item.read_text())
                config.update(config_overrides)
                config.setdefault("executor", {})["auth"] = {
                    "type": "api_key",
                    "api_key": "mock-key",
                    "base_url": f"{mock_llm_server_url}/v1",
                }
                data = yaml.dump(config).encode()
                info = tarfile.TarInfo(name=arcname)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                tar.add(str(item), arcname=arcname)
    return buf.getvalue()


def _update_agent(
    client: httpx.Client,
    session_id: str,
    bundle_bytes: bytes,
) -> dict[str, Any]:
    """
    PUT a new bundle to update an existing agent via the
    session-scoped agent endpoint.

    :param client: HTTP client pointed at the server.
    :param session_id: The session ID whose agent to update.
    :param bundle_bytes: Raw bytes of the new ``.tar.gz`` bundle.
    :returns: The updated agent response JSON.
    """
    resp = client.put(
        f"/v1/sessions/{session_id}/agent",
        files={
            "bundle": (
                "agent.tar.gz",
                bundle_bytes,
                "application/gzip",
            ),
        },
    )
    resp.raise_for_status()
    return resp.json()


def _wait_for_gate_pending(mock_llm_server_url: str, timeout: float = 30) -> None:
    """Poll until a request is blocked on the mock LLM gate."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = httpx.get(f"{mock_llm_server_url}/gate/pending", timeout=2.0)
        resp.raise_for_status()
        if resp.json().get("pending"):
            return
        time.sleep(0.1)
    raise AssertionError(f"No gate pending within {timeout}s")


def test_update_agent_zero_downtime(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    Verifies that the update endpoint doesn't disrupt in-flight
    requests and that new requests use the updated spec.

    **What this test proves:**
    - The PUT endpoint succeeds while a background request is
      running (the server doesn't crash or deadlock).
    - A request created after the update uses the new spec
      (verified via a marker phrase in the mock response).
    - The in-flight request completes without error.

    Steps:
    1. Upload compaction-test agent (version 1).
    2. Send a request whose mock response blocks on a gate.
    3. PUT a new bundle with modified instructions (version 2).
    4. Release the gate so v1 completes; send v2 request.
    5. Both requests complete successfully.
    6. V1 response does NOT contain the v2 marker.
    7. V2 response DOES contain the v2 marker.
    8. Agent metadata shows version=2 and updated_at is set.
    """
    # Use a unique model key for the agent so our mock queue is isolated.
    # The compaction-test agent ships with model: gpt-5.4 which the mock
    # maps via the "default" queue. We configure the default queue since
    # the bundled config.yaml keeps the original model name.
    reset_mock_llm(mock_llm_server_url)

    # Queue: v1 response blocks, then v2 response carries the marker.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "text": (
                    "Photosynthesis is the process by which green plants "
                    "convert light energy into chemical energy."
                ),
                "block": True,
            },
            {"text": f"The answer is 4. {_V2_MARKER}"},
        ],
    )

    # Step 1: Upload compaction-test (v1) and bind the runner.
    created = _upload_agent_with_id(
        http_client,
        _TEST_AGENT_DIR,
        mock_llm_server_url=mock_llm_server_url,
    )
    session_id = created["_session_id"]
    assert created["version"] == 1
    http_client.patch(
        f"/v1/sessions/{session_id}",
        json={"runner_id": live_runner_id},
    ).raise_for_status()

    # Step 2: Start a turn whose mock response blocks on the gate.
    response_id_1 = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Explain photosynthesis in detail.",
    )

    # Wait for the mock LLM to be blocked on the gate.
    _wait_for_gate_pending(mock_llm_server_url)

    # Step 3: Update agent to v2 with marker in instructions.
    v2_bundle = _build_updated_bundle(
        _TEST_AGENT_DIR,
        {
            "description": "Updated compaction-test v2 for e2e test",
            "instructions": (
                f"You MUST include the word '{_V2_MARKER}' somewhere in every response you give."
            ),
        },
        mock_llm_server_url=mock_llm_server_url,
    )
    updated = _update_agent(http_client, session_id, v2_bundle)
    assert updated["version"] == 2

    # Release the gate so v1 completes.
    release_mock_gate(mock_llm_server_url)

    # Step 4: New session on v2.
    session_id_2 = create_runner_bound_session(
        http_client,
        agent_name=_TEST_AGENT_NAME,
        runner_id=live_runner_id,
    )
    response_id_2 = send_user_message_to_session(
        http_client,
        session_id=session_id_2,
        content="What is 2+2? Answer briefly.",
    )

    # Step 5: Poll both to terminal state.
    body1 = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=response_id_1, timeout=60
    )
    body2 = poll_session_until_terminal(
        http_client, session_id=session_id_2, response_id=response_id_2, timeout=60
    )

    assert body1["status"] == "completed", (
        f"V1 request failed with status {body1['status']!r}. Output: {body1.get('output', [])}"
    )
    assert body2["status"] == "completed", (
        f"V2 request failed with status {body2['status']!r}. Output: {body2.get('output', [])}"
    )

    # Step 6: V1 response should NOT contain the marker.
    v1_text = final_assistant_text(body1)
    assert _V2_MARKER not in v1_text, (
        f"V1 response unexpectedly contains the v2 marker "
        f"'{_V2_MARKER}'. First 500 chars: {v1_text[:500]}"
    )

    # Step 7: V2 response MUST contain the marker.
    v2_text = final_assistant_text(body2)
    assert _V2_MARKER in v2_text, (
        f"V2 response does NOT contain the marker '{_V2_MARKER}'. First 500 chars: {v2_text[:500]}"
    )

    # Step 8: Agent metadata reflects the update.
    agent_resp = http_client.get(f"/v1/sessions/{session_id}/agent")
    agent_resp.raise_for_status()
    agent = agent_resp.json()
    assert agent["version"] == 2
    assert agent["updated_at"] is not None
