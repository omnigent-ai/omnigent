"""End-to-end test for parent -> sub-agent file passing (mock LLM).

Proves the full #900 path:

1. A file is uploaded to a PARENT session (capture its file id + bytes).
2. The parent agent calls ``sys_session_send`` with that file id in the
   object-args ``file_ids`` list (scripted via the mock LLM tool call).
3. The runner copies the parent's file into the spawned CHILD session and
   appends an ``input_file`` block (referencing the copied, child-scoped
   id) to the child's first message.
4. The CHILD session ends up with its OWN file: a DISTINCT new id whose
   bytes byte-for-byte match the original parent upload.

This is the integration-level cousin of the U1-U3 unit/route tests
(``tests/runner/test_file_tool_dispatch.py`` and the server copy-route
tests cover the edge/error cases); here we drive the whole stack with a
live server + runner + mock LLM, mirroring ``test_subagent_autowake_e2e``.

Excluded from default ``pytest`` runs via ``--ignore=tests/e2e``. Invoke
with::

    uv run --extra dev python -m pytest \\
        tests/e2e/test_files_to_subagent_e2e.py -p no:cacheprovider
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)
from tests.e2e.helpers import POLL_INTERVAL_S

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEST_MD_PATH = _REPO_ROOT / "tests" / "resources" / "test.md"

# Each flow is several serial mock-LLM turns (parent dispatch, child turn,
# parent auto-wake), so allow plenty of headroom under signal-based timeout.
pytestmark = pytest.mark.timeout(600, method="signal")


def _find_child_session_id(
    http_client: httpx.Client,
    *,
    parent_session_id: str,
    child_title: str,
    timeout: float = 180.0,
) -> str:
    """Poll the sub-agent session list until the parent's child appears.

    sys_session_send spawns the child asynchronously, so the child
    conversation (``kind=sub_agent``) shows up after the parent's
    dispatch turn ends. The runner mints the child with a deterministic
    ``"{agent}:{title}"`` title, so we match on that, then confirm via
    the single-session snapshot (which DOES carry ``parent_session_id``,
    unlike the list item) that the lineage is correct.

    :param http_client: HTTP client pointed at the live server.
    :param parent_session_id: The dispatching parent session id.
    :param child_title: The minted child title, ``"{agent}:{title}"``.
    :param timeout: Max seconds to wait for the child to appear.
    :returns: The child (sub-agent) session id.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = http_client.get(
            "/v1/sessions",
            params={"kind": "sub_agent", "limit": 1000},
        )
        resp.raise_for_status()
        for item in resp.json().get("data", []):
            if item.get("title") != child_title:
                continue
            candidate = str(item["id"])
            snap = http_client.get(f"/v1/sessions/{candidate}")
            snap.raise_for_status()
            if snap.json().get("parent_session_id") == parent_session_id:
                return candidate
        time.sleep(POLL_INTERVAL_S)
    # Diagnostic dump: show the sub_agent list and the parent's items so a
    # missing child (failed dispatch vs. mismatched title) is debuggable.
    sub_list = http_client.get("/v1/sessions", params={"kind": "sub_agent", "limit": 1000})
    parent_snap = http_client.get(f"/v1/sessions/{parent_session_id}")
    raise AssertionError(
        f"No sub-agent child session titled {child_title!r} for parent "
        f"{parent_session_id!r} appeared within {timeout:.0f}s.\n"
        f"sub_agent sessions: {sub_list.text[:1500]}\n"
        f"parent items: {parent_snap.text[:2000]}"
    )


def _wait_for_child_file(
    http_client: httpx.Client,
    *,
    child_session_id: str,
    timeout: float = 120.0,
) -> dict:
    """Poll the child session's file list until a file appears.

    :param http_client: HTTP client pointed at the live server.
    :param child_session_id: The spawned child session id.
    :param timeout: Max seconds to wait for the copied file to land.
    :returns: The child file resource dict.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = http_client.get(f"/v1/sessions/{child_session_id}/resources/files")
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if data:
            return data[0]
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"No file copied into child session {child_session_id!r} within {timeout:.0f}s."
    )


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_file_passes_from_parent_agent_to_subagent(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """A file uploaded to the parent reaches the spawned sub-agent.

    Happy path:

    - Upload ``test.md`` to the parent session; capture its id + bytes.
    - Drive the parent (mock LLM) to emit ``sys_session_send`` with
      ``args={"input": ..., "file_ids": [parent_file_id]}``.
    - Assert the child session has a file whose id != the parent id and
      whose downloaded bytes match the original.
    - Assert the child's first user message carries an ``input_file``
      block referencing the child-scoped file id.
    """
    assert _TEST_MD_PATH.exists(), f"Test file missing at {_TEST_MD_PATH}. Restore from git."
    original_bytes = _TEST_MD_PATH.read_bytes()

    uid = uuid.uuid4().hex[:6]
    parent_model = f"mock-file-parent-{uid}"
    child_model = f"mock-file-child-{uid}"
    child_marker = "CHILD_FILE_OK_2026"
    mock_base = f"{mock_llm_server_url}/v1"

    reset_mock_llm(mock_llm_server_url)

    parent_name = register_inline_agent(
        http_client,
        name=f"file-parent-{uid}",
        harness="openai-agents",
        model=parent_model,
        profile="",
        prompt=(
            "You are the file-passing E2E parent. Dispatch the analyst "
            "sub-agent via sys_session_send, forwarding the uploaded file."
        ),
        mock_llm_base_url=mock_base,
        extra_config={
            "tools": {
                "analyst": {
                    "type": "agent",
                    "description": "Test-fixture analyst that reads the forwarded file.",
                    "executor": {
                        "harness": "openai-agents",
                        "model": child_model,
                        "auth": {
                            "type": "api_key",
                            "api_key": "mock-key",
                            "base_url": mock_base,
                        },
                    },
                    "prompt": (
                        "You are the test-fixture analyst. Include "
                        f"{child_marker} verbatim in your response."
                    ),
                },
            },
        },
    )

    # Create the parent session and upload the file BEFORE dispatch so the
    # parent owns a real file id to forward.
    parent_session_id = create_runner_bound_session(
        http_client,
        agent_name=parent_name,
        runner_id=live_runner_id,
    )
    upload_resp = http_client.post(
        f"/v1/sessions/{parent_session_id}/resources/files",
        files={"file": (_TEST_MD_PATH.name, original_bytes, "text/markdown")},
    )
    upload_resp.raise_for_status()
    parent_file_id = upload_resp.json()["id"]

    # Parent mock queue:
    #   1. dispatch: sys_session_send forwarding file_ids
    #   2. ack after the tool result
    #   3. auto-wake continuation quoting the child marker
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_dispatch",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "analyst",
                                "title": "file-analysis",
                                "args": {
                                    "input": "Analyze the attached markdown file.",
                                    "file_ids": [parent_file_id],
                                },
                            }
                        ),
                    },
                ],
            },
            {"text": "Dispatched analyst, waiting for result."},
            {"text": f"The analyst returned: {child_marker}"},
        ],
        key=parent_model,
    )
    # Child mock queue: one turn that quotes the marker.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"File analyzed. {child_marker}"}],
        key=child_model,
    )

    dispatch_response_id = send_user_message_to_session(
        http_client,
        session_id=parent_session_id,
        content="Dispatch the analyst sub-agent with the uploaded file.",
    )
    poll_session_until_terminal(
        http_client,
        session_id=parent_session_id,
        response_id=dispatch_response_id,
        timeout=180,
    )

    # Discover the spawned child and assert it received a copied file.
    # The runner mints the child title as "{agent}:{title}".
    child_session_id = _find_child_session_id(
        http_client,
        parent_session_id=parent_session_id,
        child_title="analyst:file-analysis",
    )
    child_file = _wait_for_child_file(
        http_client,
        child_session_id=child_session_id,
    )
    child_file_id = child_file["id"]

    # DISTINCT new id: the child reads its OWN copy, not the parent's row.
    assert child_file_id != parent_file_id, (
        f"Child file id {child_file_id!r} must differ from the parent's "
        f"{parent_file_id!r} — the copy creates a child-scoped row."
    )

    # MATCHING bytes: download the child's copy and compare to the original.
    content_resp = http_client.get(
        f"/v1/sessions/{child_session_id}/resources/files/{child_file_id}/content"
    )
    content_resp.raise_for_status()
    assert content_resp.content == original_bytes, (
        "Child file bytes do not match the original parent upload — "
        "the copy did not preserve content."
    )

    # The copied file must NOT be visible under the parent's id in the child,
    # and the parent must still own its original row (no cross-session move).
    parent_file_resp = http_client.get(
        f"/v1/sessions/{parent_session_id}/resources/files/{parent_file_id}"
    )
    parent_file_resp.raise_for_status()

    # The child's first user message should carry an input_file block
    # referencing the child-scoped (copied) id, proving the file resolves
    # into the child's first turn.
    snap = http_client.get(f"/v1/sessions/{child_session_id}")
    snap.raise_for_status()
    blob = json.dumps(snap.json().get("items", []))
    assert child_file_id in blob, (
        f"Child file id {child_file_id!r} not referenced in the child's "
        f"conversation items — the file block did not reach the first turn."
    )
    assert "input_file" in blob, (
        "No input_file content block in the child's first message — "
        "the forwarded file was not attached as a content block."
    )
