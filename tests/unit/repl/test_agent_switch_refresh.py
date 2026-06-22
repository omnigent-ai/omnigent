"""Unit tests for the REPL's session-metadata refresh.

``_refresh_session_metadata`` re-fetches the session snapshot so an
in-place agent switch made from another client (web UI "Switch
agent") is reflected in the attached REPL: the adapter's agent name,
the toolbar label / window title, and the hydrated metadata
(``llm_model`` / ``harness`` / ``context_window``) all update. It has
two triggers: the ``session.agent_changed`` stream event (live, while
attached) and each turn start (catch-up — the event is transient
SSE-only, so one missed during a stream-pump reconnect gap or before
the REPL attached is never replayed).

Drives the real ``_SessionsChatReplAdapter`` and real SDK ``Session``
snapshots against stub client/host objects, so the full hydrate path
is exercised — not a re-implementation of it.
"""

from __future__ import annotations

import pytest
from omnigent_client._sessions import Session as SessionSnapshot
from omnigent_ui_sdk import RichBlockFormatter
from rich.text import Text

from omnigent.repl._repl import _refresh_session_metadata, _SessionsChatReplAdapter


class _SnapshotSessions:
    """
    ``client.sessions`` stub serving a fixed snapshot from ``get``.

    :param snapshot: The :class:`SessionSnapshot` to return.
    """

    def __init__(self, snapshot: SessionSnapshot) -> None:
        self._snapshot = snapshot
        self.get_calls: list[str] = []

    async def get(self, session_id: str) -> SessionSnapshot:
        """
        Return the configured snapshot.

        :param session_id: Session id requested, e.g. ``"conv_abc"``.
        :returns: The snapshot passed at construction.
        """
        self.get_calls.append(session_id)
        return self._snapshot


class _SnapshotClient:
    """
    Omnigent client stub exposing only the ``sessions.get`` surface.

    :param snapshot: Snapshot served by ``sessions.get``.
    """

    def __init__(self, snapshot: SessionSnapshot) -> None:
        self.sessions = _SnapshotSessions(snapshot)


class _RaisingSessions:
    """``client.sessions`` stub whose ``get`` always fails."""

    async def get(self, session_id: str) -> SessionSnapshot:
        """
        Simulate a snapshot fetch failure.

        :param session_id: Session id requested (ignored).
        :raises RuntimeError: Always.
        """
        raise RuntimeError("snapshot fetch failed")


class _RaisingClient:
    """Omnigent client stub whose ``sessions.get`` always fails."""

    def __init__(self) -> None:
        self.sessions = _RaisingSessions()


class _SwitchHost:
    """
    Host stub recording toolbar renames and rendered output.

    Concrete class (not MagicMock) so unexpected attribute access
    fails loudly. Only the surface ``_refresh_session_metadata``
    touches is implemented.
    """

    def __init__(self) -> None:
        self.model_names: list[str] = []
        self.outputs: list[object] = []

    def set_model_name(self, model_name: str) -> None:
        """
        Record a toolbar label update.

        :param model_name: New toolbar label, e.g. ``"claude native ui"``.
        """
        self.model_names.append(model_name)

    def output(self, renderable: object, *, soft_wrap: bool = False) -> None:
        """
        Record a rendered item.

        :param renderable: Any Rich-renderable object.
        :param soft_wrap: Ignored — matches the real host signature.
        """
        self.outputs.append(renderable)


def _make_adapter(client: object, agent_name: str = "nessie") -> _SessionsChatReplAdapter:
    """
    Build a real adapter attached to an existing session id.

    :param client: Omnigent client (stub) handed to the adapter.
    :param agent_name: Launch-time agent name, e.g. ``"nessie"``.
    :returns: Adapter with ``session_id`` pre-set (no HTTP issued).
    """
    return _SessionsChatReplAdapter(
        client,  # type: ignore[arg-type] — duck-typed stub
        agent_name,
        session_id="conv_abc",
    )


def _snapshot(agent_name: str | None) -> SessionSnapshot:
    """
    Build a post-switch session snapshot.

    Mirrors what the server returns after nessie → claude-native-ui:
    no spec-pinned model, claude-native harness, 200k window.

    :param agent_name: Bound agent name in the snapshot, or ``None``
        to simulate an old server that omits the field.
    :returns: A real SDK :class:`SessionSnapshot`.
    """
    return SessionSnapshot(
        id="conv_abc",
        agent_id="ag_new",
        status="idle",
        created_at=0,
        agent_name=agent_name,
        llm_model=None,
        harness="claude-native",
        context_window=200_000,
    )


@pytest.mark.asyncio
async def test_refresh_updates_name_toolbar_and_metadata_on_switch() -> None:
    """An agent change in the snapshot updates name, toolbar, and notice.

    Regression for the stale-toolbar report:
    after a web-UI switch nessie → claude code, the attached TUI kept
    showing "nessie" in the bottom toolbar and turn headers.
    """
    host = _SwitchHost()
    client = _SnapshotClient(_snapshot(agent_name="claude-native-ui"))
    adapter = _make_adapter(client)

    await _refresh_session_metadata(
        adapter,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        host,  # type: ignore[arg-type]
        RichBlockFormatter(),
    )

    # The adapter's public name now reflects the switched agent — this
    # drives turn-start headers, /context, and the bug-report template.
    assert adapter.model == "claude-native-ui"
    # Toolbar got exactly one rename, humanized for display. Empty
    # means the name-change branch didn't fire; a raw slug means
    # _humanize_agent_name was dropped.
    assert host.model_names == ["claude native ui"]
    # A single muted notice line tells the user why the label changed.
    assert len(host.outputs) == 1
    notice = host.outputs[0]
    assert isinstance(notice, Text)
    assert "Agent switched: nessie → claude native ui" in notice.plain
    # The rest of the snapshot hydrated too — the context ring resizes
    # from nessie's window to claude-native's 200k.
    assert adapter.context_window == 200_000
    assert adapter.harness == "claude-native"


@pytest.mark.asyncio
async def test_refresh_is_silent_when_agent_unchanged() -> None:
    """Same agent in the snapshot → no toolbar rename, no notice.

    This runs at every turn start, so a false-positive "switched"
    notice on ordinary turns would spam the transcript.
    """
    host = _SwitchHost()
    client = _SnapshotClient(_snapshot(agent_name="nessie"))
    adapter = _make_adapter(client)

    await _refresh_session_metadata(
        adapter,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        host,  # type: ignore[arg-type]
        RichBlockFormatter(),
    )

    assert adapter.model == "nessie"
    # No rename and no notice — equality (not truthiness) so a stray
    # call is caught even if it carried the same name.
    assert host.model_names == []
    assert host.outputs == []
    # Metadata still hydrates on ordinary turns (that's the point of
    # polling every turn, not only after a switch).
    assert adapter.context_window == 200_000


@pytest.mark.asyncio
async def test_refresh_keeps_launch_name_when_snapshot_omits_it() -> None:
    """``agent_name=None`` (old server) must not clobber the name.

    The launch-time name stays, no rename/notice fires, but the rest
    of the snapshot still hydrates.
    """
    host = _SwitchHost()
    client = _SnapshotClient(_snapshot(agent_name=None))
    adapter = _make_adapter(client)

    await _refresh_session_metadata(
        adapter,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        host,  # type: ignore[arg-type]
        RichBlockFormatter(),
    )

    # None means "unknown", not "renamed to nothing" — keep the
    # launch-time name rather than blanking the toolbar.
    assert adapter.model == "nessie"
    assert host.model_names == []
    assert host.outputs == []
    assert adapter.context_window == 200_000


@pytest.mark.asyncio
async def test_refresh_swallows_fetch_failure() -> None:
    """A failed snapshot fetch leaves all state untouched and raises nothing.

    The helper runs as a fire-and-forget background task; an exception
    here would surface as an unhandled-task traceback in the REPL —
    the same failure mode as the original crash.
    """
    host = _SwitchHost()
    client = _RaisingClient()
    adapter = _make_adapter(client)

    await _refresh_session_metadata(
        adapter,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        host,  # type: ignore[arg-type]
        RichBlockFormatter(),
    )

    # Nothing changed: stale-but-consistent beats a crash mid-turn.
    assert adapter.model == "nessie"
    assert host.model_names == []
    assert host.outputs == []
    assert adapter.context_window is None


def test_both_triggers_spawn_metadata_refresh() -> None:
    """Both refresh triggers in ``run_repl`` spawn the metadata refresh.

    The refresh helper is unit-tested above, but its triggers live in
    the ``_render_session_event`` closure inside ``run_repl``, which
    can't be invoked in isolation. Mirror of the source-inspection
    style used by ``test_context_ring_state_initialized_to_false``: a
    quick source assert catches the wiring being dropped (e.g. in a
    refactor of the event dispatch), which the helper tests cannot see.
    """
    import inspect
    import re

    from omnigent.repl import _repl

    src = inspect.getsource(_repl.run_repl)
    # The spawn helper must schedule the real refresh coroutine; without
    # it neither trigger reaches the TUI.
    assert "_refresh_session_metadata(session, client, host, fmt)" in src, (
        "run_repl's _spawn_metadata_refresh no longer wires "
        "_refresh_session_metadata — the toolbar/agent name will go "
        "stale after an in-place agent switch."
    )
    # The live trigger: session.agent_changed must be dispatched. Without
    # this branch an idle attached REPL only learns of a switch at its
    # next turn start.
    assert "isinstance(event, _AgentChangedEv)" in src, (
        "run_repl no longer dispatches session.agent_changed — an idle "
        "REPL won't reflect a switch until the next turn."
    )
    # Exactly two standalone call sites: the running-status (turn-start
    # catch-up) branch and the agent-changed (live) branch. 1 means a
    # trigger was dropped; the regex excludes the def line so the helper
    # definition doesn't inflate the count.
    calls = re.findall(r"^\s*_spawn_metadata_refresh\(\)\s*$", src, re.MULTILINE)
    assert len(calls) == 2, (
        f"expected 2 _spawn_metadata_refresh() call sites (turn-start "
        f"catch-up + session.agent_changed), found {len(calls)}."
    )


def _runner_snapshot(runner_id: str | None) -> SessionSnapshot:
    """
    Build a snapshot carrying a specific bound ``runner_id``.

    :param runner_id: Runner bound to the session in the snapshot, or
        ``None`` for a not-yet-bound session.
    :returns: A real SDK :class:`SessionSnapshot`.
    """
    return SessionSnapshot(
        id="conv_abc",
        agent_id="ag_new",
        status="idle",
        created_at=0,
        agent_name="nessie",
        llm_model=None,
        harness="claude-native",
        context_window=200_000,
        runner_id=runner_id,
    )


@pytest.mark.asyncio
async def test_refresh_adopts_server_relaunched_runner_id() -> None:
    """A relaunched runner_id in the snapshot becomes the adapter's own.

    Regression for the idle-runner resume bug: a host-bound runner
    idle-times-out and is relaunched server-side under a NEW runner_id
    on the next message. This metadata refresh hydrates the snapshot's
    new id into ``_bound_runner_id``; if ``_runner_id`` stayed frozen
    at the launch-time runner, every subsequent ``_bind_runner_if_needed``
    would PATCH the session back onto the dead, deregistered runner and
    the server would 400 with "runner '<id>' is not registered". The
    adapter must adopt the relaunched id so the two stay in sync.
    """
    client = _SnapshotClient(_runner_snapshot("runner_token_new"))
    adapter = _make_adapter(client)
    # Simulate the launch-time / prepare-time bind: both ids point at the
    # original daemon runner, which has since idle-died and relaunched.
    adapter._runner_id = "runner_token_old"
    adapter._bound_runner_id = "runner_token_old"

    await _refresh_session_metadata(
        adapter,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        _SwitchHost(),  # type: ignore[arg-type]
        RichBlockFormatter(),
    )

    # Both now point at the relaunched runner — a follow-up bind check
    # sees them equal and skips the PATCH entirely.
    assert adapter._bound_runner_id == "runner_token_new"
    assert adapter._runner_id == "runner_token_new"
    await adapter._bind_runner_if_needed()
    # No PATCH issued: the stub client has no bind_runner surface, so a
    # bind attempt would AttributeError. Reaching here proves it skipped.


@pytest.mark.asyncio
async def test_refresh_keeps_runner_id_when_snapshot_unbound() -> None:
    """A snapshot with ``runner_id=None`` must not wipe the runner to bind.

    A freshly-created session is not yet bound (snapshot runner_id None);
    the launch-time ``_runner_id`` is the runner we still need to PATCH,
    so the adopt-from-snapshot logic must guard on a non-empty id.
    """
    client = _SnapshotClient(_runner_snapshot(None))
    adapter = _make_adapter(client)
    adapter._runner_id = "runner_token_launch"

    await _refresh_session_metadata(
        adapter,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        _SwitchHost(),  # type: ignore[arg-type]
        RichBlockFormatter(),
    )

    assert adapter._runner_id == "runner_token_launch"


@pytest.mark.asyncio
async def test_refresh_preserves_client_owned_runner_id() -> None:
    """With a recovery callback, the client owns ``_runner_id`` — keep it.

    The ``runner_recover`` path relaunches a CLIENT-owned runner and
    drives ``_runner_id`` itself; the server snapshot must not override
    that, or a refresh racing recovery would rebind the stale runner.
    """
    client = _SnapshotClient(_runner_snapshot("runner_token_server"))
    adapter = _SessionsChatReplAdapter(
        client,  # type: ignore[arg-type]
        "nessie",
        session_id="conv_abc",
        runner_id="runner_token_client",
        runner_recover=lambda: "runner_token_client",
    )

    await _refresh_session_metadata(
        adapter,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        _SwitchHost(),  # type: ignore[arg-type]
        RichBlockFormatter(),
    )

    assert adapter._runner_id == "runner_token_client"
