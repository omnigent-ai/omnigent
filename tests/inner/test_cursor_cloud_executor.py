"""Tests for :class:`omnigent.inner.cursor_cloud_executor.CursorCloudExecutor`.

The cursor-cloud harness launches a Cursor Cloud / Background Agent run over the
``cursor-sdk`` ``AsyncClient``: it creates a cloud agent that clones a GitHub
repo, works in a fresh VM, and pushes a branch / opens a PR. The SDK is replaced
with an injected fake module (no live cloud call, API key, or network), letting
us exercise the happy path (streamed text + PR-link composition), the
branch-only result, the missing-repo guard, launch failure with the onboarding
hint, the terminal-status branches (error / cancelled), follow-up reuse (the
same agent is reused across turns so a second message stays on one branch/PR),
and cancel (interrupt_session cancels the in-flight run). Live coverage lives
in the gated e2e test.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.cursor_cloud_repo import CursorCloudRepo
from omnigent.inner.cursor_cloud_executor import (
    CursorCloudExecutor,
    _CloudSessionState,
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
# Fake cursor_sdk.errors.UnsupportedRunOperationError (module-level so all
# tests share the same class — required for isinstance checks in the executor)
# ---------------------------------------------------------------------------


class _UnsupportedRunOperationError(Exception):
    """Stand-in for ``cursor_sdk.errors.UnsupportedRunOperationError``."""


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
    cancel_exc: Exception | None = None,
    wait_exc: Exception | None = None,
) -> dict[str, Any]:
    """Install a fake ``cursor_sdk`` module and return a capture dict.

    *messages* is the SDK message stream replayed by ``run.events()``; *git*
    becomes ``RunResult.git`` (with ``.branches``). *create_exc* makes
    ``client.create_agent`` raise, exercising the launch-failure path.
    *cancel_exc* makes ``run.cancel()`` raise, exercising the terminal-cancel
    path. *wait_exc* makes ``run.wait()`` raise, exercising the mid-run failure
    path. ``state["run_factory"]`` holds the ``_FakeRun`` class so tests can
    instantiate fake runs for direct active-run injection.
    """
    state: dict[str, Any] = {
        "launch_bridge_calls": 0,
        "create_kwargs": [],
        "cloud_options": [],
        "repos": [],
        "sent": [],
        "closed": 0,  # client.aclose() calls
        "agent_closed": 0,  # agent.close() calls
        "cancel_calls": 0,  # run.cancel() calls
        "wait_exc": wait_exc,
    }

    class _FakeRun:
        async def events(self) -> Any:
            for message in messages or []:
                yield SimpleNamespace(sdk_message=message, interaction_update=None)
            for iu in interaction_updates or []:
                yield SimpleNamespace(sdk_message=None, interaction_update=iu)

        async def wait(self) -> Any:
            if state["wait_exc"] is not None:
                raise state["wait_exc"]
            return SimpleNamespace(status=status, result=result_text, git=git)

        async def cancel(self) -> None:
            state["cancel_calls"] += 1
            if cancel_exc is not None:
                raise cancel_exc

    class _FakeAgent:
        async def send(self, prompt: str) -> _FakeRun:
            state["sent"].append(prompt)
            return _FakeRun()

        async def close(self) -> None:
            state["agent_closed"] += 1

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

    # cursor_sdk.errors submodule — uses the module-level error class so that
    # isinstance checks in the executor against the imported class work correctly.
    fake_errors = types.ModuleType("cursor_sdk.errors")
    fake_errors.UnsupportedRunOperationError = _UnsupportedRunOperationError  # type: ignore[attr-defined]

    fake = types.ModuleType("cursor_sdk")
    fake.AsyncClient = _FakeClient  # type: ignore[attr-defined]
    fake.CloudAgentOptions = _FakeCloudAgentOptions  # type: ignore[attr-defined]
    fake.CloudRepository = _FakeCloudRepository  # type: ignore[attr-defined]
    fake.errors = fake_errors  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "cursor_sdk", fake)
    monkeypatch.setitem(sys.modules, "cursor_sdk.errors", fake_errors)

    # Expose _FakeRun so tests can create instances for active-run injection.
    state["run_factory"] = _FakeRun

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
    # Cloud client came from the SDK bridge (launch_bridge) and the API key was
    # threaded to create_agent.  The agent + client persist for follow-ups (not
    # closed until close() is called).
    assert state["launch_bridge_calls"] == 1
    assert state["create_kwargs"][0]["api_key"] == "crsr_x"
    assert state["closed"] == 0
    assert state["agent_closed"] == 0
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


# ---------------------------------------------------------------------------
# follow-up: persistent agent across turns
# ---------------------------------------------------------------------------


async def test_follow_up_reuses_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Second turn reuses the same AsyncAgent (launch_bridge + create_agent called
    once) so both messages land on the same cloud branch / PR."""
    state = _install_fake_sdk(
        monkeypatch,
        messages=[_assistant("ok")],
        result_text="ok",
        git=SimpleNamespace(branches=[]),
    )
    executor = CursorCloudExecutor(repo_url="https://github.com/org/repo", api_key="crsr_x")

    msgs1 = [_user("first task", session_id="s1")]
    msgs2 = [
        _user("first task", session_id="s1"),
        {"role": "assistant", "content": "ok", "session_id": "s1"},
        _user("follow up", session_id="s1"),
    ]

    _ = [e async for e in executor.run_turn(msgs1, [], "SYS")]
    _ = [e async for e in executor.run_turn(msgs2, [], "SYS")]

    # Bridge launched and agent created exactly once.
    assert state["launch_bridge_calls"] == 1
    assert len(state["create_kwargs"]) == 1
    # Both turns reached agent.send.
    assert len(state["sent"]) == 2
    # The second prompt is only the latest user message (follow-up, not full history).
    assert state["sent"][1] == "follow up"
    # Agent + client still open (follow-up possible).
    assert state["agent_closed"] == 0
    assert state["closed"] == 0


async def test_first_turn_full_history_followup_bare_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First turn prepends system prompt; follow-up sends only the latest message."""
    state = _install_fake_sdk(
        monkeypatch,
        messages=[_assistant("done")],
        result_text="done",
        git=SimpleNamespace(branches=[]),
    )
    executor = CursorCloudExecutor(repo_url="https://github.com/org/repo", api_key="crsr_x")

    msgs1 = [_user("do the thing", session_id="s2")]
    msgs2 = [
        _user("do the thing", session_id="s2"),
        {"role": "assistant", "content": "done", "session_id": "s2"},
        _user("and this too", session_id="s2"),
    ]

    _ = [e async for e in executor.run_turn(msgs1, [], "SYSTEM")]
    _ = [e async for e in executor.run_turn(msgs2, [], "SYSTEM")]

    # First turn: system prompt + user message.
    assert state["sent"][0] == "SYSTEM\n\ndo the thing"
    # Second turn: only the latest user message (no system prompt, no history).
    assert state["sent"][1] == "and this too"


async def test_follow_up_mid_run_failure_keeps_agent_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed follow-up keeps the existing agent so retry stays on one branch."""
    state = _install_fake_sdk(
        monkeypatch,
        messages=[_assistant("ok")],
        result_text="ok",
        git=SimpleNamespace(branches=[]),
    )
    executor = CursorCloudExecutor(repo_url="https://github.com/org/repo", api_key="crsr_x")

    msgs1 = [_user("first task", session_id="s_keep")]
    msgs2 = [
        _user("first task", session_id="s_keep"),
        {"role": "assistant", "content": "ok", "session_id": "s_keep"},
        _user("follow up", session_id="s_keep"),
    ]
    msgs3 = [*msgs2, {"role": "assistant", "content": "retry", "session_id": "s_keep"}]
    msgs3.append(_user("follow up retry", session_id="s_keep"))

    _ = [e async for e in executor.run_turn(msgs1, [], "SYS")]
    state["wait_exc"] = RuntimeError("temporary cloud failure")
    events2 = [e async for e in executor.run_turn(msgs2, [], "SYS")]

    errors = [e for e in events2 if isinstance(e, ExecutorError)]
    assert len(errors) == 1
    assert errors[0].retryable is True
    assert "temporary cloud failure" in errors[0].message
    assert "s_keep" in executor._session_states
    assert state["agent_closed"] == 0
    assert state["closed"] == 0

    state["wait_exc"] = None
    events3 = [e async for e in executor.run_turn(msgs3, [], "SYS")]

    assert state["launch_bridge_calls"] == 1
    assert len(state["create_kwargs"]) == 1
    assert state["sent"][1] == "follow up"
    assert state["sent"][2] == "follow up retry"
    assert any(isinstance(e, TurnComplete) for e in events3)


# ---------------------------------------------------------------------------
# cancel: interrupt_session
# ---------------------------------------------------------------------------


async def test_cancel_active_run_returns_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """interrupt_session calls run.cancel() and returns True when a run is active."""
    state = _install_fake_sdk(monkeypatch, messages=[], git=SimpleNamespace(branches=[]))
    executor = CursorCloudExecutor(repo_url="https://github.com/org/repo")

    executor._session_states["conv1"] = _CloudSessionState(active_run=state["run_factory"]())

    result = await executor.interrupt_session("conv1")

    assert result is True
    assert state["cancel_calls"] == 1


async def test_cancel_terminal_run_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """interrupt_session returns False (no raise) when run.cancel() raises
    UnsupportedRunOperationError (the run already reached a terminal state)."""
    state = _install_fake_sdk(
        monkeypatch,
        messages=[],
        git=SimpleNamespace(branches=[]),
        cancel_exc=_UnsupportedRunOperationError("already finished"),
    )
    executor = CursorCloudExecutor(repo_url="https://github.com/org/repo")

    executor._session_states["conv1"] = _CloudSessionState(active_run=state["run_factory"]())

    result = await executor.interrupt_session("conv1")

    assert result is False
    assert state["cancel_calls"] == 1  # cancel was attempted


async def test_cancel_no_active_run_returns_false() -> None:
    """interrupt_session returns False immediately when no run is in flight."""
    executor = CursorCloudExecutor(repo_url="https://github.com/org/repo")
    executor._session_states["conv1"] = _CloudSessionState()
    assert await executor.interrupt_session("conv1") is False
    assert await executor.interrupt_session("unknown") is False


# ---------------------------------------------------------------------------
# close: close() tears down all sessions
# ---------------------------------------------------------------------------


async def test_close_closes_agent_and_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """close() calls close() on the agent and aclose() on the client for every
    open session."""
    state = _install_fake_sdk(
        monkeypatch,
        messages=[_assistant("ok")],
        result_text="ok",
        git=SimpleNamespace(branches=[]),
    )
    executor = CursorCloudExecutor(repo_url="https://github.com/org/repo", api_key="crsr_x")

    # Run one turn to create a persistent session.
    _ = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]
    assert state["agent_closed"] == 0
    assert state["closed"] == 0

    await executor.close()

    assert state["agent_closed"] == 1
    assert state["closed"] == 1
    # Session state is cleared.
    assert executor._session_states == {}


# ---------------------------------------------------------------------------
# multi-repo: extra_repos included in CloudAgentOptions
# ---------------------------------------------------------------------------


async def test_extra_repos_included_in_cloud_agent_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """extra_repos are appended to the primary repo in CloudAgentOptions.repos
    so a single cloud run can clone multiple repositories."""
    state = _install_fake_sdk(
        monkeypatch,
        messages=[_assistant("ok")],
        result_text="ok",
        git=SimpleNamespace(branches=[]),
    )
    executor = CursorCloudExecutor(
        repo_url="https://github.com/o/primary",
        api_key="crsr_x",
        extra_repos=[
            CursorCloudRepo(url="https://github.com/o/second", ref=None),
        ],
    )
    _ = [e async for e in executor.run_turn([_user("hi")], [], "SYS")]

    # Both repos were passed to CloudAgentOptions.repos in order.
    cloud = state["cloud_options"][0]
    assert len(cloud.repos) == 2
    assert cloud.repos[0].url == "https://github.com/o/primary"
    assert cloud.repos[1].url == "https://github.com/o/second"
    # state["repos"] accumulates every CloudRepository constructed — same 2.
    repo_urls = [r.url for r in state["repos"]]
    assert repo_urls == ["https://github.com/o/primary", "https://github.com/o/second"]


# ---------------------------------------------------------------------------
# mid-run failure: state teardown + retry correctness
# ---------------------------------------------------------------------------


async def test_mid_run_failure_tears_down_state_and_retries_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-run failure (run.wait() raises) must:
    (a) yield ExecutorError(retryable=True),
    (b) clear _session_states so the next turn starts a fresh bridge + agent,
    (c) send the full first-turn prompt on retry (system prompt present, not
        a bare follow-up stripped of history).
    """
    # Turn 1: run.wait() raises a mid-run error.
    _install_fake_sdk(
        monkeypatch,
        messages=[_assistant("partial")],
        git=SimpleNamespace(branches=[]),
        wait_exc=RuntimeError("stream error"),
    )
    executor = CursorCloudExecutor(repo_url="https://github.com/org/repo", api_key="crsr_x")
    msgs = [_user("do the work", session_id="s_retry")]

    events1 = [e async for e in executor.run_turn(msgs, [], "SYS")]
    errors = [e for e in events1 if isinstance(e, ExecutorError)]
    assert len(errors) == 1
    assert errors[0].retryable is True
    # Session must be torn down so the retry starts fresh.
    assert executor._session_states == {}

    # Turn 2 (retry): reinstall a healthy fake — different state dict so
    # launch_bridge_calls starts at 0 for this phase.
    state2 = _install_fake_sdk(
        monkeypatch,
        messages=[_assistant("done")],
        result_text="done",
        git=SimpleNamespace(branches=[]),
    )
    events2 = [e async for e in executor.run_turn(msgs, [], "SYS")]

    # A fresh bridge and agent must have been created.
    assert state2["launch_bridge_calls"] == 1
    assert len(state2["create_kwargs"]) == 1
    # The prompt must be a first-turn prompt (system prompt + user message),
    # not a bare follow-up, because is_first_turn was never committed.
    assert state2["sent"][0] == "SYS\n\ndo the work"
    assert any(isinstance(e, TurnComplete) for e in events2)
