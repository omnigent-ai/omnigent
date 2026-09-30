"""E2E: a claude-sdk session gets the composer's reasoning-effort dial.

The dial used to be gated on native wrapper labels alone, so a session on an
SDK harness (``claude-sdk``, ``codex``) never showed it even though its
executor already honors the session's ``reasoning_effort``. The gate now also
reads the harness's declared effort family from ``/v1/harnesses``
(``claude-sdk`` → ``anthropic``).

Driven on the real web SPA against a live server + runner with a real
``claude-sdk`` agent bundle, so ``/v1/harnesses`` supplies the anthropic family
and the runner keeps the composer gear live. No LLM turn is needed — the dial
and its persistence are exercised before any message is sent:

1. the config gear opens and offers an Effort section (it did not before);
2. the ladder is the anthropic family's (``max`` present — an ``openai`` or the
   base 3-level ladder would not offer it);
3. picking ``max`` persists ``reasoning_effort == "max"`` on the session row.
"""

from __future__ import annotations

import io
import json
import tarfile
import time
import uuid

import httpx
import yaml
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _ensure_runner_online, _server_state

_COMPOSER_PLACEHOLDER = "Send a message…"


def _build_claude_sdk_bundle(name: str, mock_llm_server_url: str) -> bytes:
    """Build a one-file claude-sdk agent bundle wired at the mock LLM.

    :param name: Agent name (unique per test run).
    :param mock_llm_server_url: Mock server base URL WITHOUT ``/v1``.
    :returns: The ``.tar.gz`` bundle bytes for multipart upload.
    """
    config = {
        "name": name,
        "prompt": "You are a terse assistant.",
        "executor": {
            "harness": "claude-sdk",
            "model": "claude-sonnet-4-6",
            "auth": {"type": "api_key", "api_key": "mock-key", "base_url": mock_llm_server_url},
        },
    }
    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            yaml_bytes = yaml.safe_dump(config, sort_keys=False).encode()
            info = tarfile.TarInfo(f"{name}.yaml")
            info.size = len(yaml_bytes)
            tar.addfile(info, io.BytesIO(yaml_bytes))
        return buf.getvalue()


def _create_claude_sdk_session(base_url: str, runner_id: str, mock_llm_server_url: str) -> str:
    """Create a runner-bound session for a fresh claude-sdk agent."""
    bundle = _build_claude_sdk_bundle(f"sdk-effort-{uuid.uuid4().hex[:8]}", mock_llm_server_url)
    create_resp = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
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


def _wait_for_session_effort(
    base_url: str, session_id: str, expected: str, timeout_s: float = 15.0
):
    """Poll the real session row until its reasoning_effort reaches *expected*."""
    deadline = time.monotonic() + timeout_s
    last: object = None
    while time.monotonic() < deadline:
        resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        resp.raise_for_status()
        last = resp.json().get("reasoning_effort")
        if last == expected:
            return
        time.sleep(0.25)
    raise AssertionError(
        f"session {session_id} never persisted reasoning_effort={expected!r} (last={last!r})"
    )


def test_claude_sdk_session_offers_the_effort_dial(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory,
) -> None:
    """A claude-sdk session shows the dial and persists a pick.

    A failure means the SDK gate regressed: the Effort section is missing for a
    non-native harness, the ladder is not the declared anthropic family's, or
    the pick never reached the session's ``reasoning_effort``.
    """
    _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    session_id = _create_claude_sdk_session(live_server, runner_id, mock_llm_server_url)

    page.goto(f"{live_server}/c/{session_id}")
    expect(page.get_by_placeholder(_COMPOSER_PLACEHOLDER)).to_be_visible(timeout=30_000)

    gear = page.get_by_test_id("composer-config-gear")
    expect(gear).to_be_visible(timeout=30_000)
    expect(gear).to_be_enabled(timeout=30_000)
    gear.click()
    page.get_by_test_id("composer-agent-edit").click()
    expect(page.get_by_test_id("composer-agent-config-menu")).to_be_visible()
    expect(page.get_by_test_id("composer-agent-efforts")).to_be_visible()

    # The anthropic ladder carries max; an openai/default ladder would not.
    expect(page.get_by_test_id("composer-agent-effort-max")).to_be_visible()

    page.get_by_test_id("composer-agent-effort-max").click()

    # The durable proof the dial reached the session: the persisted row.
    _wait_for_session_effort(live_server, session_id, "max")
