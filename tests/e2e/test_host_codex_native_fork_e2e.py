"""End-to-end regression tests for codex-native fork-resume history.

The codex-native mirror of ``test_host_claude_native_fork_e2e``: a forked
codex-native session, resumed on the same host, must carry the source's
Codex *rollout* history into the clone's own thread so the agent can
recall it. Both resume shapes are exercised:

1. **Same working directory** — the clone resumes in the source's exact
   workspace.
2. **New git worktree** — the clone resumes in a freshly created worktree
   off the source repo.

The regression these guard against: codex forks used to resume *fresh* —
they got the copied Omnigent transcript as context but not Codex's
internal thread / rollout state. The current path clones the source
rollout into the clone's own ``CODEX_HOME`` under a freshly minted thread
id (``_clone_codex_rollout``) and launches ``codex resume
<our_thread_id>``. If that regresses, the clone launches fresh (no Codex
history) and the recall assertion below fails. This also exercises the
prerequisite that a host-spawned codex source records its
``external_session_id`` on discovery (otherwise the fork would have no
source thread id to clone).

Opt-in (same rationale as ``test_host_codex_native_e2e``): codex-native
needs ``codex`` on PATH and real model credentials. Set
``OMNIGENT_E2E_CODEX_NATIVE=1`` to run::

    OMNIGENT_E2E_CODEX_NATIVE=1 \
    .venv/bin/python -m pytest tests/e2e/test_host_codex_native_fork_e2e.py \
        --profile oss \
        --llm-api-key "$(databricks auth token -p oss \
            | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')" \
        -v
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from tests.e2e._claude_host_support import (
    _launch_runner,
    _send_user_message,
)
from tests.e2e.test_host_claude_native_fork_e2e import (
    _fork_session,
    _init_git_repo,
    _wait_for_external_session_id,
)
from tests.e2e.test_host_codex_native_e2e import (
    _codex_native_agent_id,
    _create_codex_host_session,
    _online_host_id,
    _poll_for_assistant_marker,
    _spawn_host_daemon,
)

# Opt-in only — see module docstring and test_host_codex_native_e2e.
pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_CODEX_NATIVE") != "1" or shutil.which("codex") is None,
    reason=("codex-native fork e2e needs `codex` on PATH and OMNIGENT_E2E_CODEX_NATIVE=1 to run"),
)


@contextmanager
def _host_daemon(tmp_path: Path, live_server: str) -> Iterator[None]:
    """
    Spawn a codex-native ``omnigent connect`` daemon for the test.

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
    Drive one codex fork-resume flow and assert history carried over.

    Plants a code word in the SOURCE session, waits for its Codex thread
    id (``external_session_id``) to be captured, forks it, binds the clone
    (same dir or a new worktree), then asks the clone to recall the code
    word. The recall only succeeds if the source rollout was cloned into
    the clone's own ``CODEX_HOME`` and resumed — a fresh launch (the
    regression) has no history and never echoes the marker.

    :param http_client: HTTP client pointed at the test server.
    :param host_id: The online host's id.
    :param agent_id: The ``codex-native-ui`` agent id.
    :param source_workspace: Workspace the source session starts in.
    :param resume_workspace: Workspace passed to the runner launch — the
        same dir as *source_workspace*, or the repo root for the worktree
        case (the host derives the worktree path from it).
    :param git: Optional worktree block for the resume launch; ``None``
        for same-dir resume.
    :returns: None.
    """
    marker = f"FORKWORD_{uuid.uuid4().hex[:6].upper()}"

    source_id = _create_codex_host_session(
        http_client, agent_id=agent_id, host_id=host_id, workspace=str(source_workspace)
    )
    # Plant the code word and force a deterministic ack so we know the
    # source turn (carrying the marker) is committed to the rollout.
    _send_user_message(
        http_client,
        session_id=source_id,
        text=(f"Remember this code word for later: {marker}. Reply with exactly one word: ACK"),
    )
    _poll_for_assistant_marker(http_client, session_id=source_id, marker="ACK", timeout=180.0)
    # The clone reads the source's Codex thread id; wait for capture (now
    # recorded on the discovery path).
    _wait_for_external_session_id(http_client, session_id=source_id, timeout=60.0)

    fork_id = _fork_session(http_client, source_id=source_id, title=f"clone of {source_id}")
    _launch_runner(
        http_client,
        host_id=host_id,
        session_id=fork_id,
        workspace=resume_workspace,
        git=git,
    )

    # Ask the clone to recall the planted word. It can only answer from the
    # cloned source rollout — a fresh (history-less) launch can't.
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
        f"clone did not recall {marker!r} (got {text!r}) — the source rollout "
        "was not cloned/resumed, so the clone launched fresh without history"
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git on PATH")
def test_fork_resume_same_dir_carries_history(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """
    A clone resumed in the source's SAME directory recalls source history.

    Guards the same-dir rollout-clone path: the source rollout is copied
    into the clone's own ``CODEX_HOME`` under a minted thread id and
    ``codex resume`` is launched against it. A regression (fresh launch)
    breaks recall.
    """
    repo = tmp_path / "srcrepo"
    _init_git_repo(repo)

    with _host_daemon(tmp_path, live_server):
        host_id = _online_host_id(http_client, timeout=30.0)
        agent_id = _codex_native_agent_id(http_client)
        _run_fork_resume_history_check(
            http_client=http_client,
            host_id=host_id,
            agent_id=agent_id,
            source_workspace=repo,
            resume_workspace=repo,
            git=None,
        )


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git on PATH")
def test_fork_resume_worktree_carries_history(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """
    A clone resumed in a NEW git worktree recalls source history.

    Guards the worktree rollout-clone path: the source rollout is cloned
    into the clone's own ``CODEX_HOME`` (which is per-session-private, not
    cwd-scoped) and the app-server is launched in the worktree, with the
    structural ``cwd`` rewritten to the worktree. A regression resumes
    fresh and the marker never surfaces.
    """
    repo = tmp_path / "srcrepo"
    _init_git_repo(repo)
    branch = f"fork-{uuid.uuid4().hex[:6]}"

    with _host_daemon(tmp_path, live_server):
        host_id = _online_host_id(http_client, timeout=30.0)
        agent_id = _codex_native_agent_id(http_client)
        _run_fork_resume_history_check(
            http_client=http_client,
            host_id=host_id,
            agent_id=agent_id,
            source_workspace=repo,
            resume_workspace=repo,
            git={"branch_name": branch},
        )
