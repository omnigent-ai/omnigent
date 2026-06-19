"""E2E tests for changed-files tracking in a non-git workspace.

Verifies that :class:`AgentEditFilesystemRegistry` (the non-git path)
correctly records file operations performed by the agent via tool calls
and surfaces them through the ``GET .../changes`` endpoint.

The server under test is started with its CWD set to a temporary
directory that is **not** inside any git repo.  ``server()`` passes
``Path.cwd()`` to the runner subprocess as its workspace, which causes
:func:`create_filesystem_registry` to return an
:class:`AgentEditFilesystemRegistry` instead of the
:class:`GitFilesystemRegistry`.

OS-env tool calls dispatched through ``proxy_stream`` use
``runner_workspace`` (the shared root) as the agent CWD.  The edit test
therefore pre-creates the target file directly inside
``non_git_workspace`` (the root) **after** learning the session id but
**before** sending the first agent message.

Two scenarios are tested:

- ``test_non_git_create_file`` — agent creates a new file; the changes
  endpoint must show it with status ``"created"``.
- ``test_non_git_edit_file`` — agent overwrites a pre-existing file that
  was seeded into the workspace root; the changes endpoint must show
  it with status ``"modified"``.

Note: ``sys_os_shell`` side-effects (e.g. ``rm``) are intentionally not
tracked — shell commands cannot be reliably attributed to a single session
in a shared workspace, so delete-via-shell has no E2E coverage here.

Usage::

    pytest tests/e2e/test_non_git_changed_files_e2e.py \\
        --llm-api-key $LLM_API_KEY -v
"""

from __future__ import annotations

import io
import json
import os
import secrets
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests.e2e.conftest import (
    configure_mock_llm,
    find_free_port,
    poll_session_until_terminal,
    reset_mock_llm,
    send_user_message_to_session,
)
from tests.e2e.helpers import HEALTH_TIMEOUT_S, POLL_INTERVAL_S

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKSPACE_WRITER_DIR = _REPO_ROOT / "tests" / "resources" / "agents" / "workspace-file-writer"

# The default environment ID used by all runner resource endpoints.
_DEFAULT_ENV = "default"

# Maximum seconds to poll the changes endpoint for a file to appear.
_CHANGES_TIMEOUT_S: float = 30.0


# ── URL builders ──────────────────────────────────────────────────────────────


def _changes_url(session_id: str) -> str:
    """Build the changes listing URL for *session_id*.

    :param session_id: Session/conversation identifier.
    :returns: URL string for ``GET .../changes``.
    """
    return f"/v1/sessions/{session_id}/resources/environments/{_DEFAULT_ENV}/changes"


# ── Module-scoped fixtures ────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def non_git_workspace() -> Iterator[Path]:
    """A temporary directory guaranteed to be outside any git repository.

    Created under the OS temp root so it is never inside the repo
    checkout.  The ``omnigent server`` subprocess is started with this
    directory as its CWD so the runner adopts it as its workspace root
    via ``Path.cwd()`` — no env-var override of the server is needed.

    :returns: Path to the empty temp workspace.
    """
    tmp = Path(tempfile.mkdtemp(prefix="omnigent_e2e_ng_"))
    yield tmp
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


_non_git_runner_state: dict[str, str] = {}


@pytest.fixture(scope="module")
def non_git_runner_id() -> str:
    """A stable runner id for the module-scoped non-git server.

    The runner id is derived from a binding token shared with the
    server so the server's tunnel allowlist accepts exactly this
    runner's WebSocket upgrade.

    :returns: Runner id string, e.g. ``"runner_token_abc123..."``.
    """
    from omnigent.runner.identity import token_bound_runner_id

    if "runner_id" not in _non_git_runner_state:
        token = secrets.token_urlsafe(32)
        _non_git_runner_state["binding_token"] = token
        _non_git_runner_state["runner_id"] = token_bound_runner_id(token)
    return _non_git_runner_state["runner_id"]


@pytest.fixture(scope="module")
def non_git_server(
    llm_api_key: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    non_git_workspace: Path,
    non_git_runner_id: str,
) -> Iterator[str]:
    """Spawn a real ``omnigent server`` whose CWD is a non-git workspace.

    The server is started with ``cwd=non_git_workspace``.  Inside
    ``server()``, ``Path.cwd()`` resolves to ``non_git_workspace`` and
    is passed as ``workspace_cwd`` to the runner subprocess.  The
    runner's :func:`create_filesystem_registry` then sees a non-git
    directory and returns an :class:`AgentEditFilesystemRegistry`.

    :param llm_api_key: The ``--llm-api-key`` option value.
    :param mock_llm_server_url: Mock LLM server URL.
    :param tmp_path_factory: pytest temp path factory.
    :param non_git_workspace: Non-git workspace directory (used as CWD).
    :param non_git_runner_id: Runner id to register.
    :returns: Server base URL, e.g. ``"http://localhost:18600"``.
    """
    port = find_free_port()
    db_path = tmp_path_factory.mktemp("e2e_ng") / "e2e.db"
    artifact_dir = tmp_path_factory.mktemp("e2e_ng_artifacts")
    server_log = tmp_path_factory.mktemp("e2e_ng_logs") / "server.log"

    binding_token = _non_git_runner_state["binding_token"]
    env: dict[str, str] = {
        **os.environ,
        "OPENAI_API_KEY": llm_api_key,
        "PYTHONPATH": (f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"),
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
    }
    if mock_llm_server_url is not None:
        env["OPENAI_BASE_URL"] = f"{mock_llm_server_url}/v1"

    log_handle = open(server_log, "w")  # noqa: SIM115
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent.cli",
            "server",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{db_path}",
            "--artifact-location",
            str(artifact_dir),
        ],
        env={**env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token},
        # CWD = non_git_workspace so that Path.cwd() inside server()
        # resolves to the non-git temp dir, causing the runner to use
        # AgentEditFilesystemRegistry instead of GitFilesystemRegistry.
        cwd=str(non_git_workspace),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://localhost:{port}"

    # Spawn runner as sibling subprocess.
    runner_log = tmp_path_factory.mktemp("e2e_ng_runner_logs") / "runner.log"
    runner_log_handle = open(runner_log, "w")  # noqa: SIM115
    runner_proc = subprocess.Popen(
        [sys.executable, "-m", "omnigent.runner._entry"],
        env={
            **env,
            "OMNIGENT_RUNNER_ID": non_git_runner_id,
            "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
            "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
            "RUNNER_SERVER_URL": base_url,
            # Without a workspace the runner builds no filesystem
            # registry (app.py), so record_change is a no-op and writes
            # never surface in GET .../changes. The real CLI always sets
            # this via _start_cli_runner_process.
            "OMNIGENT_RUNNER_WORKSPACE": str(non_git_workspace),
        },
        cwd=str(non_git_workspace),
        stdout=runner_log_handle,
        stderr=subprocess.STDOUT,
    )

    health_iters = int(HEALTH_TIMEOUT_S / POLL_INTERVAL_S)
    for _ in range(health_iters):
        try:
            health_resp = httpx.get(f"{base_url}/health", timeout=2)
            runner_resp = httpx.get(
                f"{base_url}/v1/runners/{non_git_runner_id}/status",
                timeout=2,
            )
            if (
                health_resp.status_code == 200
                and runner_resp.status_code == 200
                and runner_resp.json().get("online") is True
            ):
                break
        except httpx.ConnectError:
            pass
        time.sleep(POLL_INTERVAL_S)
    else:
        if runner_proc.poll() is None:
            runner_proc.kill()
            runner_proc.wait(timeout=5)
        runner_log_handle.close()
        proc.kill()
        log_handle.close()
        log_contents = server_log.read_text() if server_log.exists() else ""
        runner_log_contents = runner_log.read_text() if runner_log.exists() else ""
        raise RuntimeError(
            f"Non-git server did not start within {HEALTH_TIMEOUT_S}s.\n"
            f"Server log: {log_contents[-3000:]}\n"
            f"Runner log: {runner_log_contents[-3000:]}"
        )

    try:
        yield base_url
    finally:
        if runner_proc.poll() is None:
            runner_proc.send_signal(signal.SIGTERM)
            try:
                runner_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                runner_proc.kill()
                runner_proc.wait(timeout=5)
        runner_log_handle.close()
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log_handle.close()


@pytest.fixture(scope="module")
def non_git_client(non_git_server: str) -> Iterator[httpx.Client]:
    """An HTTP client pointed at *non_git_server*.

    :param non_git_server: Base URL from the :func:`non_git_server` fixture.
    :returns: Configured ``httpx.Client``.
    """
    with httpx.Client(base_url=non_git_server, timeout=60.0) as client:
        yield client


# ── Shared helpers ────────────────────────────────────────────────────────────


def _build_mock_workspace_writer_bundle(mock_llm_base_url: str) -> bytes:
    """Read the on-disk workspace-file-writer YAML, inject mock auth, tarball."""
    yaml_path = _WORKSPACE_WRITER_DIR / "workspace-file-writer.yaml"
    spec = yaml.safe_load(yaml_path.read_text())
    spec.setdefault("executor", {})["auth"] = {
        "type": "api_key",
        "api_key": "mock-key",
        "base_url": f"{mock_llm_base_url}/v1",
    }
    patched = yaml.dump(spec, sort_keys=False).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="./workspace-file-writer.yaml")
        info.size = len(patched)
        tar.addfile(info, io.BytesIO(patched))
    return buf.getvalue()


def _create_session(
    client: httpx.Client,
    *,
    runner_id: str,
    mock_llm_server_url: str,
) -> str:
    """Upload the workspace-writer agent and create a bound session.

    Returns the session id without sending any message yet, so the
    caller can pre-populate files in the session workspace before the
    first agent turn starts.

    :param client: HTTP client pointed at the non-git server.
    :param runner_id: Runner id to bind the session to.
    :param mock_llm_server_url: Mock LLM server URL used to inject
        mock auth into the agent bundle.
    :returns: The new session id, e.g. ``"conv_abc123"``.
    """
    bundle = _build_mock_workspace_writer_bundle(mock_llm_server_url)
    create_resp = client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )
    create_resp.raise_for_status()
    session_id: str = create_resp.json()["session_id"]

    bind_resp = client.patch(
        f"/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
    )
    bind_resp.raise_for_status()
    return session_id


def _poll_changes_for_file(
    client: httpx.Client,
    session_id: str,
    filename: str,
    *,
    timeout: float = _CHANGES_TIMEOUT_S,
) -> dict[str, Any] | None:
    """Poll the changes endpoint until *filename* appears or timeout expires.

    :param client: HTTP client pointed at the live server.
    :param session_id: Session to query.
    :param filename: File base name to look for in the ``name`` field,
        e.g. ``"hello.txt"``.
    :param timeout: Maximum seconds to poll.
    :returns: The matching change record dict, or ``None`` if not found
        within *timeout*.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(_changes_url(session_id))
        resp.raise_for_status()
        for entry in resp.json().get("data", []):
            if entry.get("name") == filename or entry.get("path", "").endswith(filename):
                return entry
        # time.sleep is intentional here: this is a synchronous HTTP polling
        # loop against a real out-of-process server.  asyncio event-driven
        # alternatives are not available in synchronous e2e test helpers.
        # Consistent with the startup polling in non_git_server and the
        # poll_session_until_terminal helper in conftest.py.
        time.sleep(POLL_INTERVAL_S)
    return None


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_non_git_create_file(
    non_git_client: httpx.Client,
    non_git_runner_id: str,
    non_git_workspace: Path,
    mock_llm_server_url: str,
) -> None:
    """Agent-created file appears in the changes listing with status ``"created"``.

    Verifies the full round-trip for the create path in a non-git workspace:
    1. Ask the agent to write a uniquely-named file via ``sys_os_write``.
    2. Wait for the session to reach ``idle``.
    3. Poll ``GET .../changes`` until the file appears.
    4. Assert status is ``"created"``.

    Failure modes this catches:
    - ``record_change`` not called after ``sys_os_write`` in
      ``_execute_os_env_tool`` → file never appears in the changes listing.
    - ``AgentEditFilesystemRegistry`` not selected (git registry used
      instead of the non-git one because the server CWD is a git dir)
      → changes only appear via ``git status``, not ``record_change``;
      the ``AgentEditFilesystemRegistry`` path is untested.

    :param non_git_client: HTTP client pointed at the non-git server.
    :param non_git_runner_id: Runner id registered by the fixture.
    :param non_git_workspace: Non-git temp workspace directory (= server CWD).
    :param mock_llm_server_url: Mock LLM server URL.
    """
    filename = f"create_{uuid.uuid4().hex[:8]}.txt"
    content = "created by e2e test"

    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_write_1",
                        "name": "sys_os_write",
                        "arguments": json.dumps({"path": filename, "content": content}),
                    },
                ],
            },
            {"text": "File created successfully."},
        ],
        key="default",
    )

    session_id = _create_session(
        non_git_client,
        runner_id=non_git_runner_id,
        mock_llm_server_url=mock_llm_server_url,
    )
    response_id = send_user_message_to_session(
        non_git_client,
        session_id=session_id,
        content=(
            f"Write a file named '{filename}' containing exactly: "
            f"'{content}'. Use sys_os_write. Confirm with one sentence."
        ),
    )

    result = poll_session_until_terminal(
        non_git_client, session_id=session_id, response_id=response_id, timeout=120
    )
    assert result["status"] == "completed", (
        f"Agent turn failed with status {result['status']!r}. "
        f"Error: {result.get('error')}. "
        "The workspace-file-writer agent did not complete the create successfully."
    )

    entry = _poll_changes_for_file(non_git_client, session_id, filename)
    assert entry is not None, (
        f"'{filename}' did not appear in the changes listing within "
        f"{_CHANGES_TIMEOUT_S}s. "
        "Likely cause: record_change() was not called after sys_os_write in "
        "_execute_os_env_tool, or AgentEditFilesystemRegistry is not being "
        "used (check that the server CWD is a non-git directory)."
    )
    # Status must be "created" — the file did not exist before this session.
    assert entry["status"] == "created", (
        f"Expected status 'created' for a newly written file, "
        f"got {entry['status']!r}. "
        "The net-operation logic may have incorrectly classified the write."
    )

    # The changes response must flag that tracking is limited for this non-git
    # workspace so the UI can explain *why* the list is partial (edits made
    # outside record_change — native-CLI/shell/external — are invisible here)
    # instead of letting it read as a definitive "no changes" (issue #725).
    changes_body = non_git_client.get(_changes_url(session_id)).json()
    assert changes_body["tracking"] == {
        "complete": False,
        "reason": "non_git_workspace",
    }, (
        f"Expected non-git limited-tracking flag, got {changes_body.get('tracking')!r}. "
        "A non-git workspace must report incomplete tracking so the panel can "
        "surface the limitation."
    )

    # Verify the file was actually written to the workspace root.
    # OS-env tools dispatched through proxy_stream use runner_workspace
    # (the shared root) as the agent CWD.
    written = non_git_workspace / filename
    assert written.exists(), (
        f"File '{filename}' not found at expected path {written}. "
        "The agent may have written to the wrong directory."
    )


def test_non_git_edit_file(
    non_git_client: httpx.Client,
    non_git_runner_id: str,
    non_git_workspace: Path,
    mock_llm_server_url: str,
) -> None:
    """Agent overwrite of a pre-existing file appears with status ``"modified"``.

    Creates the session first (to learn the session id), seeds the target
    file into the session workspace directory so the agent's
    ``sys_os_write`` is an overwrite rather than a first creation, then
    sends the agent message.  The changes endpoint must report the file as
    ``"modified"``.

    Failure modes this catches:
    - ``_write_impl`` returning ``{"created": False}`` not being detected
      → recorded as ``"created"`` instead of ``"modified"``.
    - ``record_change`` not called at all → file never appears.

    :param non_git_client: HTTP client pointed at the non-git server.
    :param non_git_runner_id: Runner id registered by the fixture.
    :param non_git_workspace: Non-git temp workspace directory.
    :param mock_llm_server_url: Mock LLM server URL.
    """
    filename = f"edit_{uuid.uuid4().hex[:8]}.txt"
    original_content = "original content written before session"
    updated_content = "overwritten by e2e edit test"

    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_write_2",
                        "name": "sys_os_write",
                        "arguments": json.dumps({"path": filename, "content": updated_content}),
                    },
                ],
            },
            {"text": "File overwritten successfully."},
        ],
        key="default",
    )

    # Create the session to learn its id before seeding the file.
    session_id = _create_session(
        non_git_client,
        runner_id=non_git_runner_id,
        mock_llm_server_url=mock_llm_server_url,
    )

    # Seed the file into the workspace root so the agent's write is an
    # overwrite.  OS-env tools dispatched through proxy_stream use
    # runner_workspace (the shared root) as the agent CWD.
    (non_git_workspace / filename).write_text(original_content)

    response_id = send_user_message_to_session(
        non_git_client,
        session_id=session_id,
        content=(
            f"Overwrite the file '{filename}' with exactly: "
            f"'{updated_content}'. Use sys_os_write. Confirm with one sentence."
        ),
    )

    result = poll_session_until_terminal(
        non_git_client, session_id=session_id, response_id=response_id, timeout=120
    )
    assert result["status"] == "completed", (
        f"Agent turn failed with status {result['status']!r}. "
        f"Error: {result.get('error')}. "
        "The workspace-file-writer agent did not complete the edit successfully."
    )

    entry = _poll_changes_for_file(non_git_client, session_id, filename)
    assert entry is not None, (
        f"'{filename}' did not appear in the changes listing within "
        f"{_CHANGES_TIMEOUT_S}s. "
        "record_change() may not have been called after sys_os_write."
    )
    # Status must be "modified" — the file pre-existed in the session workspace.
    assert entry["status"] == "modified", (
        f"Expected status 'modified' for an overwritten pre-existing file, "
        f"got {entry['status']!r}. "
        "If 'created': the was_created flag from _write_impl was not checked "
        "correctly in _execute_os_env_tool."
    )
