"""Unit tests for the omni qwen CLI-side helpers (no server needed).

Mirrors ``tests/test_goose_native.py`` and adds qwen-specific pure helpers
(agent-spec materialization, terminal-payload decoding, direct-tmux preflight).
"""

from __future__ import annotations

from pathlib import Path

import click
import httpx
import pytest
import yaml

from omnigent import qwen_native as qn


def test_resolve_qwen_executable_found() -> None:
    resolved = qn.resolve_qwen_executable(
        env={}, which=lambda cmd: f"/usr/local/bin/{cmd}" if cmd == "qwen" else None
    )
    assert resolved == "/usr/local/bin/qwen"


def test_resolve_qwen_executable_honors_path_override() -> None:
    resolved = qn.resolve_qwen_executable(
        env={"OMNIGENT_QWEN_PATH": "/opt/qwen"},
        which=lambda cmd: cmd if cmd == "/opt/qwen" else None,
    )
    assert resolved == "/opt/qwen"


def test_resolve_qwen_executable_missing_raises_with_hint() -> None:
    with pytest.raises(click.ClickException) as exc:
        qn.resolve_qwen_executable(env={}, which=lambda _cmd: None)
    msg = str(exc.value)
    assert "@qwen-code/qwen-code" in msg
    assert "OMNIGENT_QWEN_PATH" in msg


def test_build_qwen_launch_argv() -> None:
    launch = qn.build_qwen_launch(
        ["-m", "qwen3-coder-plus"],
        env={},
        which=lambda cmd: f"/bin/{cmd}",
    )
    assert launch.executable == "/bin/qwen"
    assert launch.argv == ["/bin/qwen", "-m", "qwen3-coder-plus"]


def test_terminal_resource_id_stable() -> None:
    assert qn.qwen_terminal_resource_id() == qn.qwen_terminal_resource_id()


def test_materialize_agent_spec_declares_qwen_native_harness(tmp_path: Path) -> None:
    spec_path = qn._materialize_qwen_agent_spec(tmp_path)
    raw = yaml.safe_load(spec_path.read_text())
    assert raw["name"] == "qwen-native-ui"
    assert raw["executor"] == {"harness": "qwen-native"}


def test_launched_terminal_from_payload_decodes_tmux(tmp_path: Path) -> None:
    sock = tmp_path / "tmux.sock"
    payload = {
        "id": "terminal_qwen_main",
        "metadata": {"tmux_socket": str(sock), "tmux_target": "sess:0.0"},
    }
    launched = qn._launched_qwen_terminal_from_payload(payload)
    assert launched.terminal_id == "terminal_qwen_main"
    assert launched.tmux_socket == sock
    assert launched.tmux_target == "sess:0.0"


def test_launched_terminal_from_payload_requires_id() -> None:
    with pytest.raises(click.ClickException):
        qn._launched_qwen_terminal_from_payload({"metadata": {}})
    with pytest.raises(click.ClickException):
        qn._launched_qwen_terminal_from_payload("not-a-dict")


def test_direct_tmux_unavailable_reason_reports_missing_pieces(tmp_path: Path) -> None:
    no_socket = qn.PreparedQwenTerminal(
        session_id="c", terminal_id="t", tmux_socket=None, tmux_target="x", reattached=False
    )
    assert "socket" in (qn._direct_tmux_unavailable_reason(no_socket) or "")

    missing_socket = qn.PreparedQwenTerminal(
        session_id="c",
        terminal_id="t",
        tmux_socket=tmp_path / "absent.sock",
        tmux_target="x",
        reattached=False,
    )
    assert "not reachable" in (qn._direct_tmux_unavailable_reason(missing_socket) or "")


# --- async server-interaction helpers (fake httpx client) --------------------


def _resp(status: int, body: object | None = None, *, method: str = "GET") -> httpx.Response:
    kwargs: dict = {"request": httpx.Request(method, "http://test/x")}
    if body is not None:
        kwargs["json"] = body
    return httpx.Response(status, **kwargs)


class _FakeClient:
    """Async httpx-client stub with per-method canned responses."""

    def __init__(self) -> None:
        self.get_responses: list[httpx.Response] = []
        self.post_response: httpx.Response | None = None
        self.calls: list[tuple[str, str, dict]] = []

    async def get(self, url: str, **kw: object) -> httpx.Response:
        self.calls.append(("GET", url, kw))
        return self.get_responses.pop(0) if self.get_responses else _resp(404)

    async def post(self, url: str, **kw: object) -> httpx.Response:
        self.calls.append(("POST", url, kw))
        assert self.post_response is not None
        return self.post_response


async def test_create_qwen_session_returns_id_and_sends_labels() -> None:
    client = _FakeClient()
    client.post_response = _resp(200, {"session_id": "conv_new"}, method="POST")
    sid = await qn._create_qwen_session(client, b"bundle", terminal_launch_args=["-m", "x"])  # type: ignore[arg-type]
    assert sid == "conv_new"
    _, url, kw = client.calls[0]
    assert url == "/v1/sessions"
    # Terminal-first labels + launch args ride along in the multipart metadata.
    import json as _json

    meta = _json.loads(kw["data"]["metadata"])
    assert meta["labels"]["omnigent.wrapper"] == "qwen-native-ui"
    assert meta["terminal_launch_args"] == ["-m", "x"]


async def test_create_qwen_session_raises_on_error_and_missing_id() -> None:
    bad = _FakeClient()
    bad.post_response = _resp(500, {"detail": "boom"}, method="POST")
    with pytest.raises(click.ClickException):
        await qn._create_qwen_session(bad, b"b")  # type: ignore[arg-type]
    noid = _FakeClient()
    noid.post_response = _resp(200, {}, method="POST")
    with pytest.raises(click.ClickException):
        await qn._create_qwen_session(noid, b"b")  # type: ignore[arg-type]


async def test_fetch_qwen_session_paths() -> None:
    ok = _FakeClient()
    ok.get_responses = [_resp(200, {"id": "conv", "labels": {}})]
    assert await qn._fetch_qwen_session(ok, "conv") == {"id": "conv", "labels": {}}
    for status in (404, 500):
        c = _FakeClient()
        c.get_responses = [_resp(status, {"detail": "x"})]
        with pytest.raises(click.ClickException):
            await qn._fetch_qwen_session(c, "conv")
    nondict = _FakeClient()
    nondict.get_responses = [_resp(200, ["not", "a", "dict"])]
    with pytest.raises(click.ClickException):
        await qn._fetch_qwen_session(nondict, "conv")


async def test_ensure_qwen_terminal_on_runner() -> None:
    ok = _FakeClient()
    ok.post_response = _resp(200, {}, method="POST")
    await qn._ensure_qwen_terminal_on_runner(ok, "conv")  # no raise
    _, url, kw = ok.calls[0]
    assert url.endswith("/resources/terminals")
    assert kw["json"]["ensure_native_terminal"] is True
    bad = _FakeClient()
    bad.post_response = _resp(503, {"detail": "no"}, method="POST")
    with pytest.raises(click.ClickException):
        await qn._ensure_qwen_terminal_on_runner(bad, "conv")


async def test_find_running_qwen_terminal_variants() -> None:
    # 404 → not created yet.
    c404 = _FakeClient()
    c404.get_responses = [_resp(404)]
    assert await qn._find_running_qwen_terminal(c404, "conv") is None
    # running:false metadata → treated as absent.
    cstopped = _FakeClient()
    cstopped.get_responses = [
        _resp(200, {"id": "terminal_qwen_main", "metadata": {"running": False}})
    ]
    assert await qn._find_running_qwen_terminal(cstopped, "conv") is None
    # transient "offline"/"not bound" → None (caller keeps polling).
    coffline = _FakeClient()
    coffline.get_responses = [_resp(503, {"detail": "runner is offline"})]
    assert await qn._find_running_qwen_terminal(coffline, "conv") is None
    # hard error → raise.
    cerr = _FakeClient()
    cerr.get_responses = [_resp(500, {"detail": "boom"})]
    with pytest.raises(click.ClickException):
        await qn._find_running_qwen_terminal(cerr, "conv")
    # healthy → decoded terminal.
    cok = _FakeClient()
    cok.get_responses = [
        _resp(
            200,
            {"id": "terminal_qwen_main", "metadata": {"tmux_socket": "/s", "tmux_target": "t:0"}},
        )
    ]
    launched = await qn._find_running_qwen_terminal(cok, "conv")
    assert launched is not None and launched.terminal_id == "terminal_qwen_main"


async def test_wait_for_qwen_terminal_ready_returns_then_times_out() -> None:
    ready = _FakeClient()
    ready.get_responses = [_resp(200, {"id": "terminal_qwen_main", "metadata": {}})]
    launched = await qn._wait_for_qwen_terminal_ready(ready, "conv", timeout_s=1.0)
    assert launched.terminal_id == "terminal_qwen_main"
    # Never appears → ClickException after the deadline.
    never = _FakeClient()  # get() returns 404 by default
    with pytest.raises(click.ClickException):
        await qn._wait_for_qwen_terminal_ready(never, "conv", timeout_s=0.05)


def test_resolve_session_id_for_resume_direct_and_no_picker() -> None:
    # Explicit id is returned verbatim (no server call).
    assert (
        qn._resolve_session_id_for_resume(
            base_url="http://t", headers={}, session_id="conv_x", resume_picker=False
        )
        == "conv_x"
    )
    # No id and no picker → None (the CLI then creates a fresh session).
    assert (
        qn._resolve_session_id_for_resume(
            base_url="http://t", headers={}, session_id=None, resume_picker=False
        )
        is None
    )


def test_preflight_local_tools_requires_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qn.shutil, "which", lambda _cmd: None)
    with pytest.raises(click.ClickException, match="tmux"):
        qn._preflight_local_tools()
    monkeypatch.setattr(qn.shutil, "which", lambda _cmd: "/usr/bin/tmux")
    qn._preflight_local_tools()  # present → no raise


def test_update_startup_progress_is_optional() -> None:
    qn._update_startup_progress(None, "msg")  # no renderer → no-op

    class _P:
        def __init__(self) -> None:
            self.msgs: list[str] = []

        def update(self, m: str) -> None:
            self.msgs.append(m)

    p = _P()
    qn._update_startup_progress(p, "hi")  # type: ignore[arg-type]
    assert p.msgs == ["hi"]


# --- _prepare_qwen_terminal_via_daemon (orchestration, patched helpers) -------


def _aret(value: object):
    async def _f(*_a: object, **_k: object) -> object:
        return value

    return _f


async def test_prepare_reattaches_running_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        qn, "_fetch_qwen_session", _aret({"labels": {"omnigent.wrapper": "qwen-native-ui"}})
    )
    term = qn.LaunchedQwenTerminal(
        terminal_id="terminal_qwen_main", tmux_socket=Path("/s"), tmux_target="t:0"
    )
    monkeypatch.setattr(qn, "_find_running_qwen_terminal", _aret(term))
    prepared = await qn._prepare_qwen_terminal_via_daemon(
        base_url="http://test",
        headers={},
        session_id="conv",
        session_bundle=None,
        qwen_args=(),
        host_id="host",
        workspace="/ws",
    )
    # A still-running terminal is reused (no daemon relaunch).
    assert prepared.reattached is True
    assert prepared.session_id == "conv"
    assert prepared.terminal_id == "terminal_qwen_main"


async def test_prepare_rejects_non_qwen_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        qn, "_fetch_qwen_session", _aret({"labels": {"omnigent.wrapper": "claude-code-native-ui"}})
    )
    with pytest.raises(click.ClickException, match="not a qwen-native session"):
        await qn._prepare_qwen_terminal_via_daemon(
            base_url="http://test",
            headers={},
            session_id="conv",
            session_bundle=None,
            qwen_args=(),
            host_id="host",
            workspace="/ws",
        )


async def test_prepare_creates_and_launches_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qn, "_create_qwen_session", _aret("conv_new"))
    monkeypatch.setattr(qn, "wait_for_host_online", _aret(None))
    monkeypatch.setattr(qn, "launch_or_reuse_daemon_runner", _aret("runner_1"))
    monkeypatch.setattr(qn, "wait_for_runner_online", _aret(None))
    monkeypatch.setattr(qn, "_bind_session_runner", _aret(None))
    monkeypatch.setattr(qn, "_ensure_qwen_terminal_on_runner", _aret(None))
    term = qn.LaunchedQwenTerminal(
        terminal_id="terminal_qwen_main", tmux_socket=Path("/s"), tmux_target="t:0"
    )
    monkeypatch.setattr(qn, "_wait_for_qwen_terminal_ready", _aret(term))
    prepared = await qn._prepare_qwen_terminal_via_daemon(
        base_url="http://test",
        headers={},
        session_id=None,
        session_bundle=b"bundle",
        qwen_args=("-m", "x"),
        host_id="host",
        workspace="/ws",
    )
    assert prepared.session_id == "conv_new"
    assert prepared.reattached is False
    assert prepared.cold_resumed is False
    assert prepared.terminal_id == "terminal_qwen_main"


async def test_prepare_requires_bundle_for_new_session() -> None:
    with pytest.raises(click.ClickException, match="requires a session bundle"):
        await qn._prepare_qwen_terminal_via_daemon(
            base_url="http://test",
            headers={},
            session_id=None,
            session_bundle=None,
            qwen_args=(),
            host_id="host",
            workspace="/ws",
        )


def test_direct_tmux_reason_target_missing_and_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sock = tmp_path / "s.sock"
    sock.write_text("")  # exists on disk
    no_target = qn.PreparedQwenTerminal(
        session_id="c", terminal_id="t", tmux_socket=sock, tmux_target=None, reattached=False
    )
    assert "target" in (qn._direct_tmux_unavailable_reason(no_target) or "")

    good = qn.PreparedQwenTerminal(
        session_id="c", terminal_id="t", tmux_socket=sock, tmux_target="x:0", reattached=False
    )
    monkeypatch.setattr(qn.shutil, "which", lambda _c: "/usr/bin/tmux")
    assert qn._direct_tmux_unavailable_reason(good) is None  # all present → attachable
    monkeypatch.setattr(qn.shutil, "which", lambda _c: None)
    assert "PATH" in (qn._direct_tmux_unavailable_reason(good) or "")
