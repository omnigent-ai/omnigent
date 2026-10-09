"""e2e: an uploaded attachment survives forking a chat into a native harness.

Journey:

  1. In an ``openai-agents`` (SDK) chat, upload an image and send it as an
     attachment. The user message item is persisted PRE-resolution, i.e. it
     keeps a raw ``file_id`` block (only the server's file store holds the
     bytes; the item does not inline them).
  2. Fork that chat into Claude Code (``claude-native``), carrying history.
     The fork deep-copies the conversation items AND must carry the
     session-scoped file resources those items reference: the copied
     attachment blocks are rewritten to fork-owned file copies.
  3. Open the forked Claude Code session. The chat transcript re-fetches the
     image from the fork's OWN session-scoped file endpoint, and the
     claude-native runner rebuilds a resumable transcript from the fork's
     items (``_ensure_local_claude_resume_transcript`` →
     ``_resolve_session_item_file_references``), fetching the image back the
     same way.

Correct behavior, asserted in order of directness:

  * the fork's user message references an attachment the fork can serve —
    its own file endpoint returns the uploaded bytes (a fork that copies
    items but not files 404s here, the deterministic root cause);
  * the forked chat renders the attachment as a LOADED image (the
    user-visible symptom when the file is missing is a broken image);
  * launching the forked terminal (which forces the transcript rebuild)
    never logs an unresolved/failed file_id resolution — the native
    executor receives the attachment resolved instead of rendering
    ``[Attachment <name> could not be loaded]``.

The fork's Claude Code launch runs against the in-process mock LLM (a mock
``anthropic`` provider config is written to ``~/.omnigent/config.yaml`` for
the duration, mirroring ``native_claude_mock_session``), so the journey boots
the real ``claude`` CLI without real credentials.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.entities import MessageData, NewConversationItem
from omnigent.process_logging import process_log_dir
from tests.e2e_ui.conftest import (
    _bind_session_runner,
    _server_state,
    _temp_omnigent_mock_config,
    seed_committed_items,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
# Checked-in 100x100 red square with a blue center.
_TEST_IMAGE_PATH = _REPO_ROOT / "tests" / "resources" / "test_image.png"

# Runner-log lines that mean the attachment did NOT survive the fork: the
# native executor received the raw file_id (resolver never inlined it), or
# the resolver's fetch through the fork's file endpoint failed after retries.
# The resolver's failure lines deliberately omit the file id, so they are
# asserted absent globally rather than paired with a specific file.
_UNRESOLVED_SIGNATURE = "Native executor received unresolved file_id"
_RESOLVE_FAILED_SIGNATURES = (
    "Attachment metadata read failed",
    "Attachment content read failed",
)

# claude-native built-in seeded unconditionally at server startup; forking
# onto it switches the fork's harness (SDK -> Claude Code).
_CLAUDE_NATIVE_TARGET = "claude-native-ui"
_CLAUDE_NATIVE_WRAPPER = "claude-code-native-ui"

_CARRY_HISTORY_LABEL_KEY = "omnigent.fork.carry_history"
_WRAPPER_LABEL_KEY = "omnigent.wrapper"

_TERMINAL_VIEW = '[data-testid="terminal-view"]'
# claude-native auto-launch + first-run pre-accept + WS attach.
_TERMINAL_READY_TIMEOUT_MS = 120_000
# After the terminal connects, the pre-launch rebuild has already run; give
# log flushing a short grace before asserting the failure lines are absent.
_LOG_SETTLE_S = 5.0

# The user text sent alongside the image, echoed in the forked chat.
_USER_PROMPT = "What is in this image?"
# Settled assistant reply seeded alongside the image so a forked transcript
# renders a complete exchange (user image → agent answer), describing the
# checked-in test image (a red square with a blue center).
_AGENT_REPLY = "It's a red square with a smaller blue square centered inside it."
_ATTACHMENT_FILENAME = "fork-carry.png"


def _agent_id_by_name(base_url: str, name: str) -> str:
    """Resolve a built-in agent's id by name from ``GET /v1/agents``."""
    resp = httpx.get(f"{base_url}/v1/agents", params={"limit": 100}, timeout=30.0)
    resp.raise_for_status()
    agent = next((a for a in resp.json()["data"] if a["name"] == name), None)
    assert agent is not None, (
        f"built-in agent {name!r} not registered on the test server — the native "
        f"targets are seeded unconditionally at startup, so absence is a server bug"
    )
    return str(agent["id"])


def _upload_image(base_url: str, session_id: str) -> str:
    """Upload the test image into *session_id*'s file store, return its file_id."""
    assert _TEST_IMAGE_PATH.exists(), f"missing test image at {_TEST_IMAGE_PATH}"
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/resources/files",
        files={"file": (_ATTACHMENT_FILENAME, _TEST_IMAGE_PATH.read_bytes(), "image/png")},
        timeout=30.0,
    )
    resp.raise_for_status()
    return str(resp.json()["id"])


def _seed_image_message(session_id: str, file_id: str, reply: str | None = None) -> None:
    """Persist a user message with an ``input_image`` block straight into the store.

    Seeds the pre-resolution item directly (no ``POST /events``) so no runner
    turn is dispatched. The source and fork share one runner; a source turn
    left running would keep that runner busy when the fork binds and launches
    Claude Code. The journey only needs the raw ``file_id`` block persisted —
    which is exactly what a real send would leave behind before resolution.

    :param reply: When set, also seed a settled assistant reply so the forked
        transcript renders a complete exchange (user image → agent answer),
        not just the unanswered attachment.
    """
    items = [
        NewConversationItem(
            type="message",
            response_id="resp_attach",
            data=MessageData(
                role="user",
                content=[
                    {"type": "input_text", "text": _USER_PROMPT},
                    {"type": "input_image", "file_id": file_id},
                ],
            ),
        )
    ]
    if reply is not None:
        items.append(
            NewConversationItem(
                type="message",
                response_id="resp_attach",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": reply}],
                    agent="hello_world",
                ),
            )
        )
    seed_committed_items(session_id, items)


def _wait_persisted_file_id_block(base_url: str, session_id: str, file_id: str) -> None:
    """Poll until a persisted user item carries the raw ``file_id`` block.

    Confirms the item is stored PRE-resolution (raw file_id, no inlined data
    URI) — that is exactly what the fork copies and what the forked surfaces
    must be able to re-resolve.
    """
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=30.0)
        resp.raise_for_status()
        for item in resp.json().get("items", []):
            data = item.get("data") if isinstance(item.get("data"), dict) else item
            if data.get("role") != "user":
                continue
            for block in data.get("content") or []:
                if isinstance(block, dict) and block.get("file_id") == file_id:
                    assert not (
                        isinstance(block.get("image_url"), str)
                        and str(block["image_url"]).startswith("data:")
                    ), f"expected raw file_id (pre-resolution) in persisted item, got {block!r}"
                    return
        time.sleep(0.5)
    raise AssertionError(
        f"raw file_id block {file_id!r} never persisted in source session {session_id!r}"
    )


def _fork_switch_to_claude_native(base_url: str, source_id: str, target_agent_id: str) -> str:
    """Fork *source_id* onto the claude-native built-in, return the fork id."""
    resp = httpx.post(
        f"{base_url}/v1/sessions/{source_id}/fork",
        json={"agent_id": target_agent_id, "title": "Fork into Claude Code"},
        timeout=60.0,
    )
    resp.raise_for_status()
    return str(resp.json()["id"])


def _fork_attachment_file_id(base_url: str, fork_id: str) -> str:
    """Return the file_id the fork's copied user message references."""
    resp = httpx.get(f"{base_url}/v1/sessions/{fork_id}", timeout=30.0)
    resp.raise_for_status()
    for item in resp.json().get("items", []):
        data = item.get("data") if isinstance(item.get("data"), dict) else item
        if data.get("role") != "user":
            continue
        for block in data.get("content") or []:
            if isinstance(block, dict) and isinstance(block.get("file_id"), str):
                return str(block["file_id"])
    raise AssertionError(f"fork {fork_id!r} has no user item with a file_id attachment block")


def _read_runner_logs() -> str:
    """Concatenate the runner's structured log files.

    The runner writes to ``<OMNIGENT_DATA_DIR>/logs/runner/runner-*.log``
    (``tests/conftest.py`` points ``OMNIGENT_DATA_DIR`` at an isolated temp
    dir for the whole test session, and the runner subprocess inherits it).
    Read every runner log so a respawn cannot hide a line.
    """
    log_dir = process_log_dir("runner")
    if not log_dir.exists():
        return ""
    parts: list[str] = []
    for path in sorted(log_dir.glob("runner-*.log")):
        try:
            parts.append(path.read_text(errors="replace"))
        except OSError:
            continue
    return "\n".join(parts)


def _open_view(page: Page, name: str) -> None:
    """Switch the session surface via the header toggle (``Chat view`` / ``Terminal view``)."""
    page.get_by_test_id("view-mode-toggle").get_by_role("button", name=name).click()


def _wait_attachment_loaded(page: Page, file_id: str) -> None:
    """Assert the fork's chat renders the user's attachment as a LOADED image.

    The forked chat re-fetches the image from the fork's own session-scoped
    file endpoint (``/v1/sessions/{fork}/resources/files/{file_id}/content``).
    A fork that lost the file 404s there and the ``<img>`` finishes with
    ``naturalWidth == 0`` — the user-visible broken-attachment symptom this
    guards against.
    """
    img = page.locator(f'img[src*="resources/files/{file_id}/content"]').first
    expect(img).to_be_attached(timeout=30_000)
    deadline = time.monotonic() + 30.0
    state: dict[str, object] = {}
    while time.monotonic() < deadline:
        state = img.evaluate("e => ({complete: e.complete, naturalWidth: e.naturalWidth})")
        if state.get("complete") and int(state.get("naturalWidth") or 0) > 0:
            return
        time.sleep(0.5)
    raise AssertionError(
        f"fork attachment image should have loaded (natural size > 0), got {state!r} — "
        f"the attachment did not survive the fork"
    )


@pytest.mark.nightly
@pytest.mark.timeout(300)
def test_fork_into_claude_native_carries_image_attachment(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Fork an SDK chat (with an uploaded image) into Claude Code → the fork
    owns a copy of the attachment, the chat renders it, and the native
    transcript rebuild resolves it instead of logging an unresolved file_id.
    """
    base_url, source_id = seeded_session
    runner_id = str(_server_state["runner_id"])
    target_agent_id = _agent_id_by_name(base_url, _CLAUDE_NATIVE_TARGET)

    # 1) User uploads an image and sends it in the SDK chat. (Seeded straight
    #    into the store — see _seed_image_message — so no source turn occupies
    #    the shared runner before the fork launches Claude Code.)
    source_file_id = _upload_image(base_url, source_id)
    _seed_image_message(source_id, source_file_id)
    _wait_persisted_file_id_block(base_url, source_id, source_file_id)
    image_bytes = _TEST_IMAGE_PATH.read_bytes()

    # 2) User forks the chat into Claude Code (carry history).
    fork_id = _fork_switch_to_claude_native(base_url, source_id, target_agent_id)
    assert fork_id != source_id

    # -- Deterministic server-side behavior: the fork's copied user message
    #    references an attachment the fork itself can serve. A fork that
    #    copies items but not their file resources 404s here.
    fork_file_id = _fork_attachment_file_id(base_url, fork_id)
    fork_content = httpx.get(
        f"{base_url}/v1/sessions/{fork_id}/resources/files/{fork_file_id}/content",
        timeout=30.0,
    )
    assert fork_content.status_code == 200, (
        f"fork's own file endpoint must serve the attachment its items reference, "
        f"got {fork_content.status_code} for file {fork_file_id!r}"
    )
    assert fork_content.content == image_bytes, "fork's attachment copy must hold the same bytes"

    # -- The source keeps its own file untouched.
    source_content = httpx.get(
        f"{base_url}/v1/sessions/{source_id}/resources/files/{source_file_id}/content",
        timeout=30.0,
    )
    assert source_content.status_code == 200
    assert source_content.content == image_bytes

    # -- The fork's labels still select the runner's rebuild-from-items path
    #    (the journey being guarded: rebuild must re-resolve the attachment).
    snap = httpx.get(f"{base_url}/v1/sessions/{fork_id}", timeout=30.0)
    snap.raise_for_status()
    labels = snap.json().get("labels") or {}
    assert labels.get(_CARRY_HISTORY_LABEL_KEY) == "1", (
        f"native-target fork must stamp carry-history (rebuild path), got {labels!r}"
    )
    assert labels.get(_WRAPPER_LABEL_KEY) == _CLAUDE_NATIVE_WRAPPER, (
        f"fork must present as the claude-native target, got {labels!r}"
    )

    # 3) User opens the forked Claude Code session.
    with _temp_omnigent_mock_config(mock_llm_server_url, "claude"):
        _bind_session_runner(base_url, fork_id, runner_id)
        page.goto(f"{base_url}/c/{fork_id}")
        expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(
            timeout=_TERMINAL_READY_TIMEOUT_MS
        )

        # -- User-visible behavior: the forked chat shows the original prompt
        #    AND the image attachment renders (it survived the fork).
        expect(page.get_by_text(_USER_PROMPT).first).to_be_visible(timeout=60_000)
        _wait_attachment_loaded(page, fork_file_id)

        # Switch to the Terminal view to force the native launch; waiting for
        # the xterm to connect forces the claude-native launch, whose
        # pre-launch transcript rebuild re-resolves the attachment.
        _open_view(page, "Terminal view")
        expect(page.locator(_TERMINAL_VIEW).last).to_have_attribute(
            "data-state", "connected", timeout=_TERMINAL_READY_TIMEOUT_MS
        )

        # -- The rebuild resolved the attachment: the runner never logged the
        #    unresolved-file_id error or a failed file_id fetch for either the
        #    source's or the fork's file id.
        time.sleep(_LOG_SETTLE_S)
        log_text = _read_runner_logs()
        for file_id in (source_file_id, fork_file_id):
            assert not (_UNRESOLVED_SIGNATURE in log_text and file_id in log_text), (
                f"runner logged {_UNRESOLVED_SIGNATURE!r} for file {file_id!r} — the "
                f"forked transcript rebuild did not resolve the attachment; last 4000 "
                f"chars:\n{log_text[-4000:]}"
            )
        for signature in _RESOLVE_FAILED_SIGNATURES:
            assert signature not in log_text, (
                f"runner logged {signature!r} — an attachment fetch failed during the "
                f"forked transcript rebuild; last 4000 chars:\n{log_text[-4000:]}"
            )

        # -- End on the user-visible outcome: back in the chat, the
        #    attachment still renders. (Also leaves a recording's final
        #    frame on the working attachment.)
        _open_view(page, "Chat view")
        _wait_attachment_loaded(page, fork_file_id)
        page.wait_for_timeout(1500)

    # -- Refcount proof, run LAST: the fork SHARES the source's blob (no bytes
    #    copied), so deleting the source session must not delete the bytes out
    #    from under the fork. Reference-counted blob deletion is what keeps the
    #    fork servable here; without it, deleting the source resurrects the
    #    original 404. Deliberately after the launch above — the source and fork
    #    share one runner, so deleting the source mid-launch would wedge it.
    del_resp = httpx.delete(f"{base_url}/v1/sessions/{source_id}", timeout=30.0)
    assert del_resp.status_code in (200, 204), del_resp.text
    fork_content_after = httpx.get(
        f"{base_url}/v1/sessions/{fork_id}/resources/files/{fork_file_id}/content",
        timeout=30.0,
    )
    assert fork_content_after.status_code == 200, (
        "fork's attachment must survive the source session's deletion — the "
        f"shared blob was reference-counted, got {fork_content_after.status_code}"
    )
    assert fork_content_after.content == image_bytes


@pytest.mark.nightly
@pytest.mark.timeout(180)
def test_fork_shared_attachment_renders_in_forked_chat(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The web-surface proof of the blob-sharing fork, without a native launch.

    Same-agent (SDK) fork of a chat that has an uploaded image: the forked
    chat must render the attachment (its own file row points at the source's
    shared blob, so the fork's content endpoint serves the bytes), and it must
    keep rendering after the SOURCE session is deleted (reference-counted blob
    deletion keeps the shared bytes alive for the fork). This is the same fix
    the native journey exercises, on the surface a user watches, and it needs
    no Claude Code launch — so it records anywhere the browser does.
    """
    base_url, source_id = seeded_session
    runner_id = str(_server_state["runner_id"])

    # 1) Upload an image and persist a settled exchange (user image + the
    #    agent's answer) straight into the store — no turn dispatched.
    source_file_id = _upload_image(base_url, source_id)
    _seed_image_message(source_id, source_file_id, reply=_AGENT_REPLY)
    _wait_persisted_file_id_block(base_url, source_id, source_file_id)
    image_bytes = _TEST_IMAGE_PATH.read_bytes()

    # 2) Plain fork (same SDK agent) — carries the attachment via a fork-owned
    #    row that shares the source's blob.
    resp = httpx.post(
        f"{base_url}/v1/sessions/{source_id}/fork",
        json={"title": "Fork with attachment"},
        timeout=60.0,
    )
    resp.raise_for_status()
    fork_id = resp.json()["id"]
    assert fork_id != source_id

    fork_file_id = _fork_attachment_file_id(base_url, fork_id)
    fork_content = httpx.get(
        f"{base_url}/v1/sessions/{fork_id}/resources/files/{fork_file_id}/content",
        timeout=30.0,
    )
    assert fork_content.status_code == 200, (
        f"fork must serve the shared attachment, got {fork_content.status_code}"
    )
    assert fork_content.content == image_bytes

    # 3) Open the forked chat — the attachment AND the agent's answer render
    #    (the whole exchange survived the fork).
    _bind_session_runner(base_url, fork_id, runner_id)
    page.goto(f"{base_url}/c/{fork_id}")
    expect(page.get_by_text(_USER_PROMPT).first).to_be_visible(timeout=60_000)
    _wait_attachment_loaded(page, fork_file_id)
    expect(page.get_by_text(_AGENT_REPLY).first).to_be_visible(timeout=60_000)
    page.wait_for_timeout(2500)  # hold on the rendered exchange (recording)

    # 4) Delete the SOURCE, reload the fork — the attachment STILL renders
    #    (reference-counted blob deletion kept the shared bytes alive).
    del_resp = httpx.delete(f"{base_url}/v1/sessions/{source_id}", timeout=30.0)
    assert del_resp.status_code in (200, 204), del_resp.text
    page.reload()
    expect(page.get_by_text(_USER_PROMPT).first).to_be_visible(timeout=60_000)
    _wait_attachment_loaded(page, fork_file_id)
    expect(page.get_by_text(_AGENT_REPLY).first).to_be_visible(timeout=60_000)
    page.wait_for_timeout(3000)  # hold on the surviving exchange (recording)
