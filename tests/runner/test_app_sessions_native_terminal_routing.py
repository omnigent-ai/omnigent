"""Tests for native terminal ensure routing and REPL terminal creation."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent import (
    codex_native_bridge,
)
from omnigent.entities.session_resources import SessionResourceView, terminal_resource_id
from omnigent.runner import create_runner_app
from omnigent.runner.app import (
    _auto_create_repl_terminal,
)
from omnigent.runner.resource_registry import (
    CODEX_NATIVE_TERMINAL_ROLE,
    OMNIGENT_REPL_TERMINAL_ROLE,
    SessionResourceRegistry,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals import TerminalRegistry
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
)
from tests.runner.helpers import NullServerClient


@dataclass
class _EnsureTerminalCase:
    """
    One routing case for the claude-native ``create_session_terminal``
    ensure-path branch.

    :param case_id: Human-readable id for the parametrize label.
    :param body: The ``POST /resources/terminals`` JSON body.
    :param existing: Whether a live ``claude``/``main`` terminal already
        exists (drives the stubbed ``get_terminal_resource``).
    :param expect_auto_create: Whether the request must route to
        ``_auto_create_claude_terminal`` (the ensure path).
    :param expect_launch: Whether the request must route to the generic
        terminal launch path instead.
    :param expect_name: ``name`` of the resource the route must return —
        identifies which collaborator produced the response.
    """

    case_id: str
    body: dict[str, object]
    existing: bool
    expect_auto_create: bool
    expect_launch: bool
    expect_name: str


@pytest.mark.parametrize(
    "case",
    [
        _EnsureTerminalCase(
            case_id="ensure_no_terminal_auto_creates",
            body={"terminal": "claude", "session_key": "main", "ensure_native_terminal": True},
            existing=False,
            expect_auto_create=True,
            expect_launch=False,
            expect_name="auto-created",
        ),
        _EnsureTerminalCase(
            case_id="ensure_existing_returns_live",
            body={"terminal": "claude", "session_key": "main", "ensure_native_terminal": True},
            existing=True,
            expect_auto_create=False,
            expect_launch=False,
            expect_name="existing",
        ),
        _EnsureTerminalCase(
            # No ensure marker => a plain claude/main launch must take the
            # generic path, NOT the ensure branch. This is the exact body
            # test_comment_relay's plain launch sends; keying on the marker
            # (not on absent spec/bridge) is what keeps that path intact.
            case_id="no_marker_uses_generic_launch",
            body={"terminal": "claude", "session_key": "main"},
            existing=False,
            expect_auto_create=False,
            expect_launch=True,
            expect_name="launched",
        ),
    ],
    ids=lambda c: c.case_id,
)
async def test_create_session_terminal_ensure_routes_claude_native(
    case: _EnsureTerminalCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``POST /resources/terminals`` routes a claude/main request correctly.

    Guards the resume "ensure" branch added so a reattach onto a reused
    daemon runner re-creates the torn-down Claude terminal: a request with
    no ``spec`` and no ``bridge_inject_dir`` must go to the full native
    ``_auto_create_claude_terminal`` (or return the live terminal if one
    exists), while a request carrying ``spec``/``bridge_inject_dir`` (the
    fresh-launch wrapper path) must still use the generic terminal launch.

    The three collaborators are stubbed so the routing decision is the only
    thing under test; each returns a distinctly-named real
    :class:`SessionResourceView`, so the response ``name`` proves which path
    handled the request. Remove the ensure branch and the auto-create cases
    fall through to the generic terminal launch path (wrong name,
    ``launched`` recorded); drop the ``not spec`` guard and the explicit-spec
    case wrongly auto-creates — either way this test fails.

    :param case: The parametrized routing scenario.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    sid = "babf6c60e977e4e5f2654080d24cab40"
    auto_create_calls: list[str] = []
    launch_calls: list[str] = []

    async def _stub_auto_create(
        session_id: str,
        resource_registry: object,
        publish_event: object,
        **_kwargs: object,
    ) -> SessionResourceView:
        """Record the ensure-path call and return a tagged terminal view."""
        del resource_registry, publish_event
        auto_create_calls.append(session_id)
        return SessionResourceView(
            id="terminal_claude_main", type="terminal", session_id=session_id, name="auto-created"
        )

    async def _stub_get_terminal(
        self: object, session_id: str, terminal_id: str
    ) -> SessionResourceView | None:
        """Return a live view only when the case seeds an existing terminal."""
        del self
        if case.existing and terminal_id == "terminal_claude_main":
            return SessionResourceView(
                id=terminal_id, type="terminal", session_id=session_id, name="existing"
            )
        return None

    async def _stub_launch_auxiliary_terminal(
        self: object, *, session_id: str, terminal_name: str, session_key: str, **_kwargs: object
    ) -> SessionResourceView:
        """Record the generic-launch call and return a tagged terminal view."""
        del self, _kwargs
        launch_calls.append(f"{terminal_name}:{session_key}")
        return SessionResourceView(
            id=terminal_resource_id(terminal_name, session_key),
            type="terminal",
            session_id=session_id,
            name="launched",
        )

    monkeypatch.setattr("omnigent.runner.app._auto_create_claude_terminal", _stub_auto_create)
    monkeypatch.setattr(SessionResourceRegistry, "get_terminal_resource", _stub_get_terminal)
    monkeypatch.setattr(
        SessionResourceRegistry,
        "launch_auxiliary_terminal",
        _stub_launch_auxiliary_terminal,
    )

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=TerminalRegistry(),
    )

    async with _runner_client(app) as client:
        resp = await client.post(f"/v1/sessions/{sid}/resources/terminals", json=case.body)

    assert resp.status_code == 200, resp.text
    # The response name identifies the path taken: only the routed
    # collaborator's view reaches the client.
    assert resp.json()["name"] == case.expect_name, (
        f"{case.case_id}: response came from the wrong path "
        f"(expected {case.expect_name!r}, got {resp.json()['name']!r})"
    )
    # auto_create fires iff this is an ensure request with no live terminal.
    # If empty when expected, the ensure branch did not route to the native
    # auto-create; if populated when not expected, the spec discriminator
    # leaked and a wrapper launch was hijacked.
    assert (auto_create_calls == [sid]) == case.expect_auto_create, (
        f"{case.case_id}: auto_create_calls={auto_create_calls}, "
        f"expected_auto_create={case.expect_auto_create}"
    )
    # The generic launch fires iff the request carried a spec (wrapper path).
    assert (launch_calls == ["claude:main"]) == case.expect_launch, (
        f"{case.case_id}: launch_calls={launch_calls}, expected_launch={case.expect_launch}"
    )


@pytest.mark.asyncio
async def test_create_session_terminal_ensure_failure_returns_json_without_live_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Native terminal ensure failures are reported to AP, not published live.

    ``ensure_native_terminal`` is called by the Omnigent server while handling a
    user message. Omnigent owns that failed transcript turn: it persists the
    consumed user message, appends the sibling ``error`` item, and
    publishes the live banner. If the runner endpoint also publishes
    ``response.error`` before returning its structured 500, the same
    terminal failure is rendered twice live and can be persisted twice by
    the relay.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    sid = "aefc71354fadf0dd2ae5c224c40e772c"

    async def _failing_auto_create(
        session_id: str,
        resource_registry: object,
        publish_event: object,
        **_kwargs: object,
    ) -> SessionResourceView:
        """Raise the native startup error the endpoint must return as JSON."""
        del session_id, resource_registry, publish_event, _kwargs
        raise ImportError("Native Claude requires the 'claude' CLI on PATH.")

    def _unexpected_live_publish(*_args: object, **_kwargs: object) -> None:
        """Fail if the ensure endpoint tries to publish the live banner."""
        raise AssertionError("ensure endpoint must not publish response.error")

    monkeypatch.setattr("omnigent.runner.app._auto_create_claude_terminal", _failing_auto_create)
    monkeypatch.setattr(
        "omnigent.runner.app._publish_native_terminal_start_error",
        _unexpected_live_publish,
    )

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=TerminalRegistry(),
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{sid}/resources/terminals",
            json={"terminal": "claude", "session_key": "main", "ensure_native_terminal": True},
        )

    assert resp.status_code == 500
    # Structured code is preserved; the message is a fixed client-safe
    # string. The raw ImportError text ("requires the 'claude' CLI") must
    # not appear in the HTTP body — it is logged on the runner instead.
    body = resp.json()
    assert body["error"]["code"] == "native_terminal_start_failed"
    assert body["error"]["message"] == (
        "Native Claude terminal failed to start; see runner logs for details."
    )
    assert "requires the 'claude' CLI" not in body["error"]["message"]


@dataclass
class _EnsureCodexTerminalCase:
    """
    One routing case for the codex-native ensure terminal branch.

    :param case_id: Human-readable id for the parametrized case.
    :param body: ``POST /resources/terminals`` JSON body.
    :param existing: Whether a live ``codex``/``main`` terminal exists.
    :param existing_native: Whether the existing terminal metadata looks
        like a runner-owned Codex remote TUI.
    :param expect_auto_create: Whether the route should call the full
        codex-native auto-create helper.
    :param expect_launch: Whether the route should fall through to the
        generic terminal launch path.
    :param expect_close: Whether the existing terminal should be closed
        before native auto-create.
    :param expect_name: Resource ``name`` expected in the HTTP response.
    """

    case_id: str
    body: dict[str, object]
    existing: bool
    existing_native: bool
    expect_auto_create: bool
    expect_launch: bool
    expect_close: bool
    expect_name: str


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        _EnsureCodexTerminalCase(
            case_id="ensure_no_terminal_auto_creates",
            body={"terminal": "codex", "session_key": "main", "ensure_native_terminal": True},
            existing=False,
            existing_native=False,
            expect_auto_create=True,
            expect_launch=False,
            expect_close=False,
            expect_name="auto-created",
        ),
        _EnsureCodexTerminalCase(
            case_id="ensure_existing_returns_live",
            body={"terminal": "codex", "session_key": "main", "ensure_native_terminal": True},
            existing=True,
            existing_native=True,
            expect_auto_create=False,
            expect_launch=False,
            expect_close=False,
            expect_name="existing",
        ),
        _EnsureCodexTerminalCase(
            case_id="ensure_existing_bash_terminal_replaces",
            body={"terminal": "codex", "session_key": "main", "ensure_native_terminal": True},
            existing=True,
            existing_native=False,
            expect_auto_create=True,
            expect_launch=False,
            expect_close=True,
            expect_name="auto-created",
        ),
        _EnsureCodexTerminalCase(
            case_id="no_marker_uses_generic_launch",
            body={"terminal": "codex", "session_key": "main"},
            existing=False,
            existing_native=False,
            expect_auto_create=False,
            expect_launch=True,
            expect_close=False,
            expect_name="launched",
        ),
    ],
    ids=lambda c: c.case_id,
)
async def test_create_session_terminal_ensure_routes_codex_native(
    case: _EnsureCodexTerminalCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``POST /resources/terminals`` routes a codex/main ensure request.

    The ensure marker must invoke the runner-owned Codex setup
    (app-server, forwarder, and TUI terminal) or return an existing
    terminal. Without the marker, a plain codex terminal launch remains a
    generic terminal request. Removing this branch makes the auto-create
    cases return ``"launched"``; over-broad routing makes the generic case
    return ``"auto-created"``.

    :param case: Parametrized routing scenario.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    sid = "ad2c1855c982b13e9c4df55b75d26ef8"
    auto_create_calls: list[str] = []
    auto_create_kwargs: list[dict[str, object]] = []
    launch_calls: list[str] = []
    close_calls: list[str] = []
    route_events: list[str] = []

    async def _stub_auto_create(
        session_id: str,
        resource_registry: object,
        publish_event: object,
        **kwargs: object,
    ) -> SessionResourceView:
        """
        Record the codex-native ensure path.

        :param session_id: Session id being ensured, e.g.
            ``"ad2c1855c982b13e9c4df55b75d26ef8"``.
        :param resource_registry: Runner resource registry collaborator.
        :param publish_event: Runner event publisher collaborator.
        :param kwargs: Additional keyword arguments such as
            ``server_client`` and ``agent_spec``.
        :returns: Tagged terminal resource view.
        """
        del resource_registry, publish_event
        route_events.append("auto-create")
        auto_create_calls.append(session_id)
        auto_create_kwargs.append(kwargs)
        return SessionResourceView(
            id="terminal_codex_main",
            type="terminal",
            session_id=session_id,
            name="auto-created",
        )

    async def _stub_get_terminal(
        self: object,
        session_id: str,
        terminal_id: str,
    ) -> SessionResourceView | None:
        """
        Return an existing terminal only for seeded cases.

        :param self: Bound registry instance.
        :param session_id: Session id being queried, e.g.
            ``"ad2c1855c982b13e9c4df55b75d26ef8"``.
        :param terminal_id: Terminal resource id, e.g.
            ``"terminal_codex_main"``.
        :returns: Existing resource view or ``None``.
        """
        del self
        if case.existing and terminal_id == "terminal_codex_main":
            return SessionResourceView(
                id=terminal_id,
                type="terminal",
                session_id=session_id,
                name="existing",
                metadata={
                    "terminal_name": "codex",
                    "session_key": "main",
                },
            )
        return None

    def _stub_terminal_resource_role(
        self: object,
        session_id: str,
        terminal_id: str,
    ) -> str | None:
        """
        Return the private resource role for seeded existing terminals.

        :param self: Bound registry instance.
        :param session_id: Session id being queried, e.g.
            ``"ad2c1855c982b13e9c4df55b75d26ef8"``.
        :param terminal_id: Terminal resource id, e.g.
            ``"terminal_codex_main"``.
        :returns: ``"codex-native"`` for native seeded terminals.
        """
        del self, session_id
        if case.existing and case.existing_native and terminal_id == "terminal_codex_main":
            return CODEX_NATIVE_TERMINAL_ROLE
        return None

    async def _stub_close_terminal(
        self: object,
        session_id: str,
        terminal_id: str,
    ) -> bool:
        """
        Record stale terminal replacement closes.

        :param self: Bound registry instance.
        :param session_id: Session id being modified, e.g.
            ``"ad2c1855c982b13e9c4df55b75d26ef8"``.
        :param terminal_id: Terminal resource id, e.g.
            ``"terminal_codex_main"``.
        :returns: ``True`` to allow replacement.
        """
        del self
        assert route_events == []
        route_events.append("close")
        close_calls.append(f"{session_id}:{terminal_id}")
        return True

    async def _stub_launch_auxiliary_terminal(
        self: object,
        *,
        session_id: str,
        terminal_name: str,
        session_key: str,
        **kwargs: object,
    ) -> SessionResourceView:
        """
        Record generic terminal launch calls.

        :param self: Bound registry instance.
        :param session_id: Session id being launched, e.g.
            ``"ad2c1855c982b13e9c4df55b75d26ef8"``.
        :param terminal_name: Terminal name, e.g. ``"codex"``.
        :param session_key: Terminal session key, e.g. ``"main"``.
        :param kwargs: Additional launch keyword arguments.
        :returns: Tagged terminal resource view.
        """
        del self, kwargs
        launch_calls.append(f"{terminal_name}:{session_key}")
        return SessionResourceView(
            id=terminal_resource_id(terminal_name, session_key),
            type="terminal",
            session_id=session_id,
            name="launched",
        )

    monkeypatch.setattr("omnigent.runner.app._auto_create_codex_terminal", _stub_auto_create)
    monkeypatch.setattr(SessionResourceRegistry, "get_terminal_resource", _stub_get_terminal)
    monkeypatch.setattr(
        SessionResourceRegistry,
        "terminal_resource_role",
        _stub_terminal_resource_role,
    )
    monkeypatch.setattr(SessionResourceRegistry, "close_terminal", _stub_close_terminal)
    monkeypatch.setattr(
        SessionResourceRegistry,
        "launch_auxiliary_terminal",
        _stub_launch_auxiliary_terminal,
    )

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=TerminalRegistry(),
    )

    async with _runner_client(app) as client:
        resp = await client.post(f"/v1/sessions/{sid}/resources/terminals", json=case.body)

    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == case.expect_name
    assert (auto_create_calls == [sid]) == case.expect_auto_create
    if case.expect_auto_create:
        assert auto_create_kwargs[0]["server_client"] is not None
        assert "agent_spec" in auto_create_kwargs[0]
    else:
        assert auto_create_kwargs == []
    assert (launch_calls == ["codex:main"]) == case.expect_launch
    assert (close_calls == [f"{sid}:terminal_codex_main"]) == case.expect_close
    expected_events = ["close", "auto-create"] if case.expect_close else []
    if case.expect_auto_create and not case.expect_close:
        expected_events = ["auto-create"]
    assert route_events == expected_events


@pytest.mark.asyncio
async def test_late_status_for_deleted_sub_agent_child_is_not_a_spurious_503() -> None:
    """
    A terminal status arriving after a sub-agent child is deleted is a no-op.

    A child created with ``sub_agent_name`` is tracked in the runner's
    sub-agent name map; that registration is what turns a no-work-entry
    terminal status into a 503 (preserve-the-handoff — see
    ``test_known_subagent_status_without_work_entry_returns_503``). Once the
    child is deleted there is nothing to preserve, so ``delete_session`` must
    drop the name. Without the pop, the lingering name makes the late status
    read ``is_runner_known_subagent=True`` with no work entry → a spurious
    ``503 subagent_delivery_not_confirmed`` (which Omnigent then retries) plus an
    unbounded leak of the name map across deleted sessions.
    """
    child_id = "045873be7e66575e49c755387fecf59a"
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={
                "session_id": child_id,
                "agent_id": "2c81960171b1c893befb5cc2598ecf5c",
                "sub_agent_name": "worker",
            },
        )
        assert create_resp.status_code == 201, create_resp.text

        del_resp = await client.delete(f"/v1/sessions/{child_id}")
        assert del_resp.status_code == 200, del_resp.text

        late_status = await client.post(
            f"/v1/sessions/{child_id}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "idle", "output": "LATE_AFTER_DELETE"},
            },
        )

    # The fix: delete drops the runner-known name, so the late status is a
    # clean 204 no-op. Without the pop the name lingers and this returns a
    # spurious 503 subagent_delivery_not_confirmed.
    assert late_status.status_code == 204, late_status.text


@dataclass
class _RecordedPatch:
    """
    A PATCH captured from the REPL terminal auto-create helper.

    :param url: Request path, e.g. ``"/v1/sessions/11c50cd73e9c32ccb0af5b9db291db8b"``.
    :param json: JSON body, e.g. ``{"labels": {"omnigent.ui": "terminal"}}``.
    """

    url: str
    json: dict[str, Any]


@pytest.mark.asyncio
async def test_auto_create_repl_terminal_launches_attach_and_stamps_label(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The REPL terminal hosts ``omnigent attach`` and stamps the UI label.

    The web UI embeds the framework's own TUI for SDK sessions through
    this terminal: the spec must run ``omnigent attach <session_id>
    --server <runner's server URL>`` (a co-drive client of the live
    session), defer the process to first attach, pin the cwd to the
    runner workspace, stamp the ``omnigent.ui: terminal`` label that
    gates the web Chat/Terminal pill, and publish the resource on the
    live stream. Each wrong value maps to a distinct user-facing break:
    wrong command/args → dead pane or wrong session; missing label →
    no pill; missing publish → pill stays gray until refresh.

    :param tmp_path: Temporary directory for the fake runner workspace.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    from omnigent._wrapper_labels import UI_MODE_LABEL_KEY, UI_MODE_TERMINAL_VALUE

    session_id = "11c50cd73e9c32ccb0af5b9db291db8b"
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(workspace))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")

    launched_specs: list[Any] = []

    class _FakeResourceRegistry:
        """Resource registry that records the launched REPL terminal spec."""

        async def launch_auxiliary_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """
            Record the terminal launch request.

            :param session_id: Session id being launched.
            :param terminal_name: Terminal name, e.g. ``"tui"``.
            :param session_key: Terminal session key, e.g. ``"main"``.
            :param spec: Terminal launch spec.
            :param resource_role: Private runner resource marker.
            :returns: Terminal resource view.
            """
            assert session_id == "11c50cd73e9c32ccb0af5b9db291db8b"
            assert terminal_name == "tui"
            assert session_key == "main"
            # The REPL role marks the pane for recreate-on-attach (a
            # dead REPL pane is relaunched instead of rejected with
            # 4404). It is distinct from CLAUDE_NATIVE_TERMINAL_ROLE,
            # so the pane's activity still does not drive the
            # session's working status.
            assert resource_role == OMNIGENT_REPL_TERMINAL_ROLE
            launched_specs.append(spec)
            return SessionResourceView(
                id="terminal_tui_main",
                type="terminal",
                session_id=session_id,
                name="tui",
            )

    class _PatchRecordingServerClient:
        """Server client that records label PATCHes from the helper."""

        def __init__(self) -> None:
            """:returns: None."""
            self.patches: list[_RecordedPatch] = []

        async def patch(self, url: str, **kwargs: Any) -> httpx.Response:
            """
            Record the PATCH and return a 200.

            :param url: Request path, e.g. ``"/v1/sessions/11c50cd73e9c32ccb0af5b9db291db8b"``.
            :param kwargs: Request keyword arguments carrying ``json``.
            :returns: HTTP 200 response.
            """
            self.patches.append(_RecordedPatch(url=url, json=kwargs.get("json") or {}))
            return httpx.Response(200, json={}, request=httpx.Request("PATCH", url))

    published_events: list[dict[str, Any]] = []
    server_client = _PatchRecordingServerClient()

    terminal_view = await _auto_create_repl_terminal(
        session_id,
        _FakeResourceRegistry(),  # type: ignore[arg-type]
        lambda _sid, event: published_events.append(event),
        server_client=server_client,  # type: ignore[arg-type]
    )

    assert terminal_view.id == "terminal_tui_main"
    assert len(launched_specs) == 1
    launched = launched_specs[0]
    # The hosted TUI is the framework's own REPL joining THIS session on
    # THIS server. A wrong interpreter/module means the pane dies at
    # first attach; a wrong session id or --server URL attaches the REPL
    # to the wrong place.
    assert launched.command == sys.executable
    assert launched.args == [
        "-m",
        "omnigent",
        "attach",
        session_id,
        "--server",
        "http://ap.example",
    ]
    # Deferred start: the REPL process must not run until the first web
    # client attaches — never-opened terminals stay an idle tmux pane,
    # and the session is fully live by first attach.
    assert launched.tmux_start_on_attach is True
    # cwd pins to the runner workspace (same convention as the
    # claude-native terminal); a wrong cwd drops the REPL into $HOME.
    assert launched.os_env.cwd == str(workspace)
    # The presentation label gates the web Chat/Terminal pill
    # (TerminalFirstContext); without this PATCH the embedded terminal
    # is unreachable from the UI.
    assert server_client.patches == [
        _RecordedPatch(
            url=f"/v1/sessions/{session_id}",
            json={"labels": {UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE}},
        )
    ]
    # The live resource event enables the toggle without a refresh
    # (snapshot-on-connect only covers clients that connect later).
    assert published_events[0]["type"] == "session.resource.created"
    assert published_events[0]["resource"]["id"] == "terminal_tui_main"


@pytest.mark.asyncio
async def test_auto_create_repl_terminal_inherits_agent_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The REPL terminal honours the agent's ``os_env.sandbox``.

    Regression for the same sandbox-override bug #175 fixed on the
    codex/claude auto-create paths but missed on the REPL path: the
    REPL auto-create built a fresh ``TerminalEnvSpec`` whose ``os_env``
    carried no ``sandbox`` and passed no ``parent_os_env``, so
    ``launch_terminal`` fell back to ``_default_sandbox_for_platform``
    (``linux_bwrap`` / ``darwin_seatbelt``) and ignored the agent YAML.
    An SDK-harness agent that declares ``os_env.sandbox.type: none``
    (relying on the outer container/VM as the boundary) was wrongly
    forced into bwrap, which then failed to start in a hardened
    container with ``native_terminal_start_failed``.

    Asserts the launched terminal spec's ``os_env.sandbox`` is the
    agent's ``none`` sandbox (not the platform default) and that the
    agent's ``os_env`` is threaded through as the launch
    ``parent_os_env`` so the rest of the policy is inherited too.

    :param tmp_path: Temporary directory for the fake runner workspace.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    session_id = "f75bf7158ce8716ae3b934522271979c"
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(workspace))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")

    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        """Captures the launched REPL terminal spec and parent os_env."""

        async def launch_auxiliary_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """Record the spec + parent_os_env and return a resource view."""
            del terminal_name, session_key, resource_role
            captured["spec"] = spec
            captured["parent_os_env"] = parent_os_env
            return SessionResourceView(
                id="terminal_tui_main",
                type="terminal",
                session_id=session_id,
                name="tui",
            )

    class _PatchRecordingServerClient:
        """Server client that absorbs the label PATCH from the helper."""

        async def patch(self, url: str, **kwargs: Any) -> httpx.Response:
            """Return a 200 for the presentation-label PATCH."""
            del kwargs
            return httpx.Response(200, json={}, request=httpx.Request("PATCH", url))

    # An agent that declares sandbox: none (runs unconfined; the outer
    # container/VM is the boundary).
    agent_os_env = OSEnvSpec(
        type="caller_process",
        cwd=".",
        sandbox=OSEnvSandboxSpec(type="none"),
    )
    agent_spec = AgentSpec(
        spec_version=1,
        name="sdk_worker",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "openai-agents", "model": "claude-default"},
        ),
        os_env=agent_os_env,
    )

    await _auto_create_repl_terminal(
        session_id,
        _FakeResourceRegistry(),  # type: ignore[arg-type]
        lambda _sid, _evt: None,
        server_client=_PatchRecordingServerClient(),  # type: ignore[arg-type]
        agent_spec=agent_spec,
    )

    launched_sandbox = captured["spec"].os_env.sandbox
    assert launched_sandbox is not None, (
        "REPL auto-create dropped the agent's sandbox; launch_terminal will "
        "fall back to _default_sandbox_for_platform (bwrap), overriding "
        "sandbox: none"
    )
    assert launched_sandbox.type == "none"
    # The whole os_env is threaded through as the inheritance parent.
    assert captured["parent_os_env"] is agent_os_env


@pytest.mark.parametrize(
    ("harness", "sub_agent_name", "expect_created"),
    [
        # SDK harness, top-level → REPL terminal auto-creates.
        ("openai-agents", None, True),
        # Sub-agent sessions surface through the parent transcript — no
        # REPL pane of their own.
        ("openai-agents", "worker", False),
        # Native harnesses own a dedicated terminal (the vendor TUI); the
        # REPL pane must not double up next to it.
        ("codex-native", None, False),
    ],
)
@pytest.mark.asyncio
async def test_create_session_repl_terminal_dispatch(
    harness: str,
    sub_agent_name: str | None,
    expect_created: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``POST /v1/sessions`` auto-creates the REPL terminal for SDK sessions only.

    Exercises the route-level dispatch condition: non-native harness AND
    top-level session AND a terminal registry present. If the condition
    regresses, either SDK sessions lose their embedded web TUI (no
    create) or native / sub-agent sessions grow a spurious second
    terminal (over-create).

    :param harness: Harness id resolved from the agent spec,
        e.g. ``"openai-agents"``.
    :param sub_agent_name: ``sub_agent_name`` in the POST body, or
        ``None`` for a top-level session.
    :param expect_created: Whether the REPL auto-create must fire.
    :param tmp_path: Temporary directory isolating bridge state.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    # Keep the codex-native branch's bridge writes inside tmp_path.
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")

    spec = AgentSpec(
        spec_version=1,
        name="dispatch-agent",
        executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
    )
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        """Return the parametrized spec for any agent id."""
        del agent_id, session_id
        return spec

    created_sessions: list[str] = []

    async def _fake_auto_create_repl(
        session_id: str,
        resource_registry: Any,
        publish_event: Any,
        *,
        server_client: Any,
        agent_spec: Any = None,
    ) -> SessionResourceView:
        """Record the dispatch instead of launching a real tmux pane."""
        del resource_registry, publish_event, server_client, agent_spec
        created_sessions.append(session_id)
        return SessionResourceView(
            id="terminal_tui_main",
            type="terminal",
            session_id=session_id,
            name="tui",
        )

    monkeypatch.setattr("omnigent.runner.app._auto_create_repl_terminal", _fake_auto_create_repl)

    async def _fake_codex_needs(server_client: Any, session_id: str) -> bool:
        """Neutralize the codex-native terminal branch (out of scope here)."""
        del server_client, session_id
        return False

    monkeypatch.setattr(
        "omnigent.runner.app._codex_session_needs_runner_terminal", _fake_codex_needs
    )

    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        # A real (empty) registry: the dispatch gate requires one, and
        # ``get()`` on it reports no existing REPL terminal.
        terminal_registry=TerminalRegistry(),
    )

    body: dict[str, Any] = {
        "session_id": "5eef02d60f39cba3fbd0ae188348643f",
        "agent_id": "26def1563ee359def46b274c20739c03",
    }
    if sub_agent_name is not None:
        body["sub_agent_name"] = sub_agent_name
    async with _runner_client(app) as client:
        resp = await client.post("/v1/sessions", json=body)

    assert resp.status_code == 201, resp.text
    # Dispatch fired exactly for the SDK top-level case. An unexpected
    # entry here means natives/sub-agents grew a REPL pane; a missing
    # one means SDK sessions lost the embedded web TUI.
    assert created_sessions == (["5eef02d60f39cba3fbd0ae188348643f"] if expect_created else [])
