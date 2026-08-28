"""Regression test for codex-native unusable with Codex CLI 0.150.1.

With Codex CLI 0.150.1, a normal first turn emits a *second*
``thread/started`` about nine seconds in, for a ``system`` thread with
``ephemeral=true`` and ``path=null``. Omnigent's forwarder treats every
non-subagent ``thread/started`` whose id differs from the active thread as a
user ``/clear`` and rotates the parent Omnigent session onto that thread
(``_maybe_rotate_session_on_thread_started`` ->
``_create_thread_replacement_session``). The session is now bound to a
non-persistable thread: the real turn's output is rejected as stale and goal
operations fail with ``ephemeral thread does not support goals``.

Why fault-injection instead of driving the live TUI
----------------------------------------------------
The triggering event is emitted only by Codex CLI **0.150.1**; the known-good
0.149.0 never emits it (per the reporter's isolated A/B run). The runner here
does not have 0.150.1 installed, so the user-facing web journey (submit a turn,
get no result) cannot be driven end to end on this build. We therefore inject
the *exact* system/ephemeral ``thread/started`` envelope from the report into
the **real** forwarder rotation entry point and assert on the real session
rotation it drives (create + bind + external-session + terminal transfer +
bridge-state rewrite via the recording AP client).

This mirrors the four regression cases the report asks for:

1. an ``ephemeral=true`` / ``threadSource=system`` ``thread/started`` must NOT
   rotate or rebind the parent session (the bug: it currently does);
2. a non-ephemeral top-level thread (a real user ``/clear``) must STILL rotate;
3. existing sub-agent ``thread/started`` handling must remain unchanged (no
   rotation);
4. after the ephemeral event the forwarder target must remain bound to the
   persistent parent thread, so goal operations stay routed to a real thread.

Case 1 and case 4 FAIL on the buggy build (rotation happens) and pass once the
fix ignores ephemeral/system ``thread/started`` before any state change.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from omnigent import codex_native_forwarder as fwd
from omnigent.codex_native_bridge import (
    CodexNativeBridgeState,
    read_bridge_state,
    write_bridge_state,
)

PARENT_SESSION = "conv_parent"
PARENT_THREAD = "thread_persistent_parent"
REPLACEMENT_SESSION = "conv_replacement"
APP_SERVER_URL = "ws://127.0.0.1:9876"


class _RecordingAP:
    """httpx-shaped Omnigent client: answers the snapshot GET, records writes.

    ``_create_thread_replacement_session`` fetches the old session snapshot
    (GET), then creates the replacement (POST /v1/sessions) and issues a series
    of PATCH/POST calls to bind and transfer it. Recording those lets the test
    assert whether a rotation was performed at all.
    """

    def __init__(self) -> None:
        """Initialize with empty POST/PATCH records."""
        self.posts: list[tuple[str, dict]] = []
        self.patches: list[tuple[str, dict]] = []

    async def get(self, url: str) -> httpx.Response:
        """Return the parent session snapshot for any GET.

        :param url: Request URL, e.g. ``"/v1/sessions/conv_parent"``.
        :returns: A 200 response carrying the parent session snapshot.
        """
        return httpx.Response(
            200,
            json={
                "id": PARENT_SESSION,
                "agent_id": "ag_codex_native",
                "runner_id": "runner_1",
                "labels": {},
            },
            request=httpx.Request("GET", url),
        )

    async def post(self, url: str, *, json: dict) -> httpx.Response:
        """Record a POST and return 200 (a session id for a create).

        :param url: Request URL, e.g. ``"/v1/sessions"``.
        :param json: JSON body of the POST.
        :returns: A 200 response; ``/v1/sessions`` returns a new id.
        """
        self.posts.append((url, json))
        body: dict = {"id": REPLACEMENT_SESSION} if url == "/v1/sessions" else {}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    async def patch(self, url: str, *, json: dict) -> httpx.Response:
        """Record a PATCH and return 200.

        :param url: Request URL, e.g. ``"/v1/sessions/conv_replacement"``.
        :param json: JSON body of the PATCH.
        :returns: A 200 response.
        """
        self.patches.append((url, json))
        return httpx.Response(200, json={}, request=httpx.Request("PATCH", url))

    def created_sessions(self) -> list[dict]:
        """Return the bodies of every ``POST /v1/sessions`` (session creates)."""
        return [body for url, body in self.posts if url == "/v1/sessions"]


def _make_target(ap: _RecordingAP) -> fwd._ForwarderTarget:
    """Build a forwarder target bound to the persistent parent thread.

    :param ap: Recording AP client the coalescers post through.
    :returns: A ``_ForwarderTarget`` on ``PARENT_SESSION`` / ``PARENT_THREAD``.
    """
    return fwd._ForwarderTarget(
        session_id=PARENT_SESSION,
        thread_id=PARENT_THREAD,
        delta_coalescer=fwd._OutputTextDeltaCoalescer(ap, PARENT_SESSION),
        usage_coalescer=fwd._SessionUsageCoalescer(ap, PARENT_SESSION),
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
    )


def _seed_bridge(tmp_path: Path) -> Path:
    """Write bridge state pinning the persistent parent thread.

    :param tmp_path: Test-scoped temp dir used as the bridge dir.
    :returns: The bridge directory path.
    """
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id=PARENT_SESSION,
            socket_path=APP_SERVER_URL,
            thread_id=PARENT_THREAD,
            codex_home=str(tmp_path / "codex_home"),
        ),
    )
    return tmp_path


def _thread_started(thread: dict) -> dict:
    """Wrap a Codex ``thread`` object in a ``thread/started`` envelope.

    :param thread: The ``params.thread`` object.
    :returns: A Codex app-server notification envelope.
    """
    return {"method": "thread/started", "params": {"thread": thread}}


def _ephemeral_system_thread() -> dict:
    """The exact ephemeral system thread Codex CLI 0.150.1 emits (from report)."""
    return {
        "id": "0195aaaa-ephemeral-system-thread",
        "ephemeral": True,
        "path": None,
        "threadSource": "system",
        "source": "vscode",
        "parentThreadId": None,
        "forkedFromId": None,
    }


async def _rotate(
    ap: _RecordingAP,
    target: fwd._ForwarderTarget,
    bridge_dir: Path,
    event: dict,
) -> bool:
    """Drive the real rotation entry point with ``event``.

    :param ap: Recording AP client.
    :param target: Forwarder target to (maybe) rotate.
    :param bridge_dir: Bridge directory.
    :param event: Codex ``thread/started`` envelope.
    :returns: ``True`` when the forwarder rotated the parent session.
    """
    return await fwd._maybe_rotate_session_on_thread_started(
        ap_client=ap,
        target=target,
        bridge_dir=bridge_dir,
        app_server_url=APP_SERVER_URL,
        event=event,
    )


async def test_ephemeral_system_thread_does_not_rotate_parent(tmp_path: Path) -> None:
    """Case 1 + 4: the ephemeral system ``thread/started`` must not rotate.

    This is the bug. On the buggy build the forwarder rotates the parent
    Omnigent session onto the ephemeral thread (creating a replacement session,
    transferring the terminal, and rewriting bridge state), which strands the
    real turn's output as stale and breaks goal reads. The parent session must
    stay bound to the persistent thread so goals keep working.
    """
    ap = _RecordingAP()
    bridge_dir = _seed_bridge(tmp_path)
    target = _make_target(ap)

    rotated = await _rotate(ap, target, bridge_dir, _thread_started(_ephemeral_system_thread()))

    assert rotated is False, "ephemeral/system thread/started must not rotate the parent session"
    # No replacement session may be created.
    assert ap.created_sessions() == []
    # The forwarder target and persisted bridge stay on the persistent thread,
    # so goal operations remain routed to a real (non-ephemeral) thread.
    assert target.session_id == PARENT_SESSION
    assert target.thread_id == PARENT_THREAD
    state = read_bridge_state(bridge_dir)
    assert state.session_id == PARENT_SESSION
    assert state.thread_id == PARENT_THREAD


async def test_real_user_clear_thread_still_rotates(tmp_path: Path) -> None:
    """Case 2: a genuine, non-ephemeral top-level ``/clear`` thread rotates.

    The fix must be narrow: a real user ``/clear`` starts a fresh persistent
    top-level thread (``ephemeral`` absent/false, no sub-agent spawn source),
    and that must keep rotating the Omnigent session as before.
    """
    ap = _RecordingAP()
    bridge_dir = _seed_bridge(tmp_path)
    target = _make_target(ap)

    clear_thread = {
        "id": "0195bbbb-real-clear-thread",
        "ephemeral": False,
        "path": "/rollout/0195bbbb.jsonl",
        "threadSource": "user",
    }
    rotated = await _rotate(ap, target, bridge_dir, _thread_started(clear_thread))

    assert rotated is True, "a real user /clear thread must still rotate the session"
    assert len(ap.created_sessions()) == 1


async def test_subagent_thread_still_ignored(tmp_path: Path) -> None:
    """Case 3: a sub-agent ``thread/started`` never rotates (unchanged behavior).

    AgentControl child threads emit ``thread/started`` when they begin. The
    fix must not disturb this check: sub-agent threads are still detected and
    ignored by the existing ``_thread_started_is_subagent`` guard.
    """
    ap = _RecordingAP()
    bridge_dir = _seed_bridge(tmp_path)
    target = _make_target(ap)

    subagent_thread = {
        "id": "0195cccc-subagent-thread",
        "ephemeral": False,
        "path": "/rollout/0195cccc.jsonl",
        "source": {
            "subAgent": {
                "thread_spawn": {
                    "parent_thread_id": PARENT_THREAD,
                    "turn_id": "turn_abc",
                }
            }
        },
    }
    rotated = await _rotate(ap, target, bridge_dir, _thread_started(subagent_thread))

    assert rotated is False, "sub-agent thread/started must not rotate the parent session"
    assert ap.created_sessions() == []


@pytest.mark.parametrize("thread_source", ["system", "vscode"])
async def test_ephemeral_thread_variants_do_not_rotate(tmp_path: Path, thread_source: str) -> None:
    """An ``ephemeral=true`` thread must not rotate regardless of source label.

    The report's event carries ``threadSource=system`` and ``source=vscode``;
    the invariant the fix must hold is that *ephemeral* threads never rotate,
    so this guards the ephemeral flag itself rather than a specific source.
    """
    ap = _RecordingAP()
    bridge_dir = _seed_bridge(tmp_path)
    target = _make_target(ap)

    thread = _ephemeral_system_thread()
    thread["threadSource"] = thread_source

    rotated = await _rotate(ap, target, bridge_dir, _thread_started(thread))

    assert rotated is False
    assert ap.created_sessions() == []
