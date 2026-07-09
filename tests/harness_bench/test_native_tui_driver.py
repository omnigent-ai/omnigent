"""Offline tests for the native-tui driver's tool/policy observation.

Network-free: a fake HTTP client feeds the driver canned session items (the
``function_call`` a native tool call persists as) and a canned SSE stream (the
``response.policy_denied`` a native tool-call DENY publishes). This exercises
``_drive_tool_turn`` and its helpers without a server, host daemon, or vendor
CLI. The live path is covered by the gated ``test_native_tui`` layer.
"""

from __future__ import annotations

import json
from typing import Any

from tests.harness_bench.driver import TurnResult
from tests.harness_bench.native_tui_driver import NativeTuiDriver, native_vendor
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
    """Context manager yielding canned SSE ``event:`` lines via iter_lines."""

    def __init__(self, event_names: list[str]) -> None:
        self._lines = [f"event: {name}" for name in event_names]

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def iter_lines(self):
        yield from self._lines


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
        stream_events: list[str] | None = None,
        policy_status: int = 200,
    ) -> None:
        self._items = items or []
        self._stream_events = stream_events or [
            "response.output_item.done",
        ]
        self._policy_status = policy_status
        self.attached_policies: list[dict] = []
        self._items_calls = 0

    def get(self, url: str, params: dict | None = None, timeout: float | None = None):
        if url.endswith("/items"):
            # First call is the pre-turn baseline (empty); later calls see the
            # item the turn produced.
            self._items_calls += 1
            data = [] if self._items_calls == 1 else self._items
            return _FakeResponse(200, {"data": data})
        return _FakeResponse(200, {})

    def post(self, url: str, json: dict | None = None, timeout: float | None = None):
        if url.endswith("/policies"):
            self.attached_policies.append(json or {})
            return _FakeResponse(self._policy_status, {"name": "bench_tool_deny"})
        return _FakeResponse(202, {})

    def stream(self, method: str, url: str, timeout: float | None = None):
        return _FakeStream(self._stream_events)


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
    # No deny policy is attached on the allow path.
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
    # The attached policy denies at the tool_call phase (name-agnostic, so it
    # blocks whatever tool the vendor calls regardless of its wire name).
    assert len(client.attached_policies) == 1
    attached = client.attached_policies[0]
    assert attached["handler"] == "omnigent.policies.builtins.cel.cel_policy"
    expr = attached["factory_params"]["expression"]
    assert 'event.type == "tool_call"' in expr
    assert '"result": "DENY"' in expr


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
    # No policy is attached when enforcement is known-inactive.
    assert client.attached_policies == []


def test_tool_turn_deny_skips_when_cel_handler_unregistered() -> None:
    """POST /policies rejecting the CEL handler (env gap) -> SKIP."""
    client = _FakeClient(policy_status=400)
    driver = _driver_with_fake("claude-native", client)

    result = driver._drive_tool_turn(deny=True)

    assert result.error and "deny policy" in result.error
    assert not result.tool_call_denied


def test_tool_turn_skips_vendor_without_tool_mapping() -> None:
    """A native with no tool-provocation entry (e.g. cursor) SKIPs cleanly."""
    client = _FakeClient()
    driver = _driver_with_fake("cursor-native", client)
    assert native_vendor("cursor-native").tool_name == ""  # precondition

    result = driver._drive_tool_turn(deny=False)

    assert result.error and "no tool-provocation" in result.error
    assert not result.tool_calls


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

    tool_result = await ToolCallingProbe().run(_Driver(), profile)
    assert tool_result.verdict is Verdict.SUPPORTED
    deny_result = await PolicyDenyProbe().run(_Driver(), profile)
    assert deny_result.verdict is Verdict.SUPPORTED


def test_format_matches_server_wire_name() -> None:
    """The driver keys on the exact wire name the server publishes."""
    from omnigent.server.routes.sessions import _format_sse
    from tests.harness_bench.native_tui_driver import _POLICY_DENIED_EVENT

    sse = _format_sse(_POLICY_DENIED_EVENT, {"type": _POLICY_DENIED_EVENT})
    # The reader parses `event: <name>`; assert the driver's key is that name.
    assert sse.startswith(f"event: {_POLICY_DENIED_EVENT}\n")
    assert json.loads(sse.split("data: ", 1)[1])["type"] == _POLICY_DENIED_EVENT
