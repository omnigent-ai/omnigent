"""UI journey: Gemini usage recorded by the antigravity-native reader shows a Session cost.
The reader posted agy's tiered display name as the usage ``model``, which no catalog id
matched; agy cannot sign in from CI, so the real reader drives this module's own server."""

from __future__ import annotations

import asyncio
import io
import json
import signal
import subprocess
import tarfile
import threading
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import yaml
from playwright.sync_api import Locator, Page, expect

from omnigent.harnesses.antigravity_native import reader as agy_reader
from tests._helpers.live_server import HarnessCredentials, start_live_server
from tests._helpers.model_catalog import offline_catalog_isolated, seed_offline_catalog

pytestmark = offline_catalog_isolated

# The priced catalog id, and agy's tiered display name the reader resolves the
# enum to.
_CATALOG_MODEL_ID = "gemini-3.8-flash"
_DISPLAY_MODEL_NAME = "Gemini 3.8 Flash (Medium)"
_AGY_MODEL_ENUM = "MODEL_PLACEHOLDER_M20"
_GEMINI_CATALOG_ENTRY: dict[str, Any] = {
    "mode": "chat",
    "context_window": {"max_input": 1_000_000, "max_output": 65_536},
    "capabilities": {"function_calling": True},
    "pricing": {
        "input_per_million_tokens": 0.30,
        "output_per_million_tokens": 2.50,
        "cache_read_per_million_tokens": 0.075,
    },
}


@pytest.fixture(scope="module")
def gemini_catalog_server(
    built_spa: None, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[str]:
    """A runner-less SPA server pricing ``gemini-3.8-flash`` from its own offline catalog."""
    root = tmp_path_factory.mktemp("agy_gemini_cost_ui")
    server_env = seed_offline_catalog(
        root / "cache", "gemini", {_CATALOG_MODEL_ID: _GEMINI_CATALOG_ENTRY}
    )
    proc, base_url = start_live_server(
        creds=HarnessCredentials(harness="openai-agents", profile=None, llm_api_key="test-key"),
        db_path=root / "e2e.db",
        artifact_dir=root / "artifacts",
        log_path=root / "server.log",
        extra_env=server_env,
    )
    try:
        yield base_url
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _build_minimal_agent_bundle(name: str) -> bytes:
    """Build a one-file agent bundle for session create (no turn is driven)."""
    config = yaml.safe_dump(
        {
            "name": name,
            "prompt": "You are a terse assistant.",
            "executor": {"harness": "openai-agents", "model": name},
        },
        sort_keys=False,
    ).encode()
    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(f"{name}.yaml")
            info.size = len(config)
            tar.addfile(info, io.BytesIO(config))
        return buf.getvalue()


def _create_session(base_url: str) -> str:
    """Create a session on the live server and return its id."""
    name = f"agy-cost-{uuid.uuid4().hex[:8]}"
    resp = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", _build_minimal_agent_bundle(name), "application/gzip")},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()["session_id"]


def _post_usage_direct(base_url: str, session_id: str, *, model: str) -> None:
    """POST an ``external_session_usage`` frame directly (cumulative tokens, no cost)."""
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_session_usage",
            "data": {
                "model": model,
                "cumulative_input_tokens": 40_000,
                "cumulative_output_tokens": 5_000,
                "cumulative_cache_read_input_tokens": 10_000,
            },
        },
        timeout=30.0,
    )
    assert resp.status_code == 202, resp.text


def _emit_usage_via_reader(
    base_url: str, session_id: str, *, model_enum: str, display_name: str
) -> None:
    """Drive the real reader to post the usage event a live agy turn would emit."""
    state = agy_reader._ReaderState(
        seen=set(),
        interacted=set(),
        model_catalog={"models": {model_enum: {"model": model_enum, "displayName": display_name}}},
    )
    step: dict[str, Any] = {
        "type": agy_reader._TYPE_PLANNER_RESPONSE,
        "status": agy_reader._STATUS_DONE,
        "stepIndex": 2,
        "metadata": {
            "modelUsage": {
                "inputTokens": "40000",
                "outputTokens": "5000",
                "cacheReadTokens": "10000",
                "model": model_enum,
            }
        },
    }

    async def _drive() -> None:
        async with httpx.AsyncClient(base_url=base_url, timeout=30) as client:
            await agy_reader._maybe_emit_session_usage(
                step, client=client, session_id=session_id, state=state
            )

    # The sync Playwright API runs inside a live event loop, so the reader's
    # async emission gets its own loop on a worker thread.
    error: list[BaseException] = []

    def _target() -> None:
        try:
            asyncio.run(_drive())
        except BaseException as exc:  # surface to the test thread
            error.append(exc)

    worker = threading.Thread(target=_target)
    worker.start()
    worker.join()
    if error:
        raise error[0]


def _open_usage_breakdown(page: Page) -> Locator:
    """Open the agent-info popover and expand the per-model usage breakdown."""
    trigger = page.get_by_test_id("agent-info-trigger")
    trigger.focus()
    trigger.press("Enter")
    usage_section = page.get_by_test_id("agent-info-usage-by-model")
    expect(usage_section).to_be_visible(timeout=30_000)
    usage_section.locator("summary").press("Enter")
    return usage_section


@pytest.mark.timeout(600)
def test_antigravity_gemini_usage_shows_cost_in_agent_info(
    page: Page,
    gemini_catalog_server: str,
) -> None:
    """The agent-info popover shows a Session cost for usage the reader recorded."""
    base_url = gemini_catalog_server
    control_id = _create_session(base_url)
    bug_id = _create_session(base_url)
    try:
        # Control: the catalog id prices, so the cost line renders.
        _post_usage_direct(base_url, control_id, model=_CATALOG_MODEL_ID)
        page.goto(f"{base_url}/c/{control_id}")
        _open_usage_breakdown(page)
        expect(page.get_by_test_id("agent-info-session-cost")).to_be_visible(timeout=30_000)

        # The same tokens, recorded by the reader under agy's tiered label.
        _emit_usage_via_reader(
            base_url,
            bug_id,
            model_enum=_AGY_MODEL_ENUM,
            display_name=_DISPLAY_MODEL_NAME,
        )
        page.goto(f"{base_url}/c/{bug_id}")
        usage_section = _open_usage_breakdown(page)

        # Tokens are shown, so a missing cost line would be an unpriced turn.
        model_groups = usage_section.locator('[data-testid^="agent-info-model-"]')
        expect(model_groups.first).to_be_visible()
        expect(page.get_by_test_id("agent-info-session-cost")).to_be_visible(timeout=15_000)
    finally:
        for sid in (control_id, bug_id):
            httpx.delete(f"{base_url}/v1/sessions/{sid}", timeout=10.0)
