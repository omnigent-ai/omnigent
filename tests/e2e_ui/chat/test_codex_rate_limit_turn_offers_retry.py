"""A codex (SDK) turn throttled with HTTP 429 must fail as a rate limit and offer Retry.

The real codex app-server exhausts its own retries against the mock ``/v1/responses`` and
reports ``exceeded retry limit, last status: 429``: that text-only failure must reach the
web UI as ``rate_limit_exceeded`` without launch-time stderr, and Retry must then work.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
import time
import uuid
from typing import Any

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
    reset_mock_llm,
)

pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None,
    reason="the codex CLI binary is required for the headless-codex rate-limit e2e",
)

_RATE_LIMIT_HEADLINE = "The model's rate limit was reached. You can retry this turn."
_RECOVERED_TEXT = "Recovered after the rate limit cleared."
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
# Codex retries a 429 a few times per turn; queue enough refusals for all of them.
_RATE_LIMIT_RESPONSES = 16
_TURN_SETTLE_TIMEOUT_S = 180.0


def _codex_bundle(name: str, model: str) -> bytes:
    """A one-file headless ``codex`` (SDK mode) agent bundle; *model* keys the mock queue."""
    spec = {
        "name": name,
        "prompt": "You are a terse assistant. Answer in as few words as possible.",
        "executor": {"harness": "codex", "model": model},
    }
    return bundle_files({f"{name}.yaml": yaml.safe_dump(spec, sort_keys=False).encode()})


def _error_fields(item: dict[str, Any]) -> dict[str, Any]:
    nested = item.get("data")
    return nested if isinstance(nested, dict) else item


def _settled_turn_error(base_url: str, session_id: str) -> dict[str, Any] | None:
    """Poll the transcript until the turn settles.

    :returns: The persisted failure item's fields, or ``None`` when an assistant
        reply landed instead (the mock's scripted refusals ran out).
    """
    deadline = time.monotonic() + _TURN_SETTLE_TIMEOUT_S
    while time.monotonic() < deadline:
        resp = httpx.get(
            f"{base_url}/v1/sessions/{session_id}/items",
            params={"limit": 200, "order": "asc"},
            timeout=10.0,
        )
        resp.raise_for_status()
        # Newest first, so an earlier persisted error is not blamed on this turn.
        items = list(reversed(resp.json().get("data", [])))
        for item in items:
            fields = _error_fields(item)
            if item.get("type") == "error" and str(fields.get("level") or "") != "info":
                return fields
        for item in items:
            role = item.get("role") or _error_fields(item).get("role")
            if item.get("type") == "message" and role == "assistant":
                return None
        time.sleep(0.5)
    raise AssertionError(f"the turn did not settle within {_TURN_SETTLE_TIMEOUT_S:.0f}s")


def _stop_runner(proc: subprocess.Popen[bytes]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)


@pytest.mark.timeout(600)
def test_codex_rate_limited_turn_offers_retry(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    # Each cleanup step is a finalizer so one failing step cannot skip the others.
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    if respawned is not None:
        request.addfinalizer(lambda: _stop_runner(respawned))
    runner_id = str(_server_state["runner_id"])
    uid = uuid.uuid4().hex[:6]
    model = f"mock-codex-429-{uid}"
    token = f"ratelimit-{uid}"
    configure_mock_llm(
        mock_llm_server_url,
        [{"error": "rate limit exceeded", "status_code": 429}] * _RATE_LIMIT_RESPONSES,
        key=model,
        match=token,
    )
    request.addfinalizer(lambda: reset_mock_llm(mock_llm_server_url))
    # The explicit title keeps background title inference off the scripted queue.
    create_resp = post_session_bundle(
        httpx.post,
        f"{live_server}/v1/sessions",
        _codex_bundle(f"codex-rate-limit-{uid}", model),
        metadata={"title": "Codex rate limit retry"},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]
    request.addfinalizer(
        lambda: httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
    )
    bind_session_runner(httpx.patch, live_server, session_id, runner_id, timeout=10.0)

    # Create the page only after setup so a recording starts at the journey.
    page = request.getfixturevalue("page")
    assert isinstance(page, Page)
    page.goto(f"{live_server}/c/{session_id}")
    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible()
    composer.fill(f"Please greet me. {token}")
    page.get_by_role("button", name="Send", exact=True).click()

    error = _settled_turn_error(live_server, session_id)
    if error is None:
        pytest.fail(
            "the turn completed instead of failing: codex outlasted the mock's "
            f"{_RATE_LIMIT_RESPONSES} scripted refusals; raise _RATE_LIMIT_RESPONSES"
        )
    pill = page.get_by_test_id("error-pill").first
    expect(pill).to_be_visible(timeout=15_000)
    expect(pill.get_by_test_id("error-headline")).to_have_text(
        _RATE_LIMIT_HEADLINE, timeout=10_000
    )
    retry = page.get_by_role("button", name=re.compile(r"^Retry")).first
    expect(retry).to_be_visible(timeout=10_000)
    pill.click()
    expect(pill).to_contain_text("429", timeout=10_000)

    message = str(error.get("message") or "")
    assert error.get("code") == "rate_limit_exceeded", error
    assert "429" in message, message
    # Turn-time stderr may legitimately follow; the launch warning must not.
    assert "PATH aliases" not in message, message

    # The provider recovers; Retry continues the turn in place.
    configure_mock_llm(mock_llm_server_url, [{"text": _RECOVERED_TEXT}], key=model)
    retry.click()
    expect(page.locator(_ASSISTANT).filter(has_text=_RECOVERED_TEXT).first).to_be_visible(
        timeout=240_000
    )
