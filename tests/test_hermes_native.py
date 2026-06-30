"""Unit tests for the omni hermes CLI-side helpers + harness wiring (no server)."""

from __future__ import annotations

import click
import pytest

from omnigent import hermes_native as hn


def test_resolve_hermes_executable_found() -> None:
    resolved = hn.resolve_hermes_executable(
        env={}, which=lambda cmd: f"/usr/local/bin/{cmd}" if cmd == "hermes" else None
    )
    assert resolved == "/usr/local/bin/hermes"


def test_resolve_hermes_executable_honors_path_override() -> None:
    resolved = hn.resolve_hermes_executable(
        env={"OMNIGENT_HERMES_PATH": "/opt/hermes"},
        which=lambda cmd: cmd if cmd == "/opt/hermes" else None,
    )
    assert resolved == "/opt/hermes"


def test_resolve_hermes_executable_missing_raises_with_hint() -> None:
    with pytest.raises(click.ClickException) as exc:
        hn.resolve_hermes_executable(env={}, which=lambda _cmd: None)
    assert "hermes-agent.nousresearch.com" in str(exc.value)


def test_build_hermes_launch_argv() -> None:
    launch = hn.build_hermes_launch(
        ["--resume", "x"],
        env={},
        which=lambda cmd: f"/bin/{cmd}",
    )
    assert launch.executable == "/bin/hermes"
    assert launch.argv == ["/bin/hermes", "--resume", "x"]


def test_terminal_resource_id_stable() -> None:
    assert hn.hermes_terminal_resource_id() == hn.hermes_terminal_resource_id()


def test_harness_registry_has_hermes_native() -> None:
    from omnigent.runtime.harnesses import _HARNESS_MODULES

    assert _HARNESS_MODULES["hermes-native"] == "omnigent.inner.hermes_native_harness"


def test_alias_and_native_membership() -> None:
    from omnigent.harness_aliases import (
        NATIVE_HARNESSES,
        canonicalize_harness,
        is_native_harness,
    )

    assert canonicalize_harness("native-hermes") == "hermes-native"
    assert "hermes-native" in NATIVE_HARNESSES
    assert "native-hermes" in NATIVE_HARNESSES
    assert is_native_harness("hermes-native") is True
    assert is_native_harness("native-hermes") is True
    # The headless ``hermes`` harness is NOT a native CLI harness.
    assert is_native_harness("hermes") is False


def test_native_coding_agent_resolves() -> None:
    from omnigent._wrapper_labels import (
        HERMES_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.harness_plugins import HERMES_NATIVE_CODING_AGENT
    from omnigent.native_coding_agents import native_coding_agent_for_harness

    agent = native_coding_agent_for_harness("native-hermes")
    assert agent is HERMES_NATIVE_CODING_AGENT
    assert agent is native_coding_agent_for_harness("hermes-native")
    assert agent.agent_name == "hermes-native-ui"
    assert agent.terminal_name == "hermes"
    assert agent.presentation_labels == {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: HERMES_NATIVE_WRAPPER_VALUE,
    }


def test_create_app_builds() -> None:
    from omnigent.inner.hermes_native_harness import create_app

    assert create_app() is not None


# --- CLI orchestration helpers (no server/daemon needed) ----------------------


def test_materialize_agent_spec_is_terminal_first_hermes_native(tmp_path) -> None:
    import yaml

    spec_path = hn._materialize_hermes_agent_spec(tmp_path)
    raw = yaml.safe_load(spec_path.read_text())
    assert raw["name"] == "hermes-native-ui"
    assert raw["executor"] == {"harness": "hermes-native"}
    assert raw["spawn"] is True
    assert "shell" in raw["terminals"]


def test_configured_hermes_command_default_and_override() -> None:
    assert hn._configured_hermes_command({}) == "hermes"
    assert hn._configured_hermes_command({"OMNIGENT_HERMES_PATH": "/opt/hermes"}) == "/opt/hermes"


def test_launched_terminal_from_payload_decodes_tmux_metadata() -> None:
    term = hn._launched_hermes_terminal_from_payload(
        {"id": "terminal_hermes_main", "metadata": {"tmux_socket": "/s", "tmux_target": "t:0.0"}}
    )
    assert term.terminal_id == "terminal_hermes_main"
    assert str(term.tmux_socket) == "/s"
    assert term.tmux_target == "t:0.0"
    # No metadata → id only, sockets None.
    bare = hn._launched_hermes_terminal_from_payload({"id": "terminal_hermes_main"})
    assert bare.tmux_socket is None and bare.tmux_target is None


def test_launched_terminal_from_payload_rejects_bad_shapes() -> None:
    with pytest.raises(click.ClickException):
        hn._launched_hermes_terminal_from_payload(["not", "a", "dict"])
    with pytest.raises(click.ClickException):
        hn._launched_hermes_terminal_from_payload({"metadata": {}})  # no id


def test_direct_tmux_unavailable_reason_branches(tmp_path, monkeypatch) -> None:
    def _prep(socket, target):
        return hn.PreparedHermesTerminal(
            session_id="c",
            terminal_id="t",
            tmux_socket=socket,
            tmux_target=target,
            reattached=False,
        )

    assert "socket path" in (hn._direct_tmux_unavailable_reason(_prep(None, "t")) or "")
    assert "tmux target" in (hn._direct_tmux_unavailable_reason(_prep(tmp_path, None)) or "")
    missing = tmp_path / "nope.sock"
    assert "not reachable" in (hn._direct_tmux_unavailable_reason(_prep(missing, "t")) or "")
    # Socket exists + tmux present → no reason (attach is available).
    sock = tmp_path / "live.sock"
    sock.write_text("")
    monkeypatch.setattr(hn.shutil, "which", lambda _c: "/usr/bin/tmux")
    assert hn._direct_tmux_unavailable_reason(_prep(sock, "t")) is None
    # tmux absent → reason.
    monkeypatch.setattr(hn.shutil, "which", lambda _c: None)
    assert "tmux is not available" in (hn._direct_tmux_unavailable_reason(_prep(sock, "t")) or "")


def test_preflight_requires_tmux(monkeypatch) -> None:
    monkeypatch.setattr(hn.shutil, "which", lambda _c: None)
    with pytest.raises(click.ClickException, match="tmux"):
        hn._preflight_local_tools()
    monkeypatch.setattr(hn.shutil, "which", lambda _c: "/usr/bin/tmux")
    hn._preflight_local_tools()  # no raise


def test_update_startup_progress_is_noop_without_renderer() -> None:
    hn._update_startup_progress(None, "hi")  # no error

    class _Progress:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def update(self, msg: str) -> None:
            self.messages.append(msg)

    prog = _Progress()
    hn._update_startup_progress(prog, "Starting…")
    assert prog.messages == ["Starting…"]


# --- daemon-flow HTTP helpers (fake async client; no real server) -------------


class _FakeResp:
    def __init__(self, status: int, payload=None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeAsyncClient:
    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp

    async def post(self, *_a, **_k):
        return self._resp

    async def get(self, *_a, **_k):
        return self._resp

    async def patch(self, *_a, **_k):
        return self._resp


async def test_create_hermes_session_returns_id_or_raises() -> None:
    ok = _FakeAsyncClient(_FakeResp(200, {"session_id": "conv_x"}))
    assert await hn._create_hermes_session(ok, b"bundle") == "conv_x"
    with pytest.raises(click.ClickException):
        await hn._create_hermes_session(_FakeAsyncClient(_FakeResp(500, {})), b"bundle")
    with pytest.raises(click.ClickException, match="session_id"):
        await hn._create_hermes_session(_FakeAsyncClient(_FakeResp(200, {})), b"bundle")


async def test_fetch_hermes_session_handles_status() -> None:
    payload = {"labels": {"omnigent.wrapper": "hermes-native-ui"}}
    assert (
        await hn._fetch_hermes_session(_FakeAsyncClient(_FakeResp(200, payload)), "c") == payload
    )
    with pytest.raises(click.ClickException, match="not found"):
        await hn._fetch_hermes_session(_FakeAsyncClient(_FakeResp(404)), "c")
    with pytest.raises(click.ClickException):
        await hn._fetch_hermes_session(_FakeAsyncClient(_FakeResp(500, {})), "c")


async def test_ensure_terminal_on_runner_raises_on_error() -> None:
    await hn._ensure_hermes_terminal_on_runner(_FakeAsyncClient(_FakeResp(200, {})), "c")  # ok
    with pytest.raises(click.ClickException):
        await hn._ensure_hermes_terminal_on_runner(_FakeAsyncClient(_FakeResp(500, {})), "c")


async def test_find_running_terminal_states() -> None:
    # 404 → not created yet.
    assert await hn._find_running_hermes_terminal(_FakeAsyncClient(_FakeResp(404)), "c") is None
    # running:false → treated as absent.
    not_running = _FakeResp(200, {"id": "terminal_hermes_main", "metadata": {"running": False}})
    assert await hn._find_running_hermes_terminal(_FakeAsyncClient(not_running), "c") is None
    # 409 not-bound → None (runner not ready), not an error.
    notbound = _FakeResp(409, {"error": {"message": "session not bound to a runner"}})
    assert await hn._find_running_hermes_terminal(_FakeAsyncClient(notbound), "c") is None
    # Live terminal with tmux metadata → decoded.
    live = _FakeResp(
        200,
        {"id": "terminal_hermes_main", "metadata": {"tmux_socket": "/s", "tmux_target": "t:0.0"}},
    )
    term = await hn._find_running_hermes_terminal(_FakeAsyncClient(live), "c")
    assert term is not None and term.tmux_target == "t:0.0"


async def test_wait_for_terminal_ready_found_and_timeout(monkeypatch) -> None:
    live = hn.LaunchedHermesTerminal(terminal_id="t", tmux_socket=None, tmux_target=None)

    async def _found(_client, _sid):
        return live

    monkeypatch.setattr(hn, "_find_running_hermes_terminal", _found)
    out = await hn._wait_for_hermes_terminal_ready(
        _FakeAsyncClient(_FakeResp(200, {})), "c", timeout_s=5
    )
    assert out is live

    async def _never(_client, _sid):
        return None

    monkeypatch.setattr(hn, "_find_running_hermes_terminal", _never)
    with pytest.raises(click.ClickException, match="did not create"):
        await hn._wait_for_hermes_terminal_ready(
            _FakeAsyncClient(_FakeResp(200, {})), "c", timeout_s=0.0
        )
