"""A fake ``cursor_sdk`` for the Cursor executor tests."""

from __future__ import annotations

import asyncio
import os
import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest


def install_fake_cursor_sdk(
    monkeypatch: pytest.MonkeyPatch,
    scripts: list[dict[str, Any]] | None = None,
    *,
    create_exc: Exception | None = None,
) -> dict[str, Any]:
    """Install a fake ``cursor_sdk`` module and return a capture dict.

    *scripts* is one dict per ``agent.send`` — ``{messages: [...], status,
    result}``. ``create_exc`` makes ``AsyncAgent.create`` raise (after the
    bridge launches), to exercise the setup-failure path.
    """
    scripts = scripts if scripts is not None else []
    state: dict[str, Any] = {
        "create_models": [],
        "create_api_keys": [],
        "custom_tools": [],
        "custom_tool_results": [],
        "launch_kwargs": [],
        "launch_cwds": [],
        "sent": [],
        "closed": 0,
        "client_closed": 0,
        "agent_closed": 0,
    }

    class _FakeRun:
        def __init__(self, script: dict[str, Any]) -> None:
            self._script = script

        async def events(self) -> Any:
            for message in self._script.get("messages", []):
                yield SimpleNamespace(sdk_message=message, interaction_update=None)
            for iu in self._script.get("interaction_updates", []):
                yield SimpleNamespace(sdk_message=None, interaction_update=iu)

        async def cancel(self) -> None:
            pass  # no-op for tests

        async def wait(self) -> Any:
            return SimpleNamespace(
                status=self._script.get("status", "finished"),
                result=self._script.get("result", ""),
            )

    class _FakeAgent:
        def __init__(self, custom_tools: dict[str, Any]) -> None:
            self._custom_tools = custom_tools

        async def send(self, prompt: str, **kwargs: Any) -> _FakeRun:
            state["sent"].append(prompt)
            script = scripts.pop(0)
            # Invoke on_delta for interaction_updates (mirrors real SDK
            # which dispatches TurnEndedUpdate via on_delta, not events).
            options = kwargs.get("options")
            on_delta = getattr(options, "on_delta", None) if options else None
            if on_delta and "interaction_updates" in script:
                for iu in script["interaction_updates"]:
                    on_delta(iu)
            for call in script.get("custom_tool_calls", []):
                tool = self._custom_tools[call["name"]]
                result = await asyncio.to_thread(
                    tool.execute,
                    call.get("args", {}),
                    call.get("ctx"),
                )
                state["custom_tool_results"].append(result)
            return _FakeRun(script)

        # AsyncAgent exposes close() (a CloseAgent RPC + tool unregister).
        async def close(self) -> None:
            state["closed"] += 1
            state["agent_closed"] += 1

    class _FakeClient:
        @classmethod
        async def launch_bridge(cls, **kwargs: Any) -> _FakeClient:
            state["launch_kwargs"].append(kwargs)
            # Record the process cwd at spawn time: the real bridge subprocess
            # inherits it (the SDK spawns without a cwd=), so the executor must
            # have chdir'd to the workspace by now.
            state["launch_cwds"].append(os.getcwd())
            return cls()

        # The real AsyncClient exposes ONLY aclose() (no close()); it owns the
        # bridge subprocess + the daemon tool-callback server, both torn down
        # there. Deliberately no close() here so a regression that closes the
        # client via close() fails (AttributeError -> swallowed -> leak).
        async def aclose(self) -> None:
            state["closed"] += 1
            state["client_closed"] += 1

        async def list_models(self, *, api_key: Any = None) -> list[Any]:
            return [
                SimpleNamespace(id="auto-smart", display_name="Auto"),
                SimpleNamespace(id="composer-2.5", display_name="Composer 2.5"),
                SimpleNamespace(id="claude-opus-4-8", display_name="Opus 4.8"),
                SimpleNamespace(id="gpt-5.5", display_name="GPT-5.5"),
            ]

    class _FakeAsyncAgent:
        @classmethod
        async def create(
            cls, *, client: Any, model: Any, api_key: Any, name: Any, local: Any
        ) -> _FakeAgent:
            state["create_models"].append(model)
            state["create_api_keys"].append(api_key)
            state["custom_tools"].append(dict(local.custom_tools or {}))
            if create_exc is not None:
                raise create_exc
            return _FakeAgent(dict(local.custom_tools or {}))

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
            self.options = {"cwd": cwd, "auto_review": auto_review, **_kw}
            state.setdefault("local_options", []).append(self)

    class _FakeSendOptions:
        def __init__(self, on_delta: Any = None, **_kw: Any) -> None:
            self.on_delta = on_delta

    fake = types.ModuleType("cursor_sdk")
    fake.AsyncClient = _FakeClient  # type: ignore[attr-defined]
    fake.AsyncAgent = _FakeAsyncAgent  # type: ignore[attr-defined]
    fake.CustomTool = _FakeCustomTool  # type: ignore[attr-defined]
    fake.LocalAgentOptions = _FakeLocalAgentOptions  # type: ignore[attr-defined]
    fake.SendOptions = _FakeSendOptions  # type: ignore[attr-defined]
    fake.__path__ = []  # type: ignore[attr-defined]
    vendor = types.ModuleType("cursor_sdk._vendor")
    vendor.resolve_bridge_path = lambda: "/usr/bin/true"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cursor_sdk", fake)
    monkeypatch.setitem(sys.modules, "cursor_sdk._vendor", vendor)
    return state
