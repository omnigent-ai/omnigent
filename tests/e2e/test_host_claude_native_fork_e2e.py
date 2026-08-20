"""End-to-end regression tests for claude-native fork-resume history.

Covers the two flows verified by hand when implementing the
clone-transcript fork path (see designs/FORK_SESSION_UX.md): a forked
claude-native session, resumed on the same host, must carry the
source's Claude history into the clone's own session so the agent can
recall it. Both resume shapes are exercised:

1. **Same working directory** — the clone resumes in the source's exact
   workspace. The cloned transcript must be written *before* launch so
   the forwarder's ``start_at_end`` seeks past the copied prefix
   (otherwise the whole transcript double-renders into the clone).
2. **New git worktree** — the clone resumes in a freshly created
   worktree off the source repo. Claude's ``--resume`` is cwd-scoped, so
   the cloned transcript must land in the *clone's* project dir (not the
   source's) or the resume silently finds nothing → "terminal resource
   not found".

The regression these guard against: forking used to ask Claude Code to
branch the source via ``--fork-session``, which double-rendered the
same-dir case and could not resume in a new worktree. The current path
clones the source transcript into the clone's own project dir under a
uuid we assign (``_clone_claude_transcript``) and launches plain
``--resume <our_uuid>``. If that regresses, the clone launches *fresh*
(no history) and the recall assertion below fails.

Why this is opt-in (same rationale as ``test_host_claude_native_e2e``):
claude-native needs a real *interactive* Claude login anchored to the
real ``$HOME`` — it cannot be relocated into CI. Set
``OMNIGENT_E2E_CLAUDE_NATIVE=1`` (with ``claude`` installed + logged
in) to run::

    OMNIGENT_E2E_CLAUDE_NATIVE=1 \\
    .venv/bin/python -m pytest tests/e2e/test_host_claude_native_fork_e2e.py \\
        --llm-api-key "mock-key" \\
        -v
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import (
    _CLAUDE_CODER_DIR,
    configure_mock_llm,
    create_runner_bound_session,
    reset_mock_llm,
    send_user_message_to_session,
    upload_agent,
)
from tests.e2e.helpers import POLL_INTERVAL_S
from tests.e2e.test_host_claude_native_e2e import (
    _claude_native_agent_id,
    _online_host_id,
    _poll_for_assistant_marker,
    _spawn_host_daemon,
    _workspace_trusted_in_claude_config,
)

# Opt-in only — see module docstring and test_host_claude_native_e2e for
# why binary presence alone is not a sufficient gate.
pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_CLAUDE_NATIVE") != "1" or shutil.which("claude") is None,
    reason=(
        "claude-native e2e needs an interactive Claude login; set "
        "OMNIGENT_E2E_CLAUDE_NATIVE=1 (and have `claude` installed + logged in) to run"
    ),
)


@contextmanager
def _workspaces_trusted_in_claude_config(workspaces: list[Path]) -> Iterator[None]:
    """
    Mark several workspaces trusted in ``~/.claude.json`` at once.

    The fork-resume tests touch two directories (the source workspace and
    the clone's resume directory — the same dir, or a new worktree), and
    BOTH must be pre-trusted or Claude shows its folder-trust dialog
    instead of the input box (which blocks injection and confounds the
    readiness gate). This nests :func:`_workspace_trusted_in_claude_config`
    so each path's original bytes are restored on exit.

    :param workspaces: Absolute workspace paths to trust, e.g.
        ``[Path("/tmp/.../src"), Path("/tmp/.../src-worktrees/fork")]``.
    :returns: Iterator yielding once every path is trusted.
    """
    # Each path under its own restore-on-exit context so the developer's
    # ~/.claude.json is left byte-identical afterwards.
    with ExitStack() as stack:
        for workspace in workspaces:
            stack.enter_context(_workspace_trusted_in_claude_config(workspace))
        yield


def _init_git_repo(repo: Path) -> None:
    """
    Initialise *repo* as a git repo with one commit.

    The worktree fork-resume path runs ``git worktree add`` off the
    source repo, which requires at least one commit (an unborn HEAD has
    nothing to branch from). Configures a local identity so the commit
    succeeds without the developer's global git config.

    :param repo: Directory to initialise, e.g.
        ``Path("/tmp/.../srcrepo")``.
    :returns: None.
    """
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "README.md").write_text("fork-resume e2e fixture\n")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    def _git(*args: str) -> None:
        """Run a git command in *repo*, raising on failure."""
        subprocess.run(["git", *args], cwd=repo, check=True, env=env)

    _git("init", "-q")
    _git("config", "user.email", "e2e@example.com")
    _git("config", "user.name", "E2E")
    _git("add", "-A")
    _git("commit", "-qm", "init")


def _create_native_session(
    client: httpx.Client, *, agent_id: str, host_id: str, workspace: Path
) -> str:
    """
    Create a claude-native session bound to *host_id* + *workspace*.

    :param client: HTTP client pointed at the test server.
    :param agent_id: The ``claude-native-ui`` agent id.
    :param host_id: The online host's id.
    :param workspace: Absolute workspace the session starts in.
    :returns: The created session/conversation id.
    """
    resp = client.post(
        "/v1/sessions",
        json={"agent_id": agent_id, "host_id": host_id, "workspace": str(workspace)},
        timeout=60.0,
    )
    resp.raise_for_status()
    return str(resp.json()["id"])


def _send_user_message(client: httpx.Client, *, session_id: str, text: str) -> None:
    """
    Inject a user message into a claude-native session.

    :param client: HTTP client pointed at the test server.
    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :param text: The message body to type into Claude's terminal.
    :returns: None.
    """
    resp = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        },
        timeout=30.0,
    )
    resp.raise_for_status()


def _wait_for_external_session_id(client: httpx.Client, *, session_id: str, timeout: float) -> str:
    """
    Poll a session until its ``external_session_id`` is captured.

    A fork only carries history when the SOURCE has a Claude session id
    recorded — the fork stamps ``omnigent.fork.source_external_session_id``
    from it, which the runner reads to find the source transcript.
    Capture happens after Claude's first turn (the hook records the
    transcript path). Forking before then would silently launch the
    clone fresh, so the test must wait.

    :param client: HTTP client pointed at the test server.
    :param session_id: Source session/conversation id.
    :param timeout: Max seconds to wait for capture.
    :returns: The captured Claude session id.
    :raises AssertionError: If not captured within *timeout*.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{session_id}", timeout=30.0)
        if resp.status_code == 200:
            external = resp.json().get("external_session_id")
            if external:
                return str(external)
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"Source session {session_id} never captured an external_session_id within "
        f"{timeout}s — the fork would have no source transcript to clone."
    )


def _fork_session(
    client: httpx.Client, *, source_id: str, title: str, agent_id: str | None = None
) -> str:
    """
    Fork a session and return the new clone's id.

    :param client: HTTP client pointed at the test server.
    :param source_id: Session/conversation id to fork.
    :param title: Title for the new clone.
    :param agent_id: Optional built-in agent to switch the fork to (e.g.
        fork a claude-sdk session into claude-native). ``None`` keeps the
        source's agent.
    :returns: The clone's session/conversation id.
    """
    body: dict[str, str] = {"title": title}
    if agent_id is not None:
        body["agent_id"] = agent_id
    resp = client.post(
        f"/v1/sessions/{source_id}/fork",
        json=body,
        timeout=60.0,
    )
    resp.raise_for_status()
    return str(resp.json()["id"])


def _builtin_agent_id(client: httpx.Client, name: str) -> str:
    """
    Return the id of a built-in agent by name from ``GET /v1/agents``.

    :param client: HTTP client pointed at the test server.
    :param name: Built-in agent name, e.g. ``"sdk-chat-builtin"``.
    :returns: The agent id.
    :raises AssertionError: If no built-in with that name is registered.
    """
    resp = client.get("/v1/agents")
    resp.raise_for_status()
    for agent in resp.json()["data"]:
        if agent["name"] == name:
            return str(agent["id"])
    raise AssertionError(f"built-in agent {name!r} not registered on the server")


def _launch_runner(
    client: httpx.Client,
    *,
    host_id: str,
    session_id: str,
    workspace: Path,
    git: dict[str, str] | None = None,
) -> dict[str, object]:
    """
    Bind a clone to a host by launching a runner (the resume step).

    Mirrors the directory-picker's ``POST /v1/hosts/{id}/runners`` call.
    When *git* is set the host creates a worktree off *workspace* and the
    runner starts there instead.

    :param client: HTTP client pointed at the test server.
    :param host_id: The online host's id.
    :param session_id: The clone's session id to bind.
    :param workspace: Source repo / working directory on the host.
    :param git: Optional ``{"branch_name": ...}`` worktree block. ``None``
        binds *workspace* directly (same-dir resume).
    :returns: The launch response JSON.
    """
    body: dict[str, object] = {"session_id": session_id, "workspace": str(workspace)}
    if git is not None:
        body["git"] = git
    resp = client.post(f"/v1/hosts/{host_id}/runners", json=body, timeout=120.0)
    resp.raise_for_status()
    return dict(resp.json())


@contextmanager
def _host_daemon(tmp_path: Path, live_server: str) -> Iterator[None]:
    """
    Spawn an ``omnigent connect`` daemon for the test's duration.

    :param tmp_path: Per-test temp dir for the daemon log.
    :param live_server: Test server URL the daemon registers with.
    :returns: Iterator yielding once the daemon is spawned; SIGTERM'd on
        exit.
    """
    daemon = _spawn_host_daemon(tmp_path=tmp_path, live_server=live_server)
    try:
        yield
    finally:
        daemon.send_signal(signal.SIGTERM)
        try:
            daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait()


def _run_fork_resume_history_check(
    *,
    http_client: httpx.Client,
    host_id: str,
    agent_id: str,
    source_workspace: Path,
    resume_workspace: Path,
    git: dict[str, str] | None,
) -> None:
    """
    Drive one fork-resume flow and assert history carried into the clone.

    Plants a code word in the SOURCE session, waits for its Claude session
    id to be captured, forks it, binds the clone (same dir or a new
    worktree), then asks the clone to recall the code word. The recall
    only succeeds if the source transcript was cloned into the clone's
    own project dir and resumed — a fresh launch (the regression) has no
    history and never echoes the marker.

    :param http_client: HTTP client pointed at the test server.
    :param host_id: The online host's id.
    :param agent_id: The ``claude-native-ui`` agent id.
    :param source_workspace: Workspace the source session starts in.
    :param resume_workspace: Workspace passed to the runner launch — the
        same dir as *source_workspace*, or the repo root for the worktree
        case (the host derives the worktree path from it).
    :param git: Optional worktree block for the resume launch; ``None``
        for same-dir resume.
    :returns: None.
    """
    marker = f"FORKWORD_{uuid.uuid4().hex[:6].upper()}"

    source_id = _create_native_session(
        http_client, agent_id=agent_id, host_id=host_id, workspace=source_workspace
    )
    # Plant the code word and force a deterministic ack so we know the
    # source turn (carrying the marker) is committed to the transcript.
    _send_user_message(
        http_client,
        session_id=source_id,
        text=(f"Remember this code word for later: {marker}. Reply with exactly one word: ACK"),
    )
    _poll_for_assistant_marker(http_client, session_id=source_id, marker="ACK", timeout=180.0)
    # The clone reads the source's Claude session id; wait for capture.
    _wait_for_external_session_id(http_client, session_id=source_id, timeout=60.0)

    fork_id = _fork_session(http_client, source_id=source_id, title=f"clone of {source_id}")
    _launch_runner(
        http_client,
        host_id=host_id,
        session_id=fork_id,
        workspace=resume_workspace,
        git=git,
    )

    # Ask the clone to recall the planted word. It can only answer from
    # the cloned source transcript — a fresh (history-less) launch can't.
    _send_user_message(
        http_client,
        session_id=fork_id,
        text=(
            "Earlier in this conversation I gave you a code word to remember. "
            "Reply with exactly that code word and nothing else."
        ),
    )
    text = _poll_for_assistant_marker(
        http_client, session_id=fork_id, marker=marker, timeout=180.0
    )
    # The marker only surfaces if the source transcript was cloned into
    # the clone's project dir and resumed — proving history transfer.
    assert marker in text, (
        f"clone did not recall {marker!r} (got {text!r}) — the source transcript "
        "was not cloned/resumed, so the clone launched fresh without history"
    )


def test_fork_resume_same_dir_carries_history(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """
    A clone resumed in the source's SAME directory recalls source history.

    Guards the same-dir clone-transcript path: the source transcript is
    cloned into the same project dir under the clone's own uuid and
    pre-written before launch, so the forwarder skips the copied prefix.
    A regression (fresh launch, or double-render) breaks recall.
    """
    workspace = tmp_path / "fork_same_dir_ws"
    workspace.mkdir()

    with _workspaces_trusted_in_claude_config([workspace]):
        with _host_daemon(tmp_path, live_server):
            host_id = _online_host_id(http_client, timeout=30.0)
            agent_id = _claude_native_agent_id(http_client)
            _run_fork_resume_history_check(
                http_client=http_client,
                host_id=host_id,
                agent_id=agent_id,
                source_workspace=workspace,
                resume_workspace=workspace,
                git=None,
            )


def test_fork_resume_worktree_carries_history(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """
    A clone resumed in a NEW git worktree recalls source history.

    Guards the worktree clone-transcript path: Claude's ``--resume`` is
    cwd-scoped, so the source transcript must be cloned into the
    *worktree's* project dir (not the source repo's) for the resume to
    find it. The pre-``--fork-session`` regression silently found nothing
    here → "terminal resource not found" and no history.
    """
    repo = tmp_path / "srcrepo"
    _init_git_repo(repo)
    branch = f"fork-{uuid.uuid4().hex[:6]}"
    # The host creates the worktree as a sibling of the repo:
    # ``<parent>/<repo-name>-worktrees/<sanitized-branch>``. Pre-trust it
    # so Claude doesn't block on its folder-trust dialog in the clone.
    worktree_path = repo.parent / f"{repo.name}-worktrees" / branch.replace("/", "-")

    with _workspaces_trusted_in_claude_config([repo, worktree_path]):
        with _host_daemon(tmp_path, live_server):
            host_id = _online_host_id(http_client, timeout=30.0)
            agent_id = _claude_native_agent_id(http_client)
            _run_fork_resume_history_check(
                http_client=http_client,
                host_id=host_id,
                agent_id=agent_id,
                source_workspace=repo,
                resume_workspace=repo,
                git={"branch_name": branch},
            )


def test_fork_sdk_source_into_native_builds_history(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    A claude-SDK source forked into claude-native recalls source history.

    This exercises the SDK→native (same provider family) switch: the source
    runs claude-sdk (no native ``external_session_id`` to clone), so the
    runner must REBUILD the clone's Claude transcript from the copied AP
    items (``_ensure_local_claude_resume_transcript`` /
    ``_claude_transcript_records_from_session_items``) under a freshly
    minted uuid, then ``--resume`` it. A regression launches fresh and the
    native clone can't recall the source's code word.

    The SDK source runs on the SERVER's runner (``live_runner_id``) — which
    is wired to the mock LLM server — while the native clone runs on
    the host daemon (Claude CLI OAuth); the fork is independent of both.

    :param live_server: The test server URL.
    :param http_client: HTTP client pointed at the test server.
    :param tmp_path: Per-test temp dir.
    :param live_runner_id: The server fixture's runner id (runs the SDK
        source turn against the mock LLM).
    :param mock_llm_server_url: Mock LLM server base URL (no ``/v1`` suffix).
    :returns: None.
    """
    workspace = tmp_path / "sdk_to_native_ws"
    workspace.mkdir()
    marker = f"FORKWORD_{uuid.uuid4().hex[:6].upper()}"

    mock_model = "mock-claude-coder-sdk"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": f"ACK — code word recorded: {marker}"},
            {"text": marker},
        ],
        key=mock_model,
    )

    # Register the claude-coder (claude-sdk, anthropic) agent as the SOURCE.
    # No model rewrite needed — we use the mock model key directly.
    sdk_agent_name = upload_agent(
        http_client,
        _CLAUDE_CODER_DIR,
        rewrite_model_for_databricks=False,
    )

    # Only the native CLONE shows Claude's folder-trust dialog; the
    # claude-sdk source (Agent SDK) does not. Trust the shared workspace so
    # the clone's terminal opens to the input box, not the trust prompt.
    with _workspaces_trusted_in_claude_config([workspace]):
        with _host_daemon(tmp_path, live_server):
            host_id = _online_host_id(http_client, timeout=30.0)
            native_agent_id = _claude_native_agent_id(http_client)

            # 1. claude-sdk SOURCE on the server's runner; plant a code word.
            source_id = create_runner_bound_session(
                http_client, agent_name=sdk_agent_name, runner_id=live_runner_id
            )
            send_user_message_to_session(
                http_client,
                session_id=source_id,
                content=(
                    f"Remember this code word for later: {marker}. "
                    "Reply with exactly one word: ACK"
                ),
            )
            _poll_for_assistant_marker(
                http_client, session_id=source_id, marker="ACK", timeout=180.0
            )

            # 2. Fork SWITCHING to claude-native. The SDK source has no
            # external_session_id, so the runner rebuilds the native
            # transcript from the copied Omnigent items (build-from-items).
            fork_id = _fork_session(
                http_client,
                source_id=source_id,
                title=f"native clone of {source_id}",
                agent_id=native_agent_id,
            )
            _launch_runner(http_client, host_id=host_id, session_id=fork_id, workspace=workspace)

            # 3. The native clone recalls the planted word — only possible
            # if the Omnigent items were rebuilt into its Claude transcript and
            # resumed; a fresh launch has no history.
            _send_user_message(
                http_client,
                session_id=fork_id,
                text=(
                    "Earlier in this conversation I gave you a code word to remember. "
                    "Reply with exactly that code word and nothing else."
                ),
            )
            text = _poll_for_assistant_marker(
                http_client, session_id=fork_id, marker=marker, timeout=180.0
            )
            assert marker in text, (
                f"native clone did not recall {marker!r} (got {text!r}) — the SDK "
                "source's Omnigent items were not rebuilt into the clone's Claude "
                "transcript, so it launched fresh without history"
            )


def test_fork_native_source_into_sdk_carries_history(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """
    A claude-native source forked into a claude-sdk agent recalls history.

    This exercises the native→SDK switch (already supported via SDK
    transcript replay — the SDK target serializes the copied Omnigent
    transcript as context). The clone binds the built-in ``sdk-chat-builtin``
    (a plain claude-sdk chat agent seeded via OMNIGENT_BUILTIN_AGENT_DIRS —
    NOT the polly supervisor, so the recall is deterministic). It runs on
    the host daemon via the Claude CLI's OAuth, like claude-native.

    Also guards the presentation-label fix: switching to an SDK target must
    drop the source's terminal-first labels so the clone is a chat session,
    not a stale interactive terminal.

    :param live_server: The test server URL.
    :param http_client: HTTP client pointed at the test server.
    :param tmp_path: Per-test temp dir.
    :returns: None.
    """
    workspace = tmp_path / "native_to_sdk_ws"
    workspace.mkdir()
    marker = f"FORKWORD_{uuid.uuid4().hex[:6].upper()}"

    with _workspaces_trusted_in_claude_config([workspace]):
        with _host_daemon(tmp_path, live_server):
            host_id = _online_host_id(http_client, timeout=30.0)
            native_agent_id = _claude_native_agent_id(http_client)
            sdk_agent_id = _builtin_agent_id(http_client, "sdk-chat-builtin")

            # 1. claude-native SOURCE on the host; plant a code word.
            source_id = _create_native_session(
                http_client, agent_id=native_agent_id, host_id=host_id, workspace=workspace
            )
            _send_user_message(
                http_client,
                session_id=source_id,
                text=(
                    f"Remember this code word for later: {marker}. "
                    "Reply with exactly one word: ACK"
                ),
            )
            _poll_for_assistant_marker(
                http_client, session_id=source_id, marker="ACK", timeout=180.0
            )

            # 2. Fork SWITCHING to the SDK chat agent. The SDK target replays
            # the copied transcript as context (no native rebuild needed).
            fork_id = _fork_session(
                http_client,
                source_id=source_id,
                title=f"sdk clone of {source_id}",
                agent_id=sdk_agent_id,
            )

            # The clone must NOT inherit the source's terminal-first labels.
            snap = http_client.get(f"/v1/sessions/{fork_id}", timeout=30.0).json()
            assert snap.get("labels", {}).get("omnigent.ui") != "terminal", (
                "SDK clone of a claude-native source must drop terminal-first "
                f"mode, got labels {snap.get('labels')!r}"
            )

            _launch_runner(http_client, host_id=host_id, session_id=fork_id, workspace=workspace)

            # 3. The SDK clone recalls the planted word from the replayed
            # transcript.
            _send_user_message(
                http_client,
                session_id=fork_id,
                text=(
                    "Earlier in this conversation I gave you a code word to remember. "
                    "Reply with exactly that code word and nothing else."
                ),
            )
            text = _poll_for_assistant_marker(
                http_client, session_id=fork_id, marker=marker, timeout=180.0
            )
            assert marker in text, (
                f"SDK clone did not recall {marker!r} (got {text!r}) — the native "
                "source's transcript was not replayed as context into the SDK clone"
            )
