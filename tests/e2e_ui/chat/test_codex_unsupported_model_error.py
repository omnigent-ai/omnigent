"""UI journey: a Codex turn rejected for an account-unsupported model shows the
provider's reason in the chat error pill, not the raw JSON error envelope.

The real web SPA, a live server + runner and the real ``codex`` CLI app-server
are driven against the mock ``/v1/responses``, which answers the turn with the
HTTP 400 a ChatGPT account returns for a model it does not allow.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import tarfile
import uuid
from typing import Any

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _ensure_runner_online, _server_state, configure_mock_llm

pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None,
    reason="the codex CLI binary is required for the codex app-server e2e",
)

_UNSUPPORTED_MODEL = "gpt-6-astra"
_UNSUPPORTED_REASON = (
    f"The '{_UNSUPPORTED_MODEL}' model is not supported when using Codex with a ChatGPT account."
)

_WORKING = '[data-testid="working-indicator"]'


@pytest.fixture
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    """Record at the full viewport so the error text stays legible in footage."""
    if "record_video_dir" not in browser_context_args:
        return browser_context_args
    return {**browser_context_args, "record_video_size": {"width": 1280, "height": 720}}


def _build_codex_bundle(name: str, model: str) -> bytes:
    """Build a one-file ``codex`` (app-server) agent bundle pinned to *model*."""
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


def _create_codex_session(base_url: str, runner_id: str, model: str) -> str:
    """Create a runner-bound session for a fresh ``codex`` agent pinned to *model*."""
    name = f"codex-unsupported-{uuid.uuid4().hex[:8]}"
    # A preset title keeps background title inference off the scripted queue.
    create_resp = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"title": "Codex unsupported model"})},
        files={"bundle": ("agent.tar.gz", _build_codex_bundle(name, model), "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]
    patch_resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch_resp.raise_for_status()
    return session_id


def _send(page: Page, text: str) -> None:
    composer = page.get_by_role("textbox", name="Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


@pytest.mark.timeout(600)
def test_codex_unsupported_model_rejection_shows_reason_not_raw_json(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The failed turn's error must read as the provider's reason, not a JSON blob."""
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    try:
        runner_id = str(_server_state["runner_id"])
        token = f"codexunsupported-{uuid.uuid4().hex[:6]}"
        # Every model request this turn makes gets the account's 400.
        configure_mock_llm(
            mock_llm_server_url,
            [{"error": _UNSUPPORTED_REASON, "status_code": 400}] * 4,
            key=_UNSUPPORTED_MODEL,
            match=token,
        )

        session_id = _create_codex_session(live_server, runner_id, _UNSUPPORTED_MODEL)
        try:
            page.goto(f"{live_server}/c/{session_id}")
            _send(page, f"Say hi. {token}")

            pill = page.get_by_test_id("error-pill").first
            expect(pill).to_be_visible(timeout=240_000)
            expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)
            pill.get_by_test_id("error-headline").click()
            content = pill.get_by_test_id("error-message-content")
            expect(content).to_be_visible(timeout=15_000)
            shown = content.inner_text()

            requests = httpx.get(f"{mock_llm_server_url}/mock/requests", timeout=10.0).json()
            models = {r.get("model") for r in requests["requests"] if token in json.dumps(r)}
            assert models == {_UNSUPPORTED_MODEL}, (
                f"the turn did not reach the provider as {_UNSUPPORTED_MODEL}: {models}"
            )

            assert _UNSUPPORTED_REASON in shown, (
                f"the provider's rejection reason must be preserved; got: {shown!r}"
            )
            # The executor appends a codex stderr tail, so only the leading error
            # body decides whether the provider envelope was unwrapped.
            body = shown.removeprefix("inner executor error:").lstrip()
            assert not body.startswith("{"), (
                f"the failed turn shows the raw provider JSON envelope: {shown!r}"
            )
            assert not re.search(r'"message"\s*:\s*"' + re.escape(_UNSUPPORTED_REASON), shown), (
                f"the rejection reason is still wrapped in the provider's JSON envelope: {shown!r}"
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
