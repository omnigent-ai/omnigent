"""Full-server transport driver (phase-2).

Unlike :class:`tests.harness_bench.driver.SdkInprocDriver` (which drives a
harness wrap subprocess directly), this driver spins up a REAL Omnigent
``server`` + ``runner`` pair, registers an agent, and drives turns through
the full session path — so policy enforcement and server-dispatched tools
are exercised the way production does, not simulated at the wrap boundary.

It reuses the exact spawn recipe of the e2e ``live_server`` fixture
(``tests/e2e/conftest.py``) via the shared compat helpers, but packaged as
a plain async context manager so the bench CLI can drive it without pytest.

Status (live-verified): server+runner lifecycle, a basic turn, and the
payoff this transport exists for — a real **server-dispatched tool call**
(a read-only builtin) and **tool-call policy enforcement** (a spec-baked
``tool_call`` deny policy blocks the call the way production does). Ad-hoc
request-level function tools are NOT used here: the SDK harnesses handle
tools internally, so they never round-trip as a server-dispatched, policy-
gated call — a builtin does.

Interrupt/cancel is verified (a long turn is interrupted mid-flight and the
server's cancellation marker confirms it stopped), and delta-level
streaming is measured via the ``/v1/sessions/{id}/stream`` SSE subscribe.

Follow-up (stacked PR): a ``--transport`` selector + driver registry so the
bench's probes run through this driver, not just its gated tests.
"""

from __future__ import annotations

import asyncio
import threading
import time
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

_TOOL_PROMPT = f"List the files using the {_TOOL_NAME} tool, then tell me how many there are."

# The server persists an interrupted turn as a synthetic user message whose
# text contains this marker (see tests/e2e/test_cancel_history.py).
_CANCELLATION_MARKER = "interrupted"
_LONG_PROMPT = (
    "Write a very detailed 600-word essay about the history of computing, in full paragraphs."
)

# Prompt long enough that a streaming harness emits clearly many deltas.
_STREAM_PROMPT = (
    "Count from 1 to 30 in words, one number per line, and add a short note after each."
)
_TERMINAL_EVENTS = frozenset({"response.completed", "response.failed", "response.cancelled"})


class FullServerDriver:
    """Drive turns through a live Omnigent server + runner.

    Async context manager: on enter it spawns the server and runner,
    waits for both to report healthy, registers *profile*'s harness as an
    agent, and creates a runner-bound session. ``run_turn`` drives one turn
    through that session.
    """

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
        # When *shared* is given (a parallel run), this driver registers its
        # agent + session on that one server+runner and spawns nothing itself.
        # When None (a solo / --jobs 1 run), it owns a private SharedFullServer
        # for back-compat with the original one-server-per-harness behavior.
        self._shared = shared
        self._owns_shared = shared is None
        # A second agent+session whose spec bakes a tool_call deny policy,
        # created lazily for the policy probe (the REST policy endpoint's
        # handler allowlist excludes make_fixed_action_callable, so the deny
        # must ride in the agent spec instead).
        self._deny_session_id: str | None = None
        self._session_id: str | None = None

    @property
    def _client(self) -> httpx.Client | None:
        return self._shared.client if self._shared is not None else None

    @staticmethod
    def unavailable(profile: BenchProfile, *, databricks_profile: str | None) -> str | None:
        """Return a skip reason if this driver cannot run *profile*, else ``None``."""
        # full-server registers the harness via an agent bundle (the SDK-wrap
        # path); a native harness needs the host-daemon/tmux provisioning only
        # the native-tui driver does, so it cannot run here even under an
        # explicit --transport full-server override.
        if profile.transport == "native-tui":
            return (
                f"{profile.harness!r} is a native-tui harness; the full-server transport "
                "registers via an agent bundle and cannot drive it (use --transport native-tui)"
            )
        creds_skip = bench_creds_skip_reason(databricks_profile)
        if creds_skip is not None:
            return creds_skip
        # Same CLI gate as the wrap driver (same binary requirement), but skip
        # its transport check — that is sdk-inproc-specific and would misreport
        # the driver name; the native case is already handled above.
        if profile.cli_binary is not None:
            return cli_unavailable_reason(profile.cli_binary)
        return None

    def __enter__(self) -> FullServerDriver:
        if self._shared is None:
            self._shared = SharedFullServer(resolve_bench_env(self._db_profile))
            self._shared.__enter__()
        agent_name = self._shared.register_agent(self._profile, deny=False)
        self._session_id = self._shared.create_session(agent_name)
        return self

    def __exit__(self, *exc: object) -> None:
        # Only tear down the server we own; an injected shared server is the
        # orchestrator's to close after all harnesses finish.
        if self._owns_shared and self._shared is not None:
            self._shared.__exit__(*exc)

    # ── async driver protocol ────────────────────────────────
    # This driver's provisioning and turns are synchronous (subprocess spawn,
    # blocking snapshot polls, a threaded SSE reader). Bridge to the bench's
    # async Driver protocol by running the blocking work in a worker thread so
    # the event loop is never blocked.

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

    async def run_interrupt_turn(self) -> TurnResult:
        return await asyncio.to_thread(self.interrupt_probe_turn)

    # ── agent + session ──────────────────────────────────────
    # The server/runner lifecycle and agent/session registration live on
    # SharedFullServer now; this driver delegates so a solo run and a parallel
    # (shared-server) run go through the same path.

    def _ensure_deny_session(self) -> str:
        """Lazily register the deny agent and its session; return the session id."""
        assert self._shared is not None
        if self._deny_session_id is None:
            name = self._shared.register_agent(self._profile, deny=True)
            self._deny_session_id = self._shared.create_session(name)
        return self._deny_session_id

    # ── tool / policy probe ──────────────────────────────────

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
        sid = self._ensure_deny_session() if deny else self._session_id
        assert sid is not None
        result = TurnResult()
        body = {
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": _TOOL_PROMPT}]},
        }
        self._client.post(f"/v1/sessions/{sid}/events", json=body).raise_for_status()

        deadline = time.monotonic() + timeout
        seen_running = False
        while time.monotonic() < deadline:
            snap = self._client.get(f"/v1/sessions/{sid}").json()
            status = snap.get("status")
            items = snap.get("items", [])
            if status in ("running", "waiting"):
                seen_running = True
            if status == "failed":
                result.failed = True
                result.error = snap.get("last_task_error") or snap.get("error")
                break
            if status == "idle" and seen_running:
                result.completed = True
                _scan_tool_items(items, result)
                result.text = _assistant_text(items)
                break
            time.sleep(_POLL_INTERVAL_S)
        else:
            result.timed_out = True
        return result

    # ── streaming probe ──────────────────────────────────────

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

    # ── interrupt probe ──────────────────────────────────────

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
                result.text = _assistant_text(items)
                break
            if status == "idle" and interrupted:
                # Settled after the interrupt; the marker lands just after.
                result.cancelled = _has_cancellation_marker(items)
                result.text = _assistant_text(items)
                break
            time.sleep(_POLL_INTERVAL_S)
        else:
            result.timed_out = True
        return result

    # ── turn ─────────────────────────────────────────────────

    def run_turn(self, prompt: str, *, timeout: float = 180.0) -> TurnResult:
        """Drive one basic turn through the full server, return a :class:`TurnResult`.

        Foundation scope: posts the user message and polls the session
        snapshot to a terminal state, filling ``text`` / ``completed`` /
        ``failed`` / ``timed_out``. A synchronous (request-phase) policy
        DENY short-circuits to ``failed``.

        The dimensions that motivated this transport — server-dispatched
        tools, tool-call policy enforcement, delta streaming, interrupt —
        are follow-ups (see the module docstring); they extend this
        signature and are not implemented yet.
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

        deadline = time.monotonic() + timeout
        seen_running = False
        while time.monotonic() < deadline:
            snap = self._client.get(f"/v1/sessions/{self._session_id}")
            snap.raise_for_status()
            body = snap.json()
            status = body.get("status")
            if status in ("running", "waiting"):
                seen_running = True
            if status == "failed":
                result.failed = True
                result.error = body.get("last_task_error") or body.get("error")
                break
            if status == "idle" and seen_running:
                result.completed = True
                result.text = _assistant_text(body.get("items", []))
                fill_snapshot_cost(result, body)
                break
            time.sleep(_POLL_INTERVAL_S)
        else:
            result.timed_out = True
        return result


def _scan_tool_items(items: list[dict], result: TurnResult) -> None:
    """Populate tool_calls and tool_call_denied from session items."""
    for raw in items:
        data = raw.get("data", raw)
        itype = raw.get("type") or data.get("type")
        if itype == "function_call":
            result.tool_calls.append(
                {
                    "call_id": data.get("call_id"),
                    "name": data.get("name"),
                    "arguments": data.get("arguments"),
                }
            )
        elif itype == "function_call_output":
            out = str(data.get("output", ""))
            if data.get("status") == "blocked" or _DENY_REASON in out:
                result.tool_call_denied = True


def _has_cancellation_marker(items: list[dict]) -> bool:
    """Whether items include the synthetic 'interrupted' user message."""
    for raw in items:
        data = raw.get("data", raw)
        if (raw.get("type") == "message") and (data.get("role") == "user"):
            if any(
                _CANCELLATION_MARKER in (b.get("text", "") or "")
                for b in data.get("content", []) or []
            ):
                return True
    return False


def _assistant_text(items: list[dict]) -> str:
    """Concatenate assistant output_text from session items."""
    out: list[str] = []
    for item in items:
        data = item.get("data", item)
        if data.get("role") == "assistant" or item.get("role") == "assistant":
            for block in data.get("content", []) or []:
                if block.get("type") in ("output_text", "text"):
                    out.append(block.get("text", ""))
    return "\n".join(t for t in out if t)
