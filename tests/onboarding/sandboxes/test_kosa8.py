"""Tests for the built-in kosa8 sandbox provider (REST API faked with httpx)."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from pathlib import Path

import click
import httpx
import pytest

from omnigent.onboarding.sandboxes import kosa8 as kosa8_module
from omnigent.onboarding.sandboxes.base import (
    DEFAULT_HOST_IMAGE,
    SandboxGoneError,
    SandboxLauncher,
)
from omnigent.onboarding.sandboxes.kosa8 import (
    DEFAULT_LABELS,
    Kosa8SandboxLauncher,
    sandbox_name_for,
)

API_KEY = "k8_test_key_never_shown"
SBX = "sbx_test123"


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(kosa8_module, "time", clock)
    return clock


def _frames(*frames: dict[str, object]) -> bytes:
    return b"".join(json.dumps(frame).encode() + b"\n" for frame in frames)


def _out(text: str | bytes, stream: str = "stdout") -> dict[str, object]:
    data = text.encode() if isinstance(text, str) else text
    return {"stream": stream, "data": base64.b64encode(data).decode()}


def _done(code: int = 0) -> dict[str, object]:
    return {"done": True, "exit": code}


class _FakeKosa8:
    """A small in-memory stand-in for the kosa8 Cloud API."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.states: list[str] = ["running"]
        self.exec_answers: list[httpx.Response] = []
        self.status_override: dict[tuple[str, str], httpx.Response] = {}
        self.files: dict[str, bytes] = {}
        self.commands: list[list[str]] = []

    def state(self) -> str:
        return self.states[0] if len(self.states) == 1 else self.states.pop(0)

    def sandbox(self, state: str | None = None) -> httpx.Response:
        return httpx.Response(200, json={"sandbox": {"id": SBX, "state": state or self.state()}})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["authorization"] == f"Bearer {API_KEY}"
        path = request.url.path
        override = self.status_override.get((request.method, path))
        if override is not None:
            return override
        if request.method == "POST" and path == "/v1/sandboxes":
            return httpx.Response(202, json={"sandbox": {"id": SBX, "state": self.state()}})
        if path == f"/v1/sandboxes/{SBX}":
            if request.method == "GET":
                return self.sandbox()
            if request.method == "DELETE":
                return httpx.Response(200, json={"deleted": True})
            if request.method == "PATCH":
                return self.sandbox()
        if request.method == "POST" and path == f"/v1/sandboxes/{SBX}/sleep":
            return self.sandbox("sleeping")
        if request.method == "POST" and path == f"/v1/sandboxes/{SBX}/wake":
            return self.sandbox("running")
        if request.method == "PUT" and path == f"/v1/sandboxes/{SBX}/files":
            self.files[request.url.params["path"]] = request.content
            return httpx.Response(200, json={"ok": True})
        if request.method == "POST" and path == f"/v1/sandboxes/{SBX}/exec":
            body = json.loads(request.content)
            assert body["stream"] is True
            self.commands.append(body["cmd"])
            if self.exec_answers:
                return self.exec_answers.pop(0)
            return httpx.Response(200, content=_frames(_done(0)))
        return httpx.Response(404, json={"error": "not_found", "message": "no such sandbox"})

    def calls(self, method: str, suffix: str = "") -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path.endswith(suffix)]


def _launcher(
    monkeypatch: pytest.MonkeyPatch,
    fake: Callable[[httpx.Request], httpx.Response],
    **kwargs: object,
) -> Kosa8SandboxLauncher:
    monkeypatch.setenv("KOSA8_API_KEY", API_KEY)
    monkeypatch.delenv("KOSA8_API_URL", raising=False)
    monkeypatch.delenv("OMNIGENT_KOSA8_HOST_IMAGE", raising=False)
    monkeypatch.delenv("OMNIGENT_KOSA8_SANDBOX_ENV", raising=False)
    return Kosa8SandboxLauncher(_transport=httpx.MockTransport(fake), **kwargs)  # type: ignore[arg-type]


# ── construction ───────────────────────────────────────


def test_capabilities() -> None:
    capabilities = Kosa8SandboxLauncher().capabilities

    assert capabilities.cli_bootstrap is True
    assert capabilities.managed_launch is True
    assert capabilities.resume_stopped is True
    assert capabilities.snapshot_restore is True
    assert capabilities.programmatic_terminate is True
    assert capabilities.file_copy is True
    assert capabilities.streaming_exec is True
    assert capabilities.foreground_exec is True
    assert capabilities.local_port_forward is False
    # A woken sandbox keeps a host on a revoked token; start_host replaces it.
    assert capabilities.resume_preserves_host is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"size": "3x6"},
        {"snapshot": "omnigent-host"},
        {"snapshot": "snap_0123456789abcdef", "size": "4x8"},
        {"snapshot": "snap_0123456789abcdef", "image": "registry.example.com/host:1"},
        {"snapshot": "snap_0123456789abcdef", "region": "eu"},
        {"labels": {"-bad": "x"}},
        {"labels": {"team": "x" * 257}},
        {"labels": {f"k{i}": "v" for i in range(16)}},
    ],
)
def test_constructor_rejects_invalid_config(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        Kosa8SandboxLauncher(**kwargs)  # type: ignore[arg-type]


def test_sandbox_name_fits_kosa8_rule() -> None:
    name = sandbox_name_for("Managed A1B2/" + "x" * 80)

    assert name.startswith("omnigent-managed-a1b2-")
    assert len(name) <= 64
    assert all(ch.isalnum() or ch in "._-" for ch in name)


def test_prepare_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KOSA8_API_KEY", raising=False)

    with pytest.raises(click.ClickException, match="KOSA8_API_KEY"):
        Kosa8SandboxLauncher().prepare()


# ── provision ──────────────────────────────────────────


def test_provision_boots_host_image_and_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKosa8()
    fake.states = ["creating", "creating", "running"]
    fake.exec_answers = [
        httpx.Response(409, json={"error": "starting", "message": "still starting"}),
        httpx.Response(200, content=_frames(_done(0))),
    ]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "llm-secret")
    launcher = _launcher(monkeypatch, fake, env=["ANTHROPIC_API_KEY"], labels={"team": "infra"})

    assert launcher.provision("managed-a1b2c3d4") == SBX

    (create,) = fake.calls("POST", "/v1/sandboxes")
    body = json.loads(create.content)
    assert body["image"] == DEFAULT_HOST_IMAGE
    assert body["persistent"] is True
    assert body["sleep_after_s"] == 0
    assert body["env"] == {"ANTHROPIC_API_KEY": "llm-secret"}
    assert body["labels"] == {**DEFAULT_LABELS, "team": "infra"}
    assert body["name"].startswith("omnigent-managed-a1b2c3d4-")
    assert "snapshot" not in body and "size" not in body
    # Readiness probe retried past the 409, and the env never rides in argv.
    assert fake.commands == [["true"], ["true"]]
    assert not fake.calls("DELETE")


def test_provision_from_snapshot_sends_only_the_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeKosa8()
    launcher = _launcher(monkeypatch, fake, snapshot="snap_0123456789abcdef")

    launcher.provision("managed-x")

    body = json.loads(fake.calls("POST", "/v1/sandboxes")[0].content)
    assert body["snapshot"] == "snap_0123456789abcdef"
    assert "image" not in body and "persistent" not in body


def test_provision_sends_size_region_and_image_override(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKosa8()
    launcher = _launcher(monkeypatch, fake, size="8x16", region="eu-hel1")
    monkeypatch.setenv("OMNIGENT_KOSA8_HOST_IMAGE", "registry.example.com/host:2")

    launcher.provision("managed-x")

    body = json.loads(fake.calls("POST", "/v1/sandboxes")[0].content)
    assert (body["image"], body["size"], body["region"]) == (
        "registry.example.com/host:2",
        "8x16",
        "eu-hel1",
    )


def test_provision_deletes_a_sandbox_that_fails_to_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKosa8()
    fake.states = ["creating", "error"]
    launcher = _launcher(monkeypatch, fake)

    with pytest.raises(click.ClickException, match="did not start"):
        launcher.provision("managed-x")

    assert len(fake.calls("DELETE", f"/v1/sandboxes/{SBX}")) == 1


def test_provision_times_out_and_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKosa8()
    fake.states = ["creating"]
    launcher = _launcher(monkeypatch, fake)

    with pytest.raises(click.ClickException, match="still creating"):
        launcher.provision("managed-x")

    assert len(fake.calls("DELETE")) == 1


def test_provision_reports_create_error_without_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKosa8()
    fake.status_override[("POST", "/v1/sandboxes")] = httpx.Response(
        402, json={"error": "payment_required", "message": "add a plan"}
    )
    launcher = _launcher(monkeypatch, fake)

    with pytest.raises(click.ClickException) as excinfo:
        launcher.provision("managed-x")

    message = excinfo.value.message
    assert "add a plan (HTTP 402, payment_required)" in message
    assert API_KEY not in message


def test_provision_requires_passthrough_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKosa8()
    launcher = _launcher(monkeypatch, fake)
    monkeypatch.setenv("OMNIGENT_KOSA8_SANDBOX_ENV", "MISSING_FOR_TEST")
    monkeypatch.delenv("MISSING_FOR_TEST", raising=False)

    with pytest.raises(click.ClickException, match="MISSING_FOR_TEST"):
        launcher.provision("managed-x")

    assert not fake.calls("POST", "/v1/sandboxes")


# ── exec ───────────────────────────────────────────────


def test_run_decodes_streamed_output(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKosa8()
    snowman = "☃".encode()
    fake.exec_answers = [
        httpx.Response(
            200,
            content=_frames(
                _out(b"hi " + snowman[:1]),
                {"stream": "stdout", "data": "", "heartbeat": True},
                _out(snowman[1:] + b"\n"),
                _out("warn\n", "stderr"),
                _done(3),
            ),
        )
    ]
    launcher = _launcher(monkeypatch, fake)

    result = launcher.run(SBX, "echo hi", check=False)

    assert (result.returncode, result.stdout, result.stderr) == (3, "hi ☃\n", "warn\n")
    assert fake.commands == [["bash", "-lc", "echo hi"]]


def test_run_check_raises_on_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKosa8()
    fake.exec_answers = [httpx.Response(200, content=_frames(_done(1)))]

    with pytest.raises(click.ClickException, match="exit 1"):
        _launcher(monkeypatch, fake).run(SBX, "false")


def test_run_raises_on_error_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKosa8()
    fake.exec_answers = [
        httpx.Response(200, content=_frames({"done": True, "error": "host restarted"}))
    ]

    with pytest.raises(click.ClickException, match="host restarted"):
        _launcher(monkeypatch, fake).run(SBX, "true")


def test_run_raises_on_truncated_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKosa8()
    fake.exec_answers = [httpx.Response(200, content=_frames(_out("partial")))]

    with pytest.raises(click.ClickException, match="without a final frame"):
        _launcher(monkeypatch, fake).run(SBX, "true")


def test_run_on_missing_sandbox_raises_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKosa8()

    with pytest.raises(SandboxGoneError):
        _launcher(monkeypatch, fake).run("sbx_missing", "true")


def test_stream_exec_yields_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKosa8()
    fake.exec_answers = [
        httpx.Response(
            200, content=_frames(_out("a\nb"), _out("c\n", "stderr"), _out("tail"), _done(0))
        )
    ]

    process = _launcher(monkeypatch, fake).stream_exec(SBX, "run")

    assert list(process.lines) == ["a\n", "bc\n", "tail\n"]
    assert process.wait() == 0


def test_wheel_install_uses_host_image_overlay() -> None:
    command = Kosa8SandboxLauncher().wheel_install_command("/tmp/oa-wheels.tgz")

    assert "--force-reinstall" in command and "/tmp/oa-wheels.tgz" in command


# ── files ──────────────────────────────────────────────


def test_put_small_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _FakeKosa8()
    local = tmp_path / "w.tgz"
    local.write_bytes(b"payload")

    _launcher(monkeypatch, fake).put(SBX, local, "/workspace/oa-wheels.tgz")

    assert fake.files == {"/workspace/oa-wheels.tgz": b"payload"}
    assert fake.commands == []


def test_put_outside_the_workspace_is_staged_and_moved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The files API reaches /workspace only, so /tmp files go there first."""
    fake = _FakeKosa8()
    monkeypatch.setattr(kosa8_module.secrets, "token_hex", lambda n: "abc")
    local = tmp_path / "w.tgz"
    local.write_bytes(b"payload")

    _launcher(monkeypatch, fake).put(SBX, local, "/tmp/oa-wheels.tgz")

    assert fake.files == {"/workspace/.omnigent-upload-abc": b"payload"}
    (move,) = fake.commands
    assert move[:2] == ["bash", "-lc"]
    assert move[2].startswith(
        "mkdir -p -- /tmp && mv -f -- /workspace/.omnigent-upload-abc /tmp/oa-wheels.tgz"
    )


def test_put_large_file_in_parts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _FakeKosa8()
    monkeypatch.setattr(kosa8_module, "_UPLOAD_CHUNK_BYTES", 4)
    monkeypatch.setattr(kosa8_module.secrets, "token_hex", lambda n: "abc")
    local = tmp_path / "w.tgz"
    local.write_bytes(b"0123456789")

    _launcher(monkeypatch, fake).put(SBX, local, "/tmp/w.tgz")

    staged = "/workspace/.omnigent-upload-abc"
    assert fake.files == {
        f"{staged}.part0000": b"0123",
        f"{staged}.part0001": b"4567",
        f"{staged}.part0002": b"89",
    }
    (join,) = fake.commands
    assert join[:2] == ["bash", "-lc"]
    assert join[2].startswith(
        f"mkdir -p -- /tmp && cat {staged}.part0000 {staged}.part0001 {staged}.part0002"
        " > /tmp/w.tgz"
    )


# ── lifecycle ──────────────────────────────────────────


def test_stop_sleeps_a_running_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKosa8()

    _launcher(monkeypatch, fake).stop(SBX)

    assert len(fake.calls("POST", "/sleep")) == 1


def test_stop_leaves_a_sleeping_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKosa8()
    fake.states = ["sleeping"]

    _launcher(monkeypatch, fake).stop(SBX)

    assert not fake.calls("POST", "/sleep")


@pytest.mark.parametrize("method", ["stop", "resume"])
def test_stop_and_resume_raise_gone_for_missing_sandbox(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    fake = _FakeKosa8()
    fake.states = ["deleted"]
    launcher = _launcher(monkeypatch, fake)

    with pytest.raises(SandboxGoneError):
        getattr(launcher, method)(SBX)
    with pytest.raises(SandboxGoneError):
        getattr(launcher, method)("sbx_missing")


def test_resume_wakes_a_sleeping_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKosa8()
    fake.states = ["sleeping", "running"]

    _launcher(monkeypatch, fake).resume(SBX)

    assert len(fake.calls("POST", "/wake")) == 1


def test_attach_reports_missing_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKosa8()

    with pytest.raises(click.ClickException, match="not found"):
        _launcher(monkeypatch, fake).attach("sbx_missing")


@pytest.mark.parametrize(
    ("state", "expected"),
    [("running", True), ("sleeping", False), ("deleted", False), ("creating", None)],
)
def test_is_running_maps_states(
    monkeypatch: pytest.MonkeyPatch, state: str, expected: bool | None
) -> None:
    fake = _FakeKosa8()
    fake.states = [state]

    assert _launcher(monkeypatch, fake).is_running(SBX) is expected


def test_is_running_unknown_on_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKosa8()
    fake.status_override[("GET", f"/v1/sandboxes/{SBX}")] = httpx.Response(503, json={})
    launcher = _launcher(monkeypatch, fake)

    assert launcher.is_running(SBX) is None
    assert launcher.is_running("sbx_missing") is False


def test_keep_alive_turns_off_idle_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKosa8()

    assert _launcher(monkeypatch, fake).keep_alive(SBX) is True

    (patch,) = fake.calls("PATCH")
    assert json.loads(patch.content) == {"sleep_after_s": 0}


def test_terminate_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKosa8()
    launcher = _launcher(monkeypatch, fake)

    launcher.terminate(SBX)
    launcher.terminate("sbx_missing")

    assert len(fake.calls("DELETE")) == 2


def test_terminate_raises_on_other_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKosa8()
    fake.status_override[("DELETE", f"/v1/sandboxes/{SBX}")] = httpx.Response(
        500, json={"error": "internal", "message": "boom"}
    )

    with pytest.raises(click.ClickException, match="boom"):
        _launcher(monkeypatch, fake).terminate(SBX)


def test_start_host_stops_a_preserved_host_first(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKosa8()
    launcher = _launcher(monkeypatch, fake)
    seen: list[str] = []

    def fake_start_host(self: SandboxLauncher, sandbox_id: str, **kwargs: object) -> str:
        seen.append(sandbox_id)
        return "started"

    monkeypatch.setattr(SandboxLauncher, "start_host", fake_start_host)

    result = launcher.start_host(
        SBX, token="t", host_id="host_1", host_name="h", server_url="https://srv.example.com"
    )

    assert result == "started" and seen == [SBX]
    assert "pkill" in fake.commands[0][2]


def test_api_url_override(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeKosa8()
    launcher = _launcher(monkeypatch, fake)
    monkeypatch.setenv("KOSA8_API_URL", "https://kosa8.example.com/")

    launcher.terminate(SBX)

    assert fake.requests[0].url.host == "kosa8.example.com"
