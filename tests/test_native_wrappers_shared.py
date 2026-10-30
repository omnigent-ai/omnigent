"""Unit tests for the CLI-side orchestration shared by the goose/kimi/pi native wrappers.

``omnigent.harnesses.{goose,kimi,pi}_native.main`` are structurally identical: resolve
the vendor CLI, create or resume an Omnigent session through a daemon runner, wait for
the runner-owned tmux terminal, and attach. Every test here runs once per wrapper, with
the few behavioral differences (pi has no cold-resume state and a legacy path env var)
carried on :class:`_Wrapper`. The server is an ``httpx.MockTransport`` and the daemon
helpers are recorded fakes; no process, tmux or network is involved.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import warnings
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import click
import httpx
import pytest
import yaml


@dataclass(frozen=True)
class _Wrapper:
    key: str  # "goose"
    label: str  # "Goose"
    module: ModuleType
    cold_resume: bool  # tracks a cold-resumed terminal separately from a reattach
    reattached_on_resume: bool  # resuming an existing session reports reattached
    host_scoped_picker: bool  # the resume picker is scoped to this machine's host id

    def fn(self, template: str) -> Any:
        return getattr(self.module, template.format(k=self.key, L=self.label))

    @property
    def env_var(self) -> str:
        return f"OMNIGENT_{self.key.upper()}_PATH"


def _wrapper(
    key: str,
    label: str,
    *,
    cold_resume: bool,
    reattached_on_resume: bool,
    host_scoped_picker: bool = False,
) -> _Wrapper:
    module = importlib.import_module(f"omnigent.harnesses.{key}_native.main")
    return _Wrapper(key, label, module, cold_resume, reattached_on_resume, host_scoped_picker)


_WRAPPERS = [
    _wrapper("goose", "Goose", cold_resume=True, reattached_on_resume=False),
    _wrapper("kimi", "Kimi", cold_resume=True, reattached_on_resume=False),
    _wrapper("pi", "Pi", cold_resume=False, reattached_on_resume=True, host_scoped_picker=True),
]


@pytest.fixture(params=_WRAPPERS, ids=[w.key for w in _WRAPPERS])
def w(request: pytest.FixtureRequest) -> _Wrapper:
    return request.param


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://server.test")


def _launched(w: _Wrapper, tid: str = "terminal_main") -> Any:
    return w.fn("Launched{L}Terminal")(
        terminal_id=tid, tmux_socket=Path("/tmp/s"), tmux_target="t:0"
    )


# ── executable resolution ────────────────────────────────────


@pytest.fixture()
def which_only(w: _Wrapper, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve binaries through the injected ``which`` only (no install-dir ladder)."""
    monkeypatch.setattr(
        w.module, "resolve_cli_binary", lambda name, *, which: which(name), raising=True
    )


def test_resolve_executable_defaults_to_vendor_command(w: _Wrapper, which_only: None) -> None:
    seen: list[str] = []

    def which(name: str) -> str | None:
        seen.append(name)
        return f"/usr/bin/{name}"

    assert w.fn("resolve_{k}_executable")(env={}, which=which) == f"/usr/bin/{w.key}"
    assert seen == [w.key]


def test_resolve_executable_honors_path_env_override(w: _Wrapper, which_only: None) -> None:
    resolved = w.fn("resolve_{k}_executable")(
        env={w.env_var: "  /opt/custom/bin  "}, which=lambda name: name
    )

    assert resolved == "/opt/custom/bin"


def test_resolve_executable_explains_how_to_install_when_missing(
    w: _Wrapper, which_only: None
) -> None:
    with pytest.raises(click.ClickException) as excinfo:
        w.fn("resolve_{k}_executable")(env={}, which=lambda name: None)

    message = excinfo.value.message
    assert f"Native {w.label} requires the '{w.key}' CLI" in message
    assert w.env_var in message


def test_build_launch_prepends_resolved_executable(w: _Wrapper, which_only: None) -> None:
    launch = w.fn("build_{k}_launch")(
        ["--flag", "value"], env={}, which=lambda name: f"/bin/{name}"
    )

    assert launch.executable == f"/bin/{w.key}"
    assert launch.argv == [f"/bin/{w.key}", "--flag", "value"]


# ── agent spec / small helpers ───────────────────────────────


def test_materialized_agent_spec_targets_the_native_harness(w: _Wrapper, tmp_path: Path) -> None:
    path = w.fn("_materialize_{k}_agent_spec")(tmp_path)

    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert path == tmp_path / f"{w.key}-native-ui.yaml"
    assert spec["name"] == f"{w.key}-native-ui"
    assert spec["executor"] == {"harness": f"{w.key}-native"}
    assert spec["os_env"]["sandbox"] == {"type": "none"}
    assert spec["terminals"]  # the default "+ New shell" terminal


def test_terminal_resource_id_is_deterministic(w: _Wrapper) -> None:
    first = w.fn("{k}_terminal_resource_id")()

    assert first == w.fn("{k}_terminal_resource_id")()
    assert w.key in first


def test_preflight_requires_tmux(w: _Wrapper, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w.module.shutil, "which", lambda name: None)
    with pytest.raises(click.ClickException, match="tmux was not found on local PATH"):
        w.module._preflight_local_tools()

    monkeypatch.setattr(w.module.shutil, "which", lambda name: "/usr/bin/tmux")
    w.module._preflight_local_tools()


def test_update_startup_progress_tolerates_missing_renderer(w: _Wrapper) -> None:
    messages: list[str] = []

    w.module._update_startup_progress(None, "ignored")
    w.module._update_startup_progress(SimpleNamespace(update=messages.append), "Working...")

    assert messages == ["Working..."]


# ── terminal payload decoding ────────────────────────────────


def test_launched_terminal_decodes_tmux_metadata(w: _Wrapper) -> None:
    decode = w.fn("_launched_{k}_terminal_from_payload")

    full = decode({"id": "t1", "metadata": {"tmux_socket": "/tmp/s", "tmux_target": "sess:0"}})
    bare = decode({"id": "t2"})
    junk = decode({"id": "t3", "metadata": {"tmux_socket": "", "tmux_target": 5}})

    assert (full.terminal_id, full.tmux_socket, full.tmux_target) == (
        "t1",
        Path("/tmp/s"),
        "sess:0",
    )
    assert (bare.tmux_socket, bare.tmux_target) == (None, None)
    assert (junk.tmux_socket, junk.tmux_target) == (None, None)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (["not", "a", "dict"], "non-object JSON"),
        ({}, "did not include terminal id"),
        ({"id": ""}, "did not include terminal id"),
        ({"id": 7}, "did not include terminal id"),
    ],
)
def test_launched_terminal_rejects_malformed_payloads(
    w: _Wrapper, payload: object, message: str
) -> None:
    with pytest.raises(click.ClickException, match=message):
        w.fn("_launched_{k}_terminal_from_payload")(payload)


# ── direct tmux attach ───────────────────────────────────────


def _prepared(w: _Wrapper, socket: Path | None, target: str | None) -> Any:
    return w.fn("Prepared{L}Terminal")(
        session_id="conv_1",
        terminal_id="t1",
        tmux_socket=socket,
        tmux_target=target,
        reattached=False,
    )


def test_direct_tmux_reasons(w: _Wrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reason = w.module._direct_tmux_unavailable_reason
    sock = tmp_path / "tmux.sock"

    assert "did not include a tmux socket" in reason(_prepared(w, None, "t:0"))
    assert "did not include a tmux target" in reason(_prepared(w, sock, None))
    assert f"tmux socket {sock} is not reachable" in reason(_prepared(w, sock, "t:0"))

    sock.write_text("", encoding="utf-8")
    monkeypatch.setattr(w.module.shutil, "which", lambda name: None)
    assert reason(_prepared(w, sock, "t:0")) == "tmux is not available on PATH."

    monkeypatch.setattr(w.module.shutil, "which", lambda name: "/usr/bin/tmux")
    assert reason(_prepared(w, sock, "t:0")) is None


async def test_attach_terminal_resource_explains_unavailable_tmux(w: _Wrapper) -> None:
    with pytest.raises(click.ClickException, match="requires direct tmux attach, but"):
        await w.module._attach_terminal_resource(_prepared(w, None, None))


async def test_attach_terminal_resource_attaches_when_reachable(
    w: _Wrapper, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sock = tmp_path / "tmux.sock"
    sock.write_text("", encoding="utf-8")
    monkeypatch.setattr(w.module.shutil, "which", lambda name: "/usr/bin/tmux")
    attached: list[tuple[Path, str]] = []

    async def fake_attach(socket_path: Path, target: str) -> None:
        attached.append((socket_path, target))

    monkeypatch.setattr(w.module, "_attach_direct_tmux", fake_attach)

    await w.module._attach_terminal_resource(_prepared(w, sock, "sess:0"))

    assert attached == [(sock, "sess:0")]


async def test_attach_direct_tmux_runs_tmux_without_nested_env(
    w: _Wrapper, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TMUX", "/tmp/outer-tmux,1,0")
    captured: dict[str, Any] = {}

    class _Process:
        async def wait(self) -> int:
            captured["waited"] = True
            return 0

    async def fake_exec(*argv: str, env: dict[str, str]) -> _Process:
        captured["argv"], captured["env"] = argv, env
        return _Process()

    monkeypatch.setattr(
        w.module,
        "asyncio",
        SimpleNamespace(**(vars(asyncio) | {"create_subprocess_exec": fake_exec})),
    )

    await w.module._attach_direct_tmux(Path("/tmp/sock"), "sess:0")

    argv = captured["argv"]
    assert argv[:3] == ("tmux", "-S", "/tmp/sock") and argv[-3:] == ("attach", "-t", "sess:0")
    # A nested TMUX var would make the attach refuse to run inside tmux.
    assert "TMUX" not in captured["env"]
    assert captured["waited"] is True


# ── resume-id resolution ─────────────────────────────────────


def test_resolve_session_id_short_circuits_without_picker(w: _Wrapper) -> None:
    resolve = w.module._resolve_session_id_for_resume

    assert (
        resolve(base_url="http://s", headers={}, session_id="conv_1", resume_picker=True)
        == "conv_1"
    )
    assert resolve(base_url="http://s", headers={}, session_id=None, resume_picker=False) is None


@pytest.mark.parametrize("host_id", ["host_local", None], ids=["registered-host", "no-host"])
def test_resolve_session_id_runs_the_picker(
    w: _Wrapper, monkeypatch: pytest.MonkeyPatch, host_id: str | None
) -> None:
    """A host-scoped wrapper hands the picker this machine's host id; others list unscoped."""
    opened: dict[str, Any] = {}

    class _Client:
        def __init__(self, *, base_url: str, headers: dict[str, str] | None) -> None:
            opened.update(base_url=base_url, headers=headers)

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    picked: dict[str, Any] = {}

    async def fake_picker(client: object, **kwargs: Any) -> str | None:
        picked.update(kwargs)
        return "conv_picked"

    identity = SimpleNamespace(host_id=host_id, name="laptop") if host_id else None
    monkeypatch.setattr("omnigent_client.OmnigentClient", _Client)
    monkeypatch.setattr("omnigent.host.identity.load_host_identity_if_present", lambda: identity)
    monkeypatch.setattr(
        "omnigent.repl._resume_picker.pick_conversation_by_wrapper_label_from_sdk", fake_picker
    )

    result = w.module._resolve_session_id_for_resume(
        base_url="http://s", headers={"X-Auth": "1"}, session_id=None, resume_picker=True
    )

    assert result == "conv_picked"
    assert opened == {"base_url": "http://s", "headers": {"X-Auth": "1"}}
    assert picked == {
        "wrapper_value": w.module._WRAPPER_LABEL_VALUE,
        "agent_name": f"{w.key}-native-ui",
        **({"host_id": host_id} if w.host_scoped_picker else {}),
    }


# ── server HTTP helpers ──────────────────────────────────────


async def test_create_session_posts_bundle_with_labels_and_launch_args(w: _Wrapper) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"session_id": "conv_new"})

    async with _client(handler) as client:
        session_id = await w.fn("_create_{k}_session")(
            client, b"bundle-bytes", terminal_launch_args=["--fast"]
        )

    (request,) = seen
    body = request.read()
    assert session_id == "conv_new"
    assert request.method == "POST" and request.url.path == "/v1/sessions"
    assert b"bundle-bytes" in body and b"omnigent.ui" in body and b"--fast" in body
    assert f"{w.key}-native-ui.tar.gz".encode() in body


async def test_create_session_omits_launch_args_when_absent(w: _Wrapper) -> None:
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.read())
        return httpx.Response(200, json={"session_id": "conv_new"})

    async with _client(handler) as client:
        await w.fn("_create_{k}_session")(client, b"x")

    assert b"terminal_launch_args" not in seen[0]


@pytest.mark.parametrize("body", [{}, {"session_id": ""}, {"session_id": 5}])
async def test_create_session_requires_a_session_id(w: _Wrapper, body: dict[str, object]) -> None:
    async with _client(lambda request: httpx.Response(200, json=body)) as client:
        with pytest.raises(click.ClickException, match="did not include session_id"):
            await w.fn("_create_{k}_session")(client, b"x")


async def test_create_session_surfaces_server_errors(w: _Wrapper) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "db down"}})

    async with _client(handler) as client:
        with pytest.raises(click.ClickException) as excinfo:
            await w.fn("_create_{k}_session")(client, b"x")

    assert f"{w.label} session creation failed (500): db down" in excinfo.value.message


async def test_fetch_session_returns_payload_and_encodes_the_id(w: _Wrapper) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "conv/1", "labels": {}})

    async with _client(handler) as client:
        payload = await w.fn("_fetch_{k}_session")(client, "conv/1")

    assert payload == {"id": "conv/1", "labels": {}}
    assert seen[0].url.raw_path == b"/v1/sessions/conv%2F1"


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(404), "Conversation 'c1' not found on the server."),
        (
            httpx.Response(500, json={"detail": "boom"}),
            r"Failed to fetch conversation 'c1' \(500\): boom",
        ),
        (httpx.Response(200, json=["not", "an", "object"]), "non-object JSON"),
    ],
)
async def test_fetch_session_errors(w: _Wrapper, response: httpx.Response, message: str) -> None:
    async with _client(lambda request: response) as client:
        with pytest.raises(click.ClickException, match=message):
            await w.fn("_fetch_{k}_session")(client, "c1")


async def test_ensure_terminal_asks_runner_to_create_native_terminal(w: _Wrapper) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    async with _client(handler) as client:
        await w.fn("_ensure_{k}_terminal_on_runner")(client, "conv_1")

    (request,) = seen
    assert request.url.path == "/v1/sessions/conv_1/resources/terminals"
    payload = json.loads(request.read())
    assert payload == {
        "terminal": w.key,
        "session_key": "main",
        "ensure_native_terminal": True,
    }


async def test_ensure_terminal_surfaces_failures(w: _Wrapper) -> None:
    async with _client(
        lambda request: httpx.Response(502, json={"detail": "runner gone"})
    ) as client:
        with pytest.raises(click.ClickException) as excinfo:
            await w.fn("_ensure_{k}_terminal_on_runner")(client, "conv_1")

    assert f"{w.label} terminal ensure failed (502): runner gone" in excinfo.value.message


async def test_find_running_terminal_decodes_the_resource(w: _Wrapper) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"id": "t1", "metadata": {"tmux_socket": "/tmp/s", "tmux_target": "s:0"}}
        )

    async with _client(handler) as client:
        terminal = await w.fn("_find_running_{k}_terminal")(client, "conv_1")

    assert terminal.terminal_id == "t1" and terminal.tmux_target == "s:0"
    assert seen[0].url.path == (
        f"/v1/sessions/conv_1/resources/terminals/{w.fn('{k}_terminal_resource_id')()}"
    )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404),
        httpx.Response(409, json={"detail": "session not bound to a runner"}),
        httpx.Response(503, json={"detail": "runner is offline"}),
        httpx.Response(200, json={"id": "t1", "metadata": {"running": False}}),
    ],
)
async def test_find_running_terminal_is_none_when_not_running(
    w: _Wrapper, response: httpx.Response
) -> None:
    async with _client(lambda request: response) as client:
        assert await w.fn("_find_running_{k}_terminal")(client, "conv_1") is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(409, json={"detail": "some other conflict"}),
        httpx.Response(500, json={"detail": "offline-looking but wrong status"}),
    ],
)
async def test_find_running_terminal_raises_for_unexpected_errors(
    w: _Wrapper, response: httpx.Response
) -> None:
    async with _client(lambda request: response) as client:
        with pytest.raises(click.ClickException, match=f"Failed to fetch {w.label} terminal"):
            await w.fn("_find_running_{k}_terminal")(client, "conv_1")


async def test_wait_for_terminal_polls_until_it_appears(
    w: _Wrapper, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = iter([None, None, _launched(w)])
    polls: list[str] = []

    async def fake_find(client: object, session_id: str) -> Any:
        polls.append(session_id)
        return next(results)

    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(w.module, f"_find_running_{w.key}_terminal", fake_find)
    monkeypatch.setattr(
        w.module,
        "asyncio",
        SimpleNamespace(**(vars(asyncio) | {"sleep": no_sleep})),
    )

    terminal = await w.fn("_wait_for_{k}_terminal_ready")(object(), "conv_1", timeout_s=30.0)

    assert terminal.terminal_id == "terminal_main"
    assert polls == ["conv_1"] * 3


async def test_wait_for_terminal_times_out(w: _Wrapper, monkeypatch: pytest.MonkeyPatch) -> None:
    async def never(client: object, session_id: str) -> None:
        return None

    monkeypatch.setattr(w.module, f"_find_running_{w.key}_terminal", never)

    with pytest.raises(click.ClickException) as excinfo:
        await w.fn("_wait_for_{k}_terminal_ready")(object(), "conv_1", timeout_s=0.0)

    assert (
        f"The runner did not create the {w.label} terminal for 'conv_1'" in excinfo.value.message
    )


# ── daemon-backed session preparation ────────────────────────


class _Recorder:
    """Records daemon interactions and serves scripted answers."""

    def __init__(self, w: _Wrapper) -> None:
        self.w = w
        self.events: list[tuple[Any, ...]] = []
        self.session_payload: dict[str, object] = {
            "labels": {w.module._WRAPPER_LABEL_KEY: w.module._WRAPPER_LABEL_VALUE}
        }
        self.running: Any = None
        self.patch_status = 200
        self.progress: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        m, k = self.w.module, self.w.key
        rec = self

        class _Client:
            async def patch(self, url: str, json: dict[str, object]) -> httpx.Response:
                rec.events.append(("patch", url, json))
                return httpx.Response(rec.patch_status, json={"detail": "nope"})

        @contextlib.asynccontextmanager
        async def open_client(*args: object, **kwargs: object) -> AsyncIterator[_Client]:
            rec.events.append(("open", args[2]))
            yield _Client()

        async def create(
            client: object, bundle: bytes, *, terminal_launch_args: Any = None
        ) -> str:
            rec.events.append(("create", bundle, terminal_launch_args))
            return "conv_new"

        async def fetch(client: object, session_id: str) -> dict[str, object]:
            rec.events.append(("fetch", session_id))
            return rec.session_payload

        async def find(client: object, session_id: str) -> Any:
            rec.events.append(("find", session_id))
            return rec.running

        async def host_online(client: object, host_id: str, *, timeout_s: float) -> None:
            rec.events.append(("host_online", host_id))

        async def launch(client: object, **kwargs: Any) -> str:
            rec.events.append(
                ("launch", kwargs["session_id"], kwargs["workspace"], kwargs["fresh"])
            )
            return "runner_1"

        async def runner_online(client: object, runner_id: str, *, timeout_s: float) -> None:
            rec.events.append(("runner_online", runner_id))

        async def bind(client: object, session_id: str, runner_id: str) -> None:
            rec.events.append(("bind", session_id, runner_id))

        async def ensure(client: object, session_id: str) -> None:
            rec.events.append(("ensure", session_id))

        async def ready(client: object, session_id: str, *, timeout_s: float) -> Any:
            rec.events.append(("ready", session_id))
            return _launched(self.w, "terminal_ready")

        monkeypatch.setattr(m, "open_daemon_client", open_client)
        monkeypatch.setattr(m, f"_create_{k}_session", create)
        monkeypatch.setattr(m, f"_fetch_{k}_session", fetch)
        monkeypatch.setattr(m, f"_find_running_{k}_terminal", find)
        monkeypatch.setattr(m, "wait_for_host_online", host_online)
        monkeypatch.setattr(m, "launch_or_reuse_daemon_runner", launch)
        monkeypatch.setattr(m, "wait_for_runner_online", runner_online)
        monkeypatch.setattr(m, "_bind_session_runner", bind)
        monkeypatch.setattr(m, f"_ensure_{k}_terminal_on_runner", ensure)
        monkeypatch.setattr(m, f"_wait_for_{k}_terminal_ready", ready)

    async def prepare(self, **overrides: Any) -> Any:
        kwargs: dict[str, Any] = {
            "base_url": "http://server.test",
            "headers": {},
            "session_id": None,
            "session_bundle": b"bundle",
            f"{self.w.key}_args": (),
            "host_id": "host_1",
            "workspace": "/work",
            "startup_progress": SimpleNamespace(update=self.progress.append),
        }
        kwargs.update(overrides)
        return await self.w.fn("_prepare_{k}_terminal_via_daemon")(**kwargs)

    def names(self) -> list[str]:
        return [event[0] for event in self.events]


@pytest.fixture()
def daemon(w: _Wrapper, monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorder = _Recorder(w)
    recorder.install(monkeypatch)
    return recorder


async def test_prepare_fresh_session_creates_launches_and_waits(
    w: _Wrapper, daemon: _Recorder
) -> None:
    prepared = await daemon.prepare(**{f"{w.key}_args": ("--fast",)})

    assert prepared.session_id == "conv_new" and prepared.terminal_id == "terminal_ready"
    assert prepared.reattached is False
    assert ("create", b"bundle", ["--fast"]) in daemon.events
    # A brand-new session skips the runner-binding read and the second host wait.
    assert ("launch", "conv_new", "/work", True) in daemon.events
    assert daemon.names().count("host_online") == 1
    assert daemon.names()[-5:] == ["launch", "runner_online", "bind", "ensure", "ready"]
    assert daemon.progress[-1] == f"{w.label} terminal ready."


async def test_prepare_fresh_session_without_launch_args_persists_none(
    w: _Wrapper, daemon: _Recorder
) -> None:
    await daemon.prepare()

    assert ("create", b"bundle", None) in daemon.events


async def test_prepare_fresh_session_requires_a_bundle(w: _Wrapper, daemon: _Recorder) -> None:
    with pytest.raises(click.ClickException, match=f"Creating a {w.label} session requires"):
        await daemon.prepare(session_bundle=None)


async def test_prepare_rejects_sessions_from_other_wrappers(
    w: _Wrapper, daemon: _Recorder
) -> None:
    daemon.session_payload = {"labels": {w.module._WRAPPER_LABEL_KEY: "someone-else"}}

    with pytest.raises(click.ClickException, match=f"is not a {w.key}-native session"):
        await daemon.prepare(session_id="conv_1")


async def test_prepare_rejects_sessions_without_labels(w: _Wrapper, daemon: _Recorder) -> None:
    daemon.session_payload = {}

    with pytest.raises(click.ClickException, match="is not a"):
        await daemon.prepare(session_id="conv_1")


async def test_prepare_reattaches_to_a_live_terminal(
    w: _Wrapper, daemon: _Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    daemon.running = _launched(w, "terminal_live")

    prepared = await daemon.prepare(session_id="conv_1", **{f"{w.key}_args": ("--x",)})

    assert prepared.reattached is True and prepared.terminal_id == "terminal_live"
    assert "launch" not in daemon.names() and "patch" not in daemon.names()
    assert (
        f"Ignoring {w.label} launch args for an already-running terminal"
        in capsys.readouterr().err
    )


async def test_prepare_reattach_without_args_stays_quiet(
    w: _Wrapper, daemon: _Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    daemon.running = _launched(w)

    await daemon.prepare(session_id="conv_1")

    assert capsys.readouterr().err == ""


async def test_prepare_relaunches_an_exited_terminal(w: _Wrapper, daemon: _Recorder) -> None:
    prepared = await daemon.prepare(session_id="conv_1")

    assert prepared.terminal_id == "terminal_ready"
    if w.cold_resume:
        assert prepared.cold_resumed is True and prepared.reattached is False
    else:
        assert prepared.reattached is w.reattached_on_resume
        assert not hasattr(prepared, "cold_resumed")
    # A resumed session waits for its host and reads the runner binding (fresh=False).
    assert ("launch", "conv_1", "/work", False) in daemon.events
    assert daemon.names().count("host_online") == 1
    assert "patch" not in daemon.names() and "create" not in daemon.names()


async def test_prepare_persists_new_launch_args_when_relaunching(
    w: _Wrapper, daemon: _Recorder
) -> None:
    await daemon.prepare(session_id="conv_1", **{f"{w.key}_args": ("--new",)})

    assert ("patch", "/v1/sessions/conv_1", {"terminal_launch_args": ["--new"]}) in daemon.events
    assert f"Updating {w.label} session..." in daemon.progress


async def test_prepare_surfaces_launch_arg_update_failures(w: _Wrapper, daemon: _Recorder) -> None:
    daemon.patch_status = 500

    with pytest.raises(click.ClickException) as excinfo:
        await daemon.prepare(session_id="conv_1", **{f"{w.key}_args": ("--new",)})

    assert f"{w.label} session launch config update failed (500): nope" in excinfo.value.message
    assert "launch" not in daemon.names()


# ── run_<x>_native / _run_with_remote_server ─────────────────


def test_run_native_requires_a_server(w: _Wrapper, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(w.module, "_preflight_local_tools", lambda: None)

    with pytest.raises(click.ClickException, match="requires a resolved Omnigent server URL"):
        w.fn("run_{k}_native")(server=None, session_id=None)


def test_run_native_checks_tmux_before_anything_else(
    w: _Wrapper, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(w.module.shutil, "which", lambda name: None)

    with pytest.raises(click.ClickException, match="tmux was not found"):
        w.fn("run_{k}_native")(server="http://s", session_id=None)


def test_run_native_generates_spec_and_delegates(
    w: _Wrapper, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(w.module, "_preflight_local_tools", lambda: None)
    calls: list[dict[str, Any]] = []

    def fake_remote(base_url: str, spec_path: Path, **kwargs: Any) -> None:
        calls.append({"base_url": base_url, "spec_exists": spec_path.exists(), **kwargs})

    monkeypatch.setattr(w.module, "_run_with_remote_server", fake_remote)

    w.fn("run_{k}_native")(
        server="http://server.test///",
        session_id="conv_1",
        extra_args=("--a", "--b"),
        resume_picker=True,
        auto_open_conversation=True,
    )

    (call,) = calls
    assert call["base_url"] == "http://server.test"
    assert call["spec_exists"] is True
    assert call["session_id"] == "conv_1" and call["resume_picker"] is True
    assert call["auto_open_conversation"] is True
    assert call[f"{w.key}_args"] == ("--a", "--b")


def test_run_native_accepts_the_deprecated_args_alias(
    w: _Wrapper, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(w.module, "_preflight_local_tools", lambda: None)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        w.module, "_run_with_remote_server", lambda base_url, spec_path, **kw: calls.append(kw)
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        w.fn("run_{k}_native")(server="http://s", session_id=None, **{f"{w.key}_args": ("--old",)})

    assert calls[0][f"{w.key}_args"] == ("--old",)
    assert any(issubclass(item.category, DeprecationWarning) for item in caught)


@dataclass
class _RemoteHarness:
    events: list[tuple[Any, ...]]
    prepared: Any


@pytest.fixture()
def remote(w: _Wrapper, monkeypatch: pytest.MonkeyPatch) -> _RemoteHarness:
    """Stub every collaborator of ``_run_with_remote_server``."""
    m = w.module
    events: list[tuple[Any, ...]] = []
    prepared = replace(_prepared(w, Path("/tmp/s"), "t:0"), session_id="conv_prepared")

    monkeypatch.setattr("omnigent.chat._remote_headers", lambda **kw: {"X-Auth": "1"})
    monkeypatch.setattr("omnigent.chat._bundle_agent", lambda path: b"bundled")
    monkeypatch.setattr(
        "omnigent.cli._ensure_host_daemon", lambda url: events.append(("daemon", url))
    )
    monkeypatch.setattr(
        "omnigent.host.identity.load_or_create_host_identity",
        lambda: SimpleNamespace(host_id="host_1"),
    )

    @contextlib.contextmanager
    def progress(*, initial_message: str) -> Any:
        events.append(("progress", initial_message))
        yield SimpleNamespace(update=lambda message: None)

    async def fake_prepare(**kwargs: Any) -> Any:
        events.append(
            ("prepare", kwargs["session_id"], kwargs["session_bundle"], kwargs["host_id"])
        )
        return prepared

    async def fake_attach(p: Any) -> None:
        events.append(("attach", p.session_id))

    monkeypatch.setattr(m, "runner_startup_progress", progress)
    monkeypatch.setattr(m, f"_prepare_{w.key}_terminal_via_daemon", fake_prepare)
    monkeypatch.setattr(m, "_attach_terminal_resource", fake_attach)
    monkeypatch.setattr(m, "conversation_url", lambda base, sid: f"{base}/c/{sid}")
    monkeypatch.setattr(
        m, "open_conversation_link_if_enabled", lambda **kw: events.append(("open", kw["enabled"]))
    )
    monkeypatch.setattr(
        m, "echo_native_resume_hint", lambda **kw: events.append(("hint", kw["native_command"]))
    )
    if hasattr(m, "echo_native_cold_resume_hint"):
        monkeypatch.setattr(
            m,
            "echo_native_cold_resume_hint",
            lambda **kw: events.append(("cold_hint", kw["agent_label"])),
        )
    return _RemoteHarness(events, prepared)


def _run_remote(w: _Wrapper, **overrides: Any) -> None:
    kwargs: dict[str, Any] = {
        "session_id": None,
        "resume_picker": False,
        f"{w.key}_args": (),
        "auto_open_conversation": False,
    }
    kwargs.update(overrides)
    w.module._run_with_remote_server("http://server.test", Path("/spec.yaml"), **kwargs)


def test_remote_new_session_prints_url_attaches_and_hints_resume(
    w: _Wrapper, remote: _RemoteHarness, capsys: pytest.CaptureFixture[str]
) -> None:
    _run_remote(w, auto_open_conversation=True)

    assert "Web UI: http://server.test/c/conv_prepared" in capsys.readouterr().err
    assert ("prepare", None, b"bundled", "host_1") in remote.events
    assert ("open", True) in remote.events
    assert remote.events[-2:] == [("attach", "conv_prepared"), ("hint", w.key)]


def test_remote_resumed_session_skips_bundle_and_resume_hint(
    w: _Wrapper, remote: _RemoteHarness
) -> None:
    _run_remote(w, session_id="conv_old")

    assert ("prepare", "conv_old", None, "host_1") in remote.events
    assert not any(event[0] == "hint" for event in remote.events)


def test_remote_cold_resume_hint_only_for_wrappers_that_track_it(
    w: _Wrapper, remote: _RemoteHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = remote.prepared
    if w.cold_resume:
        prepared = replace(prepared, cold_resumed=True)

    async def prepare(**kwargs: Any) -> Any:
        return prepared

    monkeypatch.setattr(w.module, f"_prepare_{w.key}_terminal_via_daemon", prepare)

    _run_remote(w, session_id="conv_old")

    hinted = ("cold_hint", w.label) in remote.events
    assert hinted is w.cold_resume


def test_remote_picker_cancel_returns_without_launching(
    w: _Wrapper, remote: _RemoteHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(w.module, "_resolve_session_id_for_resume", lambda **kw: None)

    _run_remote(w, resume_picker=True)

    assert remote.events == []


def test_remote_connection_failure_names_the_server(
    w: _Wrapper, remote: _RemoteHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unreachable(**kwargs: Any) -> Any:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(w.module, f"_prepare_{w.key}_terminal_via_daemon", unreachable)

    with pytest.raises(
        click.ClickException, match=r"Could not reach the omnigent server at http://server\.test"
    ):
        _run_remote(w)


# ── pi-specific behavior ─────────────────────────────────────


@pytest.fixture()
def pi() -> ModuleType:
    return importlib.import_module("omnigent.harnesses.pi_native.main")


def test_pi_prefers_canonical_path_env_over_legacy(pi: ModuleType) -> None:
    env = {"OMNIGENT_PI_PATH": " /new/pi ", "HARNESS_PI_PATH": "/old/pi"}

    assert pi._configured_pi_command(env) == "/new/pi"


def test_pi_falls_back_to_deprecated_env_with_a_warning(
    pi: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    warned: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "omnigent.harness_startup_config._warn_legacy_path",
        lambda legacy, canonical: warned.append((legacy, canonical)),
    )

    assert pi._configured_pi_command({"HARNESS_PI_PATH": " /old/pi "}) == "/old/pi"
    assert warned == [("HARNESS_PI_PATH", "OMNIGENT_PI_PATH")]
    assert pi._configured_pi_command({}) == "pi"
    assert warned == [("HARNESS_PI_PATH", "OMNIGENT_PI_PATH")]


class _Completed:
    def __init__(self, stdout: str = "", stderr: str = "") -> None:
        self.stdout, self.stderr = stdout, stderr


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected"),
    [
        ("pi 0.79.10\n", "", (0, 79, 10)),
        ("", "@earendil-works/pi-coding-agent v1.2.3", (1, 2, 3)),
        ("no version here", "", None),
        ("", "", None),
    ],
)
def test_pi_version_parses_cli_output(
    pi: ModuleType, monkeypatch: pytest.MonkeyPatch, stdout: str, stderr: str, expected: object
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> _Completed:
        calls.append(argv)
        return _Completed(stdout, stderr)

    monkeypatch.setattr("subprocess.run", fake_run)

    assert pi.pi_version("/bin/pi") == expected
    assert calls == [["/bin/pi", "--version"]]


def test_pi_version_is_none_when_the_probe_fails(
    pi: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(argv: list[str], **kwargs: object) -> None:
        raise OSError("not executable")

    monkeypatch.setattr("subprocess.run", failing)

    assert pi.pi_version("/bin/pi") is None


@pytest.mark.parametrize(
    ("version", "supported"),
    [
        ((0, 79, 0), True),
        ((0, 79, 10), True),
        ((1, 0, 0), True),
        ((0, 78, 99), False),
        (None, False),
    ],
)
def test_pi_supports_approve_from_version(
    pi: ModuleType, monkeypatch: pytest.MonkeyPatch, version: object, supported: bool
) -> None:
    monkeypatch.setattr(pi, "pi_version", lambda executable: version)

    assert pi.pi_supports_approve("/bin/pi") is supported


def test_pi_bridge_dir_is_stable_per_session(pi: ModuleType) -> None:
    first = pi.pi_bridge_dir_for_session("conv_1")

    assert first == pi.pi_bridge_dir_for_session("conv_1")
    assert first != pi.pi_bridge_dir_for_session("conv_2")
