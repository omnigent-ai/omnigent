"""Regression: a blank-label opencode ``question`` is silently dropped from chat.

When an opencode-native turn asks a ``question`` whose option list contains a
blank ``label``, :meth:`OpenCodeNativeForwarder._handle_question` used to treat
the whole question as malformed and call ``_reject_question_quietly`` —
rejecting it opencode-side with **no** POST to the
``native-permission-request`` hook. The prompt still appeared in opencode's
terminal, but chat got no approval card, no notice, and no turn failure, so the
session looked stalled while a real user was silently ignored.

This drives a REAL ``opencode serve`` (via :class:`OpenCodeNativeServer`) and a
REAL :class:`OpenCodeNativeForwarder`, capturing every POST the forwarder makes
to the Omnigent server. A well-formed question POSTs the approval card (the
positive control); a blank-label question must reach chat by *some* visible
signal — on the buggy build it posts nothing, so this test fails until the drop
is surfaced.

Opt-in: needs a functional ``opencode`` on PATH (>=1.17.7,<1.19.0); skipped
otherwise. No model credential or interactive login is required — a mock LLM
emits the ``question`` tool call.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harnesses.opencode_native.app_server import (
    OpenCodeNativeServer,
    OpenCodeVersionError,
)
from omnigent.harnesses.opencode_native.bridge import xdg_config_home_for_bridge_dir
from omnigent.harnesses.opencode_native.forwarder import OpenCodeNativeForwarder
from omnigent.harnesses.opencode_native.provider import write_opencode_provider_config
from tests.e2e.conftest import configure_mock_llm, set_fallback_mock_llm


def _functional_opencode_bin() -> str | None:
    """Return the first PATH ``opencode`` that answers ``--version``.

    Some environments shadow ``opencode`` with a gateway-config wrapper that
    needs extra env; scan PATH and pick the first binary that actually runs.
    """
    for path_dir in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(path_dir) / "opencode"
        if not (candidate.is_file() and os.access(candidate, os.X_OK)):
            continue
        try:
            probe = subprocess.run([str(candidate), "--version"], capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return str(candidate)
    return None


_OPENCODE_BIN = _functional_opencode_bin()

pytestmark = pytest.mark.skipif(
    _OPENCODE_BIN is None,
    reason="opencode-native silent-question-drop e2e needs a functional `opencode` on PATH",
)

_MODEL_KEY = "mock-model"

_QUESTION_WELL_FORMED: dict[str, Any] = {
    "questions": [
        {
            "question": "Which color?",
            "header": "Color",
            "options": [
                {"label": "Red", "description": "the color red"},
                {"label": "Blue", "description": "the color blue"},
            ],
        }
    ]
}

# Same shape, but the first option's label is blank — a value a real model can
# emit. The forwarder drops the whole question on this alone.
_QUESTION_BLANK_LABEL: dict[str, Any] = {
    "questions": [
        {
            "question": "Which color?",
            "header": "Color",
            "options": [
                {"label": "", "description": "unnamed option"},
                {"label": "Blue", "description": "the color blue"},
            ],
        }
    ]
}


def _native_permission_card_posts(captured: list[tuple[str, dict]]) -> int:
    """Count POSTs to the ``native-permission-request`` hook (the chat card)."""
    return sum(1 for path, _ in captured if "native-permission-request" in path)


def _chat_visible_question_signal(captured: list[tuple[str, dict]]) -> bool:
    """Whether the forwarder surfaced the question to chat by any visible means.

    Accepts the approval card hook POST, a turn transition to a failed/error
    status, or a dedicated notice/error conversation item — so either fix shape
    (surface the sanitized question, or fail the turn loudly) satisfies it. The
    raw ``question`` tool-call item and its fabricated ``user dismissed``
    ``function_call_output`` do NOT count: neither asks the user anything.
    """
    for path, body in captured:
        if "native-permission-request" in path:
            return True
        if body.get("type") == "external_session_status":
            status = str((body.get("data") or {}).get("status", "")).lower()
            if status in {"failed", "error", "errored"}:
                return True
        if body.get("type") == "external_conversation_item":
            item_type = (body.get("data") or {}).get("item_type")
            if item_type in {"notice", "error"}:
                return True
    return False


async def _drive_question(
    *,
    server: OpenCodeNativeServer,
    mock_url: str,
    workspace: Path,
    question_args: dict[str, Any],
    conv_id: str,
) -> list[tuple[str, dict]]:
    """Run one turn that asks *question_args* and capture the forwarder's POSTs."""
    configure_mock_llm(
        mock_url,
        [
            {
                "tool_calls": [
                    {"call_id": "q1", "name": "question", "arguments": json.dumps(question_args)}
                ]
            },
            {"text": "done"},
            {"text": "done"},
        ],
        key=_MODEL_KEY,
    )
    set_fallback_mock_llm(mock_url, "default", "done")

    captured: list[tuple[str, dict]] = []

    async def _capture(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = {}
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = {"_raw": request.content.decode(errors="replace")}
        captured.append((request.url.path, body))
        # Empty 200 == "no web verdict yet" (a real user hasn't answered).
        return httpx.Response(200, content=b"")

    driver = server.client()
    session = await driver.create_session({"title": conv_id})
    capture_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_capture), base_url="http://omnigent.invalid"
    )
    forwarder = OpenCodeNativeForwarder(
        session_id=conv_id,
        opencode_session_id=session.id,
        opencode_client=server.client(),
        server_client=capture_client,
        workspace=str(workspace),
    )
    runner = asyncio.create_task(forwarder.run(max_reconnects=0))
    try:
        await driver.prompt_async(
            session.id,
            {
                "parts": [{"type": "text", "text": f"ask ({conv_id})"}],
                "model": {"providerID": "mock", "modelID": _MODEL_KEY},
            },
        )

        async def _turn_settled() -> bool:
            return (
                any(
                    b.get("type") == "external_session_status"
                    and str((b.get("data") or {}).get("status", "")).lower() == "idle"
                    for _, b in captured
                )
                or _native_permission_card_posts(captured) >= 1
            )

        deadline = asyncio.get_event_loop().time() + 30.0
        while asyncio.get_event_loop().time() < deadline and not await _turn_settled():
            await asyncio.sleep(0.25)
        # Let any trailing events (resolution, follow-up text) land.
        await asyncio.sleep(1.5)
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        await capture_client.aclose()
        await driver.aclose()
    return captured


async def test_opencode_native_blank_option_question_reaches_chat(
    isolated_mock_llm_server_url: str, tmp_path: Path
) -> None:
    """A blank-label question must reach chat, just like a well-formed one.

    On the buggy build the well-formed question POSTs an approval card while the
    blank-label question posts nothing chat-visible — the silent drop this guards.
    """
    bridge = tmp_path / "bridge"
    bridge.mkdir(parents=True, exist_ok=True)
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)

    config = {
        "$schema": "https://opencode.ai/config.json",
        "model": f"mock/{_MODEL_KEY}",
        "permission": "ask",
        "provider": {
            "mock": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Mock",
                "options": {"baseURL": f"{isolated_mock_llm_server_url}/v1", "apiKey": "mock-key"},
                "models": {_MODEL_KEY: {"name": _MODEL_KEY}},
            }
        },
    }
    write_opencode_provider_config(xdg_config_home_for_bridge_dir(bridge), config)

    server = OpenCodeNativeServer(
        bridge_dir=bridge, workspace=workspace, opencode_path=_OPENCODE_BIN
    )
    try:
        try:
            await server.start()
        except OpenCodeVersionError as exc:
            pytest.skip(f"installed opencode is outside the supported pin: {exc}")

        well_formed = await _drive_question(
            server=server,
            mock_url=isolated_mock_llm_server_url,
            workspace=workspace,
            question_args=_QUESTION_WELL_FORMED,
            conv_id="conv-wellformed",
        )
        assert _native_permission_card_posts(well_formed) >= 1, (
            "positive control failed: a well-formed opencode question should POST the "
            f"native-permission-request approval card. POSTs: {[p for p, _ in well_formed]}"
        )

        blank_label = await _drive_question(
            server=server,
            mock_url=isolated_mock_llm_server_url,
            workspace=workspace,
            question_args=_QUESTION_BLANK_LABEL,
            conv_id="conv-blanklabel",
        )
        assert _chat_visible_question_signal(blank_label), (
            "blank-label question silently dropped: the forwarder posted no approval "
            "card, no failed status, and no notice/error item, so chat shows nothing "
            "while opencode's terminal displays the prompt (session looks stalled). "
            f"card POSTs={_native_permission_card_posts(blank_label)}; "
            f"POST paths={[p for p, _ in blank_label]}; "
            f"item/status types={sorted({b.get('type') for _, b in blank_label if b.get('type')})}"
        )
    finally:
        await server.close()
