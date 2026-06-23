"""Tests for the native Antigravity (``omnigent antigravity``) launcher.

No live agy or server is started — the terminal-launch POST is driven through
an ``httpx.MockTransport`` so the request body shape is asserted without a real
runner.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

import omnigent.antigravity_native as _mod
import omnigent.antigravity_native_bridge as bridge_mod
from omnigent._wrapper_labels import ANTIGRAVITY_NATIVE_WRAPPER_VALUE, WRAPPER_LABEL_KEY
from omnigent.antigravity_native import antigravity_terminal_resource_id
from omnigent.antigravity_native_bridge import (
    ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY,
    read_bridge_state,
    read_tmux_info,
)


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    """
    Build an async client whose requests are served by ``handler``.

    :param handler: ``httpx.MockTransport`` request handler.
    :returns: An ``httpx.AsyncClient`` bound to a base URL and the handler.
    """
    return httpx.AsyncClient(
        base_url="http://127.0.0.1:0",
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture(autouse=True)
def _stub_agy_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Resolve the agy binary to a fixed name so launch tests need no real install.

    ``build_agy_launch`` uses ``agy_binary_path()`` as ``argv[0]`` unconditionally,
    and that raises ``RuntimeError`` when agy is absent from ``PATH`` — which is the
    case in CI. Patch the name at the site where ``build_agy_launch`` looks it up
    (its own module), plus the re-export in :mod:`omnigent.antigravity_native` used
    by the direct-CLI launch path, so no test depends on agy being installed.

    This is autouse for the whole module; the real resolution / missing-agy
    ``RuntimeError`` path is covered separately in
    ``tests/test_antigravity_native_launch.py`` (which patches ``shutil.which``
    directly), so nothing here needs the unstubbed binary lookup.
    """
    monkeypatch.setattr("omnigent.antigravity_native_launch.agy_binary_path", lambda: "agy")
    monkeypatch.setattr(_mod, "agy_binary_path", lambda: "agy")


async def test_launch_terminal_body_uses_ensure_native_terminal_not_bridge_inject() -> None:
    """
    The terminal-launch POST opts in via ``ensure_native_terminal``, not ``bridge_inject_dir``.

    ``bridge_inject_dir`` is the Claude-native marker: on the runner it starts a
    Claude comment relay, tags the terminal ``CLAUDE_NATIVE_TERMINAL_ROLE``, and
    publishes Claude tmux metadata — side effects antigravity does not own and
    must not trigger. The antigravity bootstrap must therefore use the
    side-effect-free allowlist marker ``ensure_native_terminal``. This guards
    against a regression that reintroduces the Claude relay on every agy launch.
    """
    seen: dict[str, object] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "terminal_antigravity_main", "metadata": {}})

    async with _mock_client(_handler) as client:
        await _mod._launch_antigravity_terminal(
            client,
            "conv_abc123",
            argv=["agy", "--model", "gemini-2.5-pro"],
            env={"FOO": "bar"},
            command="agy",
        )

    body = seen["body"]
    assert isinstance(body, dict)
    assert body.get("ensure_native_terminal") is True
    assert "bridge_inject_dir" not in body
    assert body.get("terminal") == "antigravity"
    assert body.get("session_key") == "main"


async def test_launch_terminal_passes_spec_args_without_binary() -> None:
    """
    The launch spec carries the agy args (sans the binary) and the command separately.

    Guards the argv split: ``argv[0]`` is the binary (sent as ``command``) and
    the rest are the terminal ``spec.args`` — a mix-up would double the binary
    or drop the first flag.
    """
    seen: dict[str, object] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "terminal_antigravity_main", "metadata": {}})

    async with _mock_client(_handler) as client:
        await _mod._launch_antigravity_terminal(
            client,
            "conv_abc123",
            argv=["agy", "--conversation", "abc"],
            env={},
            command="agy",
        )

    body = seen["body"]
    assert isinstance(body, dict)
    spec = body.get("spec")
    assert isinstance(spec, dict)
    assert spec.get("command") == "agy"
    assert spec.get("args") == ["--conversation", "abc"]


async def test_launch_terminal_raises_on_error_status() -> None:
    """
    A non-2xx terminal-launch response raises a ClickException.

    The launcher cannot proceed without a terminal, so a server error must
    surface as a user-facing failure rather than a malformed success.
    """
    import click

    async with _mock_client(lambda request: httpx.Response(500, text="boom")) as client:
        with pytest.raises(click.ClickException):
            await _mod._launch_antigravity_terminal(
                client,
                "conv_abc123",
                argv=["agy"],
                env={},
                command="agy",
            )


# ---------------------------------------------------------------------------
# daemon resume reattach (Fix 2)
# ---------------------------------------------------------------------------


def _antigravity_session_payload() -> dict[str, object]:
    """
    Build a minimal antigravity-native session GET payload.

    :returns: A session payload carrying the wrapper + bridge-id labels and a
        discovered ``external_session_id``.
    """
    return {
        "labels": {
            WRAPPER_LABEL_KEY: ANTIGRAVITY_NATIVE_WRAPPER_VALUE,
            ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY: "bridge_xyz",
        },
        "external_session_id": "68caaeac-2eaf-4e2c-9b95-721b022f4903",
    }


def _patch_prepare_client(monkeypatch: pytest.MonkeyPatch, client: httpx.AsyncClient) -> None:
    """
    Make ``_prepare_antigravity_terminal_via_daemon`` use ``client``.

    The prepare fn opens its own ``async with httpx.AsyncClient(...)``; this
    swaps the constructor for a proxy that yields the test's mock-transport
    client so the GETs are served by the test handler.

    :param monkeypatch: pytest monkeypatch fixture.
    :param client: Mock-transport client to serve the prepare fn's requests.
    :returns: None.
    """

    class _ProxyClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> httpx.AsyncClient:
            return client

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(_mod.httpx, "AsyncClient", _ProxyClient)


async def test_daemon_resume_reattaches_to_running_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    A daemon resume with a live agy terminal reattaches instead of relaunching.

    This is the Fix-2 invariant: the daemon resume path must check for a
    running runner-owned terminal *before* binding/launching, and on a hit
    return ``reattached=True`` without ever calling ``_launch_and_record``
    (whose unconditional ``clear_bridge_state`` would wipe the live forwarder's
    discovered ``conversation_id``) or closing a terminal another launcher owns.

    :param monkeypatch: pytest monkeypatch fixture.
    :param tmp_path: pytest temp dir, used to isolate the bridge root.
    :returns: None.
    """
    import omnigent.antigravity_native_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    terminal_id = antigravity_terminal_resource_id()

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith(f"/resources/terminals/{terminal_id}"):
            return httpx.Response(
                200,
                json={
                    "id": terminal_id,
                    "metadata": {
                        "running": True,
                        "tmux_socket": "/tmp/s.sock",
                        "tmux_target": "main",
                    },
                },
            )
        if request.method == "GET":
            return httpx.Response(200, json=_antigravity_session_payload())
        raise AssertionError(f"unexpected request: {request.method} {path}")

    async def _boom_launch(*args: object, **kwargs: object) -> object:
        raise AssertionError("daemon resume must not relaunch when a terminal is live")

    async def _boom_host_online(*args: object, **kwargs: object) -> None:
        raise AssertionError("daemon resume must reattach before waiting on the host")

    monkeypatch.setattr(_mod, "_launch_and_record", _boom_launch)
    monkeypatch.setattr(_mod, "wait_for_host_online", _boom_host_online)

    async with _mock_client(_handler) as client:
        _patch_prepare_client(monkeypatch, client)
        prepared = await _mod._prepare_antigravity_terminal_via_daemon(
            base_url="http://127.0.0.1:0",
            headers={"Authorization": "Bearer t"},
            session_id="conv_abc123",
            session_bundle=None,
            antigravity_args=(),
            command="agy",
            model=None,
            host_id="host_1",
            workspace="/tmp/ws",
            startup_progress=None,
        )

    assert prepared.reattached is True
    assert prepared.terminal_id == terminal_id
    assert prepared.tmux_target == "main"


async def test_daemon_resume_cold_falls_through_to_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    A daemon resume with no live terminal proceeds to bind + launch.

    The reattach check must not swallow a cold resume: when the terminal GET
    reports no running terminal (404), the path falls through to the normal
    host-online/bind/launch sequence and returns ``reattached=False``.

    :param monkeypatch: pytest monkeypatch fixture.
    :param tmp_path: pytest temp dir, used to isolate the bridge root.
    :returns: None.
    """
    import omnigent.antigravity_native_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    terminal_id = antigravity_terminal_resource_id()

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith(f"/resources/terminals/{terminal_id}"):
            return httpx.Response(404, json={"error": {"code": "not_found"}})
        if request.method == "GET":
            return httpx.Response(200, json=_antigravity_session_payload())
        raise AssertionError(f"unexpected request: {request.method} {path}")

    calls: dict[str, object] = {}

    async def _record_launch(client: object, **kwargs: object) -> _mod.LaunchedAntigravityTerminal:
        calls["launch_kwargs"] = kwargs
        return _mod.LaunchedAntigravityTerminal(
            terminal_id=terminal_id, tmux_socket=None, tmux_target=None
        )

    async def _noop(*args: object, **kwargs: object) -> object:
        return None

    monkeypatch.setattr(_mod, "_launch_and_record", _record_launch)
    monkeypatch.setattr(_mod, "wait_for_host_online", _noop)
    monkeypatch.setattr(_mod, "wait_for_runner_online", _noop)
    monkeypatch.setattr(_mod, "launch_or_reuse_daemon_runner", _noop)
    monkeypatch.setattr(_mod, "_bind_session_runner", _noop)
    # The runner produces no terminal in this scenario (handler always 404s), so
    # shorten the post-bind wait to keep the fallback-to-launch test fast.
    monkeypatch.setattr(_mod, "_RUNNER_TERMINAL_AUTOCREATE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(_mod, "_RUNNER_TERMINAL_POLL_INTERVAL_S", 0.01)

    async with _mock_client(_handler) as client:
        _patch_prepare_client(monkeypatch, client)
        prepared = await _mod._prepare_antigravity_terminal_via_daemon(
            base_url="http://127.0.0.1:0",
            headers={},
            session_id="conv_abc123",
            session_bundle=None,
            antigravity_args=(),
            command="agy",
            model=None,
            host_id="host_1",
            workspace="/tmp/ws",
            startup_progress=None,
        )

    assert prepared.reattached is False
    assert "launch_kwargs" in calls
    # On resume the launch must target agy's real (discovered) conversation id.
    launch_kwargs = calls["launch_kwargs"]
    assert isinstance(launch_kwargs, dict)
    assert launch_kwargs["resume"] is True
    assert launch_kwargs["conversation_id"] == "68caaeac-2eaf-4e2c-9b95-721b022f4903"


async def test_daemon_fresh_launch_reattaches_to_runner_autocreated_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    A fresh launch reattaches to the runner's auto-created terminal, not relaunch.

    Regression for the double-launch bug: binding the runner triggers the runner's
    auto-create of the antigravity terminal (``runner/app.py``
    ``_auto_create_antigravity_terminal``). The CLI must then reattach to that
    runner-owned terminal — NOT call ``_launch_and_record`` (whose
    ``clear_bridge_state`` wipes the bridge state the runner just wrote, breaking
    web-turn injection with "Antigravity native bridge state is missing", while its
    redundant terminal POST 500s). The pre-bind reattach check (resume path) cannot
    catch this because the runner only auto-creates AFTER the bind.
    """
    import omnigent.antigravity_native_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    terminal_id = antigravity_terminal_resource_id()

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        # The runner has auto-created the terminal by the time we poll (post-bind).
        if request.method == "GET" and path.endswith(f"/resources/terminals/{terminal_id}"):
            return httpx.Response(
                200,
                json={
                    "id": terminal_id,
                    "metadata": {
                        "running": True,
                        "tmux_socket": "/tmp/s.sock",
                        "tmux_target": "main",
                    },
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {path}")

    async def _create(*args: object, **kwargs: object) -> str:
        return "conv_fresh123"

    async def _boom_launch(*args: object, **kwargs: object) -> object:
        raise AssertionError(
            "fresh launch must reattach to the runner-owned terminal, not relaunch "
            "(double-launch clobbers the runner's bridge state)"
        )

    async def _noop(*args: object, **kwargs: object) -> object:
        return None

    monkeypatch.setattr(_mod, "_create_antigravity_session", _create)
    monkeypatch.setattr(_mod, "_launch_and_record", _boom_launch)
    monkeypatch.setattr(_mod, "wait_for_host_online", _noop)
    monkeypatch.setattr(_mod, "wait_for_runner_online", _noop)
    monkeypatch.setattr(_mod, "launch_or_reuse_daemon_runner", _noop)
    monkeypatch.setattr(_mod, "_bind_session_runner", _noop)

    async with _mock_client(_handler) as client:
        _patch_prepare_client(monkeypatch, client)
        prepared = await _mod._prepare_antigravity_terminal_via_daemon(
            base_url="http://127.0.0.1:0",
            headers={},
            session_id=None,
            session_bundle=b"bundle",
            antigravity_args=(),
            command="agy",
            model=None,
            host_id="host_1",
            workspace="/tmp/ws",
            startup_progress=None,
        )

    assert prepared.reattached is True, "fresh launch must reattach to the runner-owned terminal"
    assert prepared.terminal_id == terminal_id
    assert prepared.tmux_target == "main"


async def test_local_fresh_launch_reattaches_to_runner_autocreated_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    The LOCAL (non-daemon) prepare path also reattaches to the runner-owned terminal.

    Regression for the double-launch/double-forward gap on the default
    ``omnigent antigravity`` path: the local server spawns a CLI runner, and
    binding it triggers the runner's ``_auto_create_antigravity_terminal`` exactly
    as the daemon path does. ``_prepare_antigravity_terminal`` must reattach to that
    runner-owned terminal after the bind — NOT call ``_launch_and_record`` (whose
    ``clear_bridge_state`` wipes the runner's bridge state and whose redundant
    terminal POST 500s, and which on ``reattached=False`` also starts a second CLI
    RPC reader → double-mirror). The original fix (7df3ba4d/f4ce3ce8) only patched
    ``_prepare_antigravity_terminal_via_daemon``; this guards the local-server
    path.
    """
    import omnigent.antigravity_native_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    terminal_id = antigravity_terminal_resource_id()

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        # The runner has auto-created the terminal by the time we poll (post-bind).
        if request.method == "GET" and path.endswith(f"/resources/terminals/{terminal_id}"):
            return httpx.Response(
                200,
                json={
                    "id": terminal_id,
                    "metadata": {
                        "running": True,
                        "tmux_socket": "/tmp/s.sock",
                        "tmux_target": "main",
                    },
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {path}")

    async def _create(*args: object, **kwargs: object) -> str:
        return "conv_local_fresh"

    async def _boom_launch(*args: object, **kwargs: object) -> object:
        raise AssertionError(
            "local fresh launch must reattach to the runner-owned terminal, not relaunch "
            "(double-launch clobbers the runner's bridge state and double-mirrors)"
        )

    async def _noop(*args: object, **kwargs: object) -> object:
        return None

    monkeypatch.setattr(_mod, "_create_antigravity_session", _create)
    monkeypatch.setattr(_mod, "_launch_and_record", _boom_launch)
    monkeypatch.setattr(_mod, "_bind_session_runner", _noop)

    async with _mock_client(_handler) as client:
        _patch_prepare_client(monkeypatch, client)
        prepared = await _mod._prepare_antigravity_terminal(
            base_url="http://127.0.0.1:0",
            headers={},
            session_id=None,
            runner_id="runner_local_1",
            session_bundle=b"bundle",
            antigravity_args=(),
            command="agy",
            model=None,
            startup_progress=None,
        )

    assert prepared.reattached is True, (
        "local fresh launch must reattach to the runner-owned terminal"
    )
    assert prepared.terminal_id == terminal_id
    assert prepared.tmux_target == "main"


# ---------------------------------------------------------------------------
# _launch_and_record: terminal launch + bridge-state seeding
# ---------------------------------------------------------------------------

_PLACEHOLDER_ID = "agy_conv_fresh_placeholder"


def _terminal_launch_handler(request: httpx.Request) -> httpx.Response:
    """Minimal handler that accepts the terminal POST and returns a stub resource."""
    return httpx.Response(200, json={"id": "terminal_antigravity_main", "metadata": {}})


def _terminal_launch_handler_with_pane(request: httpx.Request) -> httpx.Response:
    """Terminal POST handler that exposes a local tmux pane in the response metadata."""
    return httpx.Response(
        200,
        json={
            "id": "terminal_antigravity_main",
            "metadata": {"tmux_socket": "/tmp/agy/tmux.sock", "tmux_target": "main"},
        },
    )


async def test_launch_and_record_advertises_tmux_target_when_pane_exposed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    When the runner exposes a local tmux pane, ``_launch_and_record`` advertises it.

    A CLI-launched session's web turns are typed into the agy TUI by the executor,
    which reads the pane from ``tmux.json`` — so ``_launch_and_record`` must write
    it whenever the launched terminal carries a socket + target. (No pane ⇒ no
    write; that path is covered by the other launch tests, whose handler returns
    empty metadata.)
    """
    monkeypatch.setattr(bridge_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    bridge_id = "bridge_tmux_advertise"
    async with _mock_client(_terminal_launch_handler_with_pane) as client:
        await _mod._launch_and_record(
            client,
            session_id="conv_tmux",
            bridge_id=bridge_id,
            conversation_id="agy_conv_placeholder",
            resume=False,
            antigravity_args=(),
            command="agy",
            model=None,
            startup_progress=None,
        )
    info = read_tmux_info(bridge_mod.bridge_dir_for_bridge_id(bridge_id))
    assert info == {"socket_path": "/tmp/agy/tmux.sock", "tmux_target": "main"}


# ---------------------------------------------------------------------------
# _launch_is_headless — attended vs unattended signal (phase 4 task 1)
# ---------------------------------------------------------------------------


class _FakeStream:
    """Minimal stdin/stdout stand-in with a controllable ``isatty``."""

    def __init__(self, *, tty: bool, raises: bool = False) -> None:
        self._tty = tty
        self._raises = raises

    def isatty(self) -> bool:
        if self._raises:
            raise ValueError("I/O operation on closed file")
        return self._tty


def test_launch_is_headless_false_when_both_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """A controlling TTY on stdin AND stdout means an interactive client attaches."""
    monkeypatch.setattr(sys, "stdin", _FakeStream(tty=True))
    monkeypatch.setattr(sys, "stdout", _FakeStream(tty=True))
    assert _mod._launch_is_headless() is False


def test_launch_is_headless_true_when_stdin_not_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-TTY stdin (pipe / CI / detached) is headless."""
    monkeypatch.setattr(sys, "stdin", _FakeStream(tty=False))
    monkeypatch.setattr(sys, "stdout", _FakeStream(tty=True))
    assert _mod._launch_is_headless() is True


def test_launch_is_headless_true_when_stdout_not_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-TTY stdout (piped output) is headless."""
    monkeypatch.setattr(sys, "stdin", _FakeStream(tty=True))
    monkeypatch.setattr(sys, "stdout", _FakeStream(tty=False))
    assert _mod._launch_is_headless() is True


def test_launch_is_headless_true_on_closed_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """A closed/detached stream raising ValueError is treated as headless (safe)."""
    monkeypatch.setattr(sys, "stdin", _FakeStream(tty=True, raises=True))
    monkeypatch.setattr(sys, "stdout", _FakeStream(tty=True))
    assert _mod._launch_is_headless() is True


async def test_launch_and_record_threads_headless_skip_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``headless=True`` adds ``--dangerously-skip-permissions`` to the POSTed argv.

    Confirms the phase-4 task-1 wiring is threaded all the way through
    ``_launch_and_record`` → ``build_agy_launch`` → the terminal-launch
    ``spec.args``, not just the unit-level ``should_skip_permissions``.
    """
    monkeypatch.setattr(bridge_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")

    captured_args: list[str] = []

    def _capture_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured_args.extend(body["spec"]["args"])
        return httpx.Response(200, json={"id": "terminal_antigravity_main", "metadata": {}})

    async with _mock_client(_capture_handler) as client:
        await _mod._launch_and_record(
            client,
            session_id="conv_abc123",
            bridge_id="bridge_headless_test",
            conversation_id=_PLACEHOLDER_ID,
            resume=False,
            antigravity_args=(),
            command="agy",
            model=None,
            permission_mode=None,
            headless=True,
            startup_progress=None,
        )

    assert "--dangerously-skip-permissions" in captured_args


# ---------------------------------------------------------------------------
# _attach_terminal teardown: the eager terminal-close decision
# ---------------------------------------------------------------------------


def _prepared(reattached: bool) -> _mod.PreparedAntigravityTerminal:
    """
    Build a PreparedAntigravityTerminal for the attach-teardown tests.

    A non-None tmux socket/target makes the direct-attach branch eligible (the
    tests stub the attach itself), isolating the ``finally`` close decision.

    :param reattached: Whether this invocation reused an existing terminal.
    :returns: A prepared terminal with placeholder ids and tmux metadata.
    """
    return _mod.PreparedAntigravityTerminal(
        session_id="conv_close",
        terminal_id="term_close",
        bridge_dir=Path("/tmp/agy-close-test"),
        tmux_socket=Path("/tmp/agy-close-test.sock"),
        tmux_target="main",
        reattached=reattached,
    )


async def _run_attach_and_capture_close(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reattached: bool,
    outcome: _mod._AttachOutcome,
) -> list[str]:
    """
    Drive ``_attach_terminal`` with stubbed attach + reader + cold-start + close,
    returning the terminal ids passed to ``_close_antigravity_terminal`` (empty =
    not closed).

    The reader is stubbed to run until cancelled (so the ``finally`` teardown
    exercises its cancel path); the cold-start is a no-op; the direct tmux attach
    is stubbed to return the chosen *outcome* without touching tmux.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param reattached: ``prepared.reattached`` for this run.
    :param outcome: The ``_AttachOutcome`` the (stubbed) attach returns.
    :returns: Terminal ids ``_close_antigravity_terminal`` was called with.
    """
    closed_terminal_ids: list[str] = []

    async def _fake_reader(**_kwargs: object) -> None:
        # Run until the finally-block cancels the task, like the real reader.
        await asyncio.Event().wait()

    async def _fake_cold_start(*_args: object, **_kwargs: object) -> None:
        return None

    async def _fake_direct_attach(_socket: Path, _target: str) -> _mod._AttachOutcome:
        return outcome

    async def _fake_close(**kwargs: object) -> None:
        closed_terminal_ids.append(str(kwargs["terminal_id"]))

    monkeypatch.setattr(_mod, "run_reader_with_bridge", _fake_reader)
    monkeypatch.setattr(_mod, "_cold_start_agy_conversation", _fake_cold_start)
    monkeypatch.setattr(_mod, "_can_attach_direct_tmux", lambda _prepared: True)
    monkeypatch.setattr(_mod, "_attach_direct_tmux", _fake_direct_attach)
    monkeypatch.setattr(_mod, "_close_antigravity_terminal", _fake_close)

    await _mod._attach_terminal(
        base_url="http://test",
        headers={},
        prepared=_prepared(reattached),
        recover=None,
    )
    return closed_terminal_ids


async def test_attach_terminal_closes_on_real_exit_when_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real exit (EXITED) of a terminal THIS invocation owns closes it.

    The owning launcher is responsible for teardown; not closing here would leak
    the agy terminal resource on a normal quit.
    """
    closed = await _run_attach_and_capture_close(
        monkeypatch, reattached=False, outcome=_mod._AttachOutcome.EXITED
    )
    assert closed == ["term_close"]


async def test_attach_terminal_skips_close_on_detach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tmux DETACH must NOT close the terminal — agy keeps running for re-attach.

    Detaching is "leave it running"; closing here would kill a live agy session
    the user intends to come back to.
    """
    closed = await _run_attach_and_capture_close(
        monkeypatch, reattached=False, outcome=_mod._AttachOutcome.DETACHED
    )
    assert closed == []


async def test_attach_terminal_skips_close_when_reattached_even_on_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reattached invocation must NOT close on exit — it does not own teardown.

    This invocation reused a terminal another launcher created; closing it on our
    exit would tear down a resource we do not own (the over-close seam).
    """
    closed = await _run_attach_and_capture_close(
        monkeypatch, reattached=True, outcome=_mod._AttachOutcome.EXITED
    )
    assert closed == []


async def test_attach_terminal_skips_close_when_reattached_and_detached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reattached AND detached: both reasons to skip close — and it is skipped."""
    closed = await _run_attach_and_capture_close(
        monkeypatch, reattached=True, outcome=_mod._AttachOutcome.DETACHED
    )
    assert closed == []


async def _run_attach_and_capture_reader(
    monkeypatch: pytest.MonkeyPatch, *, reattached: bool
) -> tuple[int, int]:
    """
    Drive ``_attach_terminal`` and count the CLI reader + cold-start spawns.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param reattached: ``prepared.reattached`` for this run.
    :returns: ``(reader_starts, cold_start_starts)`` — each 0 or 1.
    """
    reader_starts = 0
    cold_start_starts = 0

    def _counting_reader(**_kwargs: object) -> object:
        # Count the CALL synchronously (deterministic) — not the task body, which
        # may be cancelled by the finally before it runs. Returns a coroutine so
        # ``asyncio.create_task(run_reader_with_bridge(...))`` still works.
        nonlocal reader_starts
        reader_starts += 1

        async def _runner() -> None:
            await asyncio.Event().wait()

        return _runner()

    def _counting_cold_start(*_args: object, **_kwargs: object) -> object:
        nonlocal cold_start_starts
        cold_start_starts += 1

        async def _runner() -> None:
            return None

        return _runner()

    async def _fake_direct_attach(_socket: Path, _target: str) -> _mod._AttachOutcome:
        return _mod._AttachOutcome.EXITED

    async def _fake_close(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(_mod, "run_reader_with_bridge", _counting_reader)
    monkeypatch.setattr(_mod, "_cold_start_agy_conversation", _counting_cold_start)
    monkeypatch.setattr(_mod, "_can_attach_direct_tmux", lambda _prepared: True)
    monkeypatch.setattr(_mod, "_attach_direct_tmux", _fake_direct_attach)
    monkeypatch.setattr(_mod, "_close_antigravity_terminal", _fake_close)

    await _mod._attach_terminal(
        base_url="http://test",
        headers={},
        prepared=_prepared(reattached),
        recover=None,
    )
    return reader_starts, cold_start_starts


async def test_attach_terminal_skips_cli_reader_when_reattached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reattaching to a runner-owned terminal must NOT start a CLI-side reader.

    The runner auto-creates "terminal + reader" together and owns the mirror for a
    runner-owned terminal. A second CLI-side reader would double-mirror every step
    (two readers POSTing the same agy conversation). So when ``reattached`` the CLI
    defers to the runner — and skips cold-start too (the runner cold-starts).
    """
    reader_starts, cold_start_starts = await _run_attach_and_capture_reader(
        monkeypatch, reattached=True
    )
    assert reader_starts == 0, "reattached must defer mirroring to the runner (no double-mirror)"
    assert cold_start_starts == 0, "reattached must not cold-start (runner owns it)"


async def test_attach_terminal_runs_cli_reader_when_not_reattached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the CLI launched its own terminal (fallback), it DOES run the reader.

    No runner-owned terminal exists in that case, so the CLI is the only mirror
    source: it spawns the RPC reader AND a one-shot cold-start (which mints agy's
    real cascade id so the reader binds it).
    """
    reader_starts, cold_start_starts = await _run_attach_and_capture_reader(
        monkeypatch, reattached=False
    )
    assert reader_starts == 1, "non-reattached (CLI-launched) path must run the RPC reader"
    assert cold_start_starts == 1, "non-reattached path must cold-start agy's conversation"


# ---------------------------------------------------------------------------
# _cold_start_agy_conversation — mint + persist + external_session_id PATCH
# ---------------------------------------------------------------------------


def _seed_bridge_state(bridge_dir: Path, conversation_id: str) -> None:
    """Seed bridge state with a given conversation id for cold-start tests."""
    from omnigent.antigravity_native_bridge import (
        AntigravityNativeBridgeState,
        write_bridge_state,
    )

    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(session_id="conv_cs", conversation_id=conversation_id),
    )


async def test_cli_cold_start_mints_and_patches_external_session_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    A placeholder-seeded CLI cold-start mints a real id, writes it, and PATCHes it.

    The fresh-fallback path: bridge state holds the ``agy_conv_*`` placeholder, so
    the cold-start ``StartCascade``s a real uuid4, persists it to bridge state
    (replacing the placeholder), and PATCHes it onto the session as
    ``external_session_id`` so a later ``--resume`` continues agy's conversation.
    """
    monkeypatch.setattr(bridge_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    bridge_dir = bridge_mod.bridge_dir_for_bridge_id("bridge_cs")
    _seed_bridge_state(bridge_dir, "agy_conv_placeholder")

    monkeypatch.setattr(_mod, "resolve_cold_start_agy_rpc_port", lambda _sock, _tgt: 52548)
    started: list[tuple[int, str]] = []
    monkeypatch.setattr(_mod, "start_cascade", lambda port, cid: started.append((port, cid)))

    patches: list[tuple[str, dict[str, object]]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        patches.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={})

    real_async_client = httpx.AsyncClient

    def _mock_async_client(**kwargs: object) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return real_async_client(transport=httpx.MockTransport(_handler), **kwargs)

    monkeypatch.setattr(_mod.httpx, "AsyncClient", _mock_async_client)

    await _mod._cold_start_agy_conversation(
        bridge_dir,
        "conv_cs",
        base_url="http://test",
        headers={},
        timeout_s=1.0,
    )

    # Minted exactly one cascade on the discovered port with a real (non-placeholder) id.
    assert len(started) == 1
    minted_port, minted_id = started[0]
    assert minted_port == 52548
    assert not _mod.is_placeholder_conversation_id(minted_id)
    # The real id reached bridge state (placeholder replaced).
    after = read_bridge_state(bridge_dir)
    assert after is not None
    assert after.conversation_id == minted_id
    # The same id was PATCHed onto the session as external_session_id.
    assert patches == [("/v1/sessions/conv_cs", {"external_session_id": minted_id})]


async def test_cli_cold_start_skips_when_conversation_already_real(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    A resume (non-placeholder id already seeded) skips cold-start AND the PATCH.

    This is the guard that makes ``--resume`` work: the real prior id is already
    seeded (and passed as ``--conversation``), so the cold-start must not mint a
    fresh conversation (which would clobber the resumed id) and must not PATCH.
    """
    monkeypatch.setattr(bridge_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    bridge_dir = bridge_mod.bridge_dir_for_bridge_id("bridge_cs_resume")
    real_id = "68caaeac-2eaf-4e2c-9b95-721b022f4903"
    _seed_bridge_state(bridge_dir, real_id)

    def _fail_ports(_sock: object, _tgt: object) -> int | None:
        raise AssertionError("cold-start must not probe ports on a non-placeholder (resume) id")

    monkeypatch.setattr(_mod, "resolve_cold_start_agy_rpc_port", _fail_ports)

    def _fail_client(**_kwargs: object) -> httpx.AsyncClient:
        raise AssertionError("cold-start must not PATCH on a resume id")

    monkeypatch.setattr(_mod.httpx, "AsyncClient", _fail_client)

    await _mod._cold_start_agy_conversation(
        bridge_dir,
        "conv_cs",
        base_url="http://test",
        headers={},
        timeout_s=1.0,
    )

    # Bridge state untouched — the resume id stands.
    after = read_bridge_state(bridge_dir)
    assert after is not None
    assert after.conversation_id == real_id


async def test_cli_cold_start_logs_when_patch_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """
    A ``>= 400`` PATCH response is logged (best-effort) but never raised.

    ``httpx`` does not raise on a 4xx/5xx response unless asked, so the cold-start
    inspects ``status_code`` and warns when the server rejects the
    ``external_session_id`` write — but the cascade id still reaches bridge state
    (the chat mirror does not depend on the resume-fidelity PATCH).
    """
    monkeypatch.setattr(bridge_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    bridge_dir = bridge_mod.bridge_dir_for_bridge_id("bridge_cs_reject")
    _seed_bridge_state(bridge_dir, "agy_conv_placeholder")

    monkeypatch.setattr(_mod, "resolve_cold_start_agy_rpc_port", lambda _sock, _tgt: 52548)
    started: list[tuple[int, str]] = []
    monkeypatch.setattr(_mod, "start_cascade", lambda port, cid: started.append((port, cid)))

    real_async_client = httpx.AsyncClient

    def _reject_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    def _mock_async_client(**kwargs: object) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return real_async_client(transport=httpx.MockTransport(_reject_handler), **kwargs)

    monkeypatch.setattr(_mod.httpx, "AsyncClient", _mock_async_client)

    with caplog.at_level(logging.WARNING, logger="omnigent.antigravity_native"):
        await _mod._cold_start_agy_conversation(
            bridge_dir,
            "conv_cs",
            base_url="http://test",
            headers={},
            timeout_s=1.0,
        )

    # The cascade was still minted + persisted; only the resume-fidelity PATCH failed.
    assert len(started) == 1
    after = read_bridge_state(bridge_dir)
    assert after is not None
    assert after.conversation_id == started[0][1]
    assert "rejected external_session_id PATCH (404)" in caplog.text


async def test_cli_cold_start_scopes_to_pane_agy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    With several agy candidates, the CLI cold-start binds THIS session's pane agy.

    Mirrors the runner cross-bind fix on the CLI fallback: when a pane is
    threaded in and the pane-scoped resolver yields a port, ``StartCascade``
    targets THAT port (61000) even though a lower foreign candidate (52548)
    exists. Exercises the REAL ``resolve_cold_start_agy_rpc_port`` dispatch by
    stubbing its rpc-module seams.
    """
    import omnigent.antigravity_native_rpc as rpc

    monkeypatch.setattr(bridge_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    bridge_dir = bridge_mod.bridge_dir_for_bridge_id("bridge_cs_pane")
    _seed_bridge_state(bridge_dir, "agy_conv_placeholder")

    # The pane scopes to a higher agy's port; a lower FOREIGN candidate exists.
    monkeypatch.setattr(rpc, "resolve_pane_agy_rpc_port", lambda _sock, _tgt: 61000)
    monkeypatch.setattr(rpc, "_candidate_agy_rpc_ports", lambda: [52548, 61000])
    started: list[tuple[int, str]] = []
    monkeypatch.setattr(_mod, "start_cascade", lambda port, cid: started.append((port, cid)))

    def _ok(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    real_async_client = httpx.AsyncClient

    def _mock_async_client(**kwargs: object) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return real_async_client(transport=httpx.MockTransport(_ok), **kwargs)

    monkeypatch.setattr(_mod.httpx, "AsyncClient", _mock_async_client)

    await _mod._cold_start_agy_conversation(
        bridge_dir,
        "conv_cs",
        base_url="http://test",
        headers={},
        tmux_socket=tmp_path / "agy.sock",
        tmux_target="main",
        timeout_s=1.0,
    )

    # StartCascade fired on the PANE-SCOPED port, NOT candidates[0] (52548).
    assert len(started) == 1
    assert started[0][0] == 61000


async def test_cli_cold_start_falls_back_when_no_pane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    No local pane (``tmux_socket``/``tmux_target`` is ``None``) → lowest candidate.

    Preserves the current CLI behavior for the WebSocket-attach / remote-runner
    path that has no local tmux socket: the cold-start falls back to
    ``_candidate_agy_rpc_ports()[0]`` and never consults the pane-scoped resolver.
    """
    import omnigent.antigravity_native_rpc as rpc

    monkeypatch.setattr(bridge_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")
    bridge_dir = bridge_mod.bridge_dir_for_bridge_id("bridge_cs_nopane")
    _seed_bridge_state(bridge_dir, "agy_conv_placeholder")

    def _no_pane_scope(_sock: object, _tgt: object) -> int | None:
        raise AssertionError("no pane → the pane-scoped resolver must not be consulted")

    monkeypatch.setattr(rpc, "resolve_pane_agy_rpc_port", _no_pane_scope)
    monkeypatch.setattr(rpc, "_candidate_agy_rpc_ports", lambda: [52548, 61000])
    started: list[tuple[int, str]] = []
    monkeypatch.setattr(_mod, "start_cascade", lambda port, cid: started.append((port, cid)))

    def _ok(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    real_async_client = httpx.AsyncClient

    def _mock_async_client(**kwargs: object) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return real_async_client(transport=httpx.MockTransport(_ok), **kwargs)

    monkeypatch.setattr(_mod.httpx, "AsyncClient", _mock_async_client)

    await _mod._cold_start_agy_conversation(
        bridge_dir,
        "conv_cs",
        base_url="http://test",
        headers={},
        tmux_socket=None,
        tmux_target=None,
        timeout_s=1.0,
    )

    assert len(started) == 1
    assert started[0][0] == 52548  # lowest candidate, pane resolver never called
