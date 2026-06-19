"""End-to-end tests for CROSS-FAMILY fork history carry into claude-native.

Covers forking an openai-family source into the anthropic NATIVE harness:
codex-native → claude-native and openai-agents (SDK) → claude-native. The
source's history is the wrong format (or has no native transcript at all)
for the target, so the runner must take the REBUILD path: synthesize the
Claude transcript from the fork's copied Omnigent items
(``_ensure_local_claude_resume_transcript``) under a freshly minted
session uuid, then ``--resume`` it.

The codex→claude test deliberately waits for the SOURCE to capture its
``external_session_id`` before forking. That makes the regression sharp:
the fork route must SKIP the ``omnigent.fork.source_external_session_id``
directive on a cross-family switch even though the source has one — if it
were stamped, the runner's clone branch would look for a native transcript
in the wrong format, find nothing, and launch FRESH (silently losing
history; the rebuild branch is only reached when the directive is absent).
The label assertions catch that deterministically before the LLM recall
does end-to-end.

The reverse direction (→ codex-native) is intentionally NOT covered yet:
the synthesized Codex rollout's ``session_meta`` must track the installed
Codex CLI's schema (e.g. 0.136 requires ``payload.timestamp`` +
``payload.cli_version`` or the whole rollout is rejected as "does not
start with session metadata" and resume launches fresh). Add the
``codex resume``-side tests together with that version-aware synthesizer
change.

Opt-in: the codex→claude test needs BOTH CLIs installed and logged in
(claude-native needs a real interactive Claude login anchored to the real
``$HOME``; codex needs ``codex`` on PATH with model credentials); the
SDK→native test needs only the Claude side. Set the matching gates::

    OMNIGENT_E2E_CLAUDE_NATIVE=1 OMNIGENT_E2E_CODEX_NATIVE=1 \\
    .venv/bin/python -m pytest tests/e2e/test_host_cross_family_fork_e2e.py \\
        --llm-api-key "mock-key" \\
        -v
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Protocol

import httpx
import pytest

from omnigent.stores.conversation_store import (
    FORK_CARRY_HISTORY_LABEL_KEY,
    FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY,
)
from tests.e2e.conftest import (
    _OPENAI_CODER_DIR,
    configure_mock_llm,
    create_runner_bound_session,
    reset_mock_llm,
    send_user_message_to_session,
    upload_agent,
)
from tests.e2e.test_host_claude_native_e2e import (
    _claude_native_agent_id,
    _online_host_id,
)
from tests.e2e.test_host_claude_native_e2e import (
    _poll_for_assistant_marker as _poll_claude_marker,
)
from tests.e2e.test_host_claude_native_fork_e2e import (
    _fork_session,
    _host_daemon,
    _init_git_repo,
    _launch_runner,
    _send_user_message,
    _wait_for_external_session_id,
    _workspaces_trusted_in_claude_config,
)
from tests.e2e.test_host_codex_native_e2e import (
    _codex_native_agent_id,
    _create_codex_host_session,
)
from tests.e2e.test_host_codex_native_e2e import (
    _poll_for_assistant_marker as _poll_codex_marker,
)

# Opt-in only: the codex→claude test drives BOTH native CLIs in one test
# (source on codex, clone on claude), so it needs both gates and both
# binaries; the SDK→native test needs only the Claude side. See
# test_host_claude_native_e2e / test_host_codex_native_e2e for why binary
# presence alone is not a sufficient gate.
_BOTH_NATIVE_GATE = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_CLAUDE_NATIVE") != "1"
    or os.environ.get("OMNIGENT_E2E_CODEX_NATIVE") != "1"
    or shutil.which("claude") is None
    or shutil.which("codex") is None,
    reason=(
        "cross-family native↔native fork e2e needs `claude` AND `codex` installed/logged in; "
        "set OMNIGENT_E2E_CLAUDE_NATIVE=1 and OMNIGENT_E2E_CODEX_NATIVE=1 to run"
    ),
)
_CLAUDE_NATIVE_GATE = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_CLAUDE_NATIVE") != "1" or shutil.which("claude") is None,
    reason=(
        "cross-family SDK→claude-native fork e2e needs an interactive Claude login; "
        "set OMNIGENT_E2E_CLAUDE_NATIVE=1 (and have `claude` installed + logged in) to run"
    ),
)


class _MarkerPoller(Protocol):
    """Signature shared by both harnesses' assistant-marker poll helpers."""

    def __call__(
        self, client: httpx.Client, *, session_id: str, marker: str, timeout: float
    ) -> str:
        """
        Poll session items until an assistant message contains *marker*.

        :param client: HTTP client pointed at the test server.
        :param session_id: Session/conversation id to poll.
        :param marker: Substring that must appear in an assistant message.
        :param timeout: Max seconds to wait.
        :returns: The matching assistant message text.
        """
        ...


def _assert_cross_family_fork_labels(
    client: httpx.Client, *, fork_id: str, expected_wrapper: str
) -> None:
    """
    Assert the fork's labels route the runner to the REBUILD path.

    Deterministic (no-LLM) check of the cross-family gating before the
    recall assertion: carry-history must be stamped (else the clone
    launches fresh by design) and the source-session directive must be
    ABSENT (else the runner attempts a wrong-format transcript clone,
    fails, and launches fresh). Also pins the presentation labels to the
    TARGET harness so the clone opens in the right UI mode.

    :param client: HTTP client pointed at the test server.
    :param fork_id: The clone's session/conversation id.
    :param expected_wrapper: The TARGET harness's wrapper label value,
        e.g. ``"claude-code-native-ui"`` or ``"codex-native-ui"``.
    :returns: None.
    """
    snap = client.get(f"/v1/sessions/{fork_id}", timeout=30.0)
    snap.raise_for_status()
    labels: dict[str, str] = snap.json().get("labels") or {}
    # Stamped → the runner rebuilds the native transcript from items.
    # Absent would mean the fork route's cross-family carry gate regressed
    # and the clone resumes blank.
    assert labels.get(FORK_CARRY_HISTORY_LABEL_KEY) == "1", (
        f"cross-family fork must stamp carry-history, got labels {labels!r}"
    )
    # Absent → the runner cannot take the (doomed) clone branch. Present
    # would mean the store stamped the source's wrong-format native session
    # id, which makes the runner clone-attempt fail and launch fresh.
    assert FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY not in labels, (
        f"cross-family fork must NOT stamp the source's native session id, got labels {labels!r}"
    )
    # The clone's UI mode must reflect the TARGET harness, not the source's.
    assert labels.get("omnigent.wrapper") == expected_wrapper, (
        f"fork should present as the TARGET harness {expected_wrapper!r}, got labels {labels!r}"
    )


def _plant_marker_and_wait(
    client: httpx.Client,
    *,
    session_id: str,
    marker: str,
    poll_marker: _MarkerPoller,
) -> None:
    """
    Plant a code word in *session_id* and wait until it is committed.

    Sends the marker with a forced one-word ACK, waits for the ACK, then
    waits for the source's ``external_session_id`` capture — the fork must
    happen AFTER capture so the test proves the cross-family gate (not the
    mere absence of a source session id) is what routes the runner to the
    rebuild path.

    :param client: HTTP client pointed at the test server.
    :param session_id: The SOURCE session id.
    :param marker: The code word to plant, e.g. ``"FORKWORD_AB12CD"``.
    :param poll_marker: The harness-matching marker-poll helper
        (``_poll_claude_marker`` or ``_poll_codex_marker``).
    :returns: None.
    """
    _send_user_message(
        client,
        session_id=session_id,
        text=(f"Remember this code word for later: {marker}. Reply with exactly one word: ACK"),
    )
    poll_marker(client, session_id=session_id, marker="ACK", timeout=180.0)
    _wait_for_external_session_id(client, session_id=session_id, timeout=60.0)


def _recall_marker_in_clone(
    client: httpx.Client,
    *,
    fork_id: str,
    marker: str,
    poll_marker: _MarkerPoller,
    source_harness: str,
    target_harness: str,
) -> None:
    """
    Ask the clone to recall the planted word and assert it surfaces.

    The recall only succeeds if the source's Omnigent items were rebuilt
    into the clone's native transcript and resumed — a fresh launch (the
    regression) has no history and never echoes the marker.

    :param client: HTTP client pointed at the test server.
    :param fork_id: The clone's session id.
    :param marker: The planted code word.
    :param poll_marker: The TARGET-harness marker-poll helper.
    :param source_harness: Source harness name for the failure message.
    :param target_harness: Target harness name for the failure message.
    :returns: None.
    """
    _send_user_message(
        client,
        session_id=fork_id,
        text=(
            "Earlier in this conversation I gave you a code word to remember. "
            "Reply with exactly that code word and nothing else."
        ),
    )
    text = poll_marker(client, session_id=fork_id, marker=marker, timeout=180.0)
    assert marker in text, (
        f"{target_harness} clone did not recall {marker!r} (got {text!r}) — the "
        f"{source_harness} source's Omnigent items were not rebuilt into the "
        f"clone's native transcript, so it launched fresh without history"
    )


@_BOTH_NATIVE_GATE
def test_fork_codex_native_into_claude_native_rebuilds_history(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """
    A codex-native source forked into claude-native recalls source history.

    The headline cross-family case: the source has a captured Codex thread
    id, but a Claude target can't resume a Codex rollout — the fork must
    skip the source-session directive and the runner must rebuild the
    Claude transcript from the copied Omnigent items, then ``--resume`` it.

    :param live_server: The test server URL.
    :param http_client: HTTP client pointed at the test server.
    :param tmp_path: Per-test temp dir.
    :returns: None.
    """
    repo = tmp_path / "srcrepo"
    _init_git_repo(repo)
    marker = f"FORKWORD_{uuid.uuid4().hex[:6].upper()}"

    # Only the claude-native CLONE shows Claude's folder-trust dialog (the
    # codex source has no such gate); pre-trust the shared workspace so the
    # clone's terminal opens to the input box.
    with _workspaces_trusted_in_claude_config([repo]):
        with _host_daemon(tmp_path, live_server):
            host_id = _online_host_id(http_client, timeout=30.0)
            codex_agent_id = _codex_native_agent_id(http_client)
            claude_agent_id = _claude_native_agent_id(http_client)

            # 1. codex-native SOURCE on the host; plant a code word and wait
            # for its Codex thread id so the cross-family skip is what's
            # under test (not a missing source session id).
            source_id = _create_codex_host_session(
                http_client, agent_id=codex_agent_id, host_id=host_id, workspace=str(repo)
            )
            _plant_marker_and_wait(
                http_client, session_id=source_id, marker=marker, poll_marker=_poll_codex_marker
            )

            # 2. Fork SWITCHING to claude-native (cross-family).
            fork_id = _fork_session(
                http_client,
                source_id=source_id,
                title=f"claude clone of {source_id}",
                agent_id=claude_agent_id,
            )
            _assert_cross_family_fork_labels(
                http_client, fork_id=fork_id, expected_wrapper="claude-code-native-ui"
            )

            # 3. Bind + resume the clone and assert recall.
            _launch_runner(http_client, host_id=host_id, session_id=fork_id, workspace=repo)
            _recall_marker_in_clone(
                http_client,
                fork_id=fork_id,
                marker=marker,
                poll_marker=_poll_claude_marker,
                source_harness="codex-native",
                target_harness="claude-native",
            )


@_CLAUDE_NATIVE_GATE
def test_fork_openai_sdk_source_into_claude_native_rebuilds_history(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    An openai-agents SDK source forked into claude-native recalls history.

    The cross-family SDK→native shape: the source family (openai) differs
    from the target's (anthropic), so — unlike the same-family
    ``test_fork_sdk_source_into_native_builds_history`` — the fork route
    used to refuse to carry history at all. The runner must rebuild the
    clone's Claude transcript from the copied Omnigent items and resume it.
    Only the Claude CLI is needed (the source runs in-process on the
    server's runner), so this is the cross-family case that runs on hosts
    without a codex login.

    Uses the mock LLM server for the openai-agents SDK source turn so no
    real OpenAI credentials are needed.

    :param live_server: The test server URL.
    :param http_client: HTTP client pointed at the test server.
    :param tmp_path: Per-test temp dir.
    :param live_runner_id: The server fixture's runner id (runs the SDK
        source turn against the mock LLM).
    :param mock_llm_server_url: Mock LLM server base URL (no ``/v1`` suffix).
    :returns: None.
    """
    workspace = tmp_path / "xfam_sdk_to_claude_ws"
    workspace.mkdir()
    marker = f"FORKWORD_{uuid.uuid4().hex[:6].upper()}"

    mock_model = "mock-openai-coder-xfam"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": f"ACK — code word recorded: {marker}"},
            {"text": marker},
        ],
        key=mock_model,
    )

    # Register the openai-coder (openai-agents, openai family) SOURCE agent.
    # No Databricks rewrite needed — we use the mock model key directly.
    sdk_agent_name = upload_agent(
        http_client,
        _OPENAI_CODER_DIR,
        rewrite_model_for_databricks=False,
    )

    # Only the claude-native CLONE shows Claude's folder-trust dialog.
    with _workspaces_trusted_in_claude_config([workspace]):
        with _host_daemon(tmp_path, live_server):
            host_id = _online_host_id(http_client, timeout=30.0)
            claude_agent_id = _claude_native_agent_id(http_client)

            # 1. openai-agents SOURCE on the server's runner; plant a code
            # word. SDK sources never capture an external_session_id, so
            # (unlike the native↔native tests) there is no capture to await.
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
            _poll_claude_marker(http_client, session_id=source_id, marker="ACK", timeout=180.0)

            # 2. Fork SWITCHING to claude-native (cross-family).
            fork_id = _fork_session(
                http_client,
                source_id=source_id,
                title=f"claude clone of {source_id}",
                agent_id=claude_agent_id,
            )
            _assert_cross_family_fork_labels(
                http_client, fork_id=fork_id, expected_wrapper="claude-code-native-ui"
            )

            # 3. Bind + resume the clone and assert recall.
            _launch_runner(http_client, host_id=host_id, session_id=fork_id, workspace=workspace)
            _recall_marker_in_clone(
                http_client,
                fork_id=fork_id,
                marker=marker,
                poll_marker=_poll_claude_marker,
                source_harness="openai-agents",
                target_harness="claude-native",
            )
