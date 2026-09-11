"""
A reasoning-effort change made in the embedded Codex terminal (native mode)
must be reflected in the chat session composer's effort control.

User journey (native codex session):

1. Open a Codex chat session; the composer gear shows the launch effort.
2. Switch to the embedded Codex terminal and change the model reasoning
   effort there (in-TUI ``/model``).
3. Return to Chat view and inspect the composer gear.
4. Observe the composer still shows the OLD effort (stale).

Why the composer stays stale (the plumbing this test drives):

For codex-native, an in-TUI ``/model`` writes ``model`` and
``model_reasoning_effort`` to ``config.toml`` and emits NO ``thread/settings``
notification. The forwarder learns config-only changes by re-reading
``config.toml`` at each ``turn/started``. It does this for the model
(``_refresh_model_from_config`` -> ``_sync_model_change``, which mirrors an
``external_model_change`` the server persists and echoes to the SPA) and for
developer instructions -- but it never re-reads ``model_reasoning_effort`` and
never calls ``_sync_reasoning_effort_change`` on the ``turn/started`` path.
Effort is only mirrored from the ``thread/settings/updated`` branch, which the
config-only ``/model`` effort change does not trigger. So the terminal effort
change never reaches the server, no ``session.reasoning_effort`` event is
published, and the composer gear keeps the stale value.

The server -> SPA half is correct on main
(``_persist_external_reasoning_effort_change`` persists ``conv.reasoning_effort``
and publishes ``session.reasoning_effort``; ``web/src/lib/sse.ts`` +
``chatStore.ts`` update the composer). The break is purely the forwarder never
OBSERVING the terminal effort change and mirroring it.

This drives the real forwarder message dispatch
(:func:`omnigent.harnesses.codex_native.forwarder._maybe_handle_turn_event`)
through a ``turn/started`` after a ``/model`` effort change lands in
``config.toml`` and asserts the effort mirror POST
(``external_reasoning_effort_change``) is made -- exactly what the composer
depends on. It FAILS on ``main`` (no such POST) and is the fail->pass target for
the fix. The live SPA journey is captured as the nightly
``tests/e2e_ui/chat/test_codex_effort_terminal_composer_mirror.py`` guard;
codex-native turns cannot complete in the repro sandbox, so this
forwarder-dispatch test is the executable reproduction (the surface exists but
the harness cannot reach the failing state live).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from omnigent.harnesses.codex_native import forwarder as fwd
from omnigent.harnesses.codex_native.bridge import (
    codex_home_for_bridge_dir,
    write_codex_config_effort,
)


class _RecordingClient:
    """
    Async ``httpx`` client stub that records POSTs and returns HTTP 200.

    Mirrors the stub used by ``tests/test_codex_native_forwarder.py``: only
    ``post`` is exercised (by ``_post_session_event``), and every call is
    recorded so the test can assert exactly what the forwarder mirrored to the
    Omnigent server.
    """

    def __init__(self) -> None:
        """Initialize with an empty record of posts."""
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url: str, *, json: dict) -> httpx.Response:
        """
        Record ``(url, json)`` and return a 200 response.

        :param url: Request URL, e.g. ``"/v1/sessions/conv_x/events"``.
        :param json: JSON body, e.g.
            ``{"type": "external_reasoning_effort_change",
            "data": {"reasoning_effort": "high"}}``.
        :returns: A real ``httpx.Response`` with status 200.
        """
        self.posts.append((url, json))
        return httpx.Response(200, request=httpx.Request("POST", url))


def _write_codex_config(bridge_dir: Path, body: str) -> Path:
    """
    Write a ``config.toml`` into the session's per-session ``CODEX_HOME``.

    This is the file an in-TUI ``/model`` rewrites (model + reasoning effort);
    the forwarder reads it at ``turn/started``.

    :param bridge_dir: The bridge dir whose ``codex-home/config.toml`` is
        written.
    :param body: Raw TOML body, e.g.
        ``'model = "gpt-5.5"\\nmodel_reasoning_effort = "high"\\n'``.
    :returns: The written ``config.toml`` path.
    """
    home = codex_home_for_bridge_dir(bridge_dir)
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.toml"
    path.write_text(body)
    return path


def _effort_mirror_posts(posts: list[tuple[str, dict]]) -> list[dict]:
    """
    Return the ``external_reasoning_effort_change`` event bodies among posts.

    :param posts: Recorded ``(url, json)`` posts from the forwarder.
    :returns: The JSON bodies whose ``type`` is the effort-mirror event.
    """
    return [
        body
        for _url, body in posts
        if body.get("type") == fwd._EXTERNAL_REASONING_EFFORT_CHANGE_TYPE
    ]


async def _drive_turn_started(
    client: _RecordingClient,
    *,
    session_id: str,
    bridge_dir: Path,
    state: fwd._CodexForwarderState,
) -> None:
    """
    Drive the real forwarder ``turn/started`` dispatch once.

    Constructs the same collaborators the live drain loop passes to
    :func:`~omnigent.harnesses.codex_native.forwarder._maybe_handle_turn_event`
    (usage coalescer, elicitation tracker; no delta coalescer / codex client)
    and dispatches a ``turn/started`` notification through it.

    :param client: Recording HTTP client stub.
    :param session_id: Omnigent conversation id.
    :param bridge_dir: The session's native-Codex bridge directory.
    :param state: Mutable forwarder state.
    :returns: None.
    """
    usage_coalescer = fwd._SessionUsageCoalescer(client, session_id)  # type: ignore[arg-type]
    elicitation_tracker = fwd._CodexElicitationTaskTracker()

    handled = await fwd._maybe_handle_turn_event(
        client,  # type: ignore[arg-type]
        session_id=session_id,
        bridge_dir=bridge_dir,
        method="turn/started",
        params={"turn": {"id": "turn_effort_mirror"}},
        usage_coalescer=usage_coalescer,
        delta_coalescer=None,
        elicitation_tracker=elicitation_tracker,
        codex_client=None,
        forwarder_state=state,
    )
    assert handled is True


def test_turn_started_mirrors_terminal_effort_change_to_composer(tmp_path: Path) -> None:
    """
    A ``/model`` effort change in the terminal must mirror at ``turn/started``.

    Setup mirrors a running native-Codex session whose launch effort
    (``medium``) has already been mirrored to Omnigent, then the user changes
    the effort to ``high`` in the embedded terminal (``/model``), which rewrites
    ``config.toml`` (model unchanged, ``model_reasoning_effort = "high"``) with
    no ``thread/settings`` notification. The next terminal turn opens
    (``turn/started``).

    The forwarder must observe the new effort from ``config.toml`` and mirror it
    to the Omnigent server as an ``external_reasoning_effort_change`` carrying
    ``high`` -- the event the server persists and echoes to the SPA so the chat
    composer gear updates. On ``main`` no such POST is made (the ``turn/started``
    path re-reads model + developer instructions but never effort), so the
    composer stays stale: this assertion reproduces the stale-composer bug.
    """
    session_id = "conv_effort_mirror"
    # Launch model + effort, both already mirrored to Omnigent at spawn.
    state = fwd._CodexForwarderState()
    state.model = "gpt-5.5"
    state.posted_model = "gpt-5.5"
    state.last_config_model = "gpt-5.5"
    state.effort = "medium"
    state.posted_effort = "medium"
    state.posted_effort_known = True

    # The user changes effort in the terminal (/model): config.toml is rewritten
    # with the SAME model but a new reasoning effort, and NO notification fires.
    _write_codex_config(
        tmp_path,
        'model = "gpt-5.5"\nmodel_reasoning_effort = "high"\n',
    )

    client = _RecordingClient()
    asyncio.run(
        _drive_turn_started(
            client,
            session_id=session_id,
            bridge_dir=tmp_path,
            state=state,
        )
    )

    effort_posts = _effort_mirror_posts(client.posts)
    # The terminal effort change must reach the server so the composer updates.
    assert effort_posts, (
        "turn/started after an in-TUI /model effort change did not "
        "mirror the new effort to Omnigent (no external_reasoning_effort_change "
        "POST), so the chat composer gear stays stale. Posts observed: "
        f"{[body.get('type') for _u, body in client.posts]}"
    )
    assert effort_posts[-1] == {
        "type": fwd._EXTERNAL_REASONING_EFFORT_CHANGE_TYPE,
        "data": {"reasoning_effort": "high"},
    }
    # The mirrored value is exactly the terminal's config.toml selection.
    assert state.posted_effort == "high"


def test_turn_started_does_not_re_mirror_unchanged_terminal_effort(tmp_path: Path) -> None:
    """
    A ``turn/started`` with an unchanged effort must not re-post the mirror.

    Guards the fix against becoming chatty: when ``config.toml`` still holds the
    already-mirrored effort (no ``/model`` change happened), the forwarder must
    not emit a spurious ``external_reasoning_effort_change`` on every turn. This
    already holds on ``main`` (effort is never read on this path) and must keep
    holding after the fix adds the missing read + sync.
    """
    session_id = "conv_effort_mirror_stable"
    state = fwd._CodexForwarderState()
    state.model = "gpt-5.5"
    state.posted_model = "gpt-5.5"
    state.last_config_model = "gpt-5.5"
    state.effort = "high"
    state.posted_effort = "high"
    state.posted_effort_known = True

    _write_codex_config(
        tmp_path,
        'model = "gpt-5.5"\nmodel_reasoning_effort = "high"\n',
    )

    client = _RecordingClient()
    asyncio.run(
        _drive_turn_started(
            client,
            session_id=session_id,
            bridge_dir=tmp_path,
            state=state,
        )
    )

    assert _effort_mirror_posts(client.posts) == []


def test_reconnect_does_not_revert_composer_set_effort(tmp_path: Path) -> None:
    """
    A composer-picked effort must survive a forwarder reconnect / resume.

    A web-composer effort pick is applied to the live thread via
    ``thread/settings/update`` and — like a model pick — mirrored into
    ``config.toml`` by the executor (``write_codex_config_effort``), keeping the
    file the effort mirror treats as source of truth consistent. On a thread
    resume / reconnect the forwarder builds a FRESH ``_CodexForwarderState``
    (``effort=None``, ``last_config_effort=None``): its first config re-read
    must find the composer's effort, not the stale launch effort.

    Without the config write, the fresh state's first read would adopt the
    stale launch value (``medium``) and POST it as an
    ``external_reasoning_effort_change``, silently reverting the composer's
    ``high`` while the live thread still runs ``high`` — exactly the
    composer-vs-terminal divergence this fix exists to eliminate.
    """
    session_id = "conv_effort_reconnect"
    # Launch config: effort medium.
    _write_codex_config(
        tmp_path,
        'model = "gpt-5.5"\nmodel_reasoning_effort = "medium"\n',
    )
    # The user picks "high" in the web composer: the executor applies it via
    # thread/settings/update AND mirrors it into config.toml (the same write
    # _start_codex_turn performs for an applied effort override).
    assert write_codex_config_effort(tmp_path, "high") is True

    # Reconnect/resume: the supervisor builds a fresh forwarder state; nothing
    # from the previous lifetime (posted efforts, baselines) survives.
    state = fwd._CodexForwarderState()
    state.model = "gpt-5.5"
    state.posted_model = "gpt-5.5"
    state.last_config_model = "gpt-5.5"

    client = _RecordingClient()
    asyncio.run(
        _drive_turn_started(
            client,
            session_id=session_id,
            bridge_dir=tmp_path,
            state=state,
        )
    )

    mirrored = [body["data"]["reasoning_effort"] for body in _effort_mirror_posts(client.posts)]
    assert "medium" not in mirrored, (
        "reconnect reverted the composer-picked effort back to the stale "
        f"launch value: mirrored={mirrored!r}"
    )
    # The fresh state re-mirrors the composer's effort (idempotent server-side).
    assert state.effort == "high"
    assert mirrored == ["high"]
