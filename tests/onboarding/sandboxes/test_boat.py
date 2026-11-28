"""Boat transport and lifecycle tests using the documented HTTP contract."""

import base64
from pathlib import Path

import click
import httpx
import pytest

from omnigent.onboarding.sandboxes.base import SandboxGoneError
from omnigent.onboarding.sandboxes.boat import BoatSandboxLauncher
from omnigent.onboarding.sandboxes.registry import instantiate
from omnigent.server.managed_hosts import parse_sandbox_config


def _patch_request(monkeypatch: pytest.MonkeyPatch, fn) -> None:
    monkeypatch.setattr(httpx.Client, "request", lambda self, *a, **kw: fn(*a, **kw))


def test_managed_configuration() -> None:
    config = parse_sandbox_config({"provider": "boat", "server_url": "https://omni.example"})
    assert config is not None and config.managed_launch_supported
    assert isinstance(config.default.launcher_factory(), BoatSandboxLauncher)
    assert isinstance(instantiate("boat"), BoatSandboxLauncher)
    with pytest.raises(ValueError):
        parse_sandbox_config(
            {"provider": "boat", "server_url": "https://omni.example", "boat": {"typo": True}}
        )


def test_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOAT_API_KEY", raising=False)
    with pytest.raises(click.ClickException, match="BOAT_API_KEY"):
        BoatSandboxLauncher().prepare()


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_errors_do_not_disclose_secrets(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    monkeypatch.setenv("BOAT_API_KEY", "test-secret")
    _patch_request(
        monkeypatch, lambda *a, **kw: httpx.Response(status, json={"message": "test-secret"})
    )
    with pytest.raises(click.ClickException, match=f"HTTP {status}") as error:
        BoatSandboxLauncher().run("bx_test", "secret-command")
    assert "test-secret" not in str(error.value)
    assert "secret-command" not in str(error.value)


def test_missing_resume_and_idempotent_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOAT_API_KEY", "test-secret")
    _patch_request(monkeypatch, lambda *a, **kw: httpx.Response(404))
    launcher = BoatSandboxLauncher()
    with pytest.raises(SandboxGoneError):
        launcher.resume("missing")
    launcher.terminate("missing")
    assert not launcher.is_running("missing")


@pytest.mark.parametrize(
    "code,timed_out,check,raises",
    [
        (0, False, True, False),
        (2, False, False, False),
        (2, False, True, True),
        (None, True, False, True),
    ],
)
def test_command_results(
    monkeypatch: pytest.MonkeyPatch, code: int | None, timed_out: bool, check: bool, raises: bool
) -> None:
    monkeypatch.setenv("BOAT_API_KEY", "test-secret")
    _patch_request(
        monkeypatch,
        lambda *a, **kw: httpx.Response(
            200,
            json={
                "ok": True,
                "exitCode": code,
                "stdout": "out",
                "stderr": "err",
                "timedOut": timed_out,
            },
        ),
    )
    launcher = BoatSandboxLauncher()
    if raises:
        with pytest.raises(click.ClickException):
            launcher.run("bx_test", "test", check=check)
    else:
        result = launcher.run("bx_test", "test", check=check)
        assert (result.returncode, result.stdout, result.stderr) == (code, "out", "err")


def test_binary_upload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BOAT_API_KEY", "test-secret")
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return httpx.Response(200, json={"ok": True})

    _patch_request(monkeypatch, request)
    source = tmp_path / "binary"
    source.write_bytes(b"\x00\xff")
    BoatSandboxLauncher().put("bx_test", source, "/tmp/binary")
    assert calls[0][0] == "PUT"
    assert base64.b64decode(calls[0][2]["json"]["content"]) == source.read_bytes()


@pytest.mark.parametrize("install_fails", [False, True])
def test_provision_isolated_and_cleanup(
    monkeypatch: pytest.MonkeyPatch, install_fails: bool
) -> None:
    monkeypatch.setenv("BOAT_API_KEY", "test-secret")
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if method == "POST" and url.endswith("/sandboxes"):
            return httpx.Response(202, json={"ok": True, "sandbox": {"id": "bx_test"}})
        if method == "GET":
            return httpx.Response(200, json={"ok": True, "sandbox": {"state": "ready"}})
        if url.endswith("/commands"):
            return httpx.Response(200, json={"ok": True, "exitCode": int(install_fails)})
        return httpx.Response(200, json={"ok": True})

    _patch_request(monkeypatch, request)
    launcher = BoatSandboxLauncher()
    if install_fails:
        with pytest.raises(click.ClickException):
            launcher.provision("managed-test")
        assert calls[-1][0] == "DELETE"
        assert calls[-1][2]["headers"]["X-Ascii-Confirm-Delete"] == "bx_test"
    else:
        assert launcher.provision("managed-test") == "bx_test"
        assert all(c[0] != "DELETE" for c in calls)
    body = calls[0][2]["json"]
    assert body == {"noEnv": True, "ttlSeconds": None, "monitoring": False}
    assert calls[0][2]["headers"]["Idempotency-Key"]


def test_attach_resumes_and_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOAT_API_KEY", "test-secret")
    states = iter(["archived", "archived", "provisioning", "ready"])
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if method == "GET":
            return httpx.Response(200, json={"ok": True, "sandbox": {"state": next(states)}})
        return httpx.Response(202, json={"ok": True})

    _patch_request(monkeypatch, request)
    monkeypatch.setattr("omnigent.onboarding.sandboxes.boat.time.sleep", lambda _: None)
    BoatSandboxLauncher().attach("bx_test")
    assert calls[2][0] == "POST" and calls[2][1].endswith("/resume")
    assert calls[2][2]["json"] == {"noEnv": True, "ttlSeconds": None}
    assert len(calls) == 5


def test_large_upload_uses_signed_url_without_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BOAT_API_KEY", "test-secret")
    calls = []

    def request(method, url, **kwargs):
        assert method == "POST" and url.endswith("/files/upload-url")
        return httpx.Response(200, json={"ok": True, "url": "https://upload.example/file"})

    def upload(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, request=httpx.Request("PUT", url))

    _patch_request(monkeypatch, request)
    monkeypatch.setattr(httpx, "put", upload)
    source = tmp_path / "large"
    source.write_bytes(b"x" * (5 * 1024 * 1024 + 1))
    BoatSandboxLauncher().put("bx_test", source, "/tmp/large")
    assert calls[0][0] == "https://upload.example/file"
    assert "headers" not in calls[0][1]
    assert calls[0][1]["content"] == source.read_bytes()


@pytest.mark.parametrize("state", ["deleted", "cancelled"])
def test_terminal_state_raises_sandbox_gone(monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    monkeypatch.setenv("BOAT_API_KEY", "test-secret")
    _patch_request(
        monkeypatch,
        lambda *a, **kw: httpx.Response(200, json={"ok": True, "sandbox": {"state": state}}),
    )
    launcher = BoatSandboxLauncher()
    with pytest.raises(SandboxGoneError):
        launcher.attach("bx_test")
    with pytest.raises(SandboxGoneError):
        launcher._wait_ready("bx_test")
    with pytest.raises(SandboxGoneError):
        launcher.resume("bx_test")


def test_keep_alive_soft_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BOAT_API_KEY", "test-secret")
    _patch_request(monkeypatch, lambda *a, **kw: httpx.Response(500))
    assert BoatSandboxLauncher().keep_alive("bx_test") is False
    assert "HTTP 500" in capsys.readouterr().err
    _patch_request(monkeypatch, lambda *a, **kw: httpx.Response(200, json={"ok": True}))
    assert BoatSandboxLauncher().keep_alive("bx_test") is True


def test_api_url_override_and_error_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOAT_API_KEY", "test-secret")
    monkeypatch.setenv("BOAT_API_URL", "https://boat.internal/api/v1/")
    urls: list[str] = []

    def request(method: str, url: str, **kw: object) -> httpx.Response:
        urls.append(url)
        return httpx.Response(429, json={"code": "rate_limited", "message": "test-secret"})

    _patch_request(monkeypatch, request)
    with pytest.raises(click.ClickException) as error:
        BoatSandboxLauncher().run("bx_test", "echo hi")
    assert urls == ["https://boat.internal/api/v1/sandboxes/bx_test/commands"]
    assert "HTTP 429 (rate_limited)" in str(error.value)
    assert "test-secret" not in str(error.value)


def test_default_host_package_is_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.metadata

    from omnigent.onboarding.sandboxes.boat import _host_package

    monkeypatch.delenv("OMNIGENT_BOAT_HOST_PACKAGE", raising=False)
    assert _host_package() == f"omnigent=={importlib.metadata.version('omnigent')}"
    monkeypatch.setenv("OMNIGENT_BOAT_HOST_PACKAGE", "omnigent==1.2.3")
    assert _host_package() == "omnigent==1.2.3"


def test_resume_preserves_recoverable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOAT_API_KEY", "test-secret")
    calls = []

    def request(method, url, **kwargs):
        calls.append(method)
        if method == "GET":
            return httpx.Response(200, json={"ok": True, "sandbox": {"state": "error"}})
        return httpx.Response(409, json={"code": "resume_failed"})

    _patch_request(monkeypatch, request)
    with pytest.raises(click.ClickException, match="resume_failed") as error:
        BoatSandboxLauncher().resume("bx_test")
    assert not isinstance(error.value, SandboxGoneError)
    assert calls == ["GET", "POST"]


def test_resume_after_snapshot_expiry_raises_sandbox_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Boat deletes a stopped sandbox's disk after 14 days, then rejects resume with
    409 snapshot_data_expired; that is a definitive absence, so a replacement is provisioned."""
    monkeypatch.setenv("BOAT_API_KEY", "test-secret")
    calls = []

    def request(method, url, **kwargs):
        calls.append(method)
        if method == "GET":
            return httpx.Response(200, json={"ok": True, "sandbox": {"state": "archived"}})
        return httpx.Response(409, json={"ok": False, "code": "snapshot_data_expired"})

    _patch_request(monkeypatch, request)
    with pytest.raises(SandboxGoneError, match="expired"):
        BoatSandboxLauncher().attach("bx_test")
    assert calls == ["GET", "GET", "POST"]


def test_resume_active_sandbox_does_not_restart_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOAT_API_KEY", "test-secret")
    methods = []

    def request(method, url, **kwargs):
        methods.append(method)
        return httpx.Response(200, json={"ok": True, "sandbox": {"state": "running"}})

    _patch_request(monkeypatch, request)
    BoatSandboxLauncher().resume("bx_test")
    assert methods == ["GET", "GET"]


@pytest.mark.parametrize(
    "status,payload,retry",
    [
        (409, {"code": "sandbox_starting"}, True),
        (502, {"code": "sandbox_direct_failed", "error": {"details": {"retryable": True}}}, True),
        (502, {"code": "sandbox_direct_failed", "message": "sandbox_restoring"}, True),
        (
            502,
            {"code": "sandbox_direct_failed", "error": {"details": {"retryable": False}}},
            False,
        ),
        (502, {"code": "sandbox_direct_failed", "message": "connection_lost"}, False),
    ],
)
def test_commands_retry_only_before_execution(
    monkeypatch: pytest.MonkeyPatch, status: int, payload: dict, retry: bool
) -> None:
    monkeypatch.setenv("BOAT_API_KEY", "test-secret")
    calls = []

    def request(method, url, **kwargs):
        calls.append(kwargs["json"]["command"])
        if len(calls) == 1:
            return httpx.Response(status, json=payload)
        return httpx.Response(200, json={"ok": True, "exitCode": 0, "stdout": "once"})

    _patch_request(monkeypatch, request)
    monkeypatch.setattr("omnigent.onboarding.sandboxes.boat.time.sleep", lambda _: None)
    launcher = BoatSandboxLauncher()
    if retry:
        assert launcher.run("bx_test", "side-effect").stdout == "once"
        assert calls == ["side-effect", "side-effect"]
    else:
        with pytest.raises(click.ClickException):
            launcher.run("bx_test", "side-effect")
        assert calls == ["side-effect"]
