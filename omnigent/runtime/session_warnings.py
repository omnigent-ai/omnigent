"""In-process index of session-scoped warnings.

A *warning* is a degraded-but-running condition the UI shows in the
session header while the session keeps working — today only
``subagent_routing_unenforced``, published when a harness's router hook
never fired so native sub-agent spawns are not being gated.

Same shape as the other transient recovery indexes
(:mod:`pending_elicitations`, :mod:`pending_inputs`): populated by the
route layer, replayed into the cold-load snapshot
(``GET /v1/sessions/{id}``) via :func:`snapshot_for`, in-memory only and
process-affine. Losing a warning on restart is acceptable — the
publisher re-posts it the next time it observes the condition.

Entries are deduplicated on ``(code, harness)`` so a forwarder that
re-observes the same condition every poll tick does not grow the list.
A warning is not sticky: the condition can be repaired mid-session (the
canary fires on a later turn), so publishers clear it with :func:`clear`
and a deleted session's entries are pruned. A clear names the codes the
caller actually checked, so one publisher's "repaired" cannot drop
another publisher's warning.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from typing import Any

_logger = logging.getLogger(__name__)

#: Warning code for "a harness ran without the router hook enforcing picks".
SUBAGENT_ROUTING_UNENFORCED = "subagent_routing_unenforced"

#: Codes this index accepts. Publishers are runner-side and reach it over
#: the event API, so an unknown code is dropped rather than stored: the UI
#: has no copy for it and an unbounded key space is a memory sink.
ACCEPTED_CODES: frozenset[str] = frozenset({SUBAGENT_ROUTING_UNENFORCED})

#: Codes published over the session's ``external_session_warning`` channel.
#: A publisher on that channel re-checks all of them every tick, so its
#: "nothing is wrong" post may clear exactly these and nothing else.
EXTERNAL_WARNING_CODES: frozenset[str] = frozenset({SUBAGENT_ROUTING_UNENFORCED})

#: Fields kept off a posted payload, and the cap on free-text values.
_KEPT_FIELDS: tuple[str, ...] = ("code", "harness", "reason")
_MAX_FIELD_LEN = 500

#: ``harness`` is part of the dedup key, so a long free-text value would let
#: one publisher mint unbounded distinct entries; it names a harness, so a
#: short cap is enough for every real value ("codex-native").
_MAX_HARNESS_LEN = 64

#: Hard cap on entries per session. Codes are allowlisted and the key's other
#: half is a harness id, so a session realistically holds one or two; the cap
#: bounds a misbehaving publisher to a fixed footprint. Oldest entries are
#: evicted first so the newest observation always lands.
_MAX_ENTRIES_PER_SESSION = 8

_warnings: dict[str, list[dict[str, Any]]] = {}
_lock = threading.Lock()


def _key(warning: dict[str, Any]) -> tuple[str, str]:
    return (str(warning.get("code") or ""), str(warning.get("harness") or ""))


def _sanitized(warning: dict[str, Any]) -> dict[str, Any] | None:
    """Reduce a posted payload to the known string fields, or reject it."""
    code = warning.get("code")
    if not isinstance(code, str) or code not in ACCEPTED_CODES:
        return None
    entry: dict[str, Any] = {}
    for field in _KEPT_FIELDS:
        value = warning.get(field)
        if isinstance(value, str) and value:
            cap = _MAX_HARNESS_LEN if field == "harness" else _MAX_FIELD_LEN
            entry[field] = value[:cap]
    return entry


def record(session_id: str, warning: dict[str, Any]) -> None:
    """
    Record one warning for *session_id*, replacing any same-key entry.

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param warning: Warning payload, e.g. ``{"code":
        "subagent_routing_unenforced", "harness": "codex-native",
        "reason": "SessionStart canary did not fire"}``. Ignored unless
        its ``code`` is in :data:`ACCEPTED_CODES`; unknown fields are
        dropped and free text is truncated. A session holds at most
        :data:`_MAX_ENTRIES_PER_SESSION` entries — the oldest is evicted
        to make room.
    """
    entry = _sanitized(warning)
    if entry is None:
        _logger.info(
            "ignoring session warning with unknown code %r for session %s",
            warning.get("code"),
            session_id,
        )
        return
    with _lock:
        entries = _warnings.setdefault(session_id, [])
        key = _key(entry)
        for index, existing in enumerate(entries):
            if _key(existing) == key:
                entries[index] = entry
                return
        while len(entries) >= _MAX_ENTRIES_PER_SESSION:
            dropped = entries.pop(0)
            _logger.info(
                "session %s holds %d warnings; dropping the oldest (%r)",
                session_id,
                _MAX_ENTRIES_PER_SESSION,
                dropped.get("code"),
            )
        entries.append(entry)


def clear(session_id: str, codes: Iterable[str] | None = None) -> None:
    """
    Drop a session's recorded warnings.

    Called when a publisher reports the condition repaired (the router
    canary fired on a later turn) and when the session is deleted, so a
    banner does not outlive what it describes.

    A caller passes the codes its own check covers: warnings published by
    someone else are evidence this caller never looked at, so a blanket
    clear would silently drop them. ``None`` is for teardown only (the
    session is gone, so every code goes with it).

    :param session_id: Session/conversation identifier, e.g.
        ``"conv_abc123"``.
    :param codes: Only drop entries carrying one of these codes, e.g.
        ``("subagent_routing_unenforced",)``. ``None`` drops all of them.
    """
    with _lock:
        if codes is None:
            _warnings.pop(session_id, None)
            return
        dropped_codes = frozenset(codes)
        entries = _warnings.get(session_id)
        if entries is None:
            return
        remaining = [entry for entry in entries if entry.get("code") not in dropped_codes]
        if remaining:
            _warnings[session_id] = remaining
        else:
            _warnings.pop(session_id, None)


def snapshot_for(session_id: str) -> list[dict[str, Any]]:
    """
    Return the warnings to replay into a session snapshot.

    :param session_id: Session/conversation identifier.
    :returns: Warning payloads in the order they were first recorded;
        empty when nothing is wrong (the common case).
    """
    with _lock:
        return [dict(entry) for entry in _warnings.get(session_id, ())]
