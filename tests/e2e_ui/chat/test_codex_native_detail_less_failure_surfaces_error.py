"""Verify a detail-less codex-native failure renders a readable error pill."""

from __future__ import annotations

import io
import json
import tarfile

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _REPO_ROOT,
    _bind_session_runner,
    _ensure_runner_online,
    _server_state,
)

_WORKING = '[data-testid="working-indicator"]'
_ERROR_PILL = '[data-testid="error-pill"]'

_MODEL_ID = "gpt-6-astra"

# Use a label-less custom agent so harness resolution stays codex-native.
_CODEX_ASTRA_AGENT_YAML = f"""\
spec_version: 1
name: codex-astra-brief

executor:
  type: omnigent
  model: {_MODEL_ID}
  config:
    harness: codex-native

prompt: |
  You are a focused coding assistant. Answer briefly.
"""


def _create_codex_native_session(base_url: str, runner_id: str) -> str:
    """Create a plain codex-native astra session bound to the live runner."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = _CODEX_ASTRA_AGENT_YAML.encode()
        info = tarfile.TarInfo("config.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"workspace": str(_REPO_ROOT)})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    _bind_session_runner(base_url, session_id, runner_id)
    return session_id


def _set_reasoning_effort(base_url: str, session_id: str, effort: str) -> None:
    """Persist an effort through the real session API and verify the stored value."""
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"reasoning_effort": effort, "silent": True},
        timeout=30.0,
    )
    resp.raise_for_status()


def _publish_native_status(
    base_url: str,
    session_id: str,
    status: str,
    *,
    response_id: str,
    output: str | None = None,
) -> None:
    """Publish the same status payload emitted by the codex-native forwarder."""
    data: dict[str, str] = {"status": status, "response_id": response_id}
    if output is not None:
        data["output"] = output
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": data},
        timeout=10.0,
    )
    resp.raise_for_status()


def test_codex_native_detail_less_failed_turn_surfaces_error(
    page: Page,
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Require a readable error pill for a detail-less native failure."""
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    session_id = _create_codex_native_session(live_server, runner_id)
    try:
        snapshot = httpx.get(f"{live_server}/v1/sessions/{session_id}", timeout=10.0).json()
        assert snapshot["harness"] == "codex-native", snapshot["harness"]
        assert "omnigent.wrapper" not in (snapshot.get("labels") or {})

        # The server accepts and persists the full Codex effort ladder.
        _set_reasoning_effort(live_server, session_id, "minimal")
        after = httpx.get(f"{live_server}/v1/sessions/{session_id}", timeout=10.0).json()
        assert after["reasoning_effort"] == "minimal", after.get("reasoning_effort")

        page.goto(f"{live_server}/c/{session_id}")
        expect(page.get_by_role("textbox", name="Message the agent")).to_be_visible(timeout=20_000)

        working = page.locator(_WORKING)
        pills = page.locator(_ERROR_PILL)

        # Turn starts: the id-bearing running edge lights the Working indicator.
        _publish_native_status(
            live_server, session_id, "running", response_id="codex_turn_minimal"
        )
        expect(working).to_be_visible(timeout=15_000)

        # Reproduce the forwarder's bare failed edge.
        _publish_native_status(live_server, session_id, "failed", response_id="codex_turn_minimal")

        # The turn is over -- Working clears...
        expect(working).to_have_count(0, timeout=15_000)

        # Match this turn's native failure rather than any ambient launch error.
        native_failure_pill = pills.filter(
            has_text="The agent ran into an error during this turn."
        )
        expect(native_failure_pill.first).to_be_visible(timeout=15_000)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except Exception:
                respawned.kill()
                respawned.wait(timeout=5)
