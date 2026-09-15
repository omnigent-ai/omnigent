"""E2E regression: Polly model switcher sends invalid Cursor SDK model id.

User journey (from the report):

1. Start a Polly chat on the Cursor SDK brain harness.
2. Use the in-chat model switcher to select ``Composer`` (a display label).
   The transcript records ``System: model changed to Composer`` and the
   session's ``model_override`` is persisted as the literal string
   ``"Composer"`` (the server's ``validate_model_override`` accepts it).
3. Send any prompt. The persisted override reaches the executor and flows
   into ``cursor_sdk.AsyncAgent.create`` unchanged, so the turn dies with::

       inner executor error: Failed to start cursor-sdk agent:
       invalid_argument: Cannot use this model: Composer.
       Available models: default, composer-2.5, ...

This test drives the executor exactly as that turn does — ``run_turn`` with
the switcher-persisted model override — against a stand-in ``cursor_sdk``
module that validates the model id the way the real SDK does (rejecting any
id outside the account catalog with the same ``invalid_argument`` shape).
The real bridge needs a ``crsr_`` API key and egress to Cursor's backend,
neither of which exists in CI, so catalog validation at the SDK boundary is
the closest faithful stand-in for the reported rejection.

The regression guard asserts the user-observable invariant: a turn on a
session whose model was picked in the switcher must not fail at agent
start, and the model actually handed to the Cursor SDK must be a valid
Cursor model id — never the raw display label. It FAILS on un-fixed code
(``_resolve_model`` passes ``"Composer"`` through verbatim and the SDK
rejects it) and passes once the switcher pick is normalized/validated to an
SDK id anywhere along the path (UI catalog, server validation, or executor
normalization).

Run::

    pytest tests/e2e/test_cursor_model_switcher_display_label_e2e.py -v

No Cursor credentials, bridge subprocess, or network access required.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.inner.cursor_executor import CursorExecutor
from omnigent.inner.executor import ExecutorError, Message, TurnComplete

# The catalog the real Cursor SDK advertised in the bug report's error
# message (abridged), plus the executor's own auto-select default. Any fix —
# UI mapping, server-side validation, or executor normalization — must land
# the SDK-bound model inside this set. Display names mirror the real
# ``AsyncClient.list_models()`` payload (``SDKModel.id`` / ``display_name``).
_CURSOR_MODELS: dict[str, str] = {
    "default": "Default",
    "auto-smart": "Auto",
    "composer-2.5": "Composer 2.5",
    "claude-opus-4-8": "Opus 4.8",
    "gpt-5.5": "GPT-5.5",
    "claude-sonnet-4-6": "Sonnet 4.6",
    "gpt-5.3-codex": "Codex 5.3",
    "gemini-3.1-pro": "Gemini 3.1 Pro",
    "claude-haiku-4-5": "Haiku 4.5",
}
_CURSOR_CATALOG: frozenset[str] = frozenset(_CURSOR_MODELS)

# The display label the model switcher persisted in the report.
_DISPLAY_LABEL = "Composer"


def _user(content: str, session_id: str = "conv-model-switch") -> Message:
    return {"role": "user", "content": content, "session_id": session_id}


def _install_catalog_validating_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Install a fake ``cursor_sdk`` that validates model ids like the real SDK.

    ``AsyncAgent.create`` raises the SDK's ``invalid_argument`` rejection for
    any model id outside :data:`_CURSOR_CATALOG` — the exact failure mode the
    report captured. A valid id yields an agent whose single turn streams one
    text chunk and finishes.
    """
    state: dict[str, Any] = {"create_models": []}

    class _FakeRun:
        async def events(self) -> Any:
            yield SimpleNamespace(
                sdk_message=SimpleNamespace(
                    type="assistant",
                    message=SimpleNamespace(
                        content=[SimpleNamespace(type="text", text="I am composer-2.5.")]
                    ),
                ),
                interaction_update=None,
            )

        async def cancel(self) -> None:
            pass

        async def wait(self) -> Any:
            return SimpleNamespace(status="finished", result="I am composer-2.5.")

    class _FakeAgent:
        async def send(self, prompt: str, **kwargs: Any) -> _FakeRun:
            return _FakeRun()

        async def close(self) -> None:
            pass

    class _FakeClient:
        @classmethod
        async def launch_bridge(cls, **kwargs: Any) -> "_FakeClient":
            return cls()

        async def list_models(self, *, api_key: Any = None) -> list[Any]:
            # Mirrors the real SDK's account-catalog listing (the same data
            # the backend's invalid_argument error points at).
            return [
                SimpleNamespace(id=mid, display_name=label)
                for mid, label in _CURSOR_MODELS.items()
            ]

        async def aclose(self) -> None:
            pass

    class _FakeAsyncAgent:
        @classmethod
        async def create(
            cls, *, client: Any, model: Any, api_key: Any, name: Any, local: Any
        ) -> _FakeAgent:
            state["create_models"].append(model)
            if model not in _CURSOR_CATALOG:
                available = ", ".join(sorted(_CURSOR_CATALOG))
                raise ValueError(
                    f"invalid_argument: Cannot use this model: {model}. "
                    f"Available models: {available}. "
                    "Use Cursor.models.list() to discover valid selections."
                )
            return _FakeAgent()

    class _FakeCustomTool:
        def __init__(
            self, execute: Any, description: Any = None, input_schema: Any = None
        ) -> None:
            self.execute = execute
            self.description = description
            self.input_schema = input_schema

    class _FakeLocalAgentOptions:
        def __init__(
            self, cwd: Any = None, custom_tools: Any = None, auto_review: Any = None, **_kw: Any
        ) -> None:
            self.cwd = cwd
            self.custom_tools = custom_tools
            self.auto_review = auto_review

    class _FakeSendOptions:
        def __init__(self, on_delta: Any = None, **_kw: Any) -> None:
            self.on_delta = on_delta

    fake = types.ModuleType("cursor_sdk")
    fake.AsyncClient = _FakeClient  # type: ignore[attr-defined]
    fake.AsyncAgent = _FakeAsyncAgent  # type: ignore[attr-defined]
    fake.CustomTool = _FakeCustomTool  # type: ignore[attr-defined]
    fake.LocalAgentOptions = _FakeLocalAgentOptions  # type: ignore[attr-defined]
    fake.SendOptions = _FakeSendOptions  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cursor_sdk", fake)
    return state


async def test_switcher_display_label_turn_survives_and_sdk_gets_valid_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn after picking ``Composer`` in the switcher must not die at agent start.

    The switcher pick is persisted as the session's model override and reaches
    the executor as its model (``HARNESS_CURSOR_MODEL`` → ``CursorExecutor``'s
    ``model=``). On un-fixed code ``_resolve_model`` forwards the display label
    verbatim, the SDK rejects it, and the user's turn yields the report's
    ``Failed to start cursor-sdk agent: invalid_argument: Cannot use this
    model: Composer`` error instead of an answer.
    """
    state = _install_catalog_validating_sdk(monkeypatch)
    executor = CursorExecutor(api_key="crsr_test", model=_DISPLAY_LABEL)
    try:
        events = [
            e async for e in executor.run_turn([_user("what model are you?")], [], "SYS")
        ]
    finally:
        await executor.close()

    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert not errors, (
        "the user's turn failed at cursor-sdk agent start — the switcher's "
        f"display label reached the SDK unmapped: {errors[0].message!r}"
    )
    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert completes, "the turn never completed"

    # The model actually handed to the Cursor SDK must be a real SDK id, never
    # the raw display label the switcher stored.
    assert state["create_models"], "the cursor-sdk agent was never created"
    sdk_model = state["create_models"][0]
    assert sdk_model != _DISPLAY_LABEL, (
        f"display label {_DISPLAY_LABEL!r} was passed to cursor_sdk.AsyncAgent.create verbatim"
    )
    assert sdk_model in _CURSOR_CATALOG, (
        f"model {sdk_model!r} handed to the Cursor SDK is not a valid Cursor model id"
    )


async def test_valid_sdk_id_recovers_after_failed_display_label_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching to ``composer-2.5`` after the failed pick must recover cleanly.

    The report's step 5–6: after the ``Composer`` failure the user switches to
    the valid id and sends another prompt in the same chat. That follow-up turn
    must complete without a cursor-sdk error. (The failed start must not wedge
    the session's executor state.)
    """
    state = _install_catalog_validating_sdk(monkeypatch)
    executor = CursorExecutor(api_key="crsr_test", model=_DISPLAY_LABEL)
    try:
        first = [
            e async for e in executor.run_turn([_user("what model are you?")], [], "SYS")
        ]
        # The user switches the session's model to the valid SDK id; the next
        # turn reaches the executor with the new override.
        executor._model_override = "composer-2.5"  # noqa: SLF001 — journey: switcher re-pick
        second = [
            e async for e in executor.run_turn([_user("and now?")], [], "SYS")
        ]
    finally:
        await executor.close()

    # First facet (asserted independently above) may or may not error here;
    # this test guards the recovery: the follow-up turn on a VALID id works.
    second_errors = [e for e in second if isinstance(e, ExecutorError)]
    assert not second_errors, (
        "the follow-up turn on valid id 'composer-2.5' still failed: "
        f"{second_errors[0].message!r}"
    )
    assert any(isinstance(e, TurnComplete) for e in second), (
        "the follow-up turn on 'composer-2.5' never completed"
    )
    assert state["create_models"][-1] == "composer-2.5"
