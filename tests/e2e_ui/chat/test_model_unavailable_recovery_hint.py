"""A provider's model-unavailable rejection must come with a recovery hint.

Journeys driven on the real SPA against a live server + runner and the real
harness CLI pointed at the mock Anthropic endpoint. The user picks an
unavailable, other-provider model id, sends a message, and the mock answers
that model with HTTP 404 (standing in for a live provider rejection). The
error card must keep the original provider message and add a concise next
step (model picker / ``/model``), live and after reload.

The unit matrix in ``tests/runner/test_launch_failure.py`` pins what the
diagnosis matches; these journeys cover the harness → runner → server → SPA
boundaries it cannot reach: the claude-sdk adapter's turn error and Pi's
forwarded error item. Claude Code appends its own ``/model`` hint and needs
no handling here.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

import click
import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from omnigent._wrapper_labels import (
    PI_NATIVE_WRAPPER_VALUE,
    UI_MODE_LABEL_KEY,
    UI_MODE_TERMINAL_VALUE,
    WRAPPER_LABEL_KEY,
)
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.helpers.ui_configuration import temp_omnigent_mock_config

_REPO_ROOT = Path(__file__).resolve().parents[3]
# An OpenRouter-style id that no configured provider serves.
_UNAVAILABLE_MODEL = "anthropic/claude-opus-4-8"
# Must match the mock anthropic provider the runner is configured with.
_CLAUDE_MOCK_MODEL = "claude-sonnet-4-20250514"
_PROMPT = "please say hello"
_HINT = re.compile(r"model picker|/model", re.IGNORECASE)
_PROVIDER_MESSAGE = re.compile(r"issue with the selected model|not found|404", re.IGNORECASE)
_TURN_TIMEOUT_MS = 150_000


def _model_not_found_404(model: str) -> list[dict[str, object]]:
    return [{"error": f"model: {model}", "status_code": 404}] * 8


def _create_claude_sdk_session(base_url: str, runner_id: str, mock_url: str) -> str:
    name = f"model-unavailable-{uuid.uuid4().hex[:8]}"
    config = {
        "name": name,
        "prompt": "You are a terse assistant. Answer in as few words as possible.",
        "executor": {
            "harness": "claude-sdk",
            "model": _CLAUDE_MOCK_MODEL,
            "auth": {"type": "api_key", "api_key": "mock-key", "base_url": mock_url},
        },
    }
    bundle = bundle_files({f"{name}.yaml": yaml.safe_dump(config, sort_keys=False).encode()})
    created = post_session_bundle(httpx.post, f"{base_url}/v1/sessions", bundle, timeout=30.0)
    created.raise_for_status()
    session_id = str(created.json()["session_id"])
    bind_session_runner(httpx.patch, base_url, session_id, runner_id, timeout=10.0)
    return session_id


def _create_pi_session(base_url: str, runner_id: str, model: str) -> str:
    from omnigent.harnesses.pi_native.main import _materialize_pi_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        spec = _materialize_pi_agent_spec(Path(tmp)).read_text().encode()
    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: PI_NATIVE_WRAPPER_VALUE,
    }
    created = post_session_bundle(
        httpx.post,
        f"{base_url}/v1/sessions",
        bundle_files({"pi-native-ui.yaml": spec}),
        metadata={"labels": labels, "workspace": str(_REPO_ROOT)},
        filename="pi-native-ui.tar.gz",
        timeout=30.0,
    )
    created.raise_for_status()
    session_id = str(created.json()["session_id"])
    # Pi reads the model at launch, so the pre-launch picker's choice is persisted before bind.
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"model_override": model, "silent": True},
        timeout=30.0,
    ).raise_for_status()
    bind_session_runner(httpx.patch, base_url, session_id, runner_id, timeout=60.0)
    return session_id


def _stop_respawned_runner(respawned: subprocess.Popen[bytes] | None) -> None:
    if respawned is None:
        return
    respawned.terminate()
    try:
        respawned.wait(timeout=5)
    except subprocess.TimeoutExpired:  # best-effort teardown
        respawned.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            respawned.wait(timeout=5)


def _delete_session(base_url: str, session_id: str) -> None:
    with contextlib.suppress(httpx.HTTPError):  # best-effort cleanup
        httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=30.0)


def _type_and_submit(page: Page, text: str) -> None:
    composer = page.get_by_role("textbox", name="Message the agent")
    expect(composer).to_be_visible(timeout=60_000)
    composer.click()
    composer.press_sequentially(text, delay=35)
    page.wait_for_timeout(800)
    page.keyboard.press("Enter")


def _expanded_pill_text(page: Page) -> str:
    pill = page.get_by_test_id("error-pill").last
    expect(pill).to_be_visible(timeout=_TURN_TIMEOUT_MS)
    toggle = pill.locator('button[aria-expanded="false"]')
    if toggle.count():
        toggle.first.click()
    expect(pill.get_by_test_id("error-message-content")).to_be_visible(timeout=15_000)
    return pill.inner_text()


def _chat_texts(page: Page) -> list[str]:
    """Every error card (expanded) and message bubble currently in the Chat view."""
    texts: list[str] = []
    for pill in page.get_by_test_id("error-pill").all():
        toggle = pill.locator('button[aria-expanded="false"]')
        if toggle.count():
            toggle.first.click()
            expect(pill.get_by_test_id("error-message-content")).to_be_visible(timeout=15_000)
        texts.append(pill.inner_text())
    texts.extend(page.locator('[data-testid="message-bubble"]').all_inner_texts())
    return texts


def _persisted_errors(base_url: str, session_id: str) -> list[dict[str, object]]:
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}/items", timeout=10.0)
    if response.status_code != 200:
        return [{"status": response.status_code}]
    body = response.json()
    items = body.get("data", body) if isinstance(body, dict) else body
    if not isinstance(items, list):
        return [{"unexpected_body": str(body)[:500]}]
    return [
        item.get("data", item)
        for item in items
        if isinstance(item, dict) and item.get("type") == "error"
    ]


def _launch_failure(base_url: str, session_id: str) -> str | None:
    """The runner's terminal-start failure for the session, when Pi never launched."""
    snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json()
    error = snapshot.get("last_task_error") or {}
    if error.get("code") == "native_terminal_start_failed":
        return str(error.get("message"))
    return None


def _screenshot(page: Page, name: str) -> None:
    if screenshot_dir := os.environ.get("E2E_SCREENSHOT_DIR"):
        Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(screenshot_dir) / f"{name}.png"))


def _open_terminal_until_connected(page: Page) -> None:
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=60_000)
    page.get_by_test_id("view-mode-terminal").click()
    terminal = page.locator('[data-testid="terminal-view"]').last
    expect(terminal).to_have_attribute("data-state", "connected", timeout=120_000)
    page.wait_for_timeout(2_000)
    page.get_by_test_id("view-mode-chat").click()


@pytest.mark.timeout(600)
def test_claude_sdk_model_unavailable_card_shows_recovery_hint_live_and_after_reload(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The claude-sdk card keeps the provider's message and adds a next step."""
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    try:
        runner_id = str(_server_state["runner_id"])
        session_id = _create_claude_sdk_session(live_server, runner_id, mock_llm_server_url)
        print(f"claude-sdk session: {session_id}")
        try:
            reset_mock_llm(mock_llm_server_url)
            set_fallback_mock_llm(mock_llm_server_url, "default", "hello")
            configure_mock_llm(
                mock_llm_server_url,
                _model_not_found_404(_UNAVAILABLE_MODEL),
                key=_UNAVAILABLE_MODEL,
            )
            page: Page = request.getfixturevalue("page")
            page.goto(f"{live_server}/c/{session_id}")
            _type_and_submit(page, f"/model {_UNAVAILABLE_MODEL}")
            expect(page.get_by_test_id("composer-agent-model-value")).to_have_text(
                _UNAVAILABLE_MODEL, timeout=15_000
            )
            page.wait_for_timeout(1_500)

            _type_and_submit(page, _PROMPT)
            cards: dict[str, str] = {"live": _expanded_pill_text(page)}
            _screenshot(page, "claude-sdk-live")
            page.reload()
            cards["reload"] = _expanded_pill_text(page)
            _screenshot(page, "claude-sdk-reload")

            snapshot = httpx.get(f"{live_server}/v1/sessions/{session_id}", timeout=10.0).json()
            print(f"persisted last_task_error: {json.dumps(snapshot.get('last_task_error'))}")
            for stage, text in cards.items():
                print(f"error card ({stage}):\n{text}\n")
                assert _UNAVAILABLE_MODEL in text, f"{stage}: model id dropped from the card"
                assert _PROVIDER_MESSAGE.search(text), f"{stage}: provider message missing"
                assert _HINT.search(text), (
                    f"{stage}: no recovery hint (model picker or /model) on the "
                    "model-unavailable card"
                )
        finally:
            _delete_session(live_server, session_id)
    finally:
        _stop_respawned_runner(respawned)


@pytest.mark.timeout(600)
def test_pi_native_model_unavailable_shows_guidance_live_and_after_reload(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A Pi session on an unavailable model surfaces a next step, not a bare failure."""
    from omnigent.harnesses.pi_native.main import resolve_pi_executable

    # The runner resolves Pi the same way; skip now instead of waiting out the launch.
    try:
        resolve_pi_executable()
    except click.ClickException as exc:
        pytest.skip(f"this host could not launch Pi: {exc.message}")
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    try:
        runner_id = str(_server_state["runner_id"])
        reset_mock_llm(mock_llm_server_url)
        set_fallback_mock_llm(mock_llm_server_url, "default", "hello")
        for key in (_UNAVAILABLE_MODEL, _UNAVAILABLE_MODEL.split("/", 1)[1]):
            configure_mock_llm(mock_llm_server_url, _model_not_found_404(key), key=key)
        # Pi routes through the runner's configured anthropic provider, not the agent spec.
        with temp_omnigent_mock_config(
            mock_llm_server_url, "claude", workflow_owned=bool(_server_state.get("workflow_owned"))
        ):
            session_id = _create_pi_session(live_server, runner_id, _UNAVAILABLE_MODEL)
            print(f"pi-native session: {session_id}")
            try:
                _drive_pi_journey(request, live_server, session_id)
            finally:
                _delete_session(live_server, session_id)
    finally:
        _stop_respawned_runner(respawned)


def _drive_pi_journey(request: pytest.FixtureRequest, live_server: str, session_id: str) -> None:
    page: Page = request.getfixturevalue("page")
    page.goto(f"{live_server}/c/{session_id}")
    try:
        _open_terminal_until_connected(page)
    except AssertionError:
        # A host whose runner cannot start Pi at all is a precondition failure,
        # not the model-unavailable behavior under test.
        launch_error = _launch_failure(live_server, session_id)
        if launch_error is not None:
            pytest.skip(f"this host could not launch Pi: {launch_error}")
        raise
    _screenshot(page, "pi-native-launch")
    composer = page.get_by_role("textbox", name="Message the agent")
    if composer.is_enabled():
        _type_and_submit(page, _PROMPT)
    else:
        print("composer disabled: Pi refused the model at launch; reading its launch card")
    try:
        expect(page.get_by_test_id("error-pill").first).to_be_visible(timeout=_TURN_TIMEOUT_MS)
    except AssertionError:
        snapshot = httpx.get(f"{live_server}/v1/sessions/{session_id}", timeout=10.0).json()
        print(
            f"no error card within {_TURN_TIMEOUT_MS} ms: status={snapshot.get('status')} "
            f"last_task_error={json.dumps(snapshot.get('last_task_error'))}"
        )
        bubbles = page.locator('[data-testid="message-bubble"]').all_inner_texts()
        print(f"bubbles: {json.dumps(bubbles)}")
        print(f"pi persisted errors: {json.dumps(_persisted_errors(live_server, session_id))}")
        raise
    screens = {"live": _chat_texts(page)}
    _screenshot(page, "pi-native-live")

    page.reload()
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=60_000)
    page.get_by_test_id("view-mode-chat").click()
    expect(page.get_by_test_id("error-pill").first).to_be_visible(timeout=60_000)
    screens["reload"] = _chat_texts(page)
    _screenshot(page, "pi-native-reload")

    print(f"pi persisted errors: {json.dumps(_persisted_errors(live_server, session_id))[:3000]}")
    for stage, texts in screens.items():
        print(f"pi chat ({stage}): {json.dumps(texts)}")
        # The id and the hint must sit on the same card, not on separate pills.
        card = next((text for text in texts if _UNAVAILABLE_MODEL in text), None)
        assert card is not None, f"{stage}: no card names the unavailable model"
        assert _PROVIDER_MESSAGE.search(card), f"{stage}: provider message missing from the card"
        assert _HINT.search(card), (
            f"{stage}: no next step (model picker or /model) on the Pi model-unavailable card"
        )
