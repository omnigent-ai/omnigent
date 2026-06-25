"""Tests for native Kiro CLI orchestration."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from click import ClickException

from omnigent._wrapper_labels import KIRO_NATIVE_WRAPPER_VALUE, WRAPPER_LABEL_KEY
from omnigent.kiro_native import (
    _KIRO_PATH_ENV,
    LaunchedKiroTerminal,
    PreparedKiroTerminal,
    _attach_terminal_resource,
    _create_kiro_session,
    _direct_tmux_unavailable_reason,
    _ensure_kiro_terminal_on_runner,
    _fetch_kiro_session,
    _find_running_kiro_terminal,
    _launched_kiro_terminal_from_payload,
    _materialize_kiro_agent_spec,
    _preflight_local_tools,
    _resolve_session_id_for_resume,
    _tmux_attach_env,
    _update_startup_progress,
    _wait_for_kiro_terminal_ready,
    build_kiro_launch,
    kiro_terminal_resource_id,
    resolve_kiro_executable,
    run_kiro_native,
)

_NO_JSON = object()


class _FakeResponse:
    """Minimal stand-in for ``httpx.Response`` used by the async helpers."""

    def __init__(self, status_code: int, *, json_body: object = _NO_JSON, text: str = "") -> None:
        self.status_code = status_code
        self._json_body = json_body
        self.text = text

    def json(self) -> object:
        if self._json_body is _NO_JSON:
            raise ValueError("no JSON body")
        return self._json_body


class _FakeClient:
    """Records calls and replays queued ``_FakeResponse`` objects per method."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self._queued: dict[str, list[_FakeResponse]] = {}

    def queue(self, method: str, *responses: _FakeResponse) -> None:
        self._queued.setdefault(method, []).extend(responses)

    async def _replay(self, method: str, url: str, kwargs: dict) -> _FakeResponse:
        self.calls.append((method, url, kwargs))
        return self._queued[method].pop(0)

    async def get(self, url: str, **kwargs: object) -> _FakeResponse:
        return await self._replay("get", url, kwargs)

    async def post(self, url: str, **kwargs: object) -> _FakeResponse:
        return await self._replay("post", url, kwargs)

    async def patch(self, url: str, **kwargs: object) -> _FakeResponse:
        return await self._replay("patch", url, kwargs)


def test_materialize_kiro_agent_spec_uses_native_identity(tmp_path: Path) -> None:
    """The generated wrapper spec targets ``kiro-native`` and terminal-first labels."""
    path = _materialize_kiro_agent_spec(tmp_path, model="auto")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert raw["name"] == "kiro-native-ui"
    assert raw["executor"] == {"harness": "kiro-native", "model": "auto"}
    assert raw["spawn"] is True


def test_materialized_kiro_agent_spec_passes_current_validator(tmp_path: Path) -> None:
    """``omnigent kiro`` must not be rejected as an unknown harness at upload."""
    from omnigent.spec._omnigent_compat import load_omnigent_yaml

    path = _materialize_kiro_agent_spec(tmp_path, model=None)

    spec = load_omnigent_yaml(path)

    assert spec.executor.config["harness"] == "kiro-native"


def test_launched_kiro_terminal_decodes_tmux_metadata() -> None:
    """Runner terminal metadata is converted into attach details."""
    terminal = _launched_kiro_terminal_from_payload(
        {
            "id": "terminal_kiro_main",
            "metadata": {
                "tmux_socket": "/tmp/kiro.sock",
                "tmux_target": "main",
            },
        }
    )

    assert terminal.terminal_id == "terminal_kiro_main"
    assert terminal.tmux_socket == Path("/tmp/kiro.sock")
    assert terminal.tmux_target == "main"


def test_build_kiro_launch_includes_resume_id() -> None:
    """Cold resume launches Kiro against the captured native session id."""
    launch = build_kiro_launch(
        ["--effort", "high"],
        resume_id="kiro-session-123",
        env={},
        which=lambda _cmd: "/usr/bin/kiro-cli",
    )

    assert launch.argv == [
        "/usr/bin/kiro-cli",
        "chat",
        "--tui",
        "--resume-id",
        "kiro-session-123",
        "--effort",
        "high",
    ]


@pytest.mark.asyncio
async def test_attach_terminal_resource_requires_tmux_metadata() -> None:
    """A runner response without tmux attach metadata fails clearly."""
    prepared = PreparedKiroTerminal(
        session_id="conv_abc",
        terminal_id="terminal_kiro_main",
        tmux_socket=None,
        tmux_target=None,
        reattached=False,
    )

    with pytest.raises(ClickException, match="Runner-owned Kiro terminal"):
        await _attach_terminal_resource(prepared)


def test_session_labels_use_kiro_wrapper_value() -> None:
    """Kiro wrapper sessions stamp the centralized wrapper label."""
    from omnigent.kiro_native import _SESSION_LABELS

    assert _SESSION_LABELS[WRAPPER_LABEL_KEY] == KIRO_NATIVE_WRAPPER_VALUE


def test_live_kiro_cli_binary_reports_version_when_installed() -> None:
    """Skippable smoke test for the native Kiro binary expected by the harness."""
    binary = shutil.which("kiro-cli")
    if binary is None:
        pytest.skip("kiro-cli is not installed on PATH")

    result = subprocess.run(
        [binary, "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0
    assert "kiro-cli" in result.stdout.lower()


def test_resolve_kiro_executable_errors_when_missing() -> None:
    """A missing kiro-cli yields an actionable install/login hint."""
    with pytest.raises(ClickException, match="kiro-cli"):
        resolve_kiro_executable(env={}, which=lambda _cmd: None)


def test_resolve_kiro_executable_honors_path_override() -> None:
    """``OMNIGENT_KIRO_PATH`` selects the executable to resolve."""
    seen: list[str] = []

    def _which(command: str) -> str:
        seen.append(command)
        return f"/opt/{command}"

    resolved = resolve_kiro_executable(env={_KIRO_PATH_ENV: "custom-kiro"}, which=_which)

    assert resolved == "/opt/custom-kiro"
    assert seen == ["custom-kiro"]


def test_build_kiro_launch_appends_model_then_prompt() -> None:
    """Model flag precedes passthrough args; a prompt is the final argv token."""
    launch = build_kiro_launch(
        ["--foo"],
        model="claude",
        prompt="hello world",
        env={},
        which=lambda _cmd: "/usr/bin/kiro-cli",
    )

    assert launch.argv == [
        "/usr/bin/kiro-cli",
        "chat",
        "--tui",
        "--model",
        "claude",
        "--foo",
        "hello world",
    ]


def test_launched_kiro_terminal_rejects_non_object_payload() -> None:
    """A non-dict runner payload is reported as malformed."""
    with pytest.raises(ClickException, match="non-object JSON"):
        _launched_kiro_terminal_from_payload(["not", "a", "dict"])


def test_launched_kiro_terminal_requires_terminal_id() -> None:
    """A payload without an id cannot be turned into attach details."""
    with pytest.raises(ClickException, match="terminal id"):
        _launched_kiro_terminal_from_payload({"metadata": {}})


def test_launched_kiro_terminal_without_metadata_has_no_tmux() -> None:
    """Missing tmux metadata leaves the attach fields unset (cold terminal)."""
    terminal = _launched_kiro_terminal_from_payload({"id": "terminal_kiro_main"})

    assert terminal.terminal_id == "terminal_kiro_main"
    assert terminal.tmux_socket is None
    assert terminal.tmux_target is None


def test_tmux_attach_env_filters_to_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only allowlisted, set environment keys reach the tmux attach process."""
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("OMNIGENT_UNLISTED_VAR", "present")

    env = _tmux_attach_env()

    assert env["TERM"] == "xterm-256color"
    assert "OMNIGENT_UNLISTED_VAR" not in env


def test_direct_tmux_unavailable_reason_reports_each_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each missing prerequisite yields a distinct, specific reason."""

    def _prepared(socket: Path | None, target: str | None) -> PreparedKiroTerminal:
        return PreparedKiroTerminal(
            session_id="conv",
            terminal_id="terminal_kiro_main",
            tmux_socket=socket,
            tmux_target=target,
            reattached=False,
        )

    assert "tmux socket path" in (_direct_tmux_unavailable_reason(_prepared(None, "main")) or "")
    assert "tmux target" in (
        _direct_tmux_unavailable_reason(_prepared(tmp_path / "s.sock", None)) or ""
    )
    assert "not reachable" in (
        _direct_tmux_unavailable_reason(_prepared(tmp_path / "missing.sock", "main")) or ""
    )

    socket = tmp_path / "live.sock"
    socket.touch()
    monkeypatch.setattr(shutil, "which", lambda _cmd: None)
    assert "tmux is not available" in (
        _direct_tmux_unavailable_reason(_prepared(socket, "main")) or ""
    )

    monkeypatch.setattr(shutil, "which", lambda _cmd: "/usr/bin/tmux")
    assert _direct_tmux_unavailable_reason(_prepared(socket, "main")) is None


def test_update_startup_progress_is_a_noop_without_renderer() -> None:
    """A ``None`` progress renderer is tolerated silently."""
    _update_startup_progress(None, "anything")


def test_update_startup_progress_forwards_to_renderer() -> None:
    """An active renderer receives the milestone message verbatim."""
    seen: list[str] = []

    class _Progress:
        def update(self, message: str) -> None:
            seen.append(message)

    _update_startup_progress(_Progress(), "Starting Kiro terminal...")

    assert seen == ["Starting Kiro terminal..."]


def test_preflight_local_tools_requires_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    """The native wrapper refuses to start without a local tmux."""
    monkeypatch.setattr(shutil, "which", lambda _cmd: None)

    with pytest.raises(ClickException, match="tmux was not found"):
        _preflight_local_tools()


def test_preflight_local_tools_passes_with_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    """A present tmux satisfies the preflight check."""
    monkeypatch.setattr(shutil, "which", lambda _cmd: "/usr/bin/tmux")

    _preflight_local_tools()


def test_kiro_terminal_resource_id_is_deterministic() -> None:
    """The terminal resource id is stable across calls."""
    assert kiro_terminal_resource_id() == kiro_terminal_resource_id()
    assert isinstance(kiro_terminal_resource_id(), str)


def test_resolve_session_id_for_resume_passthrough() -> None:
    """An explicit session id is returned without touching the network."""
    resolved = _resolve_session_id_for_resume(
        base_url="http://server",
        headers={},
        session_id="conv_explicit",
        resume_picker=False,
    )

    assert resolved == "conv_explicit"


def test_resolve_session_id_for_resume_no_picker_returns_none() -> None:
    """Without a session id or picker there is nothing to resume."""
    resolved = _resolve_session_id_for_resume(
        base_url="http://server",
        headers={},
        session_id=None,
        resume_picker=False,
    )

    assert resolved is None


def test_run_kiro_native_requires_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing server URL is a programming error surfaced as a clear message."""
    monkeypatch.setattr("omnigent.kiro_native._preflight_local_tools", lambda: None)

    with pytest.raises(ClickException, match="resolved Omnigent server URL"):
        run_kiro_native(server=None, session_id=None, kiro_args=())


def test_run_kiro_native_materializes_spec_and_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The launcher writes a spec and hands a trimmed base URL to the server path."""
    monkeypatch.setattr("omnigent.kiro_native._preflight_local_tools", lambda: None)
    captured: dict[str, object] = {}

    def _fake_remote(base_url: str, spec_path: Path, **kwargs: object) -> None:
        captured["base_url"] = base_url
        captured["spec_exists"] = spec_path.exists()
        captured["kwargs"] = kwargs

    monkeypatch.setattr("omnigent.kiro_native._run_with_remote_server", _fake_remote)

    run_kiro_native(
        server="http://server/",
        session_id=None,
        kiro_args=("--foo",),
        model="claude",
        prompt="hi",
    )

    assert captured["base_url"] == "http://server"
    assert captured["spec_exists"] is True
    assert captured["kwargs"]["model"] == "claude"


async def test_create_kiro_session_returns_id_and_persists_args() -> None:
    """A successful create returns the new id and forwards launch args as metadata."""
    client = _FakeClient()
    client.queue("post", _FakeResponse(200, json_body={"session_id": "conv_new"}))

    session_id = await _create_kiro_session(
        client, b"bundle-bytes", terminal_launch_args=["--foo"]
    )

    assert session_id == "conv_new"
    _method, _url, kwargs = client.calls[0]
    metadata = json.loads(kwargs["data"]["metadata"])
    assert metadata["terminal_launch_args"] == ["--foo"]
    assert WRAPPER_LABEL_KEY in metadata["labels"]


async def test_create_kiro_session_raises_on_error_status() -> None:
    """A 4xx/5xx create response is surfaced with the status code."""
    client = _FakeClient()
    client.queue("post", _FakeResponse(500, json_body={"error": "boom"}))

    with pytest.raises(ClickException, match="creation failed \\(500\\)"):
        await _create_kiro_session(client, b"bundle")


async def test_create_kiro_session_requires_session_id_in_body() -> None:
    """A success body lacking a session id is treated as malformed."""
    client = _FakeClient()
    client.queue("post", _FakeResponse(200, json_body={}))

    with pytest.raises(ClickException, match="did not include session_id"):
        await _create_kiro_session(client, b"bundle")


async def test_fetch_kiro_session_maps_404_to_not_found() -> None:
    """A 404 fetch is reported as a missing conversation."""
    client = _FakeClient()
    client.queue("get", _FakeResponse(404))

    with pytest.raises(ClickException, match="not found"):
        await _fetch_kiro_session(client, "conv_missing")


async def test_fetch_kiro_session_raises_on_error_status() -> None:
    """A non-404 error fetch is surfaced with the status code."""
    client = _FakeClient()
    client.queue("get", _FakeResponse(500, json_body={"error": "boom"}))

    with pytest.raises(ClickException, match="Failed to fetch conversation"):
        await _fetch_kiro_session(client, "conv")


async def test_fetch_kiro_session_rejects_non_object_payload() -> None:
    """A non-object fetch payload is malformed."""
    client = _FakeClient()
    client.queue("get", _FakeResponse(200, json_body=["unexpected"]))

    with pytest.raises(ClickException, match="non-object JSON"):
        await _fetch_kiro_session(client, "conv")


async def test_fetch_kiro_session_returns_payload() -> None:
    """A healthy fetch returns the decoded session object."""
    client = _FakeClient()
    client.queue("get", _FakeResponse(200, json_body={"labels": {"x": "y"}}))

    payload = await _fetch_kiro_session(client, "conv")

    assert payload == {"labels": {"x": "y"}}


async def test_ensure_kiro_terminal_on_runner_posts_native_flag() -> None:
    """Ensuring the terminal asks the runner for a native terminal."""
    client = _FakeClient()
    client.queue("post", _FakeResponse(200, json_body={}))

    await _ensure_kiro_terminal_on_runner(client, "conv")

    _method, _url, kwargs = client.calls[0]
    assert kwargs["json"]["ensure_native_terminal"] is True


async def test_ensure_kiro_terminal_on_runner_raises_on_error() -> None:
    """A failed ensure surfaces the status code."""
    client = _FakeClient()
    client.queue("post", _FakeResponse(503, json_body={"error": "no runner"}))

    with pytest.raises(ClickException, match="ensure failed \\(503\\)"):
        await _ensure_kiro_terminal_on_runner(client, "conv")


async def test_find_running_kiro_terminal_absent_is_none() -> None:
    """A 404 terminal lookup means no running terminal yet."""
    client = _FakeClient()
    client.queue("get", _FakeResponse(404))

    assert await _find_running_kiro_terminal(client, "conv") is None


async def test_find_running_kiro_terminal_unbound_runner_is_none() -> None:
    """A transient 'not bound to a runner' is treated as not-yet-running."""
    client = _FakeClient()
    client.queue("get", _FakeResponse(409, text="session not bound to a runner"))

    assert await _find_running_kiro_terminal(client, "conv") is None


async def test_find_running_kiro_terminal_hard_error_raises() -> None:
    """An unexpected error status is surfaced rather than swallowed."""
    client = _FakeClient()
    client.queue("get", _FakeResponse(500, json_body={"error": "boom"}))

    with pytest.raises(ClickException, match="Failed to fetch Kiro terminal"):
        await _find_running_kiro_terminal(client, "conv")


async def test_find_running_kiro_terminal_not_running_metadata_is_none() -> None:
    """A terminal explicitly flagged not-running is ignored."""
    client = _FakeClient()
    client.queue(
        "get",
        _FakeResponse(200, json_body={"id": "terminal_kiro_main", "metadata": {"running": False}}),
    )

    assert await _find_running_kiro_terminal(client, "conv") is None


async def test_find_running_kiro_terminal_returns_attach_details() -> None:
    """A live terminal payload is decoded into attach details."""
    client = _FakeClient()
    client.queue(
        "get",
        _FakeResponse(
            200,
            json_body={
                "id": "terminal_kiro_main",
                "metadata": {"tmux_socket": "/tmp/k.sock", "tmux_target": "main"},
            },
        ),
    )

    terminal = await _find_running_kiro_terminal(client, "conv")

    assert isinstance(terminal, LaunchedKiroTerminal)
    assert terminal.tmux_socket == Path("/tmp/k.sock")


async def test_wait_for_kiro_terminal_ready_returns_first_hit() -> None:
    """The poll loop returns as soon as the terminal resource appears."""
    client = _FakeClient()
    client.queue("get", _FakeResponse(200, json_body={"id": "terminal_kiro_main"}))

    terminal = await _wait_for_kiro_terminal_ready(client, "conv", timeout_s=1.0)

    assert terminal.terminal_id == "terminal_kiro_main"


async def test_wait_for_kiro_terminal_ready_times_out() -> None:
    """An absent terminal eventually fails with a timeout message."""
    client = _FakeClient()
    client.queue("get", _FakeResponse(404))

    with pytest.raises(ClickException, match="did not create the Kiro terminal"):
        await _wait_for_kiro_terminal_ready(client, "conv", timeout_s=0.05)
