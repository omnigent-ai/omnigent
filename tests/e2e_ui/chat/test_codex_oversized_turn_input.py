"""End-to-end: an oversized Codex turn (>1 MiB paste) must surface a clear over-limit reason,
with no raw JSON-RPC (-32602 / input_error_code) fragments and no generic host-setup headline."""

from __future__ import annotations

import io
import json
import shutil
import tarfile
import time
import uuid

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _ensure_runner_online, _server_state

pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None,
    reason="the codex CLI binary is required to spawn the SDK harness's app-server",
)

_CODEX_MAX_INPUT_CHARS = 1_048_576
_LOG_LINE = "2026-09-09T14:12:42.117Z INFO worker-7 heartbeat ok latency_ms=12\n"
# Fragments of the raw JSON-RPC error dict that must never reach the user.
_RAW_BLOB_FRAGMENTS = ("-32602", "input_error_code", "Codex executor error:")
_GENERIC_HOST_HEADLINE = "Something went wrong setting up the turn on the host."
_USER = '[data-testid="message-bubble"][data-role="user"]'
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_TURN_SETTLE_TIMEOUT_S = 240


def _build_codex_bundle(name: str, model: str) -> bytes:
    """Build a one-file headless-``codex`` (SDK mode) agent bundle."""
    config = {
        "name": name,
        "prompt": "You are a terse assistant. Answer in as few words as possible.",
        "executor": {"harness": "codex", "model": model},
    }
    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            yaml_bytes = yaml.safe_dump(config, sort_keys=False).encode()
            info = tarfile.TarInfo(f"{name}.yaml")
            info.size = len(yaml_bytes)
            tar.addfile(info, io.BytesIO(yaml_bytes))
        return buf.getvalue()


def _create_codex_session(base_url: str, runner_id: str) -> str:
    """Create a runner-bound session for a fresh headless-``codex`` agent."""
    name = f"codex-oversized-{uuid.uuid4().hex[:8]}"
    bundle = _build_codex_bundle(name, f"mock-{name}")
    # A preset title keeps background title inference away from the mock model.
    create_resp = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"title": "Codex oversized input"})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    ).raise_for_status()
    return session_id


def _oversized_message() -> str:
    """A pasted log comfortably past Codex's 1 MiB turn-input ceiling."""
    repeats = _CODEX_MAX_INPUT_CHARS // len(_LOG_LINE) + 2_000
    return "Summarize the errors in this log:\n" + _LOG_LINE * repeats


def _paste_into_composer(page: Page, text: str) -> None:
    """Paste *text* as one drop: ``fill`` stalls on a >1 MiB controlled textarea, so set the
    value via the native setter and dispatch the ``input`` event React listens for."""
    composer = page.get_by_role("textbox", name="Message the agent")
    expect(composer).to_be_enabled(timeout=30_000)
    composer.click()
    page.evaluate(
        "(text) => {"
        " const el = document.activeElement;"
        " const proto = window.HTMLTextAreaElement.prototype;"
        " Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, text);"
        " el.dispatchEvent(new Event('input', {bubbles: true}));"
        " }",
        text,
    )


def _wait_for_turn_settled(base_url: str, session_id: str, timeout_s: int) -> dict:
    """Poll until the turn reaches a terminal state, first requiring it to enter
    running/waiting so the pre-turn idle does not false-fire."""
    deadline = time.time() + timeout_s
    entered_active = False
    snapshot: dict = {}
    while time.time() < deadline:
        snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json()
        status = snapshot.get("status")
        if status in ("running", "waiting"):
            entered_active = True
        elif status == "failed" or (status == "idle" and entered_active):
            return snapshot
        time.sleep(1.0)
    return snapshot


@pytest.mark.timeout(600)
def test_codex_oversized_turn_input_is_not_surfaced_as_raw_rpc_blob(
    page: Page,
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """An oversized codex turn must not fail as a raw ``-32602`` blob."""
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    try:
        runner_id = str(_server_state["runner_id"])
        session_id = _create_codex_session(live_server, runner_id)
        try:
            page.goto(f"{live_server}/c/{session_id}")

            message = _oversized_message()
            assert len(message) > _CODEX_MAX_INPUT_CHARS
            _paste_into_composer(page, message)
            page.get_by_role("button", name="Send", exact=True).click()
            expect(page.locator(_USER)).to_have_count(1, timeout=60_000)

            snapshot = _wait_for_turn_settled(live_server, session_id, _TURN_SETTLE_TIMEOUT_S)
            last_error = snapshot.get("last_task_error") or {}
            pills = page.get_by_test_id("error-pill")

            if snapshot.get("status") != "failed" and not last_error:
                # A build that recovers the oversized input completes the turn.
                expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=30_000)
                return

            # The turn failed: its pills must render, then be expanded so the
            # raw message text is on screen for inspection (and the recording).
            expect(pills.first).to_be_visible(timeout=30_000)
            expect(page.locator(_WORKING)).to_have_count(0, timeout=30_000)

            surfaced: list[str] = [str(last_error.get("message", ""))]
            saw_generic_host_headline = False
            for index in range(pills.count()):
                pill = pills.nth(index)
                headline = pill.get_by_test_id("error-headline").first
                headline_text = headline.evaluate(
                    "e => (e.getAttribute('title') || e.textContent || '').trim()"
                )
                if headline_text == _GENERIC_HOST_HEADLINE:
                    saw_generic_host_headline = True
                headline.click()
                time.sleep(0.5)
                content = pill.get_by_test_id("error-message-content")
                if content.count():
                    surfaced.append(content.first.inner_text())

            for fragment in _RAW_BLOB_FRAGMENTS:
                assert all(fragment not in text for text in surfaced), (
                    f"raw JSON-RPC fragment {fragment!r} reached the user: {surfaced!r}"
                )
            assert not saw_generic_host_headline, (
                "oversized input reported as a generic host-setup failure instead "
                f"of a clear over-limit reason: {surfaced!r}"
            )
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
    finally:
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except Exception:  # best-effort teardown
                respawned.kill()
                respawned.wait(timeout=5)
