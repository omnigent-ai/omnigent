"""Full-server transport driver."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

from tests.e2e._harness_probes import cli_unavailable_reason
from tests.harness_bench.driver import TurnResult, fill_snapshot_cost
from tests.harness_bench.full_server import (
    _DENY_REASON,
    _POLL_INTERVAL_S,
    _TOOL_NAME,
    SharedFullServer,
)
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.runtime_env import bench_creds_skip_reason, resolve_bench_env
from tests.harness_bench.session_items import (
    assistant_text,
    contains_user_text,
    function_calls,
    tool_output_states,
)

_TOOL_PROMPT = f"List the files using the {_TOOL_NAME} tool, then tell me how many there are."

# Interrupted turns persist this marker in session history.
_CANCELLATION_MARKER = "interrupted"
_LONG_PROMPT = (
    "Write a very detailed 600-word essay about the history of computing, in full paragraphs."
)

_STREAM_PROMPT = (
    "Count from 1 to 30 in words, one number per line, and add a short note after each."
)
_TERMINAL_EVENTS = frozenset({"response.completed", "response.failed", "response.cancelled"})


class FullServerDriver:
    """Drive turns through a live Omnigent server and runner."""

    transport = "full-server"

    def __init__(
        self,
        profile: BenchProfile,
        *,
        databricks_profile: str | None,
        shared: SharedFullServer | None = None,
    ) -> None:
        self._profile = profile
        self._db_profile = databricks_profile
        self._shared = shared
        self._owns_shared = shared is None
        # Fixed-action policies must be baked into separate agent specs.
        self._policy_session_ids: dict[str, str] = {}
        self._session_id: str | None = None

    @property
    def _client(self) -> httpx.Client | None:
        return self._shared.client if self._shared is not None else None

    @staticmethod
    def unavailable(profile: BenchProfile, *, databricks_profile: str | None) -> str | None:
        """Return why this driver cannot run the profile, if applicable."""
        # Native harnesses require host-daemon provisioning.
        if profile.transport == "native-tui":
            return (
                f"{profile.harness!r} is a native-tui harness; the full-server transport "
                "registers via an agent bundle and cannot drive it (use --transport native-tui)"
            )
        creds_skip = bench_creds_skip_reason(databricks_profile)
        if creds_skip is not None:
            return creds_skip
        if profile.cli_binary is not None:
            return cli_unavailable_reason(profile.cli_binary)
        return None

    def __enter__(self) -> FullServerDriver:
        if self._shared is None:
            self._shared = SharedFullServer(resolve_bench_env(self._db_profile))
            self._shared.__enter__()
        agent_name = self._shared.register_agent(self._profile, policy_action=None)
        self._session_id = self._shared.create_session(agent_name)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._owns_shared and self._shared is not None:
            self._shared.__exit__(*exc)

    # Blocking server operations run off the event loop.

    async def __aenter__(self) -> FullServerDriver:
        return await asyncio.to_thread(self.__enter__)

    async def __aexit__(self, *exc: object) -> None:
        await asyncio.to_thread(self.__exit__, *exc)

    async def run_basic_turn(self, marker: str) -> TurnResult:
        prompt = f"Reply with exactly the literal string {marker} and nothing else."
        return await asyncio.to_thread(self.run_turn, prompt)

    async def run_streaming_turn(self) -> TurnResult:
        return await asyncio.to_thread(self.streaming_probe_turn)

    async def run_tool_turn(self, *, deny: bool) -> TurnResult:
        return await asyncio.to_thread(lambda: self.tool_probe_turn(deny=deny))

    async def run_policy_turn(self, *, action: str) -> TurnResult:
        return await asyncio.to_thread(lambda: self.policy_probe_turn(action=action))

    async def run_interrupt_turn(self) -> TurnResult:
        return await asyncio.to_thread(self.interrupt_probe_turn)

    def _ensure_policy_session(self, action: str) -> str:
        """Return a session whose agent bakes in the requested policy action."""
        assert self._shared is not None
        if action not in self._policy_session_ids:
            name = self._shared.register_agent(self._profile, policy_action=action)
            self._policy_session_ids[action] = self._shared.create_session(name)
        return self._policy_session_ids[action]

    def _poll_session(
        self,
        sid: str,
        result: TurnResult,
        *,
        timeout: float,
        scan_tools: bool = False,
        fill_cost: bool = False,
        stop_when: Callable[[], bool] | None = None,
    ) -> TurnResult:
        """Poll a session until it settles, fails, times out, or is stopped."""
        assert self._client is not None
        deadline = time.monotonic() + timeout
        seen_running = False
        while time.monotonic() < deadline:
            if stop_when is not None and stop_when():
                return result
            response = self._client.get(f"/v1/sessions/{sid}")
            response.raise_for_status()
            snapshot = response.json()
            status = snapshot.get("status")
            items = snapshot.get("items", [])
            if scan_tools:
                _scan_tool_items(items, result)
            if status in ("running", "waiting"):
                seen_running = True
            elif status == "failed":
                result.failed = True
                result.error = snapshot.get("last_task_error") or snapshot.get("error")
                return result
            elif status == "idle" and seen_running:
                result.completed = True
                result.text = assistant_text(items)
                if fill_cost:
                    fill_snapshot_cost(result, snapshot)
                return result
            time.sleep(_POLL_INTERVAL_S)
        result.timed_out = True
        return result

    def tool_probe_turn(self, *, deny: bool, timeout: float = 180.0) -> TurnResult:
        """Drive a turn that calls the builtin tool; return a :class:`TurnResult`.

        On the full-server transport a tool call is real and server-
        dispatched. With *deny* the turn runs against a session whose agent
        bakes a ``tool_call`` deny policy, so the server blocks the call and
        the tool output carries the deny reason.

        Fills :attr:`TurnResult.tool_calls` (the builtin call) and
        :attr:`TurnResult.tool_call_denied` (whether the server blocked it),
        plus ``completed`` / ``failed`` / ``text``.
        """
        assert self._client is not None
        sid = self._ensure_policy_session("deny") if deny else self._session_id
        assert sid is not None
        result = TurnResult()
        body = {
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": _TOOL_PROMPT}]},
        }
        self._client.post(f"/v1/sessions/{sid}/events", json=body).raise_for_status()

        return self._poll_session(sid, result, timeout=timeout, scan_tools=True)

    def policy_probe_turn(self, *, action: str, timeout: float = 90.0) -> TurnResult:
        """Drive a tool turn under a fixed tool_call policy *action*.

        ``"allow"``: the call proceeds (``tool_call_allowed`` from the non-blocked
        output). ``"ask"``: it parks on an elicitation; a background reader sets
        ``elicitation_requested`` off ``response.elicitation_request``. The ASK
        verdict is decided the moment that fires, so we resolve the elicitation
        (approval accept, to leave no dangling park) and return immediately
        rather than polling the turn to a terminal state.

        The timeout bounds the *worst* case (the model never calls the tool, so
        no elicitation fires): a bounded SKIP, not a 3-minute stall.
        """
        assert self._client is not None
        sid = self._ensure_policy_session(action)
        result = TurnResult()

        elicitation_id: dict[str, str] = {}
        stop = threading.Event()

        def _watch() -> None:
            try:
                with self._client.stream(  # type: ignore[union-attr]
                    "GET", f"/v1/sessions/{sid}/stream", timeout=timeout
                ) as resp:
                    for raw in resp.iter_lines():
                        if stop.is_set():
                            return
                        line = raw.strip()
                        if not line.startswith("data:"):
                            continue
                        try:
                            frame = json.loads(line[len("data:") :].strip())
                        except (ValueError, TypeError):
                            continue
                        if frame.get("type") == "response.elicitation_request":
                            result.elicitation_requested = True
                            eid = frame.get("elicitation_id")
                            if isinstance(eid, str):
                                elicitation_id["id"] = eid
                            else:
                                # No parseable id: verdict is recorded, but we
                                # can't resolve, so the turn parks to the deadline.
                                result.error = "elicitation_request frame had no parseable id"
                            return
            except httpx.HTTPError:
                # Best-effort watcher; an SSE read error must not fail the turn.
                pass

        watcher = None
        if action == "ask":
            watcher = threading.Thread(target=_watch, daemon=True)
            watcher.start()
            time.sleep(1.0)  # register the subscription before the turn starts

        body = {
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": _TOOL_PROMPT}]},
        }
        self._client.post(f"/v1/sessions/{sid}/events", json=body).raise_for_status()

        def _elicitation_observed() -> bool:
            if action == "ask" and result.elicitation_requested:
                if "id" in elicitation_id:
                    self._resolve_elicitation(sid, elicitation_id.pop("id"))
                return True
            return False

        self._poll_session(
            sid,
            result,
            timeout=timeout,
            scan_tools=True,
            stop_when=_elicitation_observed,
        )
        stop.set()
        if watcher is not None:
            watcher.join(timeout=5.0)
        return result

    def _resolve_elicitation(self, sid: str, elicitation_id: str) -> None:
        """Accept an outstanding elicitation via an ``approval`` event so an ASK
        turn settles (best-effort; a raced resolve is harmless)."""
        assert self._client is not None
        # The server reads the id from inside `data` (SessionEventInput has no
        # top-level elicitation_id field), so it must be nested there or the
        # resolve is a silent no-op and the park dangles.
        with contextlib.suppress(httpx.HTTPError):
            self._client.post(
                f"/v1/sessions/{sid}/events",
                json={
                    "type": "approval",
                    "data": {"elicitation_id": elicitation_id, "action": "accept"},
                },
            )

    def streaming_probe_turn(self, *, timeout: float = 120.0) -> TurnResult:
        """Measure token-level streaming via the session SSE subscribe stream.

        The full-server stream (``GET /v1/sessions/{id}/stream``) is separate
        from the message POST, so a background thread subscribes and counts
        ``response.output_text.delta`` events while the main thread posts the
        turn. More than one delta means the harness streams incrementally.
        """
        assert self._client is not None and self._session_id is not None
        sid = self._session_id
        result = TurnResult()
        done = threading.Event()

        def _read_stream() -> None:
            try:
                with self._client.stream(  # type: ignore[union-attr]
                    "GET", f"/v1/sessions/{sid}/stream", timeout=timeout
                ) as resp:
                    for line in resp.iter_lines():
                        if not line.startswith("event:"):
                            continue
                        etype = line[len("event:") :].strip()
                        if etype == "response.output_text.delta":
                            result.text_delta_count += 1
                        elif etype in _TERMINAL_EVENTS:
                            result.completed = etype == "response.completed"
                            result.cancelled = etype == "response.cancelled"
                            result.failed = etype == "response.failed"
                            return
            except httpx.HTTPError as exc:
                result.error = repr(exc)
            finally:
                done.set()

        reader = threading.Thread(target=_read_stream, daemon=True)
        reader.start()
        time.sleep(1.0)  # let the subscription register before the turn starts
        self._client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": _STREAM_PROMPT}],
                },
            },
        ).raise_for_status()
        if not done.wait(timeout):
            result.timed_out = True
        return result

    def interrupt_probe_turn(self, *, timeout: float = 120.0) -> TurnResult:
        """Start a long turn, interrupt it mid-flight, and report the outcome.

        Posts an ``interrupt`` event once the turn is running (after a short
        hold so some text streams first), then waits for the server's
        cancellation marker. Sets :attr:`TurnResult.cancelled` when the
        marker appears — the honored-interrupt signal.
        """
        assert self._client is not None and self._session_id is not None
        sid = self._session_id
        result = TurnResult()
        body = {
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": _LONG_PROMPT}]},
        }
        self._client.post(f"/v1/sessions/{sid}/events", json=body).raise_for_status()

        deadline = time.monotonic() + timeout
        interrupted = False
        while time.monotonic() < deadline:
            snap = self._client.get(f"/v1/sessions/{sid}").json()
            status = snap.get("status")
            items = snap.get("items", [])
            if status in ("running", "waiting") and not interrupted:
                # Let a little text stream so the interrupt lands mid-turn.
                time.sleep(1.5)
                self._client.post(f"/v1/sessions/{sid}/events", json={"type": "interrupt"})
                interrupted = True
            if _has_cancellation_marker(items):
                result.cancelled = True
                result.text = assistant_text(items)
                break
            if status == "idle" and interrupted:
                # Settled after the interrupt; the marker lands just after.
                result.cancelled = _has_cancellation_marker(items)
                result.text = assistant_text(items)
                break
            time.sleep(_POLL_INTERVAL_S)
        else:
            result.timed_out = True
        return result

    def run_turn(self, prompt: str, *, timeout: float = 180.0) -> TurnResult:
        """Drive one basic turn through the full server, return a :class:`TurnResult`.

        Posts the user message and polls the session snapshot to a terminal
        state, filling text, completion, failure, timeout, and usage fields.
        """
        assert self._client is not None and self._session_id is not None
        result = TurnResult()
        body: dict[str, Any] = {
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
        }
        posted = self._client.post(f"/v1/sessions/{self._session_id}/events", json=body)
        if posted.status_code == 202 and posted.json().get("denied"):
            result.failed = True
            result.error = {"denied": True, "reason": posted.json().get("reason")}
            return result
        posted.raise_for_status()

        return self._poll_session(
            self._session_id,
            result,
            timeout=timeout,
            fill_cost=True,
        )


def _scan_tool_items(items: list[dict[str, Any]], result: TurnResult) -> None:
    """Populate tool_calls and tool_call_denied from session items."""
    result.tool_calls = function_calls(items)
    result.tool_call_allowed, result.tool_call_denied = tool_output_states(
        items, deny_marker=_DENY_REASON
    )


def _has_cancellation_marker(items: list[dict[str, Any]]) -> bool:
    """Whether items include the synthetic 'interrupted' user message."""
    return contains_user_text(items, _CANCELLATION_MARKER)
