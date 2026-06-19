"""E2E tests for the session filesystem resources API.

Tests exercise the unified filesystem endpoints under
``/v1/sessions/{id}/resources/environments/{env_id}/filesystem``
that replaced the legacy ``/filesystem/changes`` and
``/filesystem/file-content`` routes (see
``designs/UI_SESSION_RESOURCES_MIGRATION.md`` §F1).

**No-LLM tests** (no LLM inference calls; ``--llm-api-key`` still
required to start the server):

- ``test_filesystem_listing_shape``: uploads the workspace-writer
  bundle as a session-scoped agent (no inference), binds the live
  runner, and verifies that the root directory listing has the
  correct envelope and per-entry fields.

- ``test_filesystem_user_write_put_round_trip``: exercises the
  user-facing ``PUT .../filesystem/{path}`` endpoint that the web
  editor's auto-save calls — write a new file, read it back, then
  overwrite it and read back again. No inference; distinct from the
  agent ``sys_os_write`` path.

**Mock-LLM tests** (driven by the mock LLM server, no real LLM
needed):

- ``test_filesystem_changes_appear_after_agent_write``: creates a
  bound session from the workspace-writer bundle, asks it to write a
  uniquely-named file via a mock ``sys_os_write`` tool call, then
  asserts it surfaces in the directory listing with a non-null
  ``status`` and that its content is readable via the file endpoint.

- ``test_diff_endpoint_shows_git_diff_for_modified_file``: asks the
  agent to overwrite an existing tracked file via a mock
  ``sys_os_write`` tool call, then asserts the diff endpoint returns
  the correct ``before`` (git HEAD) and ``after`` (modified) content.

Usage::

    pytest tests/e2e/test_filesystem_changed_files_e2e.py -v
"""

from __future__ import annotations

import io
import json
import tarfile
import time
import uuid
from pathlib import Path

import httpx
import yaml

from tests.e2e.conftest import (
    build_agent_bundle,
    configure_mock_llm,
    reset_mock_llm,
)
from tests.e2e.helpers import POLL_INTERVAL_S

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKSPACE_WRITER_DIR = _REPO_ROOT / "tests" / "resources" / "agents" / "workspace-file-writer"

# The default environment ID used by all runner resource endpoints.
_DEFAULT_ENV = "default"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _fs_root_url(session_id: str) -> str:
    """Build the root filesystem listing URL for a session.

    :param session_id: Session/conversation identifier.
    :returns: URL string for ``GET .../filesystem``.
    """
    return f"/v1/sessions/{session_id}/resources/environments/{_DEFAULT_ENV}/filesystem"


def _fs_changes_url(session_id: str) -> str:
    """Build the filesystem changes URL for a session.

    :param session_id: Session/conversation identifier.
    :returns: URL string for ``GET .../changes``.
    """
    return f"/v1/sessions/{session_id}/resources/environments/{_DEFAULT_ENV}/changes"


def _fs_file_url(session_id: str, path: str) -> str:
    """Build a file or subdirectory URL for a session filesystem.

    :param session_id: Session/conversation identifier.
    :param path: Path relative to the environment root.
    :returns: URL string for ``GET .../filesystem/{path}``.
    """
    return f"/v1/sessions/{session_id}/resources/environments/{_DEFAULT_ENV}/filesystem/{path}"


def _poll_until_session_idle(
    client: httpx.Client,
    session_id: str,
    timeout: float = 120,
) -> dict:
    """Poll GET /v1/sessions/{id} until the session leaves the running state.

    ``"idle"`` means the agent loop finished successfully; ``"failed"``
    is a terminal error.

    :param client: HTTP client pointed at the live server.
    :param session_id: The session to poll.
    :param timeout: Maximum seconds to wait before raising.
    :returns: The terminal session snapshot dict.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{session_id}")
        resp.raise_for_status()
        body = resp.json()
        if body["status"] in ("idle", "failed"):
            return body
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"Session {session_id!r} did not reach terminal state within {timeout}s")


def _create_bound_session(
    client: httpx.Client,
    *,
    live_runner_id: str,
    databricks_workspace_host: str | None,
    initial_text: str | None = None,
    mock_llm_server_url: str | None = None,
) -> str:
    """
    Create a session-scoped workspace-writer agent and bind the runner.

    :param client: HTTP client pointed at the live server.
    :param live_runner_id: Runner id registered by the live server,
        e.g. ``"runner_abc123"``.
    :param databricks_workspace_host: Workspace host URL when the
        test suite routes LLM calls through Databricks model serving.
    :param initial_text: Optional first user message to enqueue
        after binding.
    :param mock_llm_server_url: Mock LLM server URL. When set, injects
        mock auth into the agent bundle so the executor hits the mock
        server instead of a real LLM.
    :returns: The created session id.
    """
    if mock_llm_server_url is not None:
        bundle = _build_mock_workspace_writer_bundle(mock_llm_server_url)
    else:
        bundle = build_agent_bundle(
            _WORKSPACE_WRITER_DIR,
            rewrite_model_for_databricks=databricks_workspace_host is not None,
        )
    create_resp = client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )
    create_resp.raise_for_status()
    session_id: str = create_resp.json()["session_id"]

    bind_resp = client.patch(
        f"/v1/sessions/{session_id}",
        json={"runner_id": live_runner_id},
    )
    bind_resp.raise_for_status()

    if initial_text is not None:
        event_resp = client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": initial_text}],
                },
            },
        )
        event_resp.raise_for_status()

    return session_id


def _build_mock_workspace_writer_bundle(mock_llm_server_url: str) -> bytes:
    """Read the on-disk workspace-file-writer YAML, inject mock auth, tarball.

    :param mock_llm_server_url: Mock LLM server base URL.
    :returns: Gzipped tarball bytes ready for upload.
    """
    yaml_path = _WORKSPACE_WRITER_DIR / "workspace-file-writer.yaml"
    spec = yaml.safe_load(yaml_path.read_text())
    spec.setdefault("executor", {})["auth"] = {
        "type": "api_key",
        "api_key": "mock-key",
        "base_url": f"{mock_llm_server_url}/v1",
    }
    patched = yaml.dump(spec, sort_keys=False).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="./workspace-file-writer.yaml")
        info.size = len(patched)
        tar.addfile(info, io.BytesIO(patched))
    return buf.getvalue()


# ── No-LLM tests ──────────────────────────────────────────────────────────────


def test_filesystem_listing_shape(
    http_client: httpx.Client,
    live_runner_id: str,
    databricks_workspace_host: str | None,
) -> None:
    """GET .../filesystem returns a well-formed list envelope.

    Creates a real session (no initial items, so no LLM call) and calls
    the root directory listing to verify the response has the correct
    envelope shape and that every entry carries the required fields.

    A fresh session directory may be empty; the test verifies the shape
    regardless of whether data is present.

    :param http_client: HTTP client pointed at the live server.
    :param live_runner_id: Runner id registered by the live server.
    :param databricks_workspace_host: Workspace host URL when the
        test suite routes LLM calls through Databricks model serving.
    """
    session_id = _create_bound_session(
        http_client,
        live_runner_id=live_runner_id,
        databricks_workspace_host=databricks_workspace_host,
    )

    resp = http_client.get(_fs_root_url(session_id))
    resp.raise_for_status()
    body = resp.json()

    assert body["object"] == "list", f"Expected object='list', got {body.get('object')!r}"
    assert isinstance(body["data"], list), f"data must be a list, got {type(body.get('data'))}"
    assert "has_more" in body, "Response must include has_more pagination field"

    for entry in body["data"]:
        assert "id" in entry and "name" in entry and "type" in entry, (
            f"Entry missing required fields: {entry}"
        )


def test_filesystem_user_write_put_round_trip(
    http_client: httpx.Client,
    live_runner_id: str,
    databricks_workspace_host: str | None,
) -> None:
    """User PUT write round-trips: create -> read-back -> overwrite -> read-back.

    Exercises the exact endpoint the web editor's auto-save calls
    (``PUT .../filesystem/{path}`` with ``{content, encoding}``), which is a
    different code path from the agent ``sys_os_write`` flow covered by the
    other tests in this module. No inference is involved. The file is written
    into the session's sandboxed environment root (an ephemeral per-session
    directory), so no repo-tree cleanup is needed.

    Steps:
    1. PUT a brand-new file -> ``write_result`` reports ``created=True`` and
       the UTF-8 byte length of the payload.
    2. GET the file -> content matches exactly (proves the bytes were
       persisted and read back from disk, not echoed from the request).
    3. PUT again with new content -> ``created=False`` (overwrite detected,
       the auto-save re-write case) and the new byte count.
    4. GET -> content reflects the overwrite.

    :param http_client: HTTP client pointed at the live server.
    :param live_runner_id: Runner id registered by the live server.
    :param databricks_workspace_host: Workspace host URL when the
        test suite routes LLM calls through Databricks model serving.
    """
    # UUID suffix so parallel workers don't collide on the same path.
    filename = f"e2e_user_write_{uuid.uuid4().hex[:8]}.md"
    initial = "# Auto-save round-trip\n\nfirst write\n"
    modified = "# Auto-save round-trip\n\nsecond write (overwritten)\n"

    session_id = _create_bound_session(
        http_client,
        live_runner_id=live_runner_id,
        databricks_workspace_host=databricks_workspace_host,
    )

    # 1. Write a brand-new file via the user-facing PUT endpoint.
    put_resp = http_client.put(
        _fs_file_url(session_id, filename),
        json={"content": initial, "encoding": "utf-8"},
    )
    assert put_resp.status_code == 200, f"PUT write failed: {put_resp.status_code} {put_resp.text}"
    result = put_resp.json()
    assert result["object"] == "session.environment.filesystem.write_result", (
        f"Wrong write-result envelope: {result.get('object')!r}"
    )
    # created=True proves the file did not exist before this write.
    assert result["created"] is True, (
        f"Expected created=True for a new file, got {result.get('created')!r}"
    )
    assert result["operation"] == "write", (
        f"Expected operation 'write', got {result.get('operation')!r}"
    )
    # A mismatch here means the server truncated or re-encoded the body
    # instead of writing the exact bytes the editor sent.
    assert result["bytes_written"] == len(initial.encode("utf-8")), (
        f"bytes_written {result.get('bytes_written')} != "
        f"{len(initial.encode('utf-8'))} (UTF-8 payload length)"
    )

    # 2. Read it back — content must match exactly, byte for byte.
    get_resp = http_client.get(_fs_file_url(session_id, filename))
    get_resp.raise_for_status()
    body = get_resp.json()
    assert body.get("content") == initial, (
        f"Read-back content mismatch.\n  expected: {initial!r}\n  got: {body.get('content')!r}"
    )

    # 3. Overwrite the same path with new content (the auto-save re-write).
    put2 = http_client.put(
        _fs_file_url(session_id, filename),
        json={"content": modified, "encoding": "utf-8"},
    )
    assert put2.status_code == 200, f"Overwrite PUT failed: {put2.status_code} {put2.text}"
    result2 = put2.json()
    # created=False proves the endpoint detected the existing file —
    # an overwrite, not a create. If True, the write target resolved to
    # the wrong path or the existence check is broken.
    assert result2["created"] is False, (
        f"Expected created=False on overwrite, got {result2.get('created')!r}"
    )
    assert result2["bytes_written"] == len(modified.encode("utf-8")), (
        f"bytes_written {result2.get('bytes_written')} != {len(modified.encode('utf-8'))}"
    )

    # 4. Read-back reflects the overwrite (not the original content).
    get2 = http_client.get(_fs_file_url(session_id, filename))
    get2.raise_for_status()
    assert get2.json().get("content") == modified, (
        "Read-back after overwrite did not reflect the new content — the "
        "second write did not replace the file on disk."
    )


# ── Mock-LLM tests ───────────────────────────────────────────────────────────


def test_filesystem_changes_appear_after_agent_write(
    http_client: httpx.Client,
    live_runner_id: str,
    databricks_workspace_host: str | None,
    mock_llm_server_url: str,
) -> None:
    """Agent write surfaces in the directory listing with a non-null status.

    Full round-trip verification via the resources API:
    1. Configure the mock LLM to return a ``sys_os_write`` tool call
       followed by a text confirmation.
    2. Ask the agent to write a uniquely-named file via POST /v1/sessions.
    3. Extract the session_id from the response.
    4. Poll GET .../changes until the written file appears.
    5. Assert the file entry has ``status`` in (``"created"``).
    6. Assert the file content is readable via the file endpoint.

    Failure modes this catches:
    - Watchdog observer not started (lifespan wiring bug) -> listing
      never shows the file with a non-null status.
    - File written outside the watched CWD -> path never appears in events.
    - ``_ensure_session_registered`` failing silently -> incorrect start
      boundary causes the file to be invisible.

    :param http_client: HTTP client pointed at the live server.
    :param live_runner_id: Runner id registered by the live server.
    :param databricks_workspace_host: Workspace host URL when the
        test suite routes LLM calls through Databricks model serving.
    :param mock_llm_server_url: Mock LLM server URL.
    """
    # Use a UUID suffix so parallel test runs don't collide.
    filename = f"e2e_workspace_test_{uuid.uuid4().hex[:8]}.md"
    file_content = "Hello from the workspace e2e test"
    test_file = _REPO_ROOT / filename

    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_write_fs",
                        "name": "sys_os_write",
                        "arguments": json.dumps({"path": filename, "content": file_content}),
                    },
                ],
            },
            {"text": "File created successfully."},
        ],
        key="default",
    )

    try:
        session_id = _create_bound_session(
            http_client,
            live_runner_id=live_runner_id,
            databricks_workspace_host=databricks_workspace_host,
            initial_text=(
                f"Write a file named '{filename}' containing exactly: "
                f"'{file_content}'. Use sys_os_write."
            ),
            mock_llm_server_url=mock_llm_server_url,
        )

        terminal = _poll_until_session_idle(http_client, session_id, timeout=120)
        assert terminal["status"] == "idle", (
            f"Agent turn failed with status {terminal['status']!r}. "
            "The workspace-file-writer agent did not complete successfully."
        )

        # Poll the changes endpoint until the written file appears.
        # The watchdog observer may have a brief delay before delivering the event.
        deadline = time.monotonic() + 15
        found_entry: dict | None = None
        found_names: list[str] = []
        while time.monotonic() < deadline:
            changes_resp = http_client.get(_fs_changes_url(session_id))
            changes_resp.raise_for_status()
            entries = changes_resp.json()["data"]
            found_names = [e["name"] for e in entries]
            for entry in entries:
                if entry["name"] == filename:
                    found_entry = entry
                    break
            if found_entry is not None:
                break
            time.sleep(POLL_INTERVAL_S)

        assert found_entry is not None, (
            f"'{filename}' did not appear in the changes listing within 15s. "
            f"Files found: {found_names}. "
            "Likely cause: watchdog observer not started, or the session start time "
            "boundary excluded the write event."
        )
        # Status must be one of the full-word values from the F1 migration.
        assert found_entry["status"] == "created", (
            f"Expected status 'created', got {found_entry['status']!r}"
        )

        # Verify the file content is readable via the file endpoint.
        content_resp = http_client.get(_fs_file_url(session_id, filename))
        content_resp.raise_for_status()
        content_body = content_resp.json()

        assert content_body.get("object") == "session.environment.filesystem.file_content", (
            f"Wrong object type in file content response: {content_body.get('object')!r}"
        )
        # content_type must be present.
        assert content_body.get("content_type") is not None, (
            "content_type must be present in file content response (migration work item #5)"
        )
        # The content must match what the agent was asked to write.
        assert file_content in content_body.get("content", ""), (
            f"File content mismatch. Expected {file_content!r} in content, "
            f"got: {content_body.get('content', '')[:200]!r}. "
            "Either sys_os_write wrote different content or the file endpoint "
            "is not reading from the correct path."
        )
    finally:
        # Clean up the test file so it doesn't pollute the repo working tree.
        if test_file.exists():
            test_file.unlink()


def _fs_diff_url(session_id: str, path: str) -> str:
    """Build the diff URL for a session filesystem file.

    :param session_id: Session/conversation identifier.
    :param path: Path relative to the environment root.
    :returns: URL string for ``GET .../diff/{path}``.
    """
    return f"/v1/sessions/{session_id}/resources/environments/{_DEFAULT_ENV}/diff/{path}"


def test_diff_endpoint_shows_git_diff_for_modified_file(
    http_client: httpx.Client,
    live_runner_id: str,
    databricks_workspace_host: str | None,
    mock_llm_server_url: str,
) -> None:
    """The diff endpoint returns the git HEAD content as ``before`` and the modified
    content as ``after`` for a file that exists in the git repo.

    Verifies the full round-trip from agent write -> diff endpoint -> git baseline:

    1. Configure the mock LLM to return a ``sys_os_write`` tool call that
       overwrites an existing tracked file.
    2. Poll the changes endpoint until the file appears as ``"modified"``.
    3. Call ``GET .../diff/{path}`` and assert:
       - ``before`` equals the committed content from ``git show HEAD:<path>``.
       - ``after`` equals the modified content the agent wrote.

    Failure modes this catches:
    - ``get_baseline`` not wired into the diff endpoint -> ``before`` is ``None``
      when it should have content.
    - ``git show HEAD:<path>`` path construction wrong -> ``before`` is ``None``.
    - ``after`` read path broken -> ``after`` is ``None`` or wrong content.

    :param http_client: HTTP client pointed at the live server.
    :param live_runner_id: Runner id registered by the live server.
    :param databricks_workspace_host: Workspace host URL when the
        test suite routes LLM calls through Databricks model serving.
    :param mock_llm_server_url: Mock LLM server URL.
    """
    import subprocess

    # Use a file that is already tracked in git so ``git show HEAD`` has content.
    # tests/resources/test.md is a stable, small committed file.
    # We restore it in the finally block via ``git checkout``.
    target_rel = "tests/resources/test.md"
    target_abs = _REPO_ROOT / target_rel

    # Capture the committed content so we can assert against it and restore it.
    git_head_content = subprocess.check_output(
        ["git", "show", f"HEAD:{target_rel}"],
        cwd=str(_REPO_ROOT),
    ).decode("utf-8", errors="replace")

    modified_content = f"modified by e2e diff test {uuid.uuid4().hex[:8]}"

    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_write_diff",
                        "name": "sys_os_write",
                        "arguments": json.dumps({"path": target_rel, "content": modified_content}),
                    },
                ],
            },
            {"text": "File overwritten successfully."},
        ],
        key="default",
    )

    try:
        session_id = _create_bound_session(
            http_client,
            live_runner_id=live_runner_id,
            databricks_workspace_host=databricks_workspace_host,
            initial_text=(
                f"Overwrite the file '{target_rel}' with exactly this content "
                f"(no trailing newline): '{modified_content}'. Use sys_os_write."
            ),
            mock_llm_server_url=mock_llm_server_url,
        )

        terminal = _poll_until_session_idle(http_client, session_id, timeout=120)
        assert terminal["status"] == "idle", (
            f"Agent turn failed with status {terminal['status']!r}. "
            "The workspace-file-writer agent did not complete successfully."
        )

        # Poll the changes endpoint until the target file appears as modified.
        deadline = time.monotonic() + 15
        found_entry: dict | None = None
        while time.monotonic() < deadline:
            changes_resp = http_client.get(_fs_changes_url(session_id))
            changes_resp.raise_for_status()
            for entry in changes_resp.json()["data"]:
                if entry["path"] == target_rel or entry["name"] == target_abs.name:
                    found_entry = entry
                    break
            if found_entry is not None:
                break
            time.sleep(POLL_INTERVAL_S)

        assert found_entry is not None, (
            f"'{target_rel}' did not appear in the changes listing within 15s. "
            "The watchdog observer may not have recorded the write event."
        )
        assert found_entry["status"] == "modified", (
            f"Expected status 'modified' for an overwritten tracked file, "
            f"got {found_entry['status']!r}."
        )

        # Call the diff endpoint.
        diff_resp = http_client.get(_fs_diff_url(session_id, target_rel))
        assert diff_resp.status_code == 200, (
            f"Expected 200 from diff endpoint, got {diff_resp.status_code}. Body: {diff_resp.text}"
        )
        diff_body = diff_resp.json()

        assert diff_body["object"] == "session.environment.filesystem.file_diff", (
            f"Wrong object type: {diff_body.get('object')!r}"
        )
        # ``before`` must equal the content at git HEAD — proves get_baseline
        # is calling ``git show HEAD:<path>`` and returning the correct bytes.
        assert diff_body["before"] == git_head_content, (
            f"before content does not match git HEAD.\n"
            f"  expected: {git_head_content!r}\n"
            f"  got:      {diff_body['before']!r}\n"
            "get_baseline is either not calling git show or returning wrong content."
        )
        # ``after`` must contain the modified content — proves CallerProcessFilesystem
        # is reading the current on-disk state, not the snapshot.
        assert modified_content in (diff_body["after"] or ""), (
            f"after content does not contain modified text.\n"
            f"  expected substring: {modified_content!r}\n"
            f"  got: {diff_body['after']!r}\n"
            "The diff endpoint is not reading the current file content from disk."
        )
    finally:
        # Restore the tracked file to its committed state so the repo stays clean.
        subprocess.run(
            ["git", "checkout", "--", target_rel],
            cwd=str(_REPO_ROOT),
            check=False,
        )
