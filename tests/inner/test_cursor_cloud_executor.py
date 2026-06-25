"""Tests for :class:`omnigent.inner.cursor_cloud_executor.CursorCloudExecutor`.

The cursor-cloud harness launches a Cursor Cloud / Background Agent run over the
``cursor-sdk`` ``AsyncClient``: it creates a cloud agent that clones a GitHub
repo, works in a fresh VM, and pushes a branch / opens a PR. The SDK is replaced
with an injected fake module (no live cloud call, API key, or network), letting
us exercise the happy path (streamed text + PR-link composition), the
branch-only result, the missing-repo guard, launch failure with the onboarding
hint, and the terminal-status branches (error / cancelled). Live coverage lives
in the gated e2e test.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.inner.cursor_cloud_executor import (
    CursorCloudExecutor,
    _onboarding_hint,
    _resolve_cloud_model,
)
from omnigent.inner.executor import (
    ExecutorError,
    Message,
    ReasoningChunk,
    TextChunk,
    ToolCallRequest,
    TurnCancelled,
    TurnComplete,
)


def _user(content: str, session_id: str = "conv1") -> Message:
    return {"role": "user", "content": content, "session_id": session_id}


def _assistant(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="assistant",
        message=SimpleNamespace(content=[SimpleNamespace(type="text", text=text)]),
    )


def _thinking(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="thinking", text=text)


def _tool(
    name: str, call_id: str, status: str, args: Any = None, result: Any = None
) -> SimpleNamespace:
    return SimpleNamespace(
        type="tool_call", name=name, call_id=call_id, status=status, args=args, result=result
    )


def _branch(*, repo_url: str, branch: str, pr_url: str | None) -> SimpleNamespace:
    return SimpleNamespace(repo_url=repo_url, branch=branch, pr_url=pr_url)


# ---------------------------------------------------------------------------
# Fake cursor_sdk
# ---------------------------------------------------------------------------


def _install_fake_sdk(
    monkeypatch: pytest.MonkeyPatch,
    *,
    messages: list[Any] | None = None,
    interaction_updates: list[Any] | None = None,
    status: str = "finished",
    result_text: str = "",
    git: Any = None,
    create_exc: Exception | None = None,
) -> dict[str, Any]:
    """Install a fake ``cursor_sdk`` module and return a capture dict.

    *messages* is the SDK message stream replayed by ``run.events()``; *git*
    becomes ``RunResult.git`` (with ``.branches``). *create_exc* makes
    ``client.create_agent`` raise, exercising the launch-failure path.
    """
    state: dict[str, Any] = {
        "launch_bridge_calls": 0,
        "create_kwargs": [],
        "cloud_options": [],
        "repos": [],
        "sent": [],
        "closed": 0,
    }

    class _FakeRun:
        async def events(self) -> Any:
            for message in messages or []:
                yield SimpleNamespace(sdk_message=message, interaction_update=None)
            for iu in interaction_updates or []:
                yield SimpleNamespace(sdk_message=None, interaction_update=iu)

        async def wait(self) -> Any:
            return SimpleNamespace(status=status, result=result_text, git=git)

    class _FakeAgent:
        async def send(self, prompt: str) -> _FakeRun:
            state["sent"].append(prompt)
            return _FakeRun()

    class _FakeClient:
        @classmethod
        async def launch_bridge(cls) -> _FakeClient:
            # Cloud routes through the SDK's bundled bridge, the same entry the
            # local cursor harness uses (not a direct AsyncClient(base_url=...)).
            state["launch_bridge_calls"] += 1
            return cls()

        async def create_agent(
            self, *, model: Any, api_key: Any, name: Any, cloud: Any
        ) -> _FakeAgent:
            state["create_kwargs"].append(
                {"model": model, "api_key": api_key, "name": name, "cloud": cloud}
            )
            if create_exc is not None:
                raise create_exc
            return _FakeAgent()

        async def aclose(self) -> None:
            state["closed"] += 1

    class _FakeCloudRepository:
        def __init__(self, *, url: Any = None, starting_ref: Any = None) -> None:
            self.url = url
            self.starting_ref = starting_ref
            state["repos"].append(self)

    class _FakeCloudAgentOptions:
        def __init__(self, *, repos: Any = None, auto_create_pr: Any = None) -> None:
            self.repos = repos
            self.auto_create_pr = auto_create_pr
            state["cloud_options"].append(self)

    fake = types.ModuleType("cursor_sdk")
    fake.AsyncClient = _FakeClient  # type: ignore[attr-defined]
    fake.CloudAgentOptions = _FakeCloudAgentOptions  # type: ignore[attr-defined]
    fake.CloudRepository = _FakeCloudRepository  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cursor_sdk", fake)
    return state


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_resolve_cloud_model_defaults_and_drops_databricks() -> None:
    assert _resolve_cloud_model("claude-4.6-sonnet-thinking") == "claude-4.6-sonnet-thinking"
    assert _resolve_cloud_model(None) == "composer-2.5"
    assert _resolve_cloud_model("databricks-claude-sonnet-4-6") == "composer-2.5"
    assert _resolve_cloud_model("databricks/kimi") == "composer-2.5"


def test_onboarding_hint_appends_url_for_setup_errors() -> None:
    hint = _onboarding_hint("https://github.com/org/repo", "repository not found")
    assert "cursor.com/onboard?repository=https://github.com/org/repo" in hint


def test_onboarding_hint_passthrough_for_unrelated_errors() -> None:
    # An error with none of the setup/repo tokens is returned unchanged.
    assert _onboarding_hint("https://github.com/org/repo", "timeout") == "timeout"


# ---------------------------------------------------------------------------
# capability flags
# ---------------------------------------------------------------------------


def test_capabilities() -> None:
    executor = CursorCloudExecutor(repo_url="https://github.com/org/repo")
    assert executor.supports_streaming() is True
    assert executor.supports_tool_calling() is True
    # Tools execute in the cloud VM, so the Session must not re-dispatch.
    assert executor.handles_tools_internally() is True
    assert executor.supports_live_message_queue() is False


# ---------------------------------------------------------------------------
# run_turn — happy paths
# ---------------------------------------------------------------------------


async def test_run_turn_happy_path_appends_pr_url(monkeypatch: pytest.MonkeyPatch) -> None:
    git = SimpleNamespace(
        branches=[
            _branch(
                repo_url="https://github.com/org/repo",
                branch="cursor/fix-thing",
                pr_url="https://github.com/org/repo/pull/42",
            )
        ]
    )
    state = _install_fake_sdk(
        monkeypatch,
        messages=[_assistant("Fixed "), _assistant("the bug.")],
        status="finished",
        result_text="Fixed the bug.",
        git=git,
    )
    executor = CursorCloudExecutor(
        repo_url="https://github.com/org/repo", ref="main", api_key="crsr_x"
    )
    events = [e async for e in executor.run_turn([_user("fix it")], [], "SYS")]

    assert [e.text for e in events if isinstance(e, TextChunk)] == ["Fixed ", "the bug."]
    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert len(completes) == 1
    response = completes[0].response
    assert "Fixed the bug." in response
    assert "https://github.com/org/repo/pull/42" in response
    # The prompt sent to agent.send is the first-turn prompt: the system prompt
    # prepended to the user message (see _build_cursor_prompt).
    assert state["sent"] == ["SYS\n\nfix it"]
    # Cloud client came from the SDK bridge (launch_bridge), the API key was
    # threaded to create_agent, and the bridge was torn down.
    assert state["launch_bridge_calls"] == 1
    assert state["create_kwargs"][0]["api_key"] == "crsr_x"
    assert state["closed"] == 1
    # The repo URL + starting ref were threaded into CloudRepository.
    assert state["repos"][0].url == "https://github.com/org/repo"
    assert state["repos"][0].starting_ref == "main"


async def test_run_turn_branch_only_notes_no_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    git = SimpleNamespace(
        branches=[
            _branch(
                repo_url="https://github.com/org/repo",
                branch="cursor/wip",
                pr_url=None,
            )
        ]
    )
    _install_fake_sdk(
        monkeypatch,
        messages=[_assistant("Pushed work.")],
        status="finished",
        result_text="Pushed work.",
        git=git,
    )
    executor = CursorCloudExecutor(repo_url="https://github.com/org/repo", api_key="crsr_x")
    events = [e async for e in executor.run_turn([_user("do it")], [], "SYS")]

    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert len(completes) == 1
    response = completes[0].response
    assert "cursor/wip" in response
    assert "no PR opened" in response


async def test_run_turn_maps_thinking_and_tool_call_stream_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared SDK->ExecutorEvent mapping surfaces ``thinking`` as a
    ReasoningChunk and a running ``tool_call`` as a ToolCallRequest (tools run in
    the cloud VM, so they are informational, not bridged)."""
    _install_fake_sdk(
        monkeypatch,
        messages=[
            _thinking("planning the fix"),
            _tool("Read", "t1", "running", args={"path": "f.py"}),
            _assistant("done"),
        ],
        status="finished",
        result_text="done",
        git=SimpleNamespace(branches=[]),
    )
    executor = CursorCloudExecutor(repo_url="https://github.com/org/repo", api_key="crsr_x")
    events = [e async for e in executor.run_turn([_user("fix it")], [], "SYS")]

    reasoning = [e for e in events if isinstance(e, ReasoningChunk)]
    assert len(reasoning) == 1 and reasoning[0].delta == "planning the fix"

    reqs = [e for e in events if isinstance(e, ToolCallRequest)]
    assert len(reqs) == 1
    assert reqs[0].name == "Read"
    assert reqs[0].args == {"path": "f.py"}

    assert any(isinstance(e, TurnComplete) for e in events)


async def test_run_turn_turn_ended_update_carries_normalized_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream event whose ``interaction_update`` is a ``turn-ended`` with a usage
    dict produces a TurnComplete carrying the normalized usage."""
    turn_ended = SimpleNamespace(
        type="turn-ended",
        usage={"inputTokens": 1000, "outputTokens": 200, "totalTokens": 1200},
    )
    _install_fake_sdk(
        monkeypatch,
        messages=[_assistant("done")],
        interaction_updates=[turn_ended],
        status="finished",
        result_text="done",
        git=SimpleNamespace(branches=[]),
    )
    executor = CursorCloudExecutor(repo_url="https://github.com/org/repo", api_key="crsr_x")
    events = [e async for e in executor.run_turn([_user("fix it")], [], "SYS")]

    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert len(completes) == 1
    usage = completes[0].usage
    assert usage is not None
    assert usage["input_tokens"] == 1000
    assert usage["output_tokens"] == 200
    assert usage["total_tokens"] == 1200
    # Resolved cloud default model is recorded on the usage dict.
    assert usage["model"] == "composer-2.5"


async def test_run_turn_auto_create_pr_threaded(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_fake_sdk(
        monkeypatch,
        messages=[_assistant("ok")],
        result_text="ok",
        git=SimpleNamespace(branches=[]),
    )
    executor = CursorCloudExecutor(
        repo_url="https://github.com/org/repo", api_key="crsr_x", auto_create_pr=False
    )
    _ = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    assert state["cloud_options"][0].auto_create_pr is False
    # The agent name + resolved model are threaded into create_agent.
    assert state["create_kwargs"][0]["model"] == "composer-2.5"


# ---------------------------------------------------------------------------
# run_turn — guard + failure paths
# ---------------------------------------------------------------------------


async def test_run_turn_missing_repo_errors_before_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    # No fake SDK installed — the repo guard must fire before any import/use.
    executor = CursorCloudExecutor(repo_url=None, api_key="crsr_x")
    events = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert len(errors) == 1
    assert "no repository" in errors[0].message.lower()
    assert not any(isinstance(e, TurnComplete) for e in events)


async def test_run_turn_empty_prompt_completes_without_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_fake_sdk(monkeypatch, messages=[_assistant("x")])
    executor = CursorCloudExecutor(repo_url="https://github.com/org/repo", api_key="crsr_x")
    events = [
        e
        async for e in executor.run_turn(
            [{"role": "assistant", "content": "x", "session_id": "conv1"}], [], ""
        )
    ]
    assert len(events) == 1
    assert isinstance(events[0], TurnComplete) and events[0].response is None
    assert state["sent"] == []  # nothing launched


async def test_run_turn_launch_failure_includes_onboarding_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_sdk(
        monkeypatch,
        create_exc=RuntimeError("repository not found"),
    )
    executor = CursorCloudExecutor(repo_url="https://github.com/org/repo", api_key="crsr_x")
    events = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert len(errors) == 1
    assert errors[0].retryable is False
    assert "launch failed" in errors[0].message
    # The onboarding URL is appended because the error looks like a repo/setup issue.
    assert "cursor.com/onboard?repository=https://github.com/org/repo" in errors[0].message


async def test_run_turn_error_status_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_sdk(
        monkeypatch,
        messages=[_assistant("partial")],
        status="error",
        result_text="model exploded",
        git=SimpleNamespace(branches=[]),
    )
    executor = CursorCloudExecutor(repo_url="https://github.com/org/repo", api_key="crsr_x")
    events = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert len(errors) == 1
    assert errors[0].retryable is True
    assert "model exploded" in errors[0].message
    assert not any(isinstance(e, TurnComplete) for e in events)


async def test_run_turn_cancelled_status_emits_turn_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_sdk(
        monkeypatch,
        messages=[_assistant("partial")],
        status="cancelled",
        result_text="stopped",
        git=SimpleNamespace(branches=[]),
    )
    executor = CursorCloudExecutor(repo_url="https://github.com/org/repo", api_key="crsr_x")
    events = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    cancels = [e for e in events if isinstance(e, TurnCancelled)]
    assert len(cancels) == 1
    assert not any(isinstance(e, ExecutorError) for e in events)
    assert not any(isinstance(e, TurnComplete) for e in events)


async def test_run_turn_expired_status_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_sdk(
        monkeypatch,
        messages=[_assistant("partial")],
        status="expired",
        result_text="quota hit",
        git=SimpleNamespace(branches=[]),
    )
    executor = CursorCloudExecutor(repo_url="https://github.com/org/repo", api_key="crsr_x")
    events = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert len(errors) == 1 and errors[0].retryable is True
    assert "expired" in errors[0].message
    assert not any(isinstance(e, TurnComplete) for e in events)


async def test_run_turn_lazy_imports_cursor_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SDK import is lazy (inside run_turn): a missing cursor_sdk surfaces as
    a turn-time ExecutorError, not an import-time crash at construction."""
    monkeypatch.setitem(sys.modules, "cursor_sdk", None)
    executor = CursorCloudExecutor(repo_url="https://github.com/org/repo", api_key="crsr_x")
    events = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert len(errors) == 1 and "cursor-sdk" in errors[0].message


async def test_interrupt_session_unsupported_in_v1() -> None:
    executor = CursorCloudExecutor(repo_url="https://github.com/org/repo")
    assert await executor.interrupt_session("conv1") is False
