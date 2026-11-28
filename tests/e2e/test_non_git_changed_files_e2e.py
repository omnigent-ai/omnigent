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

import json
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests._helpers.server_runner import ServerRunner, server_runner
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e.conftest import (
    configure_mock_llm,
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


@pytest.fixture(scope="module")
def non_git_stack(
    llm_api_key: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    non_git_workspace: Path,
) -> Iterator[ServerRunner]:
    """Run both processes in the workspace observed by the filesystem registry."""
    env = {
        "OPENAI_API_KEY": llm_api_key,
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
    }
    with server_runner(
        tmp_path_factory.mktemp("non_git_stack"),
        workspace=non_git_workspace,
        server_cwd=non_git_workspace,
        server_env=env,
        health_timeout=HEALTH_TIMEOUT_S,
        poll_interval=POLL_INTERVAL_S,
        wait_ready=False,
    ) as stack:
        stack.start_runner(cwd=non_git_workspace, env=env)
        yield stack


@pytest.fixture(scope="module")
def non_git_runner_id(non_git_stack: ServerRunner) -> str:
    return non_git_stack.runner_id


@pytest.fixture(scope="module")
def non_git_server(non_git_stack: ServerRunner) -> str:
    return non_git_stack.base_url


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
    return bundle_files({"./workspace-file-writer.yaml": patched})


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
    create_resp = post_session_bundle(client.post, "/v1/sessions", bundle)
    create_resp.raise_for_status()
    session_id: str = create_resp.json()["session_id"]

    bind_session_runner(client.patch, "", session_id, runner_id)
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
