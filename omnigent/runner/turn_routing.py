"""In-harness first-message turn routing: loopback endpoint + replay.

The sibling of :mod:`omnigent.runner.subagent_routing`, for the MAIN
agent's model rather than a subagent spawn. A harness
``UserPromptSubmit`` hook calls this before the first real user prompt
runs to ask which model the session should be on. The gate is
**advisory**: every transport failure, unreachable endpoint, unparseable
verdict or router outage lets the prompt run unrouted, because a turn
that dies on routing infrastructure is worse than a turn on the launch
model. Two halves live here:

* **Runner side** — a loopback HTTP relay (:func:`start_turn_router`) on
  ``127.0.0.1:0``, bearer-token authenticated, advertised to the hook via
  ``turn_router.json`` in the session bridge dir. It also owns the
  **replay** (:func:`schedule_replay`), because only the runner can see
  the harness's turn bookkeeping.
* **Server side** — the policy (:func:`resolve_turn_route`), which runs
  where ``RuntimeCaps.routing_client`` lives, pins ``model_override`` and
  records the decision chip.

The two halves are joined by the server relay route
:data:`SERVER_ROUTE_PATH` (registered in
``omnigent/server/routes/sessions/routes_hooks.py``); the runner's
handler forwards to it with :func:`make_server_relay_resolver`.

**The authoritative gate is** :func:`already_routed` — the session's
routing-decision label. The hook's local :data:`MARKER_FILE` is only a
fast skip that saves a round trip; it is never the source of truth, and it
is scoped to the session that wrote it because the bridge dir it lives in
outlives that session (``/clear`` rotations, forks).

**Block-and-replay.** Neither harness can retarget the turn that is
already in flight — codex binds the turn's model before
``UserPromptSubmit`` runs (design plan §3b, spike S1), and claude's hook
output carries no model at all. So the hook blocks the prompt, the switch
is applied while nothing is running, and this module replays the captured
prompt as a normal user turn — which then runs routed. The two harnesses
differ only in *who* applies the switch: codex's hook does it over the
app-server RPC, while claude's must be done from the runner
(:func:`_apply_routed_model`) because the pane is frozen on the hook. The
ordering handshake is documented on :func:`schedule_replay`.

Once the hook blocks, the prompt exists **only** in the replay, and the
block marker stops any later hook from asking again — so a replay that
never lands loses the prompt for good and leaves the session with a
routing chip and no turn. :data:`PENDING_FILE` is the durability half:
the prompt is recorded before the block and cleared once it is delivered,
and :func:`schedule_pending_replay_recovery` drains whatever a dead
launch left behind.

**Timeout budget.** Four hops wait on each other, so each one's budget is
strictly larger than the hop it waits on — otherwise an inner hop's
fail-open branch can never run. Canonical values, outermost first:

1. harness hook timeout — :data:`HARNESS_HOOK_TIMEOUT_S` (15s, registered
   on each harness's ``UserPromptSubmit`` entry)
2. hook script HTTP request + model switch —
   :data:`HOOK_REQUEST_TIMEOUT_S` (8s) then
   :data:`SETTINGS_UPDATE_TIMEOUT_S` (5s)
3. runner loopback relay wait — :data:`RELAY_TIMEOUT_S` (7s)
4. server relay hop — :data:`SERVER_HOP_TIMEOUT_S` (6s)

Inside hop 4 sits the routing call itself
(:data:`omnigent.server.smart_routing.ROUTING_REQUEST_TIMEOUT_S`, 5s).

**The ladder is tight on purpose.** This is a hazard path: the prompt is
frozen in the pane until the hook answers, so a fail-open that takes 30
seconds is blocking in practice even though nothing errored. Every step is
one second, from the 5s routing call outwards, so a wedged server costs a
visible pause rather than a hang — and each hop still has room to run its
own fail-open branch before the hop above it gives up.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import secrets
import sys
import threading
import time
import urllib.parse
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from omnigent.runner.subagent_routing import routing_enabled, write_advertisement

if TYPE_CHECKING:
    import httpx

_logger = logging.getLogger(__name__)

#: Bridge-dir file that advertises the loopback endpoint to the hook.
ADVERTISEMENT_FILE = "turn_router.json"

#: Bridge-dir file the hook writes immediately before it blocks a prompt.
#: Two jobs: the next prompt's hook fast-skips on it (no network), and the
#: runner reads it as "this prompt was blocked, you owe it a replay". Absent
#: means the hook let the prompt run.
#:
#: **Scoped to the session that wrote it.** A bridge dir outlives the
#: conversation keyed on it — a ``/clear`` rotation re-keys the superseded
#: session and hands the SAME live dir to the new one, and a fork inherits its
#: parent's bridge id — so a bare file would make every later conversation in
#: the pane fast-skip on a verdict that was never theirs. The session id (and
#: the decision it belongs to) go INSIDE the file and a mismatch reads as
#: absent; see :func:`turn_routing_marker_present`.
MARKER_FILE = "turn_routing_done"

#: Bridge-dir file holding the prompt a routed verdict still owes a replay.
#: Written before the hook blocks and removed once the prompt is delivered
#: (or once the hook is known to have fallen open), so a runner that dies
#: mid-handshake can still deliver it on the next launch. Without it the
#: prompt lives only in an in-memory task and the marker guarantees no hook
#: will ever ask again — the blocked prompt is silently lost forever.
PENDING_FILE = "turn_replay_pending.json"

#: Bridge-dir file every hook invocation appends one line to, whatever it
#: decides. A hook that falls open before its POST is invisible otherwise —
#: the harness discards its stdout and the runner never hears from it — so
#: "the session never routed" and "the harness never fired the hook" read
#: identically in the logs. This file separates them.
TRACE_FILE = "turn_routing.log"

#: Path served by the loopback relay.
ROUTE_PATH_TEMPLATE = "/v1/sessions/{session_id}/route-turn"

#: Server relay route the runner-side handler forwards to (the server
#: process is the only one holding ``RuntimeCaps.routing_client``).
SERVER_ROUTE_PATH = "/v1/sessions/{session_id}/hooks/route-turn"

#: Hop 1: the timeout registered on the harness's hook entry. Covers the
#: hook script's whole life (hop 2a + hop 2b) plus its cold start, so the
#: harness only steps in when the script itself wedged. Stays below Claude
#: Code's own 30s ``UserPromptSubmit`` default — the harness must not be the
#: layer that gives up first.
HARNESS_HOOK_TIMEOUT_S = 22

#: Hop 2a: the hook script's HTTP budget for the routing verdict. This is
#: the user-visible cost of a wedged router — the prompt sits in the pane
#: until it expires — but it must also outlast a HEALTHY route, which is
#: the whole server-side path: candidate/catalog preparation (~3s measured)
#: before the ``routes:select`` call, then the call itself. A tighter budget
#: loses the race with a router that answered, wasting the attempt.
HOOK_REQUEST_TIMEOUT_S = 15.0

#: Hop 2b: the codex hook's ``thread/settings/update`` budget, after the
#: verdict. Claude's hook applies nothing, so it has no hop 2b. A local
#: app-server RPC over a unix socket, so seconds is already generous.
SETTINGS_UPDATE_TIMEOUT_S = 5.0

#: Hop 3: seconds the runner's loopback relay waits for a verdict.
RELAY_TIMEOUT_S = 14.0

#: Hop 4: seconds the runner waits on the server relay route. The whole
#: server-side route runs inside this — catalog preparation and then the
#: routing call (``ROUTING_REQUEST_TIMEOUT_S``), not the call alone.
SERVER_HOP_TIMEOUT_S = 13.0

#: Seconds the replay waits for the hook to confirm it blocked the prompt
#: (:data:`MARKER_FILE`). Timing out means the hook fell open, so the
#: prompt already ran and the replay is abandoned.
REPLAY_MARKER_WAIT_S = 20.0

#: Seconds the replay then waits for the blocked turn to clear.
REPLAY_IDLE_WAIT_S = 60.0

#: Poll interval for both waits.
REPLAY_POLL_S = 0.1

#: How long "no active turn" must hold before the replay trusts it, when
#: the blocked turn was never observed as active (the harness bookkeeping
#: may simply not have caught up yet).
REPLAY_IDLE_GRACE_S = 2.0

#: Seconds a recovered replay waits for the relaunched harness to publish a
#: thread it can be delivered to.
RECOVERY_READY_WAIT_S = 120.0

#: Items read back when checking whether a recovered prompt already ran.
#: The replay is always the session's first user turn, so the oldest page
#: is the only one that can contain it.
RECOVERY_ITEM_SCAN = 100

#: Scope recorded on the decision chip. The same value the server's
#: composer turn gate uses, so the UI treats both triggers alike.
_SCOPE = "turn"

#: Prompt text handed to the router, capped like the subagent path.
_PROMPT_CAP = 4000

#: Hex characters kept from a create-route prompt fingerprint. Long enough
#: that two prompts in one session never collide, short enough to keep the
#: label small.
_FINGERPRINT_CHARS = 32

#: Coroutine that returns ``(model, verdict)`` for one turn — the
#: ``omnigent.server.smart_routing.route_turn`` seam, injected so the
#: policy is testable without a routing client.
RouteTurnFn = Callable[[str | None, str], Awaitable[tuple[str | None, dict[str, Any] | None]]]

Resolver = Callable[[str, "TurnRouteRequest"], Awaitable["TurnRouteDecision"]]


async def _awaited(pending: Awaitable[TurnRouteDecision]) -> TurnRouteDecision:
    """Wrap a resolver's awaitable as a coroutine for the handler thread.

    ``asyncio.run_coroutine_threadsafe`` accepts coroutines only, while a
    :data:`Resolver` may return any awaitable.

    :param pending: The resolver's awaitable result.
    :returns: The resolved decision.
    """
    return await pending


# ── Hook-side trace ────────────────────────────────────────────────────────


def trace_turn_routing(bridge_dir: Path, outcome: str, detail: str = "") -> None:
    """Append one line describing what a hook invocation decided.

    Called from harness hook subprocesses, which have no logger and whose
    stdout is the hook protocol. Every exit path traces, so a session that
    did not route always says which gate it stopped at — the alternative is
    the state this seam shipped in, where an absent endpoint, a rejected
    advertisement and a harness that never fired the hook were all
    indistinguishable from the outside.

    Never raises and never blocks the prompt: a trace that cannot be
    written is less bad than a wedged turn.

    :param bridge_dir: Session bridge directory holding :data:`TRACE_FILE`.
    :param outcome: Short verdict tag — ``"route"``, ``"allow"``,
        ``"skip"`` or ``"fail-open"``.
    :param detail: One-line reason, e.g. ``"no usable turn_router.json"``.
    :returns: None.
    """
    line = json.dumps(
        {"at": time.time(), "outcome": outcome, "detail": detail},
        separators=(",", ":"),
    )
    try:
        with (bridge_dir / TRACE_FILE).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    # Also on stderr: the harness keeps hook stderr in its own logs, which
    # outlive a bridge dir the session teardown removes.
    print(f"omnigent route-turn hook: {outcome}: {detail}", file=sys.stderr)


# ── Wire types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TurnRouteRequest:
    """One submitted prompt awaiting a routing verdict.

    :param harness: Requesting harness id, e.g. ``"codex-native"``.
    :param prompt: The prompt text the user just submitted.
    :param turn_id: Harness turn id the prompt was submitted on, from the
        hook payload. Used to recognize when the blocked turn has cleared.
    :param model: The harness's LIVE model, from the hook payload. Never
        read from ``config.toml``, which reports the stale launch model.
    """

    harness: str
    prompt: str
    turn_id: str | None = None
    model: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> TurnRouteRequest:
        """Parse a request body into a :class:`TurnRouteRequest`.

        :param payload: Decoded JSON object from the hook script.
        :returns: Parsed request.
        :raises ValueError: If ``harness`` or ``prompt`` is missing.
        """
        harness = payload.get("harness")
        if not isinstance(harness, str) or not harness.strip():
            raise ValueError("route-turn body requires a non-empty 'harness' string")
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("route-turn body requires a non-empty 'prompt' string")
        return cls(
            harness=harness.strip(),
            prompt=prompt,
            turn_id=_opt_str(payload.get("turn_id")),
            model=_opt_str(payload.get("model")),
        )


@dataclass(frozen=True)
class TurnRouteDecision:
    """The verdict the hook enforces on a submitted prompt.

    :param action: ``"route"`` (block the prompt, switch to ``model``, and
        let the runner replay it) or ``"allow"`` (nothing routed — the
        prompt runs untouched).
    :param rationale: One-line explanation; logged, and shown on the chip.
    :param model: Servable model id to switch to; set for ``"route"``.
    :param terminal: ``True`` when this session will never route again, so
        the hook may write its fast-skip marker. ``False`` for conditions
        that can change mid-session (routing toggled off).
    :param decision_id: Identity shared by the response and the chip.
    """

    action: Literal["route", "allow"]
    rationale: str
    model: str | None = None
    terminal: bool = False
    decision_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_payload(self) -> dict[str, Any]:
        """Serialize to the response shape the hook reads.

        :returns: JSON-ready response body.
        """
        return {
            "action": self.action,
            "model": self.model,
            "rationale": self.rationale,
            "terminal": self.terminal,
            "decision_id": self.decision_id,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> TurnRouteDecision:
        """Parse a response body (used by the runner-side relay).

        :param payload: Decoded JSON response from the server route.
        :returns: Parsed decision; an unknown action degrades to
            ``"allow"``, and a ``"route"`` with no model does too — the
            hook has nothing to switch to.
        """
        action = payload.get("action")
        model = _opt_str(payload.get("model"))
        if action != "route" or model is None:
            action = "allow"
        rationale = payload.get("rationale")
        decision_id = payload.get("decision_id")
        return cls(
            action=action,
            rationale=rationale if isinstance(rationale, str) else "",
            model=model,
            terminal=bool(payload.get("terminal")),
            decision_id=decision_id if isinstance(decision_id, str) else str(uuid.uuid4()),
        )


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


# ── The block marker (session-scoped) ──────────────────────────────────────


def write_turn_routing_marker(
    bridge_dir: Path,
    *,
    session_id: str,
    decision_id: str | None = None,
) -> bool:
    """Record that *session_id*'s route-turn hook has had its one answer.

    Called from harness hook subprocesses. The session id is written INSIDE
    the file rather than implied by the directory, because the directory is
    shared: see :data:`MARKER_FILE`.

    :param bridge_dir: Session bridge directory.
    :param session_id: Session the verdict belongs to.
    :param decision_id: Verdict identity, for diagnosing a stale marker.
    :returns: ``True`` when the marker is on disk. A marker that cannot be
        written must stop the hook from blocking — it is the runner's "you
        owe this prompt a replay" handshake.
    """
    body = {"session_id": session_id, "decision_id": decision_id, "at": time.time()}
    try:
        (bridge_dir / MARKER_FILE).write_text(json.dumps(body), encoding="utf-8")
    except OSError:
        return False
    return True


def turn_routing_marker_session(bridge_dir: Path) -> str | None:
    """Return the session id the bridge dir's marker was written for.

    :param bridge_dir: Session bridge directory.
    :returns: The session id, or ``None`` when there is no marker, it is
        unreadable, or it predates the scoping (a bare file).
    """
    try:
        raw = json.loads((bridge_dir / MARKER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _opt_str(raw.get("session_id")) if isinstance(raw, dict) else None


def turn_routing_marker_present(bridge_dir: Path, session_id: str) -> bool:
    """Report whether *session_id*'s own block marker is on disk.

    A marker naming a different session reads as ABSENT — that is the whole
    point of scoping it. Bridge dirs outlive conversations (a ``/clear``
    rotation hands the live dir to a new session, a fork inherits its
    parent's), so a bare marker made every later conversation in the pane
    fast-skip on a verdict that was never theirs, and attribute the routing
    it never got to the old session id.

    Reading a stale marker as absent cannot regress route-once: the
    authoritative gate is the server's routing-decision label
    (:func:`already_routed`), and this file only ever saves a round trip.

    :param bridge_dir: Session bridge directory.
    :param session_id: Session asking.
    :returns: ``True`` only when the marker names *session_id*.
    """
    return turn_routing_marker_session(bridge_dir) == session_id


def _allow(reason: str, *, terminal: bool = False) -> TurnRouteDecision:
    """Let the prompt run unrouted and say why."""
    return TurnRouteDecision(action="allow", rationale=reason, terminal=terminal)


# ── Policy (server side) ───────────────────────────────────────────────────


def already_routed(conv: Any) -> bool:
    """Report whether this session has ever had a routing decision.

    The authoritative "route once" gate, and deliberately NOT
    ``model_override``: on codex-native the forwarder mirrors
    ``config.toml``'s model into ``model_override`` at the first
    ``turn/started``, which lands about a second into the turn — racing,
    and usually beating, this hook's round trip. Measured on a bare launch,
    that mirrored value was not even the model the thread was running (the
    stale-config trap), so neither "is it set?" nor "does it match the live
    model?" separates a real pin from the mirror. The decision label is
    written by every routing trigger (create-time, the composer turn gate,
    and this one) and by nothing else, so it is the honest signal.

    Residual gap: a session that carries a manual ``--model`` / picker pin
    AND leaves Smart Routing on gets routed once by this hook, where the
    composer gate would have declined. Closing it needs provenance on
    ``model_override`` — either the pin sites labelling their writes, or
    the forwarder no longer posting the launch model as if the user had
    switched to it. Both are changes to shipping paths that this addition
    deliberately leaves alone.

    :param conv: Conversation row for the session.
    :returns: ``True`` when something already routed this session.
    """
    from omnigent.runner.subagent_routing import ROUTING_DECISION_LABEL_KEY

    labels = getattr(conv, "labels", None) or {}
    return bool(labels.get(ROUTING_DECISION_LABEL_KEY))


def out_of_parent_family(
    conv: Any,
    parent: Any,
    parent_harness: str | None,
    harness: str | None,
) -> bool:
    """Report whether this pane runs outside its parent's routed family.

    A pinned Smart Routing parent routes its spawns inside its own family,
    so a child pane on another family's CLI has no candidate its parent's
    family could serve — routing it here would pick a model the pane cannot
    speak. The create gate refuses such a child outright, so this only
    catches a pane that exists anyway (a row created before that gate).

    :param conv: Conversation row for the pane.
    :param parent: Conversation row for its parent, or ``None``.
    :param parent_harness: The parent's resolved harness, e.g. ``"codex"``.
    :param harness: The pane's own harness, from the hook payload.
    :returns: ``True`` when the pane must not be routed.
    """
    from omnigent.runner.subagent_routing import (
        auto_harness_session,
        harness_family,
        subagent_routing_enabled,
    )

    if parent is None or conv is None:
        return False
    if not subagent_routing_enabled(getattr(parent, "subagent_routing_override", None)):
        return False
    if auto_harness_session(conv, parent):
        return False
    parent_family = harness_family(parent_harness)
    own_family = harness_family(harness)
    return parent_family is not None and own_family is not None and parent_family != own_family


def create_route_prompt_fingerprint(prompt: str) -> str:
    """Fingerprint a prompt for the create-route reuse check.

    Whitespace-insensitive at the edges only: the harness submits the prompt
    the create routed, but a launcher may add or drop a trailing newline.
    Anything else is a different prompt and must route on its own.

    :param prompt: The prompt text.
    :returns: A hex digest, empty for a blank prompt.
    """
    text = prompt.strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_FINGERPRINT_CHARS]


def create_route_covers_prompt(conv: Any, prompt: str) -> bool:
    """Report whether a create-time route already scored *prompt*.

    A native Smart Routing create routes the landing screen's prompt and pins
    what it picked; the harness then submits that same prompt, and this hook
    would score it again — a second router call, seconds later, for a verdict
    the session is already running on. The create records a fingerprint of what
    it routed, so the repeat is recognizable; a prompt the user edited before
    sending does not match and routes on its own.

    :param conv: Conversation row for the session.
    :param prompt: The submitted prompt.
    :returns: ``True`` when this prompt was already routed at create.
    """
    from omnigent.runner.subagent_routing import CREATE_ROUTE_PROMPT_LABEL_KEY

    labels = getattr(conv, "labels", None) or {}
    recorded = labels.get(CREATE_ROUTE_PROMPT_LABEL_KEY)
    return bool(recorded) and recorded == create_route_prompt_fingerprint(prompt)


async def _record_turn_decline(
    record_decline: Callable[[str], Awaitable[None]] | None,
    cause: str,
    session_id: str,
) -> None:
    """Persist the declined chip for a failed routing call, best-effort.

    The chip is a record, not a gate: a persist failure must not change the
    fail-open verdict, so every error is swallowed into a log line.

    :param record_decline: The caller's persistence coroutine, or ``None``.
    :param cause: Why nothing was routed, e.g. ``"Routing call failed: 401"``.
    :param session_id: Session the decline belongs to, for the log line.
    :returns: None.
    """
    if record_decline is None:
        return
    try:
        await record_decline(cause)
    except Exception:
        _logger.exception("route-turn: decline record failed for session=%s", session_id)


async def resolve_turn_route(
    session_id: str,
    req: TurnRouteRequest,
    *,
    conv: Any,
    parent: Any = None,
    parent_harness: str | None = None,
    route_turn: RouteTurnFn,
    reuse_create_route: Callable[[], Awaitable[bool]] | None = None,
    pin: Callable[[str], Awaitable[bool]] | None = None,
    persist: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    record_decline: Callable[[str], Awaitable[None]] | None = None,
) -> TurnRouteDecision:
    """Decide what happens to one submitted prompt.

    Order matters and is the whole re-entrancy story: the gates are checked
    FIRST and the pin is written BEFORE the verdict is returned, so a second
    hook call — including the one the replayed prompt fires — cannot route
    again even if it never saw the local marker.

    :param session_id: Session/conversation identifier.
    :param req: The prompt awaiting a verdict.
    :param conv: Conversation row for the session, or ``None``.
    :param parent: Conversation row for its parent, when it has one.
    :param parent_harness: The parent's resolved harness, for the family
        guard. ``None`` skips it (nothing to compare against).
    :param route_turn: The routing seam, called as
        ``route_turn(harness, prompt)``.
    :param reuse_create_route: Coroutine claiming the create-time decision as
        this session's routing decision, for a prompt the create already
        routed; returns ``False`` when there is nothing to claim (or the
        claim failed), and the prompt is then routed normally. ``None``
        skips the reuse path.
    :param pin: Coroutine persisting ``model_override``; returns ``False``
        when the write failed (routing is then declined, because an
        unpinned route would re-route on every later prompt). ``None``
        skips the pin (unit tests).
    :param persist: Coroutine recording the decision chip, called as
        ``persist(model, verdict)``. ``None`` skips persistence.
    :param record_decline: Coroutine persisting a declined chip when a
        routing CALL failed, called with the cause. Only the failure
        branches use it — the benign allows (already routed, routing off,
        family guard) are not failures and stay chipless. ``None`` skips it.
    :returns: The verdict the hook enforces.
    """
    from omnigent.codex_model_vocabulary import comparable_model_id

    if conv is None:
        return _allow("session not found")
    if already_routed(conv):
        return _allow("this session already has a routing decision", terminal=True)
    if not routing_enabled(
        getattr(conv, "cost_control_mode_override", None),
        parent_cost_control_mode=getattr(parent, "cost_control_mode_override", None),
    ):
        # Terminal: the hook is only registered for a session that launched
        # with routing on, so this answer means it was turned off afterwards.
        # Left non-terminal, every prompt for the rest of the session paid a
        # full round trip (the hook's whole HTTP budget on a degraded server)
        # to be told the same thing. The cost of terminality is narrow — off
        # and back on again before the FIRST prompt no longer routes that
        # prompt in the TUI; the composer gate and create-time path are
        # unaffected.
        return _allow("smart routing is off for this session", terminal=True)
    if out_of_parent_family(conv, parent, parent_harness, req.harness):
        # Non-terminal: the parent's subagent-routing switch is togglable, and
        # a pane this fires for is not supposed to exist at all (the create
        # gate refuses it), so the extra round trip buys a guard that cannot
        # go stale.
        _logger.info(
            "route-turn: session=%s harness=%s runs outside its parent's routed "
            "family; allowing unrouted",
            session_id,
            req.harness,
        )
        return _allow("this session's parent routes only its own model family")
    if create_route_covers_prompt(conv, req.prompt) and (
        reuse_create_route is None or await reuse_create_route()
    ):
        # The create already routed this exact prompt and pinned what it
        # picked, so the pane is running the verdict. Routing again would cost
        # a second judge call for the same answer — and block the prompt to
        # "switch" onto the model it is already on.
        _logger.info(
            "route-turn: session=%s reuses the create-time decision for this prompt",
            session_id,
        )
        return _allow("this session was routed at create on this prompt", terminal=True)

    try:
        model, verdict = await route_turn(req.harness, req.prompt[:_PROMPT_CAP])
    except Exception as exc:  # noqa: BLE001 — a router outage must never block a turn
        _logger.warning(
            "route-turn: router call failed for session=%s; allowing unrouted",
            session_id,
            exc_info=True,
        )
        await _record_turn_decline(
            record_decline, f"Routing call failed: {type(exc).__name__}", session_id
        )
        return _allow("routing unavailable (router call failed)")
    if not model or verdict is None:
        _logger.info(
            "route-turn: no verdict for session=%s harness=%s; allowing unrouted",
            session_id,
            req.harness,
        )
        await _record_turn_decline(
            record_decline, "Routing unavailable (router returned no verdict)", session_id
        )
        return _allow("routing unavailable (no verdict)")
    # Spelling-insensitive: codex reports its live model as a dotted slug
    # (``gpt-5.6-luna``) where the catalog writes dashes and a prefix
    # (``databricks-gpt-5-6-luna``), and claude reports whatever the picker
    # last showed. A raw ``==`` never matched for either, so a pick equal to
    # the model already running still blocked the prompt and replayed it —
    # a needless block, a needless ``/model`` echo, and a turn the user
    # watched disappear and come back.
    already_on_model = bool(req.model) and comparable_model_id(model) == comparable_model_id(
        req.model or ""
    )
    if already_on_model:
        _logger.info("route-turn: session=%s already on routed model=%s", session_id, model)

    # Both verdicts pin and record: the decision row and the label are what
    # the route-once gate reads, so a no-op that skipped them would re-route
    # on the next prompt.
    if pin is not None and not await pin(model):
        return _allow("routing unavailable (could not pin the routed model)")
    if persist is not None:
        try:
            await persist(model, verdict)
        except Exception:
            _logger.exception("route-turn: decision persist failed for session=%s", session_id)
    rationale = verdict.get("rationale")
    return TurnRouteDecision(
        # A no-op verdict must be terminal AND unblocking: there is nothing to
        # switch and nothing to replay, so the hook fast-paths on it.
        action="allow" if already_on_model else "route",
        rationale=rationale if isinstance(rationale, str) and rationale else f"Routed to {model}",
        model=model,
        terminal=True,
    )


def decision_scope() -> str:
    """Return the chip scope route-turn decisions are recorded under.

    :returns: ``"turn"`` — the same scope the composer turn gate uses, so
        the UI cannot tell the two triggers apart.
    """
    return _SCOPE


# ── Runner-side loopback relay ─────────────────────────────────────────────


@dataclass
class TurnRouter:
    """Handle for the running loopback turn router.

    :param bridge_dir: Bridge dir holding the advertisement file.
    :param session_id: Session this router serves.
    :param url: Base URL the hook POSTs to.
    :param token: Bearer token the hook presents.
    :param httpd: The backing HTTP server.
    """

    bridge_dir: Path
    session_id: str
    url: str
    token: str
    httpd: ThreadingHTTPServer
    _closed: bool = False

    def close(self) -> None:
        """Stop the server and remove its own advertisement.

        Idempotent and safe from any thread. Only an advertisement still
        naming this router's url is removed, so a newer router that reused
        the bridge dir keeps its rendezvous.
        """
        if self._closed:
            return
        self._closed = True
        self.httpd.shutdown()
        self.httpd.server_close()
        path = self.bridge_dir / ADVERTISEMENT_FILE
        try:
            advertised = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(advertised, dict) and advertised.get("url") == self.url:
            with contextlib.suppress(OSError):
                path.unlink()


def start_turn_router(
    *,
    bridge_dir: Path,
    session_id: str,
    resolver: Resolver,
    loop: asyncio.AbstractEventLoop,
    request_timeout_s: float = RELAY_TIMEOUT_S,
) -> TurnRouter:
    """Start the loopback turn router and advertise it in *bridge_dir*.

    :param bridge_dir: Session bridge directory — the same one the
        harness's hook command is pointed at.
    :param session_id: Session this router serves; requests for any other
        session id are rejected.
    :param resolver: Coroutine function returning a decision.
    :param loop: Event loop that owns *resolver*.
    :param request_timeout_s: Seconds to wait for a verdict before letting
        the prompt run unrouted. Hop 3 of the module's timeout budget.
    :returns: Started router handle; call :meth:`TurnRouter.close` when
        the session ends.
    """
    token = secrets.token_urlsafe(32)
    handler_cls = _handler_factory(session_id, token, resolver, loop, request_timeout_s)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    # Handler threads must not hold up ``close()``: the default joins every
    # in-flight request, stalling session teardown.
    httpd.daemon_threads = True
    url = f"http://{httpd.server_address[0]}:{httpd.server_address[1]}"
    write_advertisement(
        bridge_dir,
        url=url,
        token=token,
        session_id=session_id,
        filename=ADVERTISEMENT_FILE,
    )
    router = TurnRouter(
        bridge_dir=bridge_dir,
        session_id=session_id,
        url=url,
        token=token,
        httpd=httpd,
    )
    threading.Thread(
        target=httpd.serve_forever,
        name="omnigent-turn-router",
        daemon=True,
    ).start()
    return router


def _handler_factory(
    session_id: str,
    token: str,
    resolver: Resolver,
    loop: asyncio.AbstractEventLoop,
    request_timeout_s: float,
) -> type[BaseHTTPRequestHandler]:
    """Build the request handler class for one session's turn router."""
    expected_path = ROUTE_PATH_TEMPLATE.format(session_id=urllib.parse.quote(session_id, safe=""))
    # Compared as bytes: ``compare_digest`` raises TypeError on a non-ASCII
    # str, and http.server decodes headers as latin-1.
    expected_auth = f"Bearer {token}".encode()

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            presented = (self.headers.get("Authorization") or "").encode("latin-1", "replace")
            if not secrets.compare_digest(presented, expected_auth):
                self._reject(401, "unauthorized")
                return
            if _unquoted_path(self.path) != expected_path:
                self._reject(404, "not found")
                return
            try:
                payload = json.loads(self._read_body() or b"{}")
                if not isinstance(payload, dict):
                    raise ValueError("body must be a JSON object")
                req = TurnRouteRequest.from_payload(payload)
            except (ValueError, TypeError) as exc:
                self._send(400, {"error": str(exc)})
                return
            try:
                future = asyncio.run_coroutine_threadsafe(
                    _awaited(resolver(session_id, req)), loop
                )
                decision = future.result(timeout=request_timeout_s)
            except Exception:  # noqa: BLE001 — never wedge a user's prompt
                _logger.warning(
                    "route-turn: resolver failed for session=%s", session_id, exc_info=True
                )
                decision = _allow("routing endpoint failed")
            self._send(200, decision.to_payload())

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length > 0 else b""

        def _reject(self, status: int, error: str) -> None:
            # Keep-alive is on (HTTP/1.1), so an undrained body would be
            # parsed as the next request's start line.
            with contextlib.suppress(OSError, ValueError):
                self._read_body()
            self.close_connection = True
            self._send(status, {"error": error}, close=True)

        def _send(self, status: int, body: dict[str, Any], *, close: bool = False) -> None:
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            if close:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format: str, *args: Any) -> None:
            _logger.debug("turn-router: " + format, *args, extra={"session_id": session_id})

    return _Handler


def _unquoted_path(path: str) -> str:
    """Return the request path without a trailing slash or query."""
    return path.split("?", 1)[0].rstrip("/")


def make_server_relay_resolver(
    server_client: Any,
    *,
    bridge_dir: Path,
    timeout_s: float = SERVER_HOP_TIMEOUT_S,
    harness: str | None = None,
) -> Resolver:
    """Build a resolver that forwards to the server's relay route.

    A routed verdict also arms the replay before it is handed back, so the
    runner is already waiting by the time the hook blocks the prompt.

    :param server_client: Async HTTP client pointed at the AP server.
    :param bridge_dir: Session bridge directory, for the replay handshake.
    :param timeout_s: Per-request timeout. Hop 4 (innermost).
    :param harness: Harness this router serves, which selects the replay's
        settle probe. ``None`` falls back to the request's own ``harness``.
    :returns: Resolver for :func:`start_turn_router`.
    """

    async def _resolve(session_id: str, req: TurnRouteRequest) -> TurnRouteDecision:
        body = {
            "harness": req.harness,
            "prompt": req.prompt,
            "turn_id": req.turn_id,
            "model": req.model,
        }
        try:
            resp = await server_client.post(
                SERVER_ROUTE_PATH.format(session_id=urllib.parse.quote(session_id, safe="")),
                json=body,
                timeout=timeout_s,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception:  # noqa: BLE001 — server hop failures are expected
            _logger.warning(
                "route-turn: server relay failed for session=%s", session_id, exc_info=True
            )
            return _allow("routing server unreachable")
        if not isinstance(payload, dict):
            return _allow("unreadable verdict from routing server")
        decision = TurnRouteDecision.from_payload(payload)
        if decision.action == "route":
            # On disk before the verdict is handed back, so the record is
            # already there when the hook blocks the prompt: from that
            # moment the prompt exists nowhere else.
            write_pending_replay(
                bridge_dir,
                session_id=session_id,
                prompt=req.prompt,
                blocked_turn_id=req.turn_id,
                model=decision.model,
            )
            schedule_replay(
                session_id,
                prompt=req.prompt,
                bridge_dir=bridge_dir,
                blocked_turn_id=req.turn_id,
                server_client=server_client,
                harness=harness or req.harness,
                model=decision.model,
            )
        return decision

    return _resolve


# ── Pending-replay record ──────────────────────────────────────────────────


@dataclass(frozen=True)
class PendingReplay:
    """A blocked prompt that has not been confirmed delivered.

    :param session_id: Session the prompt belongs to.
    :param prompt: The captured prompt text.
    :param blocked_turn_id: Harness turn id it was submitted on, when known.
    :param model: The routed model the replay must run on.
    """

    session_id: str
    prompt: str
    blocked_turn_id: str | None = None
    model: str | None = None


def write_pending_replay(
    bridge_dir: Path,
    *,
    session_id: str,
    prompt: str,
    blocked_turn_id: str | None,
    model: str | None = None,
) -> bool:
    """Record that *prompt* is owed a replay.

    :param bridge_dir: Session bridge directory.
    :param session_id: Session/conversation identifier.
    :param prompt: The captured prompt text.
    :param blocked_turn_id: Harness turn id the prompt was submitted on.
    :param model: The routed model the replay must carry.
    :returns: ``True`` when the record is on disk. A failure is logged and
        degrades to the pre-existing in-memory-only behaviour rather than
        declining the route.
    """
    path = bridge_dir / PENDING_FILE
    body = {
        "session_id": session_id,
        "prompt": prompt,
        "blocked_turn_id": blocked_turn_id,
        "model": model,
        "written_at": time.time(),
    }
    try:
        path.write_text(json.dumps(body), encoding="utf-8")
    except OSError:
        _logger.warning(
            "route-turn: could not record the pending replay for session=%s; "
            "a runner restart before the replay lands would drop the prompt",
            session_id,
            exc_info=True,
        )
        return False
    return True


def read_pending_replay(bridge_dir: Path) -> PendingReplay | None:
    """Read the bridge dir's pending-replay record.

    :param bridge_dir: Session bridge directory.
    :returns: The record, or ``None`` when absent or unreadable.
    """
    try:
        raw = json.loads((bridge_dir / PENDING_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    session_id = _opt_str(raw.get("session_id"))
    prompt = raw.get("prompt")
    if session_id is None or not isinstance(prompt, str) or not prompt.strip():
        return None
    return PendingReplay(
        session_id=session_id,
        prompt=prompt,
        blocked_turn_id=_opt_str(raw.get("blocked_turn_id")),
        model=_opt_str(raw.get("model")),
    )


def clear_pending_replay(bridge_dir: Path) -> None:
    """Drop the pending-replay record. Idempotent.

    :param bridge_dir: Session bridge directory.
    :returns: None.
    """
    with contextlib.suppress(OSError):
        (bridge_dir / PENDING_FILE).unlink()


# ── The replay ─────────────────────────────────────────────────────────────


def schedule_replay(
    session_id: str,
    *,
    prompt: str,
    bridge_dir: Path,
    blocked_turn_id: str | None,
    server_client: Any,
    harness: str | None = None,
    model: str | None = None,
    idle: Callable[[str | None], bool] | None = None,
) -> asyncio.Task[None]:
    """Arm the replay of a prompt the hook is about to block.

    Ordering — the block must be in effect before the replay lands, and
    the prompt must run exactly once. Two file-mediated handshakes with
    the hook do that, both bounded:

    1. **Did the hook actually block?** The hook writes
       :data:`MARKER_FILE` immediately before it emits its block, so the
       marker means "the prompt was dropped, you owe it a replay". No
       marker inside :data:`REPLAY_MARKER_WAIT_S` means the hook fell open,
       the prompt already ran on the old model, and the replay is
       abandoned — a double-run is the one outcome worse than an unrouted
       turn.
    2. **Has the blocked prompt cleared?** :func:`_settle_probe` answers
       that per harness. Delivering into an aborting turn would steer it
       instead of starting a fresh routed one. Once the wait expires the
       prompt is delivered anyway: losing it is worse than delivering it
       early.

    Then, on harnesses whose hook could not apply the model itself, the
    switch happens here (:func:`_apply_routed_model`) — after the block is
    in effect and before the prompt exists, so nothing races it.

    Finally the prompt goes through the **normal events path** — the same
    ``POST /v1/sessions/{id}/events`` the web composer uses — so it is
    persisted, mirrored and executed like any other user turn. That path
    re-checks the routing gate and finds ``model_override`` pinned, so it
    routes nothing and records no second decision.

    The delivery carries the routed model as the event's own
    ``model_override``. That is what makes claude route: its hook cannot
    switch the model, so the switch has to ride the replayed turn, where
    the executor types ``/model`` under its inject lock before the message.
    Codex has already switched its own thread by then, and re-asserting the
    same model there is a no-op.

    :param session_id: Session/conversation identifier.
    :param prompt: The captured prompt text to re-deliver.
    :param bridge_dir: Session bridge directory (marker + bridge state).
    :param blocked_turn_id: Harness turn id the blocked prompt was
        submitted on. ``None`` falls back to the harness's own settle
        signal.
    :param server_client: Runner→server client used to deliver the prompt.
    :param harness: Harness the blocked prompt came from, which selects the
        settle probe and the actuator.
    :param model: Routed model, carried in-band on the replayed turn and
        applied before delivery on harnesses whose hook could not switch it.
        ``None`` skips the switch.
    :param idle: Override for the "has the blocked prompt cleared?" probe,
        taking the blocked turn id. ``None`` picks one by *harness*.
    :returns: The scheduled task (tests await it; callers ignore it).
    """
    return asyncio.get_running_loop().create_task(
        _replay(
            session_id,
            prompt=prompt,
            bridge_dir=bridge_dir,
            blocked_turn_id=blocked_turn_id,
            server_client=server_client,
            harness=harness,
            model=model,
            idle=idle,
        ),
        name=f"turn-routing-replay-{session_id}",
    )


async def _replay(
    session_id: str,
    *,
    prompt: str,
    bridge_dir: Path,
    blocked_turn_id: str | None,
    server_client: Any,
    harness: str | None,
    model: str | None,
    idle: Callable[[str | None], bool] | None,
) -> None:
    """Run the replay handshake and deliver the prompt. See
    :func:`schedule_replay` for the ordering contract."""
    turn_cleared = idle if idle is not None else _settle_probe(bridge_dir, harness)
    # Our OWN marker: a fork or a ``/clear`` rotation can leave another
    # session's marker in this dir, and treating it as ours would replay a
    # prompt whose hook never blocked.
    if not await _wait_for(
        lambda: turn_routing_marker_present(bridge_dir, session_id), REPLAY_MARKER_WAIT_S
    ):
        _logger.warning(
            "route-turn: no block marker for session=%s within %.0fs; "
            "the hook fell open, so the prompt is not replayed",
            session_id,
            REPLAY_MARKER_WAIT_S,
        )
        # The prompt ran on the old model, so nothing is owed — leaving the
        # record would make a later launch deliver it a second time.
        clear_pending_replay(bridge_dir)
        return
    if not await _wait_for_cleared(turn_cleared, blocked_turn_id):
        _logger.warning(
            "route-turn: blocked turn %s never cleared for session=%s; "
            "replaying anyway rather than losing the prompt",
            blocked_turn_id,
            session_id,
        )
    await _apply_routed_model(session_id, bridge_dir=bridge_dir, harness=harness, model=model)
    if not await _deliver_prompt(
        session_id, prompt=prompt, server_client=server_client, model=model
    ):
        _logger.warning(
            "route-turn: the routed model is applied for session=%s but the prompt "
            "was not delivered; it stays recorded for the next launch to replay",
            session_id,
        )
        return
    clear_pending_replay(bridge_dir)
    _logger.info("route-turn: replayed the routed prompt for session=%s", session_id)


async def _deliver_prompt(
    session_id: str,
    *,
    prompt: str,
    server_client: Any,
    model: str | None = None,
) -> bool:
    """POST *prompt* as a normal user turn on the events path.

    :param session_id: Session/conversation identifier.
    :param prompt: Prompt text to deliver.
    :param server_client: Runner→server client.
    :param model: Routed model sent as the event's ``model_override``, so a
        harness whose hook could not switch applies it on this turn.
    :returns: ``True`` when the server accepted the delivery.
    """
    body: dict[str, Any] = {
        "type": "message",
        "data": {
            "role": "user",
            "content": [{"type": "input_text", "text": prompt}],
        },
    }
    if model:
        body["model_override"] = model
    try:
        resp = await server_client.post(
            f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}/events",
            json=body,
            timeout=60.0,
        )
        resp.raise_for_status()
    except Exception:
        _logger.exception("route-turn: replay delivery failed for session=%s", session_id)
        return False
    return True


# ── Crash recovery for a replay that never landed ──────────────────────────


def schedule_pending_replay_recovery(
    session_id: str,
    *,
    bridge_dir: Path,
    server_client: Any,
    ready: Callable[[], bool] | None = None,
) -> asyncio.Task[None] | None:
    """Deliver a blocked prompt a previous launch never got to replay.

    The replay itself is an in-memory task, so a runner that exits between
    the hook's block and the delivery leaves the prompt nowhere: the block
    already consumed it, the marker stops any hook from asking again, and
    the session just sits there with a routing chip and no turn. This drains
    the on-disk record instead, once per launch.

    Skipped unless the block marker is also present — a record without one
    means the hook fell open and the prompt already ran.

    :param session_id: Session/conversation identifier.
    :param bridge_dir: Session bridge directory.
    :param server_client: Runner→server client. ``None`` skips recovery.
    :param ready: Override for the "harness can take a turn" probe. ``None``
        waits for the codex bridge state to publish a thread.
    :returns: The scheduled task, or ``None`` when there is nothing to do.
    """
    if server_client is None:
        return None
    pending = read_pending_replay(bridge_dir)
    if pending is None:
        return None
    if pending.session_id != session_id:
        # A bridge dir shared with another session (a fork inherits the
        # parent's bridge id); that session's prompt is not ours to send.
        return None
    if not turn_routing_marker_present(bridge_dir, session_id):
        clear_pending_replay(bridge_dir)
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    return loop.create_task(
        _recover_pending_replay(
            session_id,
            pending=pending,
            bridge_dir=bridge_dir,
            server_client=server_client,
            ready=ready,
        ),
        name=f"turn-routing-replay-recovery-{session_id}",
    )


async def _recover_pending_replay(
    session_id: str,
    *,
    pending: PendingReplay,
    bridge_dir: Path,
    server_client: Any,
    ready: Callable[[], bool] | None,
) -> None:
    """Wait for the harness, confirm the prompt never ran, then deliver it."""
    probe = ready if ready is not None else _bridge_thread_probe(bridge_dir)
    if not await _wait_for(probe, RECOVERY_READY_WAIT_S):
        _logger.warning(
            "route-turn: no live thread for session=%s within %.0fs; the recovered "
            "prompt stays recorded for the next launch",
            session_id,
            RECOVERY_READY_WAIT_S,
        )
        return
    already = await _prompt_already_ran(session_id, pending.prompt, server_client)
    if already is None:
        # Cannot tell, so do not guess: a duplicate turn is worse than one
        # more launch spent trying.
        _logger.warning(
            "route-turn: could not read session=%s items to check the recovered "
            "prompt; leaving it recorded rather than risk a double-run",
            session_id,
        )
        return
    if already:
        _logger.info(
            "route-turn: recovered prompt for session=%s already ran; dropping the record",
            session_id,
        )
        clear_pending_replay(bridge_dir)
        return
    if not await _deliver_prompt(
        session_id,
        prompt=pending.prompt,
        server_client=server_client,
        model=pending.model,
    ):
        return
    clear_pending_replay(bridge_dir)
    _logger.info(
        "route-turn: recovered and replayed a prompt a previous launch blocked "
        "but never delivered for session=%s",
        session_id,
    )


def _user_message_texts(items: Any) -> list[str]:
    """Collect the text of every user message block in an items page.

    :param items: The ``data`` array from ``GET /sessions/{id}/items``, whose
        entries are flat api dicts (``{"type": "message", "role": "user",
        "content": [{"type": "input_text", "text": ...}]}``).
    :returns: Every text block on a user message, in page order.
    """
    texts: list[str] = []
    if not isinstance(items, list):
        return texts
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        if item.get("role") != "user":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                texts.append(block["text"])
    return texts


async def _prompt_already_ran(session_id: str, prompt: str, server_client: Any) -> bool | None:
    """Report whether *prompt* is already a turn on the session.

    Compared STRUCTURALLY — the message blocks' own text fields, exactly —
    never as a substring of the serialized page. Serializing escapes the text
    (``\\n``, ``\\"``, ``\\uXXXX``), so any prompt carrying a newline, a quote
    or a non-ASCII character never matched its own recorded copy and the
    recovery re-delivered it: a duplicate first turn on every crash-recovered
    session. The same test read the other way round too — a short prompt
    ("continue") matched anywhere in the JSON, including an unrelated id or an
    assistant's prose, and the user's prompt was dropped for good.

    :param session_id: Session/conversation identifier.
    :param prompt: The recovered prompt text.
    :param server_client: Runner→server client.
    :returns: ``True`` / ``False``, or ``None`` when the check itself failed.
    """
    try:
        resp = await server_client.get(
            f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}/items",
            params={"limit": RECOVERY_ITEM_SCAN, "order": "asc"},
            timeout=30.0,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception:  # noqa: BLE001 — an unreadable session is "unknown"
        _logger.debug("route-turn: item scan failed for session=%s", session_id, exc_info=True)
        return None
    if not isinstance(payload, dict):
        return None
    # A blocked prompt is persisted nowhere, so finding the exact text on a
    # user message means the replay (or the user re-typing it) already landed.
    return prompt in _user_message_texts(payload.get("data"))


def _bridge_thread_probe(bridge_dir: Path) -> Callable[[], bool]:
    """Build the "codex has published a thread to deliver to" probe."""

    def _ready() -> bool:
        from omnigent.codex_native_bridge import read_bridge_state

        state = read_bridge_state(bridge_dir)
        return state is not None and bool(state.thread_id)

    return _ready


async def _apply_routed_model(
    session_id: str,
    *,
    bridge_dir: Path,
    harness: str | None,
    model: str | None,
) -> bool:
    """Put the pane on the routed model, for harnesses whose hook cannot.

    Codex's hook switches the thread itself (an RPC to the app-server,
    which accepts a second client mid-turn), so nothing is left to do here.
    Claude's cannot: its only mid-session switch is a keystroke into the
    tmux pane, and the pane is frozen waiting on the hook subprocess that
    would be typing. So the switch moves here — after the block is in
    effect, before the replayed prompt exists.

    Fails open: a switch that does not take is logged and the prompt is
    still delivered, on the pane's current model. Losing the prompt is
    worse, and the recorded decision already names the routed model.

    Accepted trade-off: ``/model <id>`` also saves the pick as the person's
    global default in ``~/.claude/settings.json``.

    :param session_id: Session/conversation identifier.
    :param bridge_dir: Session bridge directory.
    :param harness: Harness the blocked prompt came from.
    :param model: Routed model id, or ``None`` to skip.
    :returns: ``True`` when the pane was switched (or needed no switch).
    """
    if harness != "claude-native" or not model:
        return True
    from omnigent.claude_model_vocabulary import claude_model_command_arg, normalized_model_id
    from omnigent.claude_native_bridge import (
        SWITCH_MODEL_DIALOG_HINT,
        inject_slash_command,
        read_claude_status_model,
        read_model_env,
    )

    live = read_claude_status_model(bridge_dir)
    if live and normalized_model_id(live) == normalized_model_id(model):
        # Already there. Skipping keeps a needless ``/model`` echo out of the
        # transcript, which otherwise sits ahead of the very first turn.
        _logger.info(
            "route-turn: session=%s pane is already on %s; no switch typed", session_id, model
        )
        return True
    env = read_model_env(bridge_dir) or None
    arg = claude_model_command_arg(model, env)
    if arg is None:
        _logger.warning(
            "route-turn: routed model %r has no spelling session=%s accepts (pins=%s); "
            "replaying on the launch model",
            model,
            session_id,
            sorted(env or ()),
        )
        return False
    try:
        await asyncio.to_thread(
            inject_slash_command,
            bridge_dir,
            command=f"/model {arg}",
            auto_confirm=True,
            confirm_hint=SWITCH_MODEL_DIALOG_HINT,
        )
    except (RuntimeError, ValueError):
        _logger.warning(
            "route-turn: could not switch session=%s onto %s; replaying on the launch model",
            session_id,
            model,
            exc_info=True,
        )
        return False
    _logger.info("route-turn: session=%s pane switched onto %s", session_id, model)
    return True


def _settle_probe(bridge_dir: Path, harness: str | None) -> Callable[[str | None], bool]:
    """Build the "has the blocked prompt cleared?" probe for *harness*.

    The two harnesses signal it differently, because they block at
    different depths. Codex has already opened a turn by the time
    ``UserPromptSubmit`` runs, so its bridge state names an active turn the
    replay must wait out. Claude blocks before any turn exists — nothing is
    persisted and no turn id reaches the hook — so the honest signal there
    is the pane itself being back at a mounted input box.

    :param bridge_dir: Session bridge directory.
    :param harness: Harness the blocked prompt came from. An unknown
        harness falls back to the codex bridge-state probe, which reads as
        "cleared" when it finds no state.
    :returns: Probe taking the blocked turn id, returning ``True`` when the
        replay may be delivered.
    """
    if harness == "claude-native":

        def _pane_ready(blocked_turn_id: str | None) -> bool:
            del blocked_turn_id
            from omnigent.claude_native_bridge import claude_pane_ready

            return claude_pane_ready(bridge_dir)

        return _pane_ready

    def _cleared(blocked_turn_id: str | None) -> bool:
        from omnigent.codex_native_bridge import read_bridge_state

        state = read_bridge_state(bridge_dir)
        active = state.active_turn_id if state is not None else None
        return active is None or active != blocked_turn_id

    return _cleared


async def _wait_for(predicate: Callable[[], bool], timeout_s: float) -> bool:
    """Poll *predicate* until true or *timeout_s* elapses."""
    deadline = time.monotonic() + timeout_s
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(REPLAY_POLL_S)


async def _wait_for_cleared(
    idle: Callable[[str | None], bool],
    blocked_turn_id: str | None,
) -> bool:
    """Wait until the blocked turn is no longer the harness's current one.

    A "cleared" reading is only trusted once the blocked turn has been
    seen as current, or after :data:`REPLAY_IDLE_GRACE_S` — otherwise the
    harness bookkeeping simply may not have caught up with a turn that
    started milliseconds ago, and the replay would land mid-abort.
    """
    started = time.monotonic()
    deadline = started + REPLAY_IDLE_WAIT_S
    seen_active = blocked_turn_id is None
    while True:
        cleared = idle(blocked_turn_id)
        if not cleared:
            seen_active = True
        elif seen_active or time.monotonic() - started >= REPLAY_IDLE_GRACE_S:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(REPLAY_POLL_S)


# ── Per-session lifecycle (runner side) ────────────────────────────────────

_session_routers: dict[str, TurnRouter] = {}
_lifecycle_lock = threading.Lock()

# Harnesses whose ``UserPromptSubmit`` hook reads a turn-router
# advertisement.
_TURN_HOOK_HARNESSES = frozenset({"codex-native", "claude-native"})


def supports_in_harness_turn_routing(harness: str | None) -> bool:
    """Report whether *harness* routes its first prompt from inside itself.

    The CLI reads this to decide whether ``--smart-routing`` with no ``-p``
    is servable: a harness that hooks its own ``UserPromptSubmit`` can be
    launched bare and route on whatever the user types, while any other
    harness still needs the prompt up front.

    :param harness: Canonical harness id, e.g. ``"claude-native"``.
    :returns: ``True`` when this harness carries the ``route-turn`` hook.
    """
    return harness in _TURN_HOOK_HARNESSES


def ensure_session_turn_router(
    session_id: str,
    *,
    bridge_dir: Path,
    server_client: httpx.AsyncClient | None,
    harness: str | None = None,
    routing_enabled: bool = True,
    loop: asyncio.AbstractEventLoop | None = None,
) -> TurnRouter | None:
    """Start (once) the loopback turn router serving *session_id*.

    Never raises: a router that cannot bind a socket must not take the
    harness launch down with it, and only the harnesses whose hooks read
    the advertisement get one at all.

    :param session_id: Session/conversation identifier.
    :param bridge_dir: Directory the advertisement is written into — the
        same one the harness's hook command is pointed at.
    :param server_client: Runner→server client. ``None`` skips the router
        (nowhere to relay verdicts, nowhere to replay).
    :param harness: Harness being launched; gates the start.
    :param routing_enabled: Whether the session launched with Smart Routing
        on. ``False`` skips the router entirely, and the harness launch reads
        the absent advertisement as "do not register the route-turn hook" —
        so a session that will never route pays no per-prompt round trip.
    :param loop: Event loop owning the relay. ``None`` uses the running one.
    :returns: The running router handle, or ``None``.
    """
    if server_client is None or harness not in _TURN_HOOK_HARNESSES or not routing_enabled:
        return None
    try:
        resolver_loop = loop if loop is not None else asyncio.get_running_loop()
        with _lifecycle_lock:
            existing = _session_routers.get(session_id)
            if existing is not None:
                write_advertisement(
                    bridge_dir,
                    url=existing.url,
                    token=existing.token,
                    session_id=session_id,
                    filename=ADVERTISEMENT_FILE,
                )
                return existing
            router = start_turn_router(
                bridge_dir=bridge_dir,
                session_id=session_id,
                resolver=make_server_relay_resolver(
                    server_client, bridge_dir=bridge_dir, harness=harness
                ),
                loop=resolver_loop,
            )
            _session_routers[session_id] = router
    except (OSError, RuntimeError):
        _logger.warning(
            "turn router could not start for session=%s harness=%s",
            session_id,
            harness,
            exc_info=True,
        )
        return None
    # Nothing from the rendezvous is logged — neither the URL nor the paths
    # and ids that reach it — so a log file can never carry the bearer token
    # or the identifiers that address it. The advertisement on disk names both.
    _logger.info("turn router started", extra={"session_id": session_id})
    return router


def shutdown_session_turn_router(session_id: str, router: TurnRouter | None = None) -> None:
    """Stop the turn router serving *session_id* and forget it.

    Cheap and safe for a session that never had one, and safe to call
    twice.

    :param session_id: Session/conversation identifier.
    :param router: The handle the caller started. When given, the teardown
        is identity-scoped so a delayed teardown from a previous launch
        cannot close the router a re-create has since installed.
    """
    with _lifecycle_lock:
        current = _session_routers.get(session_id)
        if router is not None and current is not router:
            return
        _session_routers.pop(session_id, None)
    if current is None:
        return
    with contextlib.suppress(Exception):
        current.close()
