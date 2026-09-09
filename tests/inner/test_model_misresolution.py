"""Regression tests for silent model mis-resolution.

Two live failure modes plus two settled facets, each pinned at the executor
turn boundary — the exact code the harness subprocess runs for a user's
turn. (The full UI journey — Polly's in-chat model switcher — needs the
``cursor-sdk`` package, a ``CURSOR_API_KEY``, and a built SPA; the
scripted-SDK boundary below is the repo's established idiom for this
executor, see ``tests/inner/test_cursor_executor.py``.)

Live facet 1 — Cursor SDK display-label / typo passthrough:
    ``CursorExecutor.run_turn`` resolves the requested model via
    ``_resolve_model``, which only strips ``databricks-*`` ids and remaps
    the legacy ``auto`` alias. Every other unknown string — the display
    label ``"Composer"`` the model switcher can persist, or a typo'd id —
    is forwarded VERBATIM to ``cursor_sdk.AsyncAgent.create``, which
    rejects it and kills the user's turn with
    ``Failed to start cursor-sdk agent: invalid_argument: Cannot use this
    model: Composer``. The fix must stop unknown ids crossing the SDK
    boundary unresolved: reject them loudly before dispatch, or map them
    to a valid catalog id.

Live facet 2 — Claude SDK silent Opus preference:
    On the Databricks-profile gateway path, a session with no pinned model
    (every orchestrator sub-agent whose spec declares no ``model:``) is
    resolved by ``_resolve_databricks_claude_model`` with a hardcoded
    ``opus > sonnet > haiku > fable`` precedence — silently dispatching
    the most expensive endpoint with no log line and no UI signal. That is
    exactly the "UI shows the selected model, AI Gateway metrics show
    Opus" divergence users report. The fix must make the substitution
    observable (log the resolved model) or fail loudly.

Settled facets (green guards):
    - An explicitly selected model IS honored end-to-end on the claude-sdk
      turn path.
    - ``cursor-native`` workers no longer resolve to provider kind
      ``"none"`` (fixed by the curated cursor catalog).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from omnigent.inner.executor import ExecutorConfig, ExecutorError

# ---------------------------------------------------------------------------
# Scripted cursor_sdk — the SDK boundary; mirrors the account catalog from the
# report's error message. ``create`` records the model it was handed and
# enforces the same validation the real backend applies.
# ---------------------------------------------------------------------------

_CURSOR_ACCOUNT_MODELS: dict[str, str] = {
    "default": "Default",
    "auto-smart": "Auto",
    "composer-2.5": "Composer 2.5",
    "claude-opus-4-8": "Opus 4.8",
    "claude-opus-4-7": "Opus 4.7",
    "claude-sonnet-4-6": "Sonnet 4.6",
    "gpt-5.5": "GPT-5.5",
    "gpt-5.3-codex": "Codex 5.3",
}


def _install_scripted_cursor_sdk(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Install a scripted ``cursor_sdk`` module; returns a capture dict.

    ``state["create_models"]`` records every model id that crossed the SDK
    boundary. Ids outside the account catalog raise the backend's
    ``invalid_argument`` error, exactly as the real backend reports it.
    """
    state: dict[str, Any] = {"create_models": []}

    class _Run:
        async def events(self) -> Any:
            return
            yield  # pragma: no cover — makes this an async generator

        async def wait(self) -> Any:
            return types.SimpleNamespace(status="finished", result="ok")

        async def cancel(self) -> None:
            pass

    class _Agent:
        async def send(self, prompt: str, **_kw: Any) -> _Run:
            return _Run()

        async def close(self) -> None:
            pass

    class _Client:
        @classmethod
        async def launch_bridge(cls, **_kw: Any) -> _Client:
            return cls()

        async def aclose(self) -> None:
            pass

        async def list_models(self, *, api_key: Any = None) -> list[Any]:
            # Mirrors cursor_sdk.AsyncClient.list_models(): SDKModel rows
            # with ``id`` and ``display_name`` from the account catalog.
            return [
                types.SimpleNamespace(id=model_id, display_name=display_name)
                for model_id, display_name in _CURSOR_ACCOUNT_MODELS.items()
            ]

    class _AsyncAgent:
        @classmethod
        async def create(
            cls, *, client: Any, model: Any, api_key: Any, name: Any, local: Any
        ) -> _Agent:
            state["create_models"].append(model)
            if model not in _CURSOR_ACCOUNT_MODELS:
                raise RuntimeError(
                    f"invalid_argument: Cannot use this model: {model}. "
                    f"Available models: {', '.join(sorted(_CURSOR_ACCOUNT_MODELS))}. "
                    "Use Cursor.models.list() to discover valid selections."
                )
            return _Agent()

    class _CustomTool:
        def __init__(self, execute: Any, description: Any = None, input_schema: Any = None):
            self.execute = execute

    class _LocalAgentOptions:
        def __init__(self, **kw: Any) -> None:
            self.custom_tools = kw.get("custom_tools")

    class _SendOptions:
        def __init__(self, **_kw: Any) -> None:
            pass

    fake = types.ModuleType("cursor_sdk")
    fake.AsyncClient = _Client  # type: ignore[attr-defined]
    fake.AsyncAgent = _AsyncAgent  # type: ignore[attr-defined]
    fake.CustomTool = _CustomTool  # type: ignore[attr-defined]
    fake.LocalAgentOptions = _LocalAgentOptions  # type: ignore[attr-defined]
    fake.SendOptions = _SendOptions  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cursor_sdk", fake)
    return state


async def _run_cursor_turn(requested_model: str, state_holder: dict[str, Any]) -> list[Any]:
    """Drive one real ``CursorExecutor.run_turn`` with *requested_model*."""
    from omnigent.inner.cursor_executor import CursorExecutor

    executor = CursorExecutor(api_key="crsr_test")
    events: list[Any] = []
    try:
        async for event in executor.run_turn(
            [{"role": "user", "content": "what model are you?", "session_id": "conv1"}],
            [],
            "SYS",
            config=ExecutorConfig(model=requested_model),
        ):
            events.append(event)
    finally:
        await executor.close()
    return events


# ---------------------------------------------------------------------------
# Live facet 1 — Cursor SDK forwards unknown ids verbatim (FAILS on main)
# ---------------------------------------------------------------------------


async def test_cursor_display_label_must_not_cross_the_sdk_boundary_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The display label 'Composer' must be resolved, not forwarded.

    Journey: Polly (Cursor SDK) chat → model switcher picks "Composer" →
    the next prompt dispatches with ``config.model="Composer"`` → today the
    turn dies with the SDK's ``invalid_argument`` error.

    A fix may reject the unknown id loudly BEFORE dispatch or map it to
    its canonical SDK id (``composer-2.5``); either way the raw label must
    never reach ``AsyncAgent.create``.
    """
    state = _install_scripted_cursor_sdk(monkeypatch)
    events = await _run_cursor_turn("Composer", state)

    assert "Composer" not in state["create_models"], (
        "the unresolved display label 'Composer' was forwarded verbatim to "
        f"cursor_sdk.AsyncAgent.create (events: "
        f"{[e.message for e in events if isinstance(e, ExecutorError)]})"
    )


async def test_cursor_typo_model_id_must_not_cross_the_sdk_boundary_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typo'd ids fail loudly instead of forwarding to the backend.

    ``composr-2.5`` (a one-letter typo of a valid id) must not be handed
    to the SDK as-is — it must be rejected with an actionable error before
    dispatch, or resolved against the catalog.
    """
    state = _install_scripted_cursor_sdk(monkeypatch)
    events = await _run_cursor_turn("composr-2.5", state)

    assert "composr-2.5" not in state["create_models"], (
        "the typo'd model id 'composr-2.5' was forwarded verbatim to "
        f"cursor_sdk.AsyncAgent.create (events: "
        f"{[e.message for e in events if isinstance(e, ExecutorError)]})"
    )


# ---------------------------------------------------------------------------
# Live facet 2 — Claude SDK silent Opus preference (FAILS on main)
# ---------------------------------------------------------------------------


class _FakeWorkspace(BaseHTTPRequestHandler):
    """A local HTTP server answering the two real Databricks listings."""

    def do_GET(self) -> None:
        if self.path.startswith("/api/2.1/unity-catalog/model-services"):
            body = json.dumps(
                {
                    "model_services": [
                        {"name": "model-services/system.ai.claude-sonnet-4-6"},
                        {"name": "model-services/system.ai.claude-opus-4-8"},
                        {"name": "model-services/system.ai.claude-haiku-4-5"},
                    ]
                }
            ).encode()
            self.send_response(200)
        elif self.path.startswith("/ai-gateway/anthropic/v1/models"):
            body = json.dumps(
                {
                    "data": [
                        {"id": "databricks-claude-sonnet-4-6"},
                        {"id": "databricks-claude-opus-4-8"},
                        {"id": "databricks-claude-haiku-4-5"},
                    ]
                }
            ).encode()
            self.send_response(200)
        else:
            body = b"{}"
            self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:  # quiet
        pass


@pytest.fixture()
def fake_databricks_workspace(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> HTTPServer:
    """Serve a live-shape workspace catalog and point a profile at it."""
    server = HTTPServer(("127.0.0.1", 0), _FakeWorkspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = f"http://127.0.0.1:{server.server_address[1]}"
    cfg = tmp_path / "databrickscfg"
    cfg.write_text(f"[repro]\nhost = {host}\ntoken = dapi-fake-token\n")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))
    # Ambient workspace credentials outrank the config file in the SDK's
    # resolution order; a developer machine (or CI) with them set would
    # silently retarget these tests at a real host.
    for ambient in (
        "DATABRICKS_HOST",
        "DATABRICKS_TOKEN",
        "DATABRICKS_CLIENT_ID",
        "DATABRICKS_CLIENT_SECRET",
        "DATABRICKS_ACCOUNT_ID",
        "DATABRICKS_AUTH_TYPE",
    ):
        monkeypatch.delenv(ambient, raising=False)
    # httpx must not route localhost through a CI egress proxy.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    yield server
    server.shutdown()


class _StopAfterModelCapture(Exception):
    """Sentinel: halt the turn once the dispatch model is known."""


async def test_claude_sdk_unpinned_model_substitution_must_be_observable(
    fake_databricks_workspace: HTTPServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The silent-Opus substitution must not be silent.

    Journey: a Claude-SDK orchestrator session runs with a selected model,
    and dispatches sub-agents whose spec pins no ``model:``. Each such
    sub-agent turn hits the Databricks-profile resolution path, which
    walks the workspace catalog with a hardcoded ``opus > sonnet > haiku``
    preference — so the user sees the selected model in the UI while
    billing records Opus.

    The executor substituting a model the user never chose is only
    acceptable when it is OBSERVABLE. The codebase's own standard is set by
    ``cursor_executor._resolve_model``, which WARNS when a selection is not
    honored ("Warn, not debug: the requested model is silently NOT
    honored"). So this substitution must either fail loudly or emit a
    WARNING-or-higher record naming the resolved model id. On main the
    only signal is a routine constructor INFO line (emitted for every
    session, substitution or not) — this test fails.
    """
    from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

    caplog.set_level(logging.INFO)

    # Construction reads the profile from DATABRICKS_CONFIG_FILE (the fake
    # workspace above) — the same file a real user's setup writes.
    executor = ClaudeSDKExecutor(gateway=True, databricks_profile="repro")

    captured: dict[str, Any] = {}

    async def _capture_model(sdk: Any, *, session_key: Any, options: Any, model: Any) -> Any:
        captured["model"] = model
        raise _StopAfterModelCapture()

    from unittest.mock import patch

    events: list[Any] = []
    with patch.object(executor, "_get_or_create_client", side_effect=_capture_model):
        try:
            async for event in executor.run_turn(
                [{"role": "user", "content": "hi", "session_id": "sub1"}], [], ""
            ):
                events.append(event)
        except _StopAfterModelCapture:
            pass

    assert not [e.message for e in events if isinstance(e, ExecutorError)]
    # The workspace serves opus, so the opus-first precedence must resolve it
    # (a different capture here would mean the resolution path regressed, not
    # that the substitution became acceptable).
    model = captured.get("model")
    assert model == "databricks-claude-opus-4-8", (
        f"unpinned session resolved {model!r}; expected the workspace's opus "
        "endpoint via the documented opus-first precedence"
    )

    signals = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING and model in record.getMessage()
    ]
    assert signals, (
        f"unpinned session was silently routed to {model!r} (the workspace "
        "also serves sonnet and haiku) with no WARNING-level record naming "
        "the substituted model — silent model mis-resolution (cursor's "
        "_resolve_model warns in the equivalent situation; claude-sdk "
        "must not be quieter about silently picking the most expensive "
        "endpoint)"
    )


# ---------------------------------------------------------------------------
# Settled facets — green guards (PASS on main)
# ---------------------------------------------------------------------------


async def test_claude_sdk_explicitly_selected_model_is_honored(
    fake_databricks_workspace: HTTPServer,
) -> None:
    """An explicit selection is dispatched exactly as chosen.

    Selecting ``databricks-claude-sonnet-4-6`` must dispatch exactly that
    id — never a silent Opus substitute.
    """
    from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

    executor = ClaudeSDKExecutor(
        gateway=True,
        databricks_profile="repro",
        model="databricks-claude-sonnet-4-6",
    )

    captured: dict[str, Any] = {}

    async def _capture_model(sdk: Any, *, session_key: Any, options: Any, model: Any) -> Any:
        captured["model"] = model
        raise _StopAfterModelCapture()

    from unittest.mock import patch

    with patch.object(executor, "_get_or_create_client", side_effect=_capture_model):
        try:
            async for _ in executor.run_turn(
                [{"role": "user", "content": "hi", "session_id": "main1"}], [], ""
            ):
                pass
        except _StopAfterModelCapture:
            pass

    assert captured.get("model") == "databricks-claude-sonnet-4-6"


def test_cursor_native_provider_resolution_is_not_none() -> None:
    """cursor-native resolves a real provider, never kind ``"none"``.

    ``sys_list_models`` previously reported cursor-native workers as
    ``source: "none"``; the curated catalog resolves them as the
    cursor-agent CLI subscription.
    """
    from omnigent.model_catalog import resolve_model_provider

    provider = resolve_model_provider(object(), "cursor-native")
    assert provider.kind == "subscription"
    assert provider.cli == "cursor-agent"


# Keep `asyncio` imported for readers running individual tests via -k with
# stricter collection settings; the suite itself uses asyncio_mode="auto".
_ = asyncio
