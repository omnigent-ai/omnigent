"""Transport drivers: launch a harness and drive turns for the probes.

A driver hides the transport (how a turn is started and how events come
back) behind a small harness-agnostic surface the probes call. The only
driver today is :class:`SdkInprocDriver`, which spawns a single harness
wrap subprocess via :class:`HarnessProcessManager` and drives turns over
the wrap's ``POST /v1/sessions/{conv}/events`` SSE endpoint — the same
path exercised by ``tests/e2e/test_harness_wrap_e2e.py``.

Native transports (tmux TUI, app-server, HTTP/SSE) are phase-2 drivers
keyed by :attr:`BenchProfile.transport`; a profile on a transport with no
driver yields :meth:`unavailable`, and the bench renders its
transport-dependent probes as ``SKIPPED``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from omnigent.runtime.harnesses.process_manager import HarnessProcessManager
from tests.e2e._harness_probes import cli_unavailable_reason
from tests.harness_bench.profile import BenchProfile

# Proto-style policy verdict strings the wrap's policy_verdict event accepts.
POLICY_ALLOW = "POLICY_ACTION_ALLOW"
POLICY_DENY = "POLICY_ACTION_DENY"

_CONV_ID = "conv_bench"


@dataclass
class TurnResult:
    """Everything a probe needs to inspect after one turn.

    :param events: Every decoded SSE event dict, in order.
    :param text: Concatenation of all ``response.output_text.delta``
        payloads.
    :param text_delta_count: Number of ``response.output_text.delta``
        events — the streaming signal (>1 = token-level deltas, 1 = a
        single complete blob).
    :param reasoning_delta_count: Number of reasoning-delta events, if the
        harness forwards any.
    :param tool_calls: The ``response.tool_call`` events observed, each a
        raw event dict carrying ``call_id`` / ``name`` / ``arguments``.
    :param policy_actions: The verdict strings this driver posted back
        (one per ``policy_evaluation.requested``), so a probe can tell a
        DENY was actually delivered.
    :param completed: Whether a terminal ``response.completed`` was seen.
    :param failed: Whether a terminal ``response.failed`` was seen.
    :param error: The error payload from ``response.failed``, if any.
    :param timed_out: Whether the stream did not reach a terminal event
        within the probe's timeout.
    """

    events: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""
    text_delta_count: int = 0
    reasoning_delta_count: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    policy_actions: list[str] = field(default_factory=list)
    completed: bool = False
    failed: bool = False
    error: Any = None
    timed_out: bool = False

    @property
    def event_types(self) -> list[str]:
        """The ``type`` of every event, in order."""
        return [e.get("type", "") for e in self.events]


class SdkInprocDriver:
    """Drive turns through a single harness wrap subprocess.

    Use as an async context manager::

        async with SdkInprocDriver(profile, databricks_profile="prof") as d:
            result = await d.run_turn("Reply with FOO.")

    The context manager owns the :class:`HarnessProcessManager` lifecycle
    and a short-pathed tmp parent (macOS ``AF_UNIX`` path limit).
    """

    transport = "sdk-inproc"

    def __init__(self, profile: BenchProfile, *, databricks_profile: str) -> None:
        self._profile = profile
        self._databricks_profile = databricks_profile
        self._pm: HarnessProcessManager | None = None
        self._client: httpx.AsyncClient | None = None
        self._tmp_parent: Path | None = None

    @staticmethod
    def unavailable(profile: BenchProfile, *, databricks_profile: str | None) -> str | None:
        """Return a skip reason if this driver cannot run *profile*, else ``None``.

        Checks, in order: a supplied Databricks profile (no gateway route
        without one), and a runnable harness CLI binary. Mirrors the e2e
        suite's gating so the bench skips — rather than errors — in
        environments missing creds or a vendor CLI.
        """
        if not databricks_profile:
            return "no --profile / databricks profile provided; live probes need a gateway route"
        if profile.cli_binary is not None:
            reason = cli_unavailable_reason(profile.cli_binary)
            if reason is not None:
                return reason
        return None

    async def __aenter__(self) -> SdkInprocDriver:
        self._tmp_parent = Path("/tmp") / f"omni-bench-{uuid.uuid4().hex[:8]}"
        self._tmp_parent.mkdir(mode=0o700)
        self._pm = HarnessProcessManager(tmp_parent=self._tmp_parent)
        await self._pm.start()
        p = self._profile
        self._client = await self._pm.get_client(
            _CONV_ID,
            p.harness,
            env={
                f"{p.env_prefix}GATEWAY": "true",
                f"{p.env_prefix}DATABRICKS_PROFILE": self._databricks_profile,
                f"{p.env_prefix}MODEL": p.model,
            },
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._pm is not None:
            await self._pm.shutdown()
        if self._tmp_parent is not None:
            shutil.rmtree(self._tmp_parent, ignore_errors=True)

    async def run_turn(
        self,
        prompt: str,
        *,
        tools: list[dict[str, Any]] | None = None,
        policy_action: str = POLICY_ALLOW,
        policy_reason: str | None = None,
        auto_tool_output: str | None = None,
        interrupt_on_first_delta: bool = False,
        timeout: float = 120.0,
    ) -> TurnResult:
        """Start one turn and drain its event stream into a :class:`TurnResult`.

        Handles the three downward round-trips the wrap may need mid-turn:

        - ``policy_evaluation.requested`` → posts a ``policy_verdict`` with
          *policy_action* (set ``POLICY_DENY`` to exercise enforcement).
        - ``response.tool_call`` → when *auto_tool_output* is set, posts a
          ``tool_result`` so a tool-calling turn can complete.
        - *interrupt_on_first_delta* → posts an ``interrupt`` event the
          first time text streams, to exercise cancellation.

        :param prompt: The user text for the turn.
        :param tools: Optional tool specs forwarded verbatim as the wrap's
            passthrough ``tools`` field (Chat-Completions shape:
            ``[{"type": "function", "function": {...}}]``).
        :param policy_action: Verdict posted for every policy evaluation.
        :param policy_reason: Reason string sent with a DENY verdict.
        :param auto_tool_output: Stringified output auto-returned for each
            tool call; ``None`` leaves tool calls unanswered.
        :param interrupt_on_first_delta: Post an interrupt once text starts.
        :param timeout: Seconds to wait for a terminal event before marking
            the result :attr:`TurnResult.timed_out`.
        :returns: The drained :class:`TurnResult`.
        """
        assert self._client is not None, "driver used outside its async context"
        body: dict[str, Any] = {
            "type": "message",
            "role": "user",
            "model": f"{self._profile.harness}-bench-agent",
            "content": [{"type": "input_text", "text": prompt}],
        }
        if tools is not None:
            body["tools"] = tools

        result = TurnResult()
        try:
            await asyncio.wait_for(
                self._drive(
                    body,
                    result,
                    policy_action,
                    policy_reason,
                    auto_tool_output,
                    interrupt_on_first_delta,
                ),
                timeout=timeout,
            )
        except (asyncio.TimeoutError, httpx.ReadTimeout):
            result.timed_out = True
        return result

    async def _drive(
        self,
        body: dict[str, Any],
        result: TurnResult,
        policy_action: str,
        policy_reason: str | None,
        auto_tool_output: str | None,
        interrupt_on_first_delta: bool,
    ) -> None:
        """POST the turn and consume the SSE stream into *result* in place."""
        client = self._client
        assert client is not None
        interrupted = False
        async with client.stream("POST", f"/v1/sessions/{_CONV_ID}/events", json=body) as response:
            response.raise_for_status()
            buffer = ""
            async for chunk in response.aiter_text():
                buffer += chunk
                while "\n\n" in buffer:
                    frame, _, buffer = buffer.partition("\n\n")
                    event = _decode_frame(frame)
                    if event is None:
                        continue
                    result.events.append(event)
                    etype = event.get("type", "")

                    if etype == "response.output_text.delta":
                        result.text += event.get("delta", "")
                        result.text_delta_count += 1
                        if interrupt_on_first_delta and not interrupted:
                            interrupted = True
                            await self._post({"type": "interrupt"})
                    elif etype in _REASONING_DELTA_TYPES:
                        result.reasoning_delta_count += 1
                    elif etype == "response.tool_call":
                        result.tool_calls.append(event)
                        if auto_tool_output is not None:
                            await self._post(
                                {
                                    "type": "tool_result",
                                    "call_id": event.get("call_id", ""),
                                    "output": auto_tool_output,
                                }
                            )
                    elif etype == "policy_evaluation.requested":
                        verdict: dict[str, Any] = {
                            "type": "policy_verdict",
                            "evaluation_id": event["evaluation_id"],
                            "action": policy_action,
                        }
                        if policy_reason is not None:
                            verdict["reason"] = policy_reason
                        result.policy_actions.append(policy_action)
                        await self._post(verdict)
                    elif etype == "response.completed":
                        result.completed = True
                    elif etype == "response.failed":
                        result.failed = True
                        result.error = event.get("error") or event.get("response", {}).get("error")

    async def _post(self, payload: dict[str, Any]) -> None:
        """POST a downward event on the wrap's events endpoint (best-effort).

        Downward events race the turn's terminal state (e.g. an interrupt
        landing just as the turn ends). A failed post here is benign — the
        probe reads the outcome from the stream — so the error is suppressed.
        """
        assert self._client is not None
        with contextlib.suppress(httpx.HTTPError):
            await self._client.post(f"/v1/sessions/{_CONV_ID}/events", json=payload)


# Reasoning-delta event names vary across harness wraps; match the common
# spellings so the reasoning signal is captured without per-harness code.
_REASONING_DELTA_TYPES: frozenset[str] = frozenset(
    {"response.reasoning.delta", "response.reasoning_summary_text.delta"}
)


def _decode_frame(frame: str) -> dict[str, Any] | None:
    """Decode one SSE frame's ``data:`` line into an event dict, or ``None``."""
    data_line = next(
        (line for line in frame.splitlines() if line.startswith("data:")),
        None,
    )
    if data_line is None:
        return None
    try:
        decoded = json.loads(data_line[len("data:") :].strip())
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None
