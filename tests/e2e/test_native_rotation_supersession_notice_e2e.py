"""Regression: native session rotation must notify the superseded conversation.

All three rotation-capable native harnesses move the Omnigent session binding
onto a fresh conversation when the user starts a new vendor conversation in the
pane (``/new`` in Codex, ``/clear`` in Antigravity/Claude). Only ``claude-native``
tells the abandoned OLD conversation about it: after rotating it calls
``_post_clear_supersession`` (``omnigent/harnesses/claude_native/forwarder.py``),
which posts three events to the OLD conversation's ``/events`` endpoint, in order:

1. ``external_session_status: idle`` -- so the old conversation's "Working..."
   spinner stops (its terminal moved to the new session, so it will never
   receive the turn-end edge that would normally clear it);
2. a persisted assistant ``external_conversation_item`` (a ``message``) linking
   to the new conversation -- the durable record that survives reconnects;
3. a transient ``external_session_superseded`` event the server republishes as
   ``session.superseded`` (``reason="clear"``), so a client actively viewing the
   old conversation auto-redirects.

``codex-native`` and ``antigravity-native`` rotate the binding, transfer the
terminal, and PATCH the old session's ``runner_id`` to ``""`` -- but post NONE of
the three supersession events. So the old web conversation is stranded: its
"Working..." spinner never clears, it gains no link to where work continued, and
a user watching it never redirects.

What this test drives
---------------------
The user-facing failure is on the **web** surface (the old conversation view),
triggered by a **terminal** action (``/new`` / ``/clear``). This test pins the
gap at the harness-level producer boundary the fix touches: it drives the
**real** codex/antigravity rotation entry points through a recording Omnigent
client and asserts on the exact set of HTTP posts each rotation makes to the OLD
session -- deterministically, and for BOTH harnesses in one place (e2e_ui has no
real ``agy`` binary). The codex web half is additionally driven live end-to-end
in ``tests/e2e_ui/chat/test_codex_rotation_supersession_redirect.py`` (real Codex
``/new`` in the SPA terminal against the mock LLM, showing the stranded browser
that never redirects). The web redirect/notice machinery the missing posts feed
is present and already covered by
``tests/e2e_ui/chat/test_session_superseded_redirect.py``; the bug is purely that
codex/antigravity never emit the events.

* ``test_claude_native_rotation_posts_supersession_notice`` is the positive
  control -- it pins the three-post contract on the only producer that has it,
  and passes today.
* ``test_codex_native_rotation_posts_supersession_notice`` and
  ``test_antigravity_native_rotation_posts_supersession_notice`` drive the real
  codex/antigravity rotations and assert the SAME three posts land on the OLD
  session. Both FAIL on buggy main (zero posts) and pass once the fix hoists the
  supersession notice into the shared rotation paths.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.antigravity_native import reader as areader
from omnigent.harnesses.antigravity_native.bridge import (
    AntigravityNativeBridgeState,
    write_bridge_state as agy_write_bridge_state,
)
from omnigent.harnesses.claude_native.forwarder import _post_clear_supersession
from omnigent.harnesses.codex_native import forwarder as cfwd
from omnigent.harnesses.codex_native.bridge import (
    CodexNativeBridgeState,
    write_bridge_state as codex_write_bridge_state,
)

OLD_SESSION = "conv_old"
NEW_SESSION = "conv_new"
APP_SERVER_URL = "ws://127.0.0.1:9876"

# The three posts to the OLD conversation that the supersession notice is made
# of. Their absence is exactly the three consequences the bug report enumerates:
# a spinner that never clears, no durable link, and no auto-redirect.
IDLE_STATUS = "external_session_status"
NOTICE_ITEM = "external_conversation_item"
SUPERSEDED_EVENT = "external_session_superseded"
SUPERSESSION_EVENT_TYPES = frozenset({IDLE_STATUS, NOTICE_ITEM, SUPERSEDED_EVENT})


class _RecordingAP:
    """httpx-shaped Omnigent client: answers the snapshot GET, records writes.

    The rotation entry points fetch the old session snapshot (GET), create the
    replacement (POST ``/v1/sessions``), and issue PATCH/POST calls to bind,
    transfer the terminal, and (for the supersession notice) POST events to the
    OLD session. Recording every call lets us assert exactly which supersession
    events -- if any -- reached the old conversation.
    """

    def __init__(self) -> None:
        """Initialize with an empty call log."""
        self.calls: list[tuple[str, str, dict]] = []  # (method, url, json_body)

    async def get(self, url: str) -> httpx.Response:
        """Return the old-session snapshot for any GET, and record it.

        :param url: Request URL, e.g. ``"/v1/sessions/conv_old"``.
        :returns: A 200 response carrying the old session snapshot.
        """
        self.calls.append(("GET", url, {}))
        return httpx.Response(
            200,
            json={
                "id": OLD_SESSION,
                "agent_id": "ag_native",
                "runner_id": "runner_1",
                "labels": {},
            },
            request=httpx.Request("GET", url),
        )

    async def post(self, url: str, *, json: dict) -> httpx.Response:
        """Record a POST and return 200 (a new id for a session create).

        :param url: Request URL, e.g. ``"/v1/sessions"`` or an ``/events`` post.
        :param json: JSON body of the POST.
        :returns: A 200 response; ``/v1/sessions`` returns a new id.
        """
        self.calls.append(("POST", url, json))
        body: dict = {"id": NEW_SESSION} if url == "/v1/sessions" else {}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    async def patch(self, url: str, *, json: dict) -> httpx.Response:
        """Record a PATCH and return 200.

        :param url: Request URL, e.g. ``"/v1/sessions/conv_new"``.
        :param json: JSON body of the PATCH.
        :returns: A 200 response.
        """
        self.calls.append(("PATCH", url, json))
        return httpx.Response(200, json={}, request=httpx.Request("PATCH", url))

    def supersession_events_to_old(self) -> list[str]:
        """Return supersession event types POSTed to the OLD session's /events.

        :returns: The ordered ``type`` values of the supersession posts that
            targeted ``/v1/sessions/conv_old/events`` (a subset of
            :data:`SUPERSESSION_EVENT_TYPES`), e.g.
            ``["external_session_status", "external_conversation_item",
            "external_session_superseded"]`` after the fix, or ``[]`` on the
            buggy build.
        """
        old_events_url = f"/v1/sessions/{OLD_SESSION}/events"
        return [
            str(body.get("type"))
            for (method, url, body) in self.calls
            if method == "POST"
            and url == old_events_url
            and body.get("type") in SUPERSESSION_EVENT_TYPES
        ]


def _assert_notified(ap: _RecordingAP, *, harness: str) -> None:
    """Assert the rotation posted all three supersession events to the old session.

    :param ap: The recording client the rotation ran through.
    :param harness: Harness name for the failure message, e.g. ``"codex-native"``.
    """
    posted = ap.supersession_events_to_old()
    missing = SUPERSESSION_EVENT_TYPES - set(posted)
    assert not missing, (
        f"{harness} rotation did not notify the superseded conversation "
        f"({OLD_SESSION}): missing {sorted(missing)} of the three supersession "
        f"posts. Posted to old /events: {posted or 'NOTHING'}. The old web "
        f"conversation is stranded -- 'Working...' never clears "
        f"({IDLE_STATUS!r}), no link to the new chat ({NOTICE_ITEM!r}), and no "
        f"auto-redirect ({SUPERSEDED_EVENT!r}). claude-native posts all three "
        f"via _post_clear_supersession; codex/antigravity must too."
    )


async def test_claude_native_rotation_posts_supersession_notice() -> None:
    """Positive control: claude-native posts all three supersession events.

    Pins the contract the fix must extend to the other harnesses. Passes today.
    """
    ap = _RecordingAP()

    await _post_clear_supersession(
        ap,
        old_session_id=OLD_SESSION,
        new_session_id=NEW_SESSION,
        agent_name="claude",
    )

    _assert_notified(ap, harness="claude-native")


async def test_codex_native_rotation_posts_supersession_notice(tmp_path: Path) -> None:
    """A Codex ``/new`` rotation must notify the superseded conversation.

    Drives the real ``_maybe_rotate_session_on_thread_started`` with the exact
    envelope Codex's app-server emits when the user runs ``/new`` -- a fresh,
    non-ephemeral, top-level ``user`` thread. On buggy main the rotation happens
    (a replacement session is created, the terminal transferred) but no
    supersession events reach the old session, so this FAILS. It passes once the
    fix posts the notice from the codex rotation path.
    """
    ap = _RecordingAP()
    codex_write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id=OLD_SESSION,
            socket_path=APP_SERVER_URL,
            thread_id="thread_persistent_parent",
            codex_home=str(tmp_path / "codex_home"),
        ),
    )
    target = cfwd._ForwarderTarget(
        session_id=OLD_SESSION,
        thread_id="thread_persistent_parent",
        delta_coalescer=cfwd._OutputTextDeltaCoalescer(ap, OLD_SESSION),
        usage_coalescer=cfwd._SessionUsageCoalescer(ap, OLD_SESSION),
        elicitation_tracker=cfwd._CodexElicitationTaskTracker(),
    )
    # The exact shape Codex emits on a user /new: a new top-level thread that is
    # neither ephemeral nor a sub-agent, so the forwarder rotates onto it.
    new_thread_event = {
        "method": "thread/started",
        "params": {
            "thread": {
                "id": "0195bbbb-real-clear-thread",
                "ephemeral": False,
                "path": "/rollout/0195bbbb.jsonl",
                "threadSource": "user",
            }
        },
    }

    rotated = await cfwd._maybe_rotate_session_on_thread_started(
        ap_client=ap,
        target=target,
        bridge_dir=tmp_path,
        app_server_url=APP_SERVER_URL,
        event=new_thread_event,
    )

    # Precondition: the rotation actually ran (this is a genuine /new, not an
    # ephemeral/sub-agent thread the forwarder ignores). If this regresses the
    # bug's whole premise is gone.
    assert rotated is True, "a real user /new must rotate the codex-native session"
    assert target.session_id == NEW_SESSION

    _assert_notified(ap, harness="codex-native")


async def test_antigravity_native_rotation_posts_supersession_notice(tmp_path: Path) -> None:
    """An Antigravity ``/clear`` rotation must notify the superseded conversation.

    Drives the real ``_rotate_session_for_cascade`` (the agy reader's ``/clear``
    rotation entry point) against a freshly minted cascade. On buggy main it
    rotates the binding + transfers the terminal but posts no supersession events
    to the old session, so this FAILS. It passes once the fix posts the notice
    from the antigravity rotation path.
    """
    ap = _RecordingAP()
    agy_write_bridge_state(
        tmp_path,
        AntigravityNativeBridgeState(session_id=OLD_SESSION, conversation_id="old-cascade"),
    )

    new_session_id = await areader._rotate_session_for_cascade(
        client=ap,
        old_session_id=OLD_SESSION,
        new_cascade_id="68caaeac-2eaf-4e2c-9b95-721b022f4903",
        bridge_dir=tmp_path,
    )

    # Precondition: the rotation succeeded (created + bound the replacement).
    assert new_session_id == NEW_SESSION, "the agy /clear rotation must create a new session"

    _assert_notified(ap, harness="antigravity-native")
