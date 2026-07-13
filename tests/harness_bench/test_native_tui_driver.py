"""Offline tests for the native-tui driver's tool/policy observation.

Network-free: a fake HTTP client feeds the driver canned session items (the
``function_call`` a native tool call persists as) and a canned SSE stream (the
policy and elicitation events native tool-call policies publish). This exercises
the tool and policy turns without a server, host daemon, or vendor CLI.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from tests.harness_bench.driver import TurnResult
from tests.harness_bench.native_tui_driver import NativeTuiDriver, native_vendor
from tests.harness_bench.probes.omnigent_mcp import OmnigentMcpProbe
from tests.harness_bench.probes.policy_allow import PolicyAllowProbe
from tests.harness_bench.probes.policy_ask import PolicyAskProbe
from tests.harness_bench.probes.policy_deny import PolicyDenyProbe
from tests.harness_bench.probes.tool_calling import ToolCallingProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.verdict import Verdict


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        pass


class _FakeStream:
    """Context manager yielding canned SSE lines via iter_lines."""

    def __init__(self, frames: list[str | dict], *, tail_delay: float = 0.0) -> None:
        self._lines = []
        self._tail_delay = tail_delay
        for frame in frames:
            if isinstance(frame, str):
                self._lines.append(f"event: {frame}")
            else:
                self._lines.extend([f"event: {frame['type']}", f"data: {json.dumps(frame)}"])

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def iter_lines(self):
        yield from self._lines
        if self._tail_delay:
            time.sleep(self._tail_delay)


class _FakeClient:
    """A minimal stand-in for the driver's httpx.Client.

    - ``GET .../items`` returns empty on the first call (the pre-turn baseline),
      then ``items`` (the function_call records the turn produced) — mirroring
      real timing where the tool item persists only after the turn runs.
    - ``GET .../stream`` yields ``stream_events`` as SSE ``event:`` lines.
    - ``POST .../policies`` records the attach and returns ``policy_status``.
    - other POSTs (the message post) are no-ops.
    """

    def __init__(
        self,
        *,
        items: list[dict] | None = None,
        stream_events: list[str | dict] | None = None,
        stream_tail_delay: float = 0.0,
        policy_status: int = 200,
    ) -> None:
        self._items = items or []
        self._stream_events = stream_events or [
            "response.output_item.done",
        ]
        self._stream_tail_delay = stream_tail_delay
        self._policy_status = policy_status
        self.attached_policies: list[dict] = []
        self.deleted_policies: list[str] = []
        self.posted_events: list[dict] = []
        self._items_calls = 0
        self.patched_sessions: list[tuple[str, dict]] = []

    def get(self, url: str, params: dict | None = None, timeout: float | None = None):
        if url.endswith("/items"):
            if "/conv_fork/" in url:
                return _FakeResponse(200, {"data": self._items})
            self._items_calls += 1
            data = [] if self._items_calls == 1 else self._items
            return _FakeResponse(200, {"data": data})
        return _FakeResponse(200, {})

    def post(self, url: str, json: dict | None = None, timeout: float | None = None):
        if url.endswith("/fork"):
            return _FakeResponse(201, {"id": "conv_fork"})
        if url.endswith("/policies"):
            self.attached_policies.append(json or {})
            return _FakeResponse(self._policy_status, {"id": "spol_test"})
        if url.endswith("/events"):
            self.posted_events.append(json or {})
        return _FakeResponse(202, {})

    def patch(self, url: str, json: dict | None = None):
        self.patched_sessions.append((url, json or {}))
        return _FakeResponse(200, {})

    def delete(self, url: str, timeout: float | None = None):
        self.deleted_policies.append(url.rsplit("/", 1)[-1])
        return _FakeResponse(200, {"deleted": True})

    def stream(self, method: str, url: str, timeout: float | None = None):
        return _FakeStream(self._stream_events, tail_delay=self._stream_tail_delay)


def _driver_with_fake(harness: str, client: _FakeClient) -> NativeTuiDriver:
    profile = BenchProfile(
        harness=harness,
        model="m",
        env_prefix="HARNESS_X_",
        marker="X",
        transport="native-tui",
    )
    driver = NativeTuiDriver(profile, databricks_profile="oss")
    driver._client = client  # type: ignore[assignment]
    driver._session_id = "conv_test"
    return driver


def _function_call_item(name: str) -> dict:
    return {"type": "function_call", "data": {"call_id": "c1", "name": name, "arguments": "{}"}}


def test_tool_turn_observes_function_call_item() -> None:
    """deny=False: a new function_call item populates result.tool_calls."""
    client = _FakeClient(items=[_function_call_item("Bash")])
    driver = _driver_with_fake("claude-native", client)

    result = driver._drive_tool_turn(deny=False)

    assert [tc["name"] for tc in result.tool_calls] == ["Bash"]
    assert result.completed
    assert not result.tool_call_denied
    assert client.attached_policies == []


def test_tool_turn_deny_attaches_policy_and_observes_denied_event() -> None:
    """deny=True: attaches a CEL deny and sets tool_call_denied on the stream event."""
    client = _FakeClient(
        items=[],  # the blocked tool never runs, so no function_call item persists
        stream_events=["response.policy_denied", "response.output_item.done"],
    )
    driver = _driver_with_fake("claude-native", client)

    result = driver._drive_tool_turn(deny=True)

    assert result.tool_call_denied
    assert len(client.attached_policies) == 1
    attached = client.attached_policies[0]
    assert attached["handler"] == "omnigent.policies.builtins.cel.cel_policy"
    expr = attached["factory_params"]["expression"]
    assert 'event.type == "tool_call"' in expr
    assert '"result": "DENY"' in expr
    assert client.deleted_policies == ["spol_test"]


def test_tool_turn_deny_observes_denied_event_after_terminal() -> None:
    """A policy_denied that lands AFTER output_item.done is still caught.

    On a live deny turn the tool can run anyway (vendor doesn't enforce), so the
    turn's output_item.done arrives first and response.policy_denied lands just
    after. The reader must keep watching past the terminal event on a deny turn
    (the grace window), not stop on output_item.done and miss the deny.
    """
    client = _FakeClient(
        items=[_function_call_item("Bash")],  # tool ran (vendor didn't enforce)
        stream_events=[
            "response.output_item.done",  # terminal — but not the end on a deny turn
            "session.heartbeat",
            "response.policy_denied",  # lands just after
        ],
    )
    driver = _driver_with_fake("claude-native", client)

    result = driver._drive_tool_turn(deny=True)

    assert result.tool_call_denied  # caught despite arriving after the terminal event


def test_tool_turn_deny_skips_when_policy_enforcement_inactive() -> None:
    """Fail-open (policy hook disabled) -> SKIP, never a false UNSUPPORTED."""
    client = _FakeClient()
    driver = _driver_with_fake("claude-native", client)
    driver._policy_hook_disabled_reason = "Codex CLI too old"

    result = driver._drive_tool_turn(deny=True)

    assert result.error and "inactive" in result.error
    assert not result.tool_call_denied
    assert not result.tool_calls
    assert client.attached_policies == []


def test_tool_turn_deny_skips_when_cel_handler_unregistered() -> None:
    """POST /policies rejecting the CEL handler (env gap) -> SKIP."""
    client = _FakeClient(policy_status=400)
    driver = _driver_with_fake("claude-native", client)

    result = driver._drive_tool_turn(deny=True)

    assert result.error and "deny policy" in result.error
    assert not result.tool_call_denied


def test_policy_allow_attaches_policy_and_observes_tool_call() -> None:
    client = _FakeClient(items=[_function_call_item("Bash")])
    driver = _driver_with_fake("claude-native", client)

    result = driver._drive_policy_turn(action="allow")

    assert result.tool_call_allowed
    assert [tc["name"] for tc in result.tool_calls] == ["Bash"]
    assert '"result": "ALLOW"' in client.attached_policies[0]["factory_params"]["expression"]
    assert client.deleted_policies == ["spol_test"]


def test_policy_allow_reader_stops_on_terminal_event() -> None:
    client = _FakeClient(
        items=[_function_call_item("Bash")],
        stream_events=["response.output_item.done"],
        stream_tail_delay=1.0,
    )
    driver = _driver_with_fake("claude-native", client)

    started = time.monotonic()
    result = driver._drive_policy_turn(action="allow")

    assert result.tool_call_allowed
    assert time.monotonic() - started < 0.5


def test_policy_ask_observes_and_resolves_elicitation() -> None:
    client = _FakeClient(
        stream_events=[
            {
                "type": "response.elicitation_request",
                "elicitation_id": "elicit_test",
                "params": {},
            }
        ]
    )
    driver = _driver_with_fake("claude-native", client)

    result = driver._drive_policy_turn(action="ask")

    assert result.elicitation_requested
    assert '"result": "ASK"' in client.attached_policies[0]["factory_params"]["expression"]
    assert client.posted_events[-1] == {
        "type": "approval",
        "data": {"elicitation_id": "elicit_test", "action": "accept"},
    }
    assert client.deleted_policies == ["spol_test"]


def test_policy_turn_skips_when_policy_enforcement_inactive() -> None:
    client = _FakeClient()
    driver = _driver_with_fake("claude-native", client)
    driver._policy_hook_disabled_reason = "Codex CLI too old"

    result = driver._drive_policy_turn(action="allow")

    assert result.error and "inactive" in result.error
    assert client.attached_policies == []


def test_policy_turn_skips_when_cel_handler_unregistered() -> None:
    client = _FakeClient(policy_status=400)
    driver = _driver_with_fake("claude-native", client)

    result = driver._drive_policy_turn(action="ask")

    assert result.error and "ask policy" in result.error
    assert not result.elicitation_requested


def test_policy_turn_rejects_unknown_action() -> None:
    driver = _driver_with_fake("claude-native", _FakeClient())

    with pytest.raises(ValueError, match="unsupported native policy action"):
        driver._drive_policy_turn(action="defer")


def test_tool_turn_skips_vendor_without_tool_mapping() -> None:
    """A native with no tool-provocation entry (e.g. cursor) SKIPs cleanly."""
    client = _FakeClient()
    driver = _driver_with_fake("cursor-native", client)
    assert native_vendor("cursor-native").tool_name == ""  # precondition

    result = driver._drive_tool_turn(deny=False)

    assert result.error and "no tool-provocation" in result.error
    assert not result.tool_calls


def test_mcp_tool_turn_observes_prefixed_omnigent_tool() -> None:
    client = _FakeClient(items=[_function_call_item("mcp__omnigent__sys_session_list")])
    driver = _driver_with_fake("claude-native", client)

    result = driver._drive_mcp_tool_turn()

    assert result.completed
    assert [call["name"] for call in result.tool_calls] == ["mcp__omnigent__sys_session_list"]
    assert "mcp__omnigent__sys_session_list" in str(client.posted_events[-1])


def test_mcp_tool_turn_rejects_unrelated_suffix_match() -> None:
    client = _FakeClient(items=[_function_call_item("other_sys_session_list")])
    driver = _driver_with_fake("claude-native", client)

    result = driver._drive_mcp_tool_turn(timeout=0.01)

    assert not result.completed
    assert [call["name"] for call in result.tool_calls] == ["other_sys_session_list"]


def test_mcp_tool_turn_retries_when_first_prompt_has_no_call() -> None:
    client = _FakeClient(items=[])
    driver = _driver_with_fake("claude-native", client)

    result = driver._drive_mcp_tool_turn(timeout=0.01)

    assert not result.completed
    messages = [event for event in client.posted_events if event.get("type") == "message"]
    assert len(messages) == 2
    assert "empty argument object" in str(messages[-1])


def test_mcp_tool_turn_advances_baseline_after_wrong_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient()
    driver = _driver_with_fake("claude-native", client)
    baselines: list[int] = []

    def _poll(baseline: int, result: TurnResult, timeout: float) -> None:
        baselines.append(baseline)
        name = "Bash" if len(baselines) == 1 else "mcp__omnigent__sys_session_list"
        result.tool_calls.append({"name": name})

    monkeypatch.setattr(driver, "_poll_new_tool_calls", _poll)

    result = driver._drive_mcp_tool_turn(timeout=0.01)

    assert result.completed
    assert baselines == [0, 1]


def test_mcp_tool_turn_skips_non_mcp_native_relay() -> None:
    client = _FakeClient()
    driver = _driver_with_fake("pi-native", client)

    result = driver._drive_mcp_tool_turn()

    assert result.error and "no Omnigent MCP bridge" in result.error
    assert client.posted_events == []


def test_fork_turn_binds_clone_and_replays_copied_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "FORK_MARKER"
    copied_message = {
        "type": "message",
        "data": {
            "role": "user",
            "content": [{"type": "input_text", "text": marker}],
        },
    }
    client = _FakeClient(items=[copied_message])
    driver = _driver_with_fake("claude-native", client)
    driver._runner_id = "runner_test"
    seen_session_ids: list[str | None] = []

    def _recall(prompt: str) -> TurnResult:
        assert marker not in prompt
        seen_session_ids.append(driver._session_id)
        return TurnResult(completed=True, text=marker)

    monkeypatch.setattr(driver, "_drive_turn", _recall)

    result = driver._drive_fork_turn(marker)

    assert result.created and result.history_copied and result.recalled
    assert seen_session_ids == ["conv_fork"]
    assert client.patched_sessions == [("/v1/sessions/conv_fork", {"runner_id": "runner_test"})]
    assert driver._session_id == "conv_fork"


async def test_probes_read_native_tool_result_as_supported() -> None:
    """The transport-agnostic probes turn the native TurnResults into verdicts."""
    profile = BenchProfile(
        harness="claude-native",
        model="m",
        env_prefix="HARNESS_X_",
        marker="X",
        transport="native-tui",
    )

    class _Driver:
        async def run_tool_turn(self, *, deny: bool) -> TurnResult:
            if deny:
                return TurnResult(
                    completed=True, tool_calls=[{"name": "Bash"}], tool_call_denied=True
                )
            return TurnResult(completed=True, tool_calls=[{"name": "Bash"}])

        async def run_policy_turn(self, *, action: str) -> TurnResult:
            if action == "allow":
                return TurnResult(
                    completed=True,
                    tool_calls=[{"name": "Bash"}],
                    tool_call_allowed=True,
                )
            return TurnResult(elicitation_requested=True)

        async def run_mcp_tool_turn(self) -> TurnResult:
            return TurnResult(
                completed=True,
                tool_calls=[{"name": "mcp__omnigent__sys_session_list"}],
            )

    tool_result = await ToolCallingProbe().run(_Driver(), profile)
    assert tool_result.verdict is Verdict.SUPPORTED
    deny_result = await PolicyDenyProbe().run(_Driver(), profile)
    assert deny_result.verdict is Verdict.SUPPORTED
    allow_result = await PolicyAllowProbe().run(_Driver(), profile)
    assert allow_result.verdict is Verdict.SUPPORTED
    ask_result = await PolicyAskProbe().run(_Driver(), profile)
    assert ask_result.verdict is Verdict.SUPPORTED
    mcp_result = await OmnigentMcpProbe().run(_Driver(), profile)
    assert mcp_result.verdict is Verdict.SUPPORTED


def test_format_matches_server_wire_name() -> None:
    """The driver keys on the exact wire name the server publishes."""
    from omnigent.server.routes.sessions import _format_sse
    from tests.harness_bench.native_tui_driver import _POLICY_DENIED_EVENT

    sse = _format_sse(_POLICY_DENIED_EVENT, {"type": _POLICY_DENIED_EVENT})
    assert sse.startswith(f"event: {_POLICY_DENIED_EVENT}\n")
    assert json.loads(sse.split("data: ", 1)[1])["type"] == _POLICY_DENIED_EVENT
