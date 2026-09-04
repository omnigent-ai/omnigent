"""E2E regression: a top-level custom codex-native agent with ``yolo: true``
must run terminal commands unattended.

Reported journey: a user authors a custom
top-level agent bundle —

.. code-block:: yaml

    executor:
      type: omnigent
      config:
        harness: codex-native
        yolo: true
    llm:
      reasoning_effort: xhigh

— starts a fresh session from it, and asks the agent to remove a throwaway
file in ``/tmp``. Because the create path derives the Codex full-bypass launch
state (``--dangerously-bypass-approvals-and-sandbox``) from the spec only for
NAMED sub-agent creates, the top-level session's ``terminal_launch_args``
stays NULL, Codex launches at its default approval stance, and the ``rm``
parks the turn on a command-approval request instead of running unattended.

Journey (driven end-to-end through the browser):

1. create a fresh session from the custom codex-native bundle
   (``yolo: true``, no picker-supplied overrides);
2. ask the agent to delete a scratch file under ``/tmp`` (outside the session
   workspace, the reporter's trigger) — a command Codex escalates as
   ``require_escalated``;
3. the turn completes unattended, with no approval card and without Codex
   consulting the approvals reviewer, because the bundle explicitly opted into
   full bypass.

The LLM side is Codex's real CLI against the in-process mock Responses API:
the scripted turn emits the same escalated ``exec_command`` Codex's model
produces for an out-of-workspace ``rm``, and a scripted approvals reviewer is
armed to answer any escalation. On the buggy default-stance launch
(``terminal_launch_args`` NULL) Codex escalates the command and consults that
reviewer — the gate an unattended orchestrator can never clear. With ``yolo``
honored, Codex launches with approvals and sandbox bypassed and never escalates,
so the reviewer is never called.

What this test asserts (the fix-controlled, deterministic discriminator):

- the created session persists the full-bypass ``terminal_launch_args``;
- no approval card appears and the turn completes (reply bubble);
- **the approvals reviewer is never consulted** — the escalated command's text
  never reaches the mock as a review request. This is the observable proxy for
  "ran unattended": it is 0 with the fix and non-zero on the buggy stance.

The reviewer-consultation count is the reliable discriminator in this CI
harness. The real filesystem side effect is NOT asserted: Omnigent wraps the
Codex terminal in its default ``linux_bwrap`` sandbox (the spec declares no
``os_env.sandbox: none``), which gives the process a private ``/tmp``, so a
bypassed ``rm`` never touches the host file — and the mock scripts identical
completion tokens on both stances, so the assistant's reply text and the
(absent) approval card look the same either way. The user-facing correction the
reporter sees in production — the escalated command running without a gate —
therefore surfaces here as the reviewer never being consulted, which this test
checks directly.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tarfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _REPO_ROOT,
    _bind_session_runner,
    _ensure_runner_online,
    _server_state,
    _temp_omnigent_mock_config,
    configure_mock_llm,
    reset_mock_llm,
    set_fallback_mock_llm,
)

from ..messages.test_message_render_parity import _ASSISTANT, _ensure_chat_view, _send

pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None or shutil.which("tmux") is None,
    reason="codex-native e2e needs the `codex` CLI and `tmux` on PATH.",
)

_APPROVAL_CARD = '[data-testid="approval-card"]'
# Must match the model in the mock openai provider config written by
# _temp_omnigent_mock_config (conftest._CODEX_MOCK_MODEL). The reported spec
# pins gpt-5.6-terra; the model id is incidental to the yolo journey (model
# propagation already works per the report), so the mock catalog model keeps
# this lane deterministic.
_CODEX_MOCK_MODEL = "gpt-4o"
# Codex boots in the session terminal on the first turn; cold CI runners are
# slow, and the mock turn itself is instant once the bridge is up.
_TURN_OUTCOME_TIMEOUT_MS = 240_000

# The reporter's custom top-level bundle: codex-native + explicit yolo, with
# the spec-level reasoning effort. No picker-supplied overrides accompany the
# create, exactly like `omnigent run <dir>` on the minimal config.
_CUSTOM_YOLO_AGENT_YAML = f"""\
spec_version: 1
name: codex-top-level-probe

executor:
  type: omnigent
  config:
    harness: codex-native
    yolo: true

llm:
  model: {_CODEX_MOCK_MODEL}
  reasoning_effort: xhigh
"""


def _request_user_text(parsed: object) -> str:
    """Concatenate the ``role="user"`` text of a captured mock request.

    Mirrors the mock server's own user-input extraction so a review request
    (which frames the escalated command as user-role content) is recognised the
    same way the server routed it, while transcript echoes of the command as
    tool output / assistant content are excluded.

    :param parsed: A captured request body from ``GET /mock/requests``.
    :returns: Space-joined user-role text (``""`` when none).
    """
    parts: list[str] = []

    def grab(content: object) -> None:
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, dict):
            text = content.get("text")
            if isinstance(text, str):
                parts.append(text)
            if content.get("content") is not None:
                grab(content["content"])
        elif isinstance(content, list):
            for block in content:
                grab(block)

    if not isinstance(parsed, dict):
        return ""
    response_input = parsed.get("input")
    if isinstance(response_input, str):
        grab(response_input)
    elif isinstance(response_input, list):
        for item in response_input:
            if isinstance(item, dict) and item.get("role") == "user":
                grab(item.get("content"))
    return " ".join(parts)


def _reviewer_consultation_count(mock_url: str, command: str) -> int:
    """Count mock requests that consulted the approvals reviewer for *command*.

    Codex frames a command-approval review as a user-role request quoting the
    command verbatim. Counting requests whose USER-role text contains *command*
    isolates those review requests from transcript echoes (the command appears
    as tool output / assistant content on later turns regardless of stance).

    :param mock_url: Mock Responses API base URL.
    :param command: The escalated command text, e.g. ``"rm -f /tmp/probe"``.
    :returns: Number of captured review requests quoting *command*.
    """
    captured = httpx.get(f"{mock_url}/mock/requests", timeout=10.0).json()["requests"]
    return sum(1 for req in captured if command in _request_user_text(req))


def _create_custom_codex_yolo_session(base_url: str, runner_id: str) -> str:
    """Create a top-level session from the reporter's custom bundle.

    The same multipart ``POST /v1/sessions`` create + runner bind that
    ``omnigent run <dir>`` performs: a session-scoped agent registered from
    the uploaded bundle, with NO terminal_launch_args / model / effort in the
    metadata. Unlike the ``native_codex_session`` fixture this is NOT the
    ``omnigent codex`` wrapper — no wrapper labels — because the bug lives in
    the custom-agent top-level create path.

    :param base_url: Spawned server base URL.
    :param runner_id: The token-bound runner id to bind.
    :returns: The new session/conversation id.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = _CUSTOM_YOLO_AGENT_YAML.encode()
        info = tarfile.TarInfo("config.yaml")  # strict spec_version:1 parser path
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"workspace": str(_REPO_ROOT)})},
        files={
            "bundle": (
                "codex-top-level-probe.tar.gz",
                buf.getvalue(),
                "application/gzip",
            )
        },
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    _bind_session_runner(base_url, session_id, runner_id)
    return session_id


@pytest.fixture
def custom_codex_yolo_mock_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A top-level custom codex-native yolo session on the mock Responses API.

    Always writes the mock provider config so the real ``codex`` CLI routes
    to the in-process mock server and the journey stays deterministic and
    credential-free.

    :returns: ``(base_url, session_id)``.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    with _temp_omnigent_mock_config(mock_llm_server_url, "codex"):
        session_id = _create_custom_codex_yolo_session(live_server, runner_id)
        try:
            yield (live_server, session_id)
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
            if respawned is not None:
                respawned.terminate()
                try:
                    respawned.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    respawned.kill()
                    respawned.wait(timeout=5)


@pytest.mark.timeout(600)
def test_custom_codex_yolo_top_level_runs_unattended(
    page: Page,
    custom_codex_yolo_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A ``yolo: true`` custom top-level codex-native turn must not need a human.

    The discriminator is whether Codex consults the approvals reviewer for the
    escalated out-of-workspace ``rm``: with the bundle's full bypass applied at
    launch it never escalates (0 consultations, runs unattended); on the buggy
    NULL-launch-args stance it launches at ``approval_policy=on-request``,
    escalates, and consults the reviewer — the gate a headless orchestrator can
    never clear. See the module docstring for why the real filesystem deletion
    is not asserted (Omnigent's default ``linux_bwrap`` sandbox masks it).
    """
    base_url, session_id = custom_codex_yolo_mock_session

    nonce = uuid.uuid4().hex[:8]
    user_marker = f"codex-yolo-{nonce}"
    assistant_token = f"codex-yolo-done-{nonce}"
    # The reporter's throwaway /tmp probe file: outside the session workspace
    # (the repo root), so Codex's default workspace-write stance escalates the
    # deletion. Pre-created so the ``rm`` targets a real path; cleaned up in the
    # finally. Its on-host existence is deliberately NOT asserted (bwrap gives
    # the terminal a private /tmp, so a bypassed rm never touches the host).
    probe_file = Path("/tmp") / f"codex-yolo-probe-{nonce}.txt"
    probe_file.write_text("throwaway probe for the yolo journey\n")
    rm_cmd = f"rm -f {probe_file}"

    reset_mock_llm(mock_llm_server_url)
    # Main-turn script: the escalated out-of-workspace deletion, then
    # completion tokens. Internal requests that embed the chat transcript
    # (helper threads, title generation) also match the marker, so pad with
    # extra token entries.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call-{nonce}",
                        "name": "exec_command",
                        "arguments": json.dumps(
                            {
                                "cmd": rm_cmd,
                                "sandbox_permissions": "require_escalated",
                                "justification": "Remove the user's throwaway probe file.",
                            }
                        ),
                    }
                ]
            },
        ]
        + [{"text": assistant_token}] * 6,
        key=user_marker,
        match=user_marker,
    )
    # Codex's automatic approvals reviewer (the bare default stance the buggy
    # launch normalizes to). Its review request quotes the command verbatim
    # in the request text, which routes here (longest content match). DENY:
    # an unattended orchestrator has no human to fall back to, so on the
    # buggy stance the deletion must not happen.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": '{"outcome":"deny"}'}] * 6,
        key=f"reviewer-{nonce}",
        match=rm_cmd,
    )
    # Stray internal Codex calls (model-routed, no transcript) must not stall.
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "")

    try:
        page.goto(f"{base_url}/c/{session_id}")
        _ensure_chat_view(page)
        _send(
            page,
            f"Context marker {user_marker}. Delete the scratch file "
            f"{probe_file}, then reply with exactly: {assistant_token}",
        )

        approval_card = page.locator(_APPROVAL_CARD)
        reply = page.locator(_ASSISTANT, has_text=assistant_token)

        # Wait for a decisive state: the turn completed (token bubble) or it
        # parked on an approval request.
        expect(approval_card.or_(reply).first).to_be_visible(timeout=_TURN_OUTCOME_TIMEOUT_MS)

        # The turn runs unattended: no human approval card, and it reaches the
        # completion bubble. (In this mock+bwrap harness these hold on the buggy
        # stance too — Codex routes the escalation to the automatic reviewer
        # rather than a human card — so they frame the journey but do not
        # discriminate the fix on their own; the reviewer check below does.)
        expect(approval_card).to_have_count(0)
        expect(reply.first).to_be_visible(timeout=60_000)

        # THE DISCRIMINATOR: with the bundle's full bypass applied at launch,
        # Codex never escalates the out-of-workspace rm, so the approvals
        # reviewer is never consulted — the command's text never reaches the
        # mock as a review request. On the buggy NULL-launch-args stance Codex
        # launches at approval_policy=on-request, escalates, and consults the
        # reviewer (the gate an unattended orchestrator can never clear), so the
        # command text appears in the mock's review request(s). This is the
        # observable, deterministic proxy for "ran unattended" that survives the
        # harness masking the real /tmp deletion (see module docstring).
        reviewer_consultations = _reviewer_consultation_count(mock_llm_server_url, rm_cmd)
        assert reviewer_consultations == 0, (
            "top-level custom codex-native agent with yolo: true still escalated "
            f"the deletion for approval ({reviewer_consultations} reviewer "
            "consultation(s)): the full-bypass launch state from the bundle spec "
            "never reached the Codex session, so the command was gated behind an "
            "approval no unattended orchestrator can give."
        )

        # Tie the observed unattended run back to the server fix: the top-level
        # create must have persisted the bundle's full-bypass launch args.
        detail = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json()
        assert detail.get("terminal_launch_args") == [
            "--dangerously-bypass-approvals-and-sandbox"
        ], (
            "top-level custom codex-native session did not persist the bundle's "
            f"full-bypass launch args: terminal_launch_args="
            f"{detail.get('terminal_launch_args')!r}."
        )
    finally:
        probe_file.unlink(missing_ok=True)
