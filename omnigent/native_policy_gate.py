"""Locally cached "no policy can fire" gate for native-harness policy hooks.

Every native session installs the ``evaluate-policy`` hook on
``PreToolUse``, ``PostToolUse``, and ``UserPromptSubmit``, and each firing
costs one **blocking** request to ``POST /v1/sessions/{id}/policies/evaluate``
before the harness may proceed. On loopback that is a few milliseconds. Across
a region it is a full round trip per hook — two per tool call — so a
twenty-tool turn spends seconds waiting on the wire to be told, over and over,
that nothing is configured.

The server already answers that case from a fast path: with no agent
guardrails, no server-wide defaults and no session policies, nothing can fire
and the verdict is an unconditional ALLOW. This module carries that answer back
to the client so the *next* hook need not ask. The server stamps
``gate: {"no_policies": true}`` on those fast-path responses; the hook records
it under the bridge directory, and later hooks that find a live gate emit "no
opinion" without touching the network.

Correctness rests on three things:

- **Absence means ask.** A response without the field clears the gate, so a
  session that has policies (or whose posture the server did not report) always
  round-trips. Only the provably-empty case is ever skipped.
- **Invalidation.** Adding a policy mid-session — via the CRUD API or
  ``sys_add_policy`` — clears the gate through the runner, so enforcement
  begins on the very next hook, as it does today. This is best-effort by
  construction: the notification travels over the session's runner tunnel,
  which lives on one replica, and the mutation request is not replica-routed.
  On a multi-replica deployment a mutation that lands elsewhere cannot reach
  the tunnel, and expiry below is then the real bound. (Making the policy
  routes signal ``wrong_replica`` so the caller re-addresses would close
  that; it changes a user-facing route's contract, so it is left alone here.)
- **Expiry.** The gate is short-lived regardless, so a missed invalidation
  (dropped tunnel, a replica that never saw the mutation) costs at most
  :data:`GATE_TTL_S` of lag rather than a silently ungoverned session.

For a *session*-scoped policy, when the push lands, enforcement begins on the
very next hook as it does today. For a *server-wide default* the bound is
looser: the server's own default-policy cache may already be up to 30s stale
when it stamps a gate, and the gate then lives its own :data:`GATE_TTL_S` from
that moment — the two windows are sequential, not shared, so a newly added
default can take up to their sum to bite.

The gate is a small file in the harness's bridge directory, so the two kinds of
caller share it: the in-runner tool relay (which serves claude-native's hook as
a bare curl) and the hook subprocesses other harnesses spawn per event. Reading
it costs a stat and a short parse — nothing next to the round trip it replaces.

Invalidation needs the bridge directory for a session, which only the writer
knows, so writers register the mapping in :func:`note_session_bridge_dir`. A
session whose gate was only ever written by a subprocess hook has no mapping in
the runner, so its invalidation falls back to expiry — the bounded case above.

Set ``OMNIGENT_NATIVE_POLICY_GATE=0`` to disable skipping entirely and send
every hook to the server.

Kept import-light on purpose: hook subprocesses import this on their blocking
path, so it must not pull the policy engine (or anything else heavy) in.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

# How long a recorded "nothing can fire" verdict may be trusted.
#
# Deliberately well under ``_DEFAULT_POLICY_SPECS_CACHE``'s 30s (the server-side
# TTL that already bounds how long a default-policy change can go unseen), and
# not merely equal to it, because expiry is the *only* bound that always holds:
# the push that clears a gate rides the session's runner tunnel, so on a
# multi-replica deployment a mutation landing on another replica never reaches
# it. Ten seconds keeps that worst case small while still collapsing a
# tool-heavy turn from one round trip per hook to a handful.
GATE_TTL_S = 10.0

# Gate file inside the harness bridge directory.
GATE_FILE = "policy_gate.json"

# Opt-out. Any of these values sends every hook to the server.
_DISABLED_VALUES = frozenset({"0", "false", "no", "off"})
_ENV_VAR = "OMNIGENT_NATIVE_POLICY_GATE"

# The engine injects an unconditional ASK in front of ``sys_add_policy`` so an
# agent cannot install policies on itself unseen. The server's own fast path
# exempts it, so this one must never be skipped locally either — even though a
# native hook does not normally see it (``mcp__omnigent__*`` tools are gated on
# the relay/MCP path instead).
_NEVER_SKIPPED_TOOLS = frozenset({"sys_add_policy", "mcp__omnigent__sys_add_policy"})


@dataclass(frozen=True)
class PolicyGate:
    """
    A recorded server verdict that no policy can fire for this session.

    :param no_policies: What the server reported: ``True`` when no agent
        guardrail, server default, or session policy exists.
    :param expires_at: Wall-clock time after which the record is stale
        (:func:`time.time` scale).
    """

    no_policies: bool
    expires_at: float

    def live(self, *, now: float | None = None) -> bool:
        """
        Report whether this record still licenses skipping the server.

        :param now: Current time, for tests. Defaults to :func:`time.time`.
        :returns: ``True`` when the gate is affirmative and unexpired.
        """
        return self.no_policies and (now if now is not None else time.time()) < self.expires_at


def gate_enabled() -> bool:
    """
    Report whether local gating is enabled for this process.

    :returns: ``False`` when ``OMNIGENT_NATIVE_POLICY_GATE`` opts out.
    """
    return (os.environ.get(_ENV_VAR) or "").strip().lower() not in _DISABLED_VALUES


# Bridge directory last known to hold a gate for a session, so an
# invalidation addressed by session id can find the file. Populated by writers
# inside the runner process; empty for sessions whose gates only ever came from
# a hook subprocess.
_BRIDGE_DIRS_BY_SESSION: dict[str, Path] = {}


def note_session_bridge_dir(session_id: str, bridge_dir: Path) -> None:
    """
    Record which bridge directory holds a session's gate.

    :param session_id: Omnigent session/conversation id.
    :param bridge_dir: The session's native harness bridge directory.
    :returns: None.
    """
    _BRIDGE_DIRS_BY_SESSION[session_id] = bridge_dir


def forget_session(session_id: str) -> None:
    """
    Drop a finished session's bridge-directory mapping.

    :param session_id: Omnigent session/conversation id.
    :returns: None.
    """
    _BRIDGE_DIRS_BY_SESSION.pop(session_id, None)


def clear_gate_for_session(session_id: str) -> bool:
    """
    Clear a session's gate so its next hook asks the server again.

    The runner calls this when the server reports that the session's
    policies changed.

    :param session_id: Omnigent session/conversation id.
    :returns: ``True`` when a bridge directory was known and cleared;
        ``False`` when none is registered, in which case the session's gate
        lapses on expiry instead.
    """
    bridge_dir = _BRIDGE_DIRS_BY_SESSION.get(session_id)
    if bridge_dir is None:
        return False
    clear_gate(bridge_dir)
    return True


def _gate_path(bridge_dir: Path) -> Path:
    """
    Return the gate file's path inside a bridge directory.

    :param bridge_dir: Native harness bridge directory.
    :returns: Path to the gate file (which may not exist).
    """
    return bridge_dir / GATE_FILE


def read_gate(bridge_dir: Path) -> PolicyGate | None:
    """
    Read the recorded gate, or ``None`` when there isn't a usable one.

    Never raises: a missing, truncated, or malformed file reads as "no
    gate", which sends the caller to the server.

    :param bridge_dir: Native harness bridge directory.
    :returns: The recorded gate, or ``None``.
    """
    try:
        raw = _gate_path(bridge_dir).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    no_policies = payload.get("no_policies")
    expires_at = payload.get("expires_at")
    if not isinstance(no_policies, bool) or not isinstance(expires_at, (int, float)):
        return None
    return PolicyGate(no_policies=no_policies, expires_at=float(expires_at))


def clear_gate(bridge_dir: Path) -> None:
    """
    Drop any recorded gate so the next hook asks the server.

    Called when policies change and when the server's answer stops
    reporting a posture. Best-effort: a failure to unlink leaves the gate
    in place until it expires.

    :param bridge_dir: Native harness bridge directory.
    :returns: None.
    """
    with contextlib.suppress(OSError):
        _gate_path(bridge_dir).unlink()


def record_gate(
    bridge_dir: Path,
    gate: object,
    *,
    session_id: str | None = None,
    ttl_s: float = GATE_TTL_S,
) -> None:
    """
    Record (or clear) the gate from an ``EvaluationResponse``'s field.

    :param bridge_dir: Native harness bridge directory.
    :param gate: The response's ``gate`` value. ``{"no_policies": true}``
        records an affirmative gate; anything else — including a missing
        field — clears it, so only a posture the server actually reported
        can suppress a call.
    :param session_id: Session this gate belongs to. In-runner callers pass
        it so a later invalidation can find this bridge directory.
    :param ttl_s: How long the record may be trusted, in seconds.
    :returns: None.
    """
    if session_id is not None:
        note_session_bridge_dir(session_id, bridge_dir)
    affirmative = isinstance(gate, dict) and gate.get("no_policies") is True
    if not affirmative:
        clear_gate(bridge_dir)
        return
    payload = json.dumps({"no_policies": True, "expires_at": time.time() + ttl_s})
    path = _gate_path(bridge_dir)
    # Written via a unique temp file + replace: hook subprocesses read this
    # concurrently and must never observe a half-written record.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        bridge_dir.mkdir(parents=True, exist_ok=True)
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()


def may_skip_policy_call(
    bridge_dir: Path,
    *,
    tool_name: object = None,
    now: float | None = None,
) -> bool:
    """
    Report whether this hook event can be answered without the server.

    :param bridge_dir: Native harness bridge directory.
    :param tool_name: Tool the event is about, when it is a tool phase.
        Read straight out of the harness's hook JSON, so any type is
        accepted; only the exact strings the engine gates unconditionally
        (``sys_add_policy``) force a round trip.
    :param now: Current time, for tests.
    :returns: ``True`` when a live gate says no policy can fire.
    """
    if not gate_enabled():
        return False
    if tool_name in _NEVER_SKIPPED_TOOLS:
        return False
    gate = read_gate(bridge_dir)
    return gate is not None and gate.live(now=now)
