"""Tests for the daemon-routed remote-claude launch helpers.

Covers the client side of HOST_BY_DEFAULT: acquiring a daemon-spawned
runner (reuse-online / launch / clear-stale) and the
``_prepare_claude_terminal_via_daemon`` orchestration that persists the
user's pass-through args on the session so the runner applies them. See
designs/NATIVE_RUNNER_SERVER_LAUNCH.md.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import click
import httpx
import pytest

from omnigent import claude_native, codex_native, native_terminal
from omnigent.claude_native_bridge import BRIDGE_ID_LABEL_KEY
from omnigent.host import daemon_launch

pytestmark = pytest.mark.asyncio


async def test_native_wrappers_share_daemon_terminal_helpers() -> None:
    """
    Claude and Codex wrappers use the same daemon terminal helpers.

    Codex used to import moved private helpers from ``claude_native``,
    which made module import fail during default server startup. These
    helpers are now owned by ``native_terminal`` and re-exported under
    existing wrapper-local names for focused monkeypatch tests.
    """
    assert claude_native._attach_url is native_terminal.terminal_attach_url
    assert codex_native._attach_url is native_terminal.terminal_attach_url
    assert claude_native._bind_session_runner is native_terminal.bind_session_runner
    assert codex_native._bind_session_runner is native_terminal.bind_session_runner
    assert (
        claude_native._DAEMON_HOST_ONLINE_TIMEOUT_S
        == codex_native._DAEMON_HOST_ONLINE_TIMEOUT_S
        == native_terminal.DAEMON_HOST_ONLINE_TIMEOUT_S
    )


async def test_launch_or_reuse_daemon_runner_reuses_online_runner() -> None:
    """
    An already-bound, still-online runner is reused — no new launch.

    Resuming into a live session must not spawn a second runner. If the
    helper POSTed to ``/runners`` here it would 400 ("already bound") or
    spawn a duplicate; reuse keeps the existing one.
    """
    posted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Route the reuse-path requests."""
        path = request.url.path
        if request.method == "GET" and path == "/v1/sessions/conv_a":
            return httpx.Response(200, json={"runner_id": "runner_live"})
        if request.method == "GET" and path == "/v1/runners/runner_live/status":
            return httpx.Response(200, json={"runner_id": "runner_live", "online": True})
        if request.method == "POST" and path.endswith("/runners"):
            posted.append(path)
            return httpx.Response(200, json={"runner_id": "unexpected"})
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://e.com"
    ) as client:
        runner_id = await daemon_launch.launch_or_reuse_daemon_runner(
            client, host_id="host_1", session_id="conv_a", workspace="/w"
        )

    assert runner_id == "runner_live"
    # No launch POST fired — proves the online runner was reused, not respawned.
    assert posted == []


async def test_launch_or_reuse_daemon_runner_launches_when_unbound() -> None:
    """
    A session with no runner triggers a launch on the host endpoint.

    The launch request must carry the session id + workspace so the
    daemon spawns the runner in the right place and binds it.
    """
    launched: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        """Route the launch-path requests."""
        path = request.url.path
        if request.method == "GET" and path == "/v1/sessions/conv_a":
            return httpx.Response(200, json={})
        if request.method == "POST" and path == "/v1/hosts/host_1/runners":
            launched["body"] = json.loads(request.content)
            return httpx.Response(200, json={"runner_id": "runner_new", "status": "launching"})
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://e.com"
    ) as client:
        runner_id = await daemon_launch.launch_or_reuse_daemon_runner(
            client, host_id="host_1", session_id="conv_a", workspace="/work"
        )

    assert runner_id == "runner_new"
    assert launched["body"] == {"session_id": "conv_a", "workspace": "/work"}


async def test_launch_or_reuse_daemon_runner_clears_stale_binding() -> None:
    """
    A bound-but-offline runner is cleared before launching a fresh one.

    The launch endpoint binds atomically only when ``runner_id IS
    NULL``; without clearing the dead binding first it would 400. The
    clear must therefore precede the launch.
    """
    events: list[tuple[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Route the stale-binding-path requests, recording order."""
        path = request.url.path
        if request.method == "GET" and path == "/v1/sessions/conv_a":
            return httpx.Response(200, json={"runner_id": "runner_dead"})
        if request.method == "GET" and path == "/v1/runners/runner_dead/status":
            return httpx.Response(200, json={"runner_id": "runner_dead", "online": False})
        if request.method == "PATCH" and path == "/v1/sessions/conv_a":
            events.append(("patch", json.loads(request.content)))
            return httpx.Response(200, json={})
        if request.method == "POST" and path == "/v1/hosts/host_1/runners":
            events.append(("launch", None))
            return httpx.Response(200, json={"runner_id": "runner_fresh"})
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://e.com"
    ) as client:
        runner_id = await daemon_launch.launch_or_reuse_daemon_runner(
            client, host_id="host_1", session_id="conv_a", workspace="/w"
        )

    assert runner_id == "runner_fresh"
    # The stale binding was cleared (runner_id="") strictly before the
    # launch — the ordering the atomic NULL-bind requires.
    assert ("patch", {"runner_id": ""}) in events
    assert events.index(("patch", {"runner_id": ""})) < events.index(("launch", None))


async def test_create_claude_session_persists_terminal_launch_args() -> None:
    """
    The daemon-flow create persists pass-through args and omits the
    bridge-id label.

    ``omnigent claude --server X <flags>`` must carry the flags to a
    daemon-spawned runner: they're written to the session's
    ``terminal_launch_args`` at create so the runner applies them when it
    auto-launches the terminal. The bridge-id label is omitted
    (``bridge_id=None``) so the bridge dir keys by session id — the
    convention the runner's auto-create uses. A missing
    ``terminal_launch_args`` would silently drop the user's flags; a
    present bridge-id label would split the bridge dir between the
    harness and the terminal.
    """
    captured: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        """Capture the multipart create body."""
        if request.method == "POST" and request.url.path == "/v1/sessions":
            captured["body"] = request.content
            return httpx.Response(201, json={"session_id": "conv_new"})
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://e.com"
    ) as client:
        session_id = await claude_native._create_claude_session(
            client,
            b"bundle",
            bridge_id=None,
            terminal_launch_args=["--dangerously-skip-permissions"],
        )

    assert session_id == "conv_new"
    body = captured["body"]
    assert b'"terminal_launch_args"' in body
    assert b"--dangerously-skip-permissions" in body
    # bridge_id=None → the bridge-id label must NOT be written.
    assert BRIDGE_ID_LABEL_KEY.encode() not in body


def _install_daemon_seam_mocks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    prepared: claude_native.PreparedClaudeTerminal,
    attach_outcome: claude_native._AttachOutcome,
    captured: dict[str, Any],
    ensured: list[str],
) -> None:
    """
    Patch the high-level seams of ``_run_with_remote_server``.

    Stubs the daemon-ensure, host identity, session-resolve, cwd-align,
    bundle, launch-state, the per-daemon prepare (capturing its kwargs),
    and the attach (returning *attach_outcome*) so the orchestration can
    run in-process without a server, daemon, or runner.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param prepared: Prepared terminal the fake prepare returns.
    :param attach_outcome: Outcome the fake attach returns.
    :param captured: Dict the fake prepare populates with its kwargs.
    :param ensured: List the daemon-ensure stub appends the server url
        to.
    :returns: None.
    """
    monkeypatch.setattr("omnigent.chat._remote_headers", lambda server_url=None, **k: {})
    monkeypatch.setattr("omnigent.chat._server_auth", lambda server_url=None, **k: None)
    monkeypatch.setattr("omnigent.chat._bundle_agent", lambda path: b"bundle")
    monkeypatch.setattr(
        "omnigent.cli._ensure_host_daemon",
        lambda url: ensured.append(url),
    )
    monkeypatch.setattr(
        "omnigent.host.identity.load_or_create_host_identity",
        lambda *a, **k: SimpleNamespace(host_id="host_1", name="h"),
    )
    monkeypatch.setattr(
        claude_native,
        "_resolve_session_id_for_resume",
        lambda **k: k.get("session_id"),
    )
    monkeypatch.setattr(
        claude_native, "_align_working_directory_with_session", lambda *a, **k: None
    )
    monkeypatch.setattr(claude_native, "_record_launch_for_fresh_session", lambda sid: None)

    async def _fake_prepare(**kwargs: Any) -> claude_native.PreparedClaudeTerminal:
        """Capture prepare kwargs and return the canned terminal."""
        captured.update(kwargs)
        return prepared

    async def _fake_attach(**kwargs: Any) -> claude_native._AttachOutcome:
        """Return the canned attach outcome."""
        del kwargs
        return attach_outcome

    monkeypatch.setattr(claude_native, "_prepare_claude_terminal_via_daemon", _fake_prepare)
    monkeypatch.setattr(claude_native, "_attach_with_transcript_forwarder", _fake_attach)


def test_run_with_remote_server_routes_through_daemon(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A fresh remote launch ensures the daemon and hands the launch to it.

    Proves the CLI no longer spawns the runner itself: it calls
    ``_ensure_host_daemon`` for the server, then routes the launch
    through ``_prepare_claude_terminal_via_daemon`` with this host's id,
    the cwd as workspace, and the user's ``claude_args`` (so the runner
    can apply them).
    """
    spec_path = tmp_path / "claude.yaml"
    spec_path.write_text("name: claude-native-ui\nprompt: hi\n")
    monkeypatch.chdir(tmp_path)
    captured: dict[str, Any] = {}
    ensured: list[str] = []
    prepared = claude_native.PreparedClaudeTerminal(
        session_id="conv_new",
        terminal_id=claude_native.claude_terminal_resource_id(),
        bridge_dir=tmp_path / "bridge",
        reattached=False,
    )
    _install_daemon_seam_mocks(
        monkeypatch,
        prepared=prepared,
        attach_outcome=claude_native._AttachOutcome.EXITED,
        captured=captured,
        ensured=ensured,
    )

    claude_native._run_with_remote_server(
        "https://example.com",
        spec_path,
        session_id=None,
        resume_picker=False,
        claude_args=("--dangerously-skip-permissions",),
    )

    # Daemon ensured for exactly this server URL.
    assert ensured == ["https://example.com"]
    # The launch was routed through the daemon prepare with this host,
    # the cwd workspace, and the user's args.
    assert captured["host_id"] == "host_1"
    assert captured["workspace"] == str(tmp_path.resolve())
    assert captured["claude_args"] == ("--dangerously-skip-permissions",)
    assert captured["session_id"] is None


def test_run_with_remote_server_detach_prints_resume_hint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    A tmux detach on a resumed session prints the copyable resume command.

    The daemon owns the runner, so a detach leaves it running for the web
    UI; the user gets a ``--resume`` command to reattach. This pins the
    detach-hint UX preserved from the pre-daemon flow.
    """
    spec_path = tmp_path / "claude.yaml"
    spec_path.write_text("name: claude-native-ui\nprompt: hi\n")
    captured: dict[str, Any] = {}
    ensured: list[str] = []
    prepared = claude_native.PreparedClaudeTerminal(
        session_id="conv_existing",
        terminal_id=claude_native.claude_terminal_resource_id(),
        bridge_dir=tmp_path / "bridge",
        reattached=True,
    )
    _install_daemon_seam_mocks(
        monkeypatch,
        prepared=prepared,
        attach_outcome=claude_native._AttachOutcome.DETACHED,
        captured=captured,
        ensured=ensured,
    )

    claude_native._run_with_remote_server(
        "https://example.com",
        spec_path,
        session_id="conv_existing",
        resume_picker=False,
        claude_args=(),
    )

    err = capsys.readouterr().err
    assert "Detached. Agent still running at https://example.com/c/conv_existing" in err
    # Exact resume command: server + session only — a --profile part
    # here would tell the user to run a flag that no longer exists.
    assert (
        "Resume with: omnigent claude --server https://example.com --resume conv_existing"
    ) in err


def test_run_with_remote_server_unreachable_server_raises_clean_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    An unreachable Omnigent server fails with an actionable message, not a
    raw httpx traceback.

    The daemon flow's first server contact is the session create; if the
    server isn't reachable that surfaces as ``httpx.ConnectError``. The
    wrapper must turn it into a ``ClickException`` naming the URL so the
    user knows what to check, rather than dumping a connection-pool
    stack trace.
    """
    spec_path = tmp_path / "claude.yaml"
    spec_path.write_text("name: claude-native-ui\nprompt: hi\n")
    monkeypatch.chdir(tmp_path)
    captured: dict[str, Any] = {}
    ensured: list[str] = []
    prepared = claude_native.PreparedClaudeTerminal(
        session_id="conv_new",
        terminal_id=claude_native.claude_terminal_resource_id(),
        bridge_dir=tmp_path / "bridge",
        reattached=False,
    )
    _install_daemon_seam_mocks(
        monkeypatch,
        prepared=prepared,
        attach_outcome=claude_native._AttachOutcome.EXITED,
        captured=captured,
        ensured=ensured,
    )

    async def _boom(**kwargs: Any) -> claude_native.PreparedClaudeTerminal:
        """Simulate the server being unreachable at create."""
        del kwargs
        raise httpx.ConnectError("All connection attempts failed")

    monkeypatch.setattr(claude_native, "_prepare_claude_terminal_via_daemon", _boom)

    with pytest.raises(click.ClickException) as exc_info:
        claude_native._run_with_remote_server(
            "https://unreachable.example",
            spec_path,
            session_id=None,
            resume_picker=False,
            claude_args=(),
        )

    assert "Could not reach the omnigent server at https://unreachable.example" in str(
        exc_info.value
    )
