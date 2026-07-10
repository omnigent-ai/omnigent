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
from tests.harness_bench.runtime_env import bench_creds_skip_reason, resolve_bench_env


class ProvisioningError(RuntimeError):
    """An *expected* provisioning failure that should skip the harness quietly.

    Raised by a driver's ``__aenter__`` when the environment cannot bring a
    harness up through no fault of the bench — e.g. an own-auth native whose
    vendor CLI is installed but not logged in, so its forwarder never wires up.
    The orchestrator turns this into a capability-neutral skip and logs only the
    reason (no traceback), reserving the full stack for *unexpected* exceptions
    that signal a genuine driver bug.
    """


# Proto-style policy verdict strings the wrap's policy_verdict event accepts.
POLICY_ALLOW = "POLICY_ACTION_ALLOW"
POLICY_DENY = "POLICY_ACTION_DENY"

# Proto-style policy evaluation phases (see _scaffold.evaluate_policy and
# omnigent/native_policy_hook.py). A tool call is gated at PHASE_TOOL_CALL;
# the request/result phases fire at other points in the turn. The policy
# probe must scope its DENY to PHASE_TOOL_CALL so a DENY on the request
# phase cannot masquerade as a tool-call guardrail pass.
PHASE_TOOL_CALL = "PHASE_TOOL_CALL"

_CONV_ID = "conv_bench"

# Wrap-transport turn shapes for the semantic driver methods. The wrap path
# provokes a tool call with a request-level function tool (unlike full-server,
# which uses a builtin); prompts are long enough that a streaming harness
# emits many deltas and an interrupted turn has visibly less output.
_STREAM_PROMPT = (
    "Count from 1 to 30 in words, one number per line, and add a short note after each."
)
_LONG_PROMPT = (
    "Write a very detailed 600-word essay about the history of computing, in full paragraphs."
)
_BENCH_TOOL_NAME = "bench_tool"
_BENCH_DENY_REASON = "bench-policy-deny"
_BENCH_TOOL_SPEC = [
    {
        "type": "function",
        "function": {
            "name": _BENCH_TOOL_NAME,
            "description": "A bench probe tool. Call it when asked.",
            "parameters": {
                "type": "object",
                "properties": {"arg": {"type": "string"}},
                "required": ["arg"],
            },
        },
    }
]

# Substrings in a turn error that mean the *environment* is the problem
# (auth, entitlement, gateway, connectivity) rather than a real capability
# gap. Turns that fail this way are reported SKIPPED, never UNSUPPORTED, so
# a bad token or an unentitled gateway route can never masquerade as
# capability drift.
_INFRA_ERROR_MARKERS: tuple[str, ...] = (
    "403",
    "401",
    "Forbidden",
    "Unauthorized",
    "Invalid Token",
    "invalid token",
    "unexpected status",
    "Connection",
    "connection",
    "Temporarily Unavailable",
    "502",
    "503",
    "504",
    # Sequencing, not capability: a prior turn on the shared session had not
    # fully settled. Reported SKIPPED so it never reads as a capability gap.
    "already processing",
    # Token provisioning failed before the harness could reach the model — an
    # environment/auth gap (a missing/empty gateway token, a provider auth
    # command that produced nothing), not a capability the harness lacks.
    # Seen on full-server for codex ("provider auth command ... empty token")
    # and pi ("could not fetch a gateway token").
    "could not fetch a gateway token",
    "provider auth command",
    "empty token",
    "Failed to resolve external API key auth",
    # Own-auth harness whose vendor CLI is not installed / not logged in (e.g.
    # an ACP harness like rovo: "Ensure `acli` is installed and you are logged
    # in"). The vendor process exits before a turn can run — an environment/
    # login gap, not a capability the harness lacks, so it must SKIP not drift.
    "are logged in",
    "AcpProcessExited",
    "ACP subprocess",
    "ACP session",
)


def _error_text(error: object) -> str:
    """Flatten a turn error (dict or str) into searchable text."""
    if isinstance(error, dict):
        return f"{error.get('message', '')} {error.get('code', '')}"
    return str(error or "")


def infra_failure_reason(result: TurnResult) -> str | None:
    """Return a concise env-skip reason if a turn failed on infra/auth, else ``None``.

    Lets probes distinguish "the gateway rejected us" (an environment
    problem the operator must fix) from "the harness cannot do this" (a
    capability fact). Only the latter should ever count as UNSUPPORTED.
    """
    if not result.failed:
        return None
    text = _error_text(result.error)
    if not any(marker in text for marker in _INFRA_ERROR_MARKERS):
        return None
    for code in ("403", "401"):
        if code in text:
            # Provider-neutral: any harness can hit this when its credential is
            # expired or shadowed by an ambient env var (a stale bearer/API-key/
            # token) that takes precedence over the configured auth source.
            return (
                f"auth rejected ({code} Invalid/Forbidden token); the harness "
                "credential is stale or shadowed by an ambient env var. Refresh "
                "the harness auth source (profile, API key, or token env var)"
            )
    if "already processing" in text:
        return "session busy from a prior turn (sequencing, not a capability gap)"
    if any(
        marker in text
        for marker in (
            "could not fetch a gateway token",
            "provider auth command",
            "empty token",
            "Failed to resolve external API key auth",
        )
    ):
        return (
            "gateway/provider token could not be provisioned for this transport "
            "(environment/auth gap, not a capability the harness lacks)"
        )
    if any(
        marker in text
        for marker in ("are logged in", "AcpProcessExited", "ACP subprocess", "ACP session")
    ):
        return (
            "vendor CLI not installed or not logged in (own-auth harness); "
            "the agent process exited before a turn could run"
        )
    if "unexpected status" in text:
        return "gateway returned an unexpected status (environment/auth issue)"
    return "environment/connectivity error reaching the gateway"


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
    :param policy_actions: ``(phase, action)`` pairs this driver posted back
        (one per ``policy_evaluation.requested``), so a probe can tell which
        verdict was delivered for which phase.
    :param tool_call_denied: Whether a ``PHASE_TOOL_CALL`` evaluation was
        answered DENY — the only signal that proves a tool-call guardrail
        (not a request/result-phase DENY) was actually exercised.
    :param completed: Whether a terminal ``response.completed`` was seen.
    :param cancelled: Whether a terminal ``response.cancelled`` was seen
        (the harness honored an interrupt).
    :param failed: Whether a terminal ``response.failed`` was seen.
    :param error: The error payload from ``response.failed``, if any.
    :param timed_out: Whether the stream did not reach a terminal event
        within the probe's timeout.
    :param total_tokens: Cumulative token count the turn reported, if any
        (``None`` when the transport surfaced no usage). Read from the session
        snapshot (``SessionResponse.last_total_tokens``) or the completed
        turn's ``usage``.
    :param total_cost_usd: Cumulative USD cost the turn reported, if any
        (``None`` when unobserved or the model is unpriced). Read from
        ``SessionResponse.total_cost_usd`` / ``session.usage`` /
        ``response.completed`` usage.
    :param elicitation_requested: Whether an elicitation was raised during the
        turn — the signal an ASK policy fired (server publishes
        ``response.elicitation_request`` / lands a pending elicitation on the
        snapshot). The policy_ask probe keys on this.
    :param tool_call_allowed: Whether a surfaced tool call produced a
        non-blocked ``function_call_output`` — i.e. the call proceeded. This is
        set for *any* non-blocked tool output, not only under an ALLOW policy;
        the policy_allow probe's correctness comes from driving a real
        ``action=allow`` session (``_ensure_policy_session("allow")``), so a set
        flag there means the ALLOW policy let the call through.
    """

    events: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""
    text_delta_count: int = 0
    reasoning_delta_count: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    policy_actions: list[tuple[str, str]] = field(default_factory=list)
    tool_call_denied: bool = False
    completed: bool = False
    cancelled: bool = False
    failed: bool = False
    error: Any = None
    timed_out: bool = False
    total_tokens: int | None = None
    total_cost_usd: float | None = None
    elicitation_requested: bool = False
    tool_call_allowed: bool = False

    @property
    def reached_terminal(self) -> bool:
        """Whether the stream ended on any terminal event (done/cancelled/failed)."""
        return self.completed or self.cancelled or self.failed

    @property
    def event_types(self) -> list[str]:
        """The ``type`` of every event, in order."""
        return [e.get("type", "") for e in self.events]


def fill_snapshot_cost(result: TurnResult, snapshot: dict[str, Any]) -> None:
    """Populate *result*'s usage/cost from a session snapshot (``SessionResponse``).

    The server tracks cumulative usage on the conversation and exposes it on the
    snapshot as ``total_cost_usd`` (priced spend; ``None`` for an unpriced model)
    and ``last_total_tokens`` (token count). Both server-backed drivers poll the
    snapshot to a terminal state, so this is the uniform cost read point. A
    tokens-only snapshot (no price) leaves ``total_cost_usd`` ``None`` — the cost
    probe reports that as PARTIAL, not a failure.
    """
    tokens = snapshot.get("last_total_tokens")
    if isinstance(tokens, int):
        result.total_tokens = tokens
    cost = snapshot.get("total_cost_usd")
    if isinstance(cost, (int, float)):
        result.total_cost_usd = float(cost)


class SdkInprocDriver:
    """Drive turns through a single harness wrap subprocess.

    Use as an async context manager::

        async with SdkInprocDriver(profile, databricks_profile="prof") as d:
            result = await d.run_turn("Reply with FOO.")

    The context manager owns the :class:`HarnessProcessManager` lifecycle
    and a short-pathed tmp parent (macOS ``AF_UNIX`` path limit).
    """

    transport = "sdk-inproc"

    def __init__(self, profile: BenchProfile, *, databricks_profile: str | None) -> None:
        self._profile = profile
        self._databricks_profile = databricks_profile
        self._pm: HarnessProcessManager | None = None
        self._client: httpx.AsyncClient | None = None
        self._tmp_parent: Path | None = None

    @staticmethod
    def unavailable(profile: BenchProfile, *, databricks_profile: str | None) -> str | None:
        """Return a skip reason if this driver cannot run *profile*, else ``None``.

        Checks, in order: the profile's transport matches this driver,
        resolvable gateway credentials (``--profile``, a configured
        ``~/.omnigent`` profile, or ambient ``OPENAI_*`` — like ``omni run``),
        and a runnable harness CLI binary. Mirrors the e2e suite's gating so the
        bench skips — rather than errors — in environments missing creds or a
        vendor CLI, or when a profile declares a transport this driver does not
        implement (e.g. a native/community harness).
        """
        if profile.transport != SdkInprocDriver.transport:
            return (
                f"transport {profile.transport!r} not supported by the "
                f"{SdkInprocDriver.transport!r} driver"
            )
        creds_skip = bench_creds_skip_reason(databricks_profile)
        if creds_skip is not None:
            return creds_skip
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
        # Resolve the effective profile the way `omni run` does (the --profile
        # override, else the config-derived one). May be None when auth comes
        # from ambient OPENAI_*, in which case the wrap inherits that env.
        resolved = resolve_bench_env(self._databricks_profile)
        wrap_env = {
            f"{p.env_prefix}GATEWAY": "true",
            f"{p.env_prefix}MODEL": p.model,
        }
        if resolved.db_profile:
            wrap_env[f"{p.env_prefix}DATABRICKS_PROFILE"] = resolved.db_profile
        self._client = await self._pm.get_client(_CONV_ID, p.harness, env=wrap_env)
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
        deny_phases: frozenset[str] = frozenset(),
        policy_reason: str | None = None,
        auto_tool_output: str | None = None,
        interrupt_on_first_delta: bool = False,
        timeout: float = 120.0,
    ) -> TurnResult:
        """Start one turn and drain its event stream into a :class:`TurnResult`.

        Handles the three downward round-trips the wrap may need mid-turn:

        - ``policy_evaluation.requested`` → posts a ``policy_verdict``,
          answering DENY only for evaluations whose ``phase`` is in
          *deny_phases* and ALLOW otherwise. Scoping the DENY by phase is
          what lets the policy probe prove a *tool-call* guardrail rather
          than accidentally denying the request phase.
        - ``response.output_item.done`` (function_call, action_required) →
          when *auto_tool_output* is set, posts a ``tool_result`` so a
          tool-calling turn can complete.
        - *interrupt_on_first_delta* → posts an ``interrupt`` event the
          first time text streams, to exercise cancellation.

        :param prompt: The user text for the turn.
        :param tools: Optional tool specs forwarded verbatim as the wrap's
            passthrough ``tools`` field (Chat-Completions shape:
            ``[{"type": "function", "function": {...}}]``).
        :param deny_phases: Policy phases to answer DENY (e.g.
            ``{PHASE_TOOL_CALL}``); all other phases are answered ALLOW.
            Empty (default) answers ALLOW to every phase.
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
                    deny_phases,
                    policy_reason,
                    auto_tool_output,
                    interrupt_on_first_delta,
                ),
                timeout=timeout,
            )
        except (asyncio.TimeoutError, httpx.ReadTimeout):
            result.timed_out = True
        return result

    # ── semantic driver protocol ─────────────────────────────
    # The probe-facing surface (see tests/harness_bench/transport.py). Each
    # method wraps run_turn with the wrap-transport mechanism for one
    # capability dimension, so probes stay transport-agnostic.

    async def run_basic_turn(self, marker: str) -> TurnResult:
        return await self.run_turn(
            f"Reply with exactly the literal string {marker} and nothing else."
        )

    async def run_streaming_turn(self) -> TurnResult:
        return await self.run_turn(_STREAM_PROMPT)

    async def run_tool_turn(self, *, deny: bool) -> TurnResult:
        """Provoke a tool call via a request-level function tool.

        With *deny*, answer the tool-call policy evaluation DENY (and post no
        tool result, since a blocked call never runs); otherwise auto-answer
        the call so the turn completes.
        """
        if deny:
            return await self.run_turn(
                f"Call the {_BENCH_TOOL_NAME} tool with arg='go'. It is required.",
                tools=_BENCH_TOOL_SPEC,
                deny_phases=frozenset({PHASE_TOOL_CALL}),
                policy_reason=_BENCH_DENY_REASON,
                timeout=150.0,
            )
        return await self.run_turn(
            f"You must call the {_BENCH_TOOL_NAME} tool with arg='go', "
            "then reply with the tool's output verbatim.",
            tools=_BENCH_TOOL_SPEC,
            auto_tool_output="bench-tool-ok",
            timeout=150.0,
        )

    async def run_policy_turn(self, *, action: str) -> TurnResult:
        """The wrap-direct path has no server-side policy/elicitation surface, so
        an explicit ALLOW/ASK verdict cannot be observed here. Return an
        unmeasured result so the policy_allow / policy_ask probes SKIP (never a
        false verdict), mirroring how Policy DENY reports on this transport."""
        return TurnResult()

    async def run_interrupt_turn(self) -> TurnResult:
        return await self.run_turn(_LONG_PROMPT, interrupt_on_first_delta=True, timeout=120.0)

    async def _drive(
        self,
        body: dict[str, Any],
        result: TurnResult,
        deny_phases: frozenset[str],
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
                    elif etype == "response.output_item.done":
                        # Server-dispatched tool calls arrive as an
                        # output_item.done carrying a function_call item with
                        # status "action_required" (see _scaffold.dispatch_tool).
                        # We must answer with a tool_result or the turn parks
                        # forever waiting on the dispatch future.
                        item = event.get("item") or {}
                        if (
                            item.get("type") == "function_call"
                            and item.get("status") == "action_required"
                        ):
                            call_id = item.get("call_id", "")
                            result.tool_calls.append(item)
                            if auto_tool_output is not None:
                                await self._post(
                                    {
                                        "type": "tool_result",
                                        "call_id": call_id,
                                        "output": auto_tool_output,
                                    }
                                )
                    elif etype == "policy_evaluation.requested":
                        # Answer DENY only for phases the caller asked to deny
                        # (e.g. PHASE_TOOL_CALL); ALLOW every other phase so a
                        # request/result-phase evaluation cannot be mistaken
                        # for a tool-call guardrail.
                        phase = str(event.get("phase", ""))
                        action = POLICY_DENY if phase in deny_phases else POLICY_ALLOW
                        verdict: dict[str, Any] = {
                            "type": "policy_verdict",
                            "evaluation_id": event["evaluation_id"],
                            "action": action,
                        }
                        if action == POLICY_DENY and policy_reason is not None:
                            verdict["reason"] = policy_reason
                        # Record only after a successful post, so a raced /
                        # rejected verdict is not counted as delivered.
                        if await self._post(verdict):
                            result.policy_actions.append((phase, action))
                            if action == POLICY_DENY and phase == PHASE_TOOL_CALL:
                                result.tool_call_denied = True
                    elif etype == "response.completed":
                        result.completed = True
                        # The wrap-direct path has no server snapshot; usage, if
                        # the wrap forwards it, rides on the completed response.
                        # Absent -> stays None and the cost probe SKIPs.
                        usage = (event.get("response") or {}).get("usage") or {}
                        tok = usage.get("total_tokens")
                        if isinstance(tok, int):
                            result.total_tokens = tok
                        cost = usage.get("cost_usd")
                        if isinstance(cost, (int, float)):
                            result.total_cost_usd = float(cost)
                    elif etype == "response.cancelled":
                        result.cancelled = True
                    elif etype == "response.failed":
                        result.failed = True
                        result.error = event.get("error") or event.get("response", {}).get("error")

    async def _post(self, payload: dict[str, Any]) -> bool:
        """POST a downward event on the wrap's events endpoint (best-effort).

        Downward events race the turn's terminal state (e.g. an interrupt
        landing just as the turn ends). A failed post here is benign — the
        probe reads the outcome from the stream — so the error is suppressed.

        :returns: ``True`` if the post got a non-error response, else
            ``False``. Callers that record a verdict as "delivered" gate on
            this so a raced/rejected post is not counted.
        """
        assert self._client is not None
        try:
            resp = await self._client.post(f"/v1/sessions/{_CONV_ID}/events", json=payload)
        except httpx.HTTPError:
            return False
        return not resp.is_error


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
