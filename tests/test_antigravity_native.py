"""Tests for the native Antigravity (``omnigent antigravity``) launcher.

No live agy or server is started — the terminal-launch POST is driven through
an ``httpx.MockTransport`` so the request body shape is asserted without a real
runner.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

import omnigent.antigravity_native as _mod
import omnigent.antigravity_native_bridge as bridge_mod
from omnigent._wrapper_labels import ANTIGRAVITY_NATIVE_WRAPPER_VALUE, WRAPPER_LABEL_KEY
from omnigent.antigravity_native import antigravity_terminal_resource_id
from omnigent.antigravity_native_bridge import (
    ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY,
    AntigravityNativeBridgeState,
    read_bridge_state,
    read_tmux_info,
    write_bridge_state,
)


def _mock_client(handler: object) -> httpx.AsyncClient:
    """
    Build an async client whose requests are served by ``handler``.

    :param handler: ``httpx.MockTransport`` request handler.
    :returns: An ``httpx.AsyncClient`` bound to a base URL and the handler.
    """
    return httpx.AsyncClient(
        base_url="http://127.0.0.1:0",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
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


# ---------------------------------------------------------------------------
# _launch_and_record: forwarded_step_index cursor preservation (Fix 3)
# ---------------------------------------------------------------------------

_REAL_UUID = "68caaeac-2eaf-4e2c-9b95-721b022f4903"
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


async def test_launch_and_record_resume_same_conv_preserves_forwarded_step_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    Cold ``--resume`` of the SAME conversation preserves ``forwarded_step_index``.

    Regression for the HIGH bug: before the fix, ``_launch_and_record`` called
    ``clear_bridge_state`` unconditionally and then seeded state with
    ``forwarded_step_index=None``.  On a cold resume the forwarder then started
    at high-water -1 and re-POSTed the entire prior transcript.

    This test pre-seeds bridge state with a real UUID and cursor=14, calls
    ``_launch_and_record(resume=True, conversation_id=<that UUID>)``, and asserts
    the cursor is still 14 in the written state.

    :param monkeypatch: pytest monkeypatch fixture.
    :param tmp_path: Temporary directory, used to isolate the bridge root.
    :returns: None.
    """
    monkeypatch.setattr(bridge_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")

    bridge_id = "bridge_resume_test"
    from omnigent.antigravity_native_bridge import prepare_bridge_dir

    bridge_dir = prepare_bridge_dir(bridge_id)

    # Pre-seed: simulate a prior run that forwarded up to step 14.
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(
            session_id="conv_abc123",
            conversation_id=_REAL_UUID,
            forwarded_step_index=14,
        ),
    )

    async with _mock_client(_terminal_launch_handler) as client:
        await _mod._launch_and_record(
            client,
            session_id="conv_abc123",
            bridge_id=bridge_id,
            conversation_id=_REAL_UUID,
            resume=True,
            antigravity_args=(),
            command="agy",
            model=None,
            startup_progress=None,
        )

    after = read_bridge_state(bridge_dir)
    assert after is not None, "bridge state must exist after _launch_and_record"
    assert after.forwarded_step_index == 14, (
        "cursor must be preserved on same-conversation cold resume; "
        f"got {after.forwarded_step_index}"
    )


async def test_launch_and_record_fresh_launch_resets_forwarded_step_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    A fresh launch with a placeholder id resets ``forwarded_step_index`` to None.

    The placeholder id used on a fresh launch will never match the prior run's
    real UUID, so the cursor must be reset (not preserved) and the full new
    transcript will be mirrored from step 0.

    :param monkeypatch: pytest monkeypatch fixture.
    :param tmp_path: Temporary directory, used to isolate the bridge root.
    :returns: None.
    """
    monkeypatch.setattr(bridge_mod, "_BRIDGE_ROOT", tmp_path / "antigravity-native")

    bridge_id = "bridge_fresh_test"
    from omnigent.antigravity_native_bridge import prepare_bridge_dir

    bridge_dir = prepare_bridge_dir(bridge_id)

    # Pre-seed: prior run left cursor=14 with the real UUID.
    write_bridge_state(
        bridge_dir,
        AntigravityNativeBridgeState(
            session_id="conv_abc123",
            conversation_id=_REAL_UUID,
            forwarded_step_index=14,
        ),
    )

    # Fresh launch: pass a placeholder id (never matches the prior real UUID).
    async with _mock_client(_terminal_launch_handler) as client:
        await _mod._launch_and_record(
            client,
            session_id="conv_abc123",
            bridge_id=bridge_id,
            conversation_id=_PLACEHOLDER_ID,
            resume=False,
            antigravity_args=(),
            command="agy",
            model=None,
            startup_progress=None,
        )

    after = read_bridge_state(bridge_dir)
    assert after is not None, "bridge state must exist after _launch_and_record"
    assert after.forwarded_step_index is None, (
        f"cursor must be reset on fresh launch; got {after.forwarded_step_index}"
    )


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
