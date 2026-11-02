"""Tests for the host-side Databricks credential materializer."""

from __future__ import annotations

import configparser
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import anyio
import httpx
import psutil
import pytest

from omnigent.host import databricks_credential as dc
from omnigent.host.identity import HOST_TOKEN_ENV_VAR


class _Resp:
    def __init__(self, status_code: int, payload: dict | None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # These tests model a managed sandbox host, where IS_SANDBOX=1 is baked into
    # the image; see test_noop_off_sandbox for the local-host (unset) no-op.
    monkeypatch.setenv("IS_SANDBOX", "1")
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "host-tok")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(tmp_path / ".databrickscfg"))
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)


@pytest.fixture
def _credential_env(tmp_path: Path) -> dict[str, str]:
    source = Path(__file__).resolve().parents[2]
    credential_home = tmp_path / "home"
    credential_home.mkdir()
    return {
        "HOME": str(credential_home),
        "PYTHONPATH": os.pathsep.join(
            str(source / entry) for entry in (".", "sdks/python-client", "sdks/ui")
        ),
        "PYTHONDONTWRITEBYTECODE": "1",
        "IS_SANDBOX": "1",
        HOST_TOKEN_ENV_VAR: "host-tok",
        "DATABRICKS_CONFIG_FILE": str(tmp_path / ".databrickscfg"),
        "OMNIGENT_CONFIG_HOME": str(tmp_path / "config"),
        "OMNIGENT_DATA_DIR": str(tmp_path / "data"),
    }


def _patch_get(monkeypatch: pytest.MonkeyPatch, resp_or_exc) -> dict:
    seen: dict = {}

    async def fake_get(self: httpx.AsyncClient, url: str, headers: dict, timeout: float):
        seen["url"] = url
        seen["headers"] = headers
        if isinstance(resp_or_exc, Exception):
            raise resp_or_exc
        return resp_or_exc

    monkeypatch.setattr(dc.httpx.AsyncClient, "get", fake_get)
    # Unit scenarios drive HTTP policy in-process; real boundary scenarios use _fetch.
    monkeypatch.setattr(dc, "_fetch", dc._fetch_in_worker)
    return seen


def _connected(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Patch the broker to return a connected credential; return the seen dict."""
    return _patch_get(
        monkeypatch,
        _Resp(
            200, {"connected": True, "token": "dbx-tok", "workspace_host": "https://ws.example/"}
        ),
    )


def test_writes_host_only_profile_and_sets_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example/", "host-1") is True
    # Broker hit with the host token header, generic provider-keyed URL.
    assert seen["url"] == "https://omni.example/v1/hosts/host-1/credentials/databricks"
    assert seen["headers"]["X-Omnigent-Host-Token"] == "host-tok"
    # Profile written with only the host (trailing slash stripped) — no token.
    cfg = configparser.ConfigParser()
    cfg.read(tmp_path / ".databrickscfg")
    assert cfg["omnigent"]["host"] == "https://ws.example"
    assert "token" not in cfg["omnigent"]
    assert os.environ["DATABRICKS_CONFIG_PROFILE"] == "omnigent"


def test_bearer_is_never_persisted_to_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example", "h") is True
    # The broker returned "dbx-tok"; it must appear nowhere on disk.
    assert "dbx-tok" not in (tmp_path / ".databrickscfg").read_text()
    assert "dbx-tok" not in (tmp_path / dc._SIDECAR_NAME).read_text()


def test_sidecar_records_broker_coords_privately(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example", "host-1") is True
    sidecar = tmp_path / dc._SIDECAR_NAME
    assert json.loads(sidecar.read_text()) == {
        "server": "https://omni.example",
        "host_id": "host-1",
        "host_token": "host-tok",
        "workspace_host": "https://ws.example",
    }
    assert stat.S_IMODE(os.stat(sidecar).st_mode) == 0o600


def test_preserves_existing_profiles(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg_path = tmp_path / ".databrickscfg"
    cfg_path.write_text("[other]\nhost = https://keep.example\ntoken = keep\n")
    _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example", "h") is True
    cfg = configparser.ConfigParser()
    cfg.read(cfg_path)
    assert cfg["other"]["host"] == "https://keep.example"  # untouched
    assert cfg["other"]["token"] == "keep"
    assert cfg["omnigent"]["host"] == "https://ws.example"


def test_replaces_stale_token_from_older_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An older build wrote a static token; the new write must drop it.
    cfg_path = tmp_path / ".databrickscfg"
    cfg_path.write_text("[omnigent]\nhost = https://old.example\ntoken = stale\n")
    _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example", "h") is True
    cfg = configparser.ConfigParser()
    cfg.read(cfg_path)
    assert cfg["omnigent"]["host"] == "https://ws.example"
    assert "token" not in cfg["omnigent"]
    assert "stale" not in cfg_path.read_text()


def test_noop_when_not_connected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_get(monkeypatch, _Resp(200, {"connected": False}))
    assert dc.configure_host_databricks("https://omni.example", "h") is False
    assert "DATABRICKS_CONFIG_PROFILE" not in os.environ
    assert not (tmp_path / dc._SIDECAR_NAME).exists()


def test_noop_without_host_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(HOST_TOKEN_ENV_VAR, raising=False)
    called = _patch_get(monkeypatch, _Resp(200, {"connected": True, "token": "t"}))
    assert dc.configure_host_databricks("https://omni.example", "h") is False
    assert called == {}  # broker never hit


def test_noop_off_sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Local-host safety: a connected owner running ``omnigent host`` on a laptop
    (no IS_SANDBOX) must not get their real ~/.databrickscfg written. This is an
    auto-applied sandbox integration — it no-ops entirely off a managed sandbox,
    even with a valid host token and a connected broker, and never hits the broker."""
    monkeypatch.delenv("IS_SANDBOX", raising=False)
    called = _patch_get(
        monkeypatch,
        _Resp(200, {"connected": True, "token": "t", "workspace_host": "https://ws.example"}),
    )
    assert dc.configure_host_databricks("https://omni.example", "h") is False
    assert called == {}  # broker never hit — pure no-op
    assert not (tmp_path / ".databrickscfg").exists()  # no local footprint
    assert not (tmp_path / dc._SIDECAR_NAME).exists()
    assert "DATABRICKS_CONFIG_PROFILE" not in os.environ


def test_noop_on_broker_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_get(monkeypatch, httpx.ConnectError("down"))
    assert dc.configure_host_databricks("https://omni.example", "h") is False


def test_noop_on_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_get(monkeypatch, _Resp(404, None))
    assert dc.configure_host_databricks("https://omni.example", "h") is False


def test_broker_token_command_none_without_sidecar() -> None:
    assert dc.broker_token_command("https://ws.example") is None


def test_broker_token_command_bakes_sidecar_path_for_the_connected_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example", "h") is True
    # Trailing-slash-insensitive match against the connected workspace host.
    cmd = dc.broker_token_command("https://ws.example/")
    assert cmd is not None
    assert "python3 -m omnigent.host.databricks_credential token" in cmd
    assert str(tmp_path / dc._SIDECAR_NAME) in cmd


def test_broker_token_command_none_for_a_different_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example", "h") is True
    # A gateway pinned to another workspace must not borrow the broker token.
    assert dc.broker_token_command("https://other.example") is None


def test_main_prints_broker_bearer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example", "h") is True
    capsys.readouterr()  # discard configure-time log output
    assert dc.main(["token"]) == 0
    assert capsys.readouterr().out.strip() == "dbx-tok"


def test_main_uses_explicit_coords_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example", "h") is True
    sidecar = tmp_path / dc._SIDECAR_NAME
    capsys.readouterr()  # discard configure-time log output
    assert dc.main(["token", "--coords", str(sidecar)]) == 0
    assert capsys.readouterr().out.strip() == "dbx-tok"


@pytest.mark.parametrize("workspace", ["https://ws.example", "https://ws.example/"])
def test_main_prints_bearer_only_for_the_saved_workspace(
    workspace: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example", "h") is True
    capsys.readouterr()
    assert (
        dc.main(["token", "--coords", str(tmp_path / dc._SIDECAR_NAME), "--workspace", workspace])
        == 0
    )
    assert capsys.readouterr().out.strip() == "dbx-tok"


def test_main_withholds_bearer_when_saved_workspace_differs_from_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    called = _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example", "h") is True
    called.clear()
    capsys.readouterr()
    assert (
        dc.main(
            [
                "token",
                "--coords",
                str(tmp_path / dc._SIDECAR_NAME),
                "--workspace",
                "https://saved-workspace.example",
            ]
        )
        == 1
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert "dbx-tok" not in output.err
    assert called == {}


def test_main_silent_without_sidecar(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    called = _connected(monkeypatch)
    assert dc.main(["token"]) == 0  # no sidecar on disk
    assert capsys.readouterr().out == ""
    assert called == {}  # never reached the broker


def test_main_silent_when_broker_declines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example", "h") is True
    # Owner later disconnects: broker now returns not-connected.
    _patch_get(monkeypatch, _Resp(200, {"connected": False}))
    capsys.readouterr()  # discard configure-time log output
    assert dc.main(["token"]) == 0
    assert capsys.readouterr().out == ""


def test_main_ignores_unknown_operation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example", "h") is True
    capsys.readouterr()  # discard configure-time log output
    assert dc.main(["store"]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("pinned_workspace", [False, True])
def test_main_withholds_token_when_broker_workspace_changed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    pinned_workspace: bool,
) -> None:
    """Reconnect edge: the sidecar pins workspace A, but the owner has since
    reconnected to workspace B, so the broker now vends B's bearer. main() must
    withhold it rather than present B's token to the A-pinned gateway base URL."""
    _connected(monkeypatch)  # sidecar pinned to https://ws.example
    assert dc.configure_host_databricks("https://omni.example", "h") is True
    _patch_get(
        monkeypatch,
        _Resp(
            200,
            {"connected": True, "token": "dbx-tok-B", "workspace_host": "https://ws-b.example"},
        ),
    )
    capsys.readouterr()  # discard configure-time log output
    argv = ["token", "--workspace", "https://ws.example"] if pinned_workspace else ["token"]
    assert dc.main(argv) == 0
    assert capsys.readouterr().out == ""  # withheld: workspace mismatch


def test_https_url_on_workspace_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only HTTPS URLs on the workspace host's netloc are accepted; other schemes,
    other hosts, and empty inputs are refused. A scheme-less workspace host is
    treated as HTTPS."""
    ok = dc.https_url_on_workspace_host
    assert ok("https://ws.example/ai-gateway/anthropic", "https://ws.example") is True
    assert ok("https://ws.example/x", "ws.example") is True  # scheme-less host
    assert ok("http://ws.example/x", "https://ws.example") is False  # not HTTPS
    assert ok("https://evil.example/x", "https://ws.example") is False  # other host
    assert ok("", "https://ws.example") is False
    assert ok("https://ws.example/x", "") is False


def test_api_key_auth_precludes_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit ApiKeyAuth — spec-level OR the global ``auth:`` block — suppresses
    the managed-connect broker fallback. A spec's own explicit key wins; when the
    spec declares no auth (or there is no spec, e.g. pi), the global block decides.
    A DatabricksAuth or no key anywhere lets the broker run.
    """
    from types import SimpleNamespace

    from omnigent.spec.types import ApiKeyAuth, DatabricksAuth

    key_spec = SimpleNamespace(executor=SimpleNamespace(auth=ApiKeyAuth(api_key="sk-x")))
    dbx_spec = SimpleNamespace(executor=SimpleNamespace(auth=DatabricksAuth(profile="p")))
    bare_spec = SimpleNamespace(executor=SimpleNamespace(auth=None))

    # No global auth configured: only a spec-level key precludes.
    monkeypatch.setattr("omnigent.runtime.workflow._load_global_auth", lambda: None)
    assert dc.api_key_auth_precludes_broker(key_spec) is True
    assert dc.api_key_auth_precludes_broker(dbx_spec) is False
    assert dc.api_key_auth_precludes_broker(bare_spec) is False
    assert dc.api_key_auth_precludes_broker(None) is False

    # A GLOBAL ApiKeyAuth must also preclude the broker — including in the spec
    # branch when the spec declares no auth of its own (the reroute bug this guards).
    monkeypatch.setattr(
        "omnigent.runtime.workflow._load_global_auth", lambda: ApiKeyAuth(api_key="sk-g")
    )
    assert dc.api_key_auth_precludes_broker(None) is True
    assert dc.api_key_auth_precludes_broker(bare_spec) is True
    # A spec-level DatabricksAuth is authoritative and is not a key, so the broker
    # still runs even when a global key exists.
    assert dc.api_key_auth_precludes_broker(dbx_spec) is False


def _scripted_get(monkeypatch: pytest.MonkeyPatch, steps: list) -> list[int]:
    """Serve *steps* in order (exceptions raised, responses returned); return call count."""
    calls: list[int] = []

    async def fake_get(self: httpx.AsyncClient, url: str, headers: dict, timeout: float):
        calls.append(1)
        step = steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    monkeypatch.setattr(dc.httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(dc, "_fetch", dc._fetch_in_worker)
    return calls


_BEARER_OK = _Resp(
    200, {"connected": True, "token": "dbx-tok", "workspace_host": "https://ws.example/"}
)


def test_main_retries_transient_broker_failures(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A connection blip or 503 must not empty the bearer and fail the turn."""
    _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example", "h") is True
    capsys.readouterr()  # discard configure-time log output
    monkeypatch.setattr(dc, "_RETRY_BACKOFF_S", (0.0, 0.0))
    calls = _scripted_get(monkeypatch, [httpx.ConnectError("reset"), _Resp(503, None), _BEARER_OK])
    assert dc.main(["token"]) == 0
    assert capsys.readouterr().out.strip() == "dbx-tok"
    assert len(calls) == 3


def test_main_gives_up_after_retries_are_exhausted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example", "h") is True
    capsys.readouterr()
    monkeypatch.setattr(dc, "_RETRY_BACKOFF_S", (0.0, 0.0))
    calls = _scripted_get(
        monkeypatch, [httpx.ConnectError("down"), httpx.ReadTimeout("slow"), _Resp(500, None)]
    )
    assert dc.main(["token"]) == 0
    assert capsys.readouterr().out == ""
    assert len(calls) == 3


def test_main_retries_stay_within_one_timeout_budget(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A slow broker gets no extra attempts past the single-attempt timeout."""
    _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example", "h") is True
    capsys.readouterr()
    clock = [0.0]
    monkeypatch.setattr(dc, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    timeouts: list[float] = []

    async def slow_get(self: httpx.AsyncClient, url: str, headers: dict, timeout: float):
        timeouts.append(timeout)
        clock[0] += timeout
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(dc.httpx.AsyncClient, "get", slow_get)
    monkeypatch.setattr(dc, "_fetch", dc._fetch_in_worker)
    assert dc.main(["token"]) == 0
    assert capsys.readouterr().out == ""
    assert timeouts == [dc._TIMEOUT_S]


def test_main_treats_a_non_transport_http_error_as_final(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example", "h") is True
    capsys.readouterr()
    monkeypatch.setattr(dc, "_RETRY_BACKOFF_S", (0.0, 0.0))
    calls = _scripted_get(monkeypatch, [httpx.TooManyRedirects("loop")])
    assert dc.main(["token"]) == 0
    assert capsys.readouterr().out == ""
    assert len(calls) == 1


@pytest.mark.parametrize("status_code", [401, 403])
def test_main_does_not_retry_a_rejected_launch_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], status_code: int
) -> None:
    """A 401/403 is permanent, so it is answered once rather than retried."""
    _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example", "h") is True
    capsys.readouterr()
    monkeypatch.setattr(dc, "_RETRY_BACKOFF_S", (0.0, 0.0))
    calls = _scripted_get(monkeypatch, [_Resp(status_code, None)])
    assert dc.main(["token"]) == 0
    assert capsys.readouterr().out == ""
    assert len(calls) == 1


def test_broker_does_not_retry_after_backoff_uses_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(dc, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(dc, "_TIMEOUT_S", 1.0)
    monkeypatch.setattr(dc, "_RETRY_BACKOFF_S", (0.5,))

    async def oversleep(_delay: float) -> None:
        clock[0] += 2.0

    monkeypatch.setattr(dc, "anyio", SimpleNamespace(fail_after=anyio.fail_after, sleep=oversleep))
    calls = _scripted_get(monkeypatch, [_Resp(503, None), _BEARER_OK])
    assert dc._fetch_in_worker("https://omni.example", "host", "test-token") is None
    assert len(calls) == 1


async def test_broker_cancels_retry_and_closes_worker_from_an_active_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dc, "_TIMEOUT_S", 0.5)
    monkeypatch.setattr(dc, "_RETRY_BACKOFF_S", (0.01, 0.01))
    timeouts: list[float] = []
    clients: list[httpx.AsyncClient] = []
    workers: list[threading.Thread] = []
    cancelled = threading.Event()

    async def get(self: httpx.AsyncClient, url: str, headers: dict, timeout: float):
        timeouts.append(timeout)
        if len(timeouts) == 1:
            return _Resp(503, None)
        clients.append(self)
        workers.append(threading.current_thread())
        try:
            await anyio.sleep_forever()
        finally:
            cancelled.set()

    monkeypatch.setattr(dc.httpx.AsyncClient, "get", get)
    started = time.monotonic()
    assert dc._fetch_in_worker("https://omni.example", "host", "test-token") is None
    assert time.monotonic() - started < 1.5
    assert len(timeouts) == 2
    assert timeouts[1] < timeouts[0] <= 0.5
    assert cancelled.is_set()
    assert clients[0].is_closed
    assert not workers[0].is_alive()


@pytest.mark.parametrize("reader", ["fetch", "cli"])
def test_broker_deadline_closes_a_slow_response(
    tmp_path: Path,
    _credential_env: dict[str, str],
    reader: str,
) -> None:
    """Periodic response bytes must not extend the total credential deadline."""
    disconnected = threading.Event()
    stopped = threading.Event()
    received: list[str] = []
    deadline = dc._TIMEOUT_S
    assert deadline == 15.0

    class BrokerHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            received.append(self.path)
            body = b" " * 1500 + b'{"connected":false}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                for offset in range(0, len(body), 10):
                    self.wfile.write(body[offset : offset + 10])
                    self.wfile.flush()
                    if stopped.wait(0.2):
                        break
            except (BrokenPipeError, ConnectionResetError):
                disconnected.set()

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), BrokerHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        coords = tmp_path / dc._SIDECAR_NAME
        coords.write_text(
            json.dumps(
                {
                    "server": url,
                    "host_id": "host",
                    "host_token": "test-token",
                    "workspace_host": "https://workspace.example.test",
                }
            )
        )
        started = time.monotonic()
        if reader == "cli":
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "omnigent.host.databricks_credential",
                    "token",
                    "--coords",
                    str(coords),
                ],
                env=_credential_env,
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=deadline + 5,
            )
            assert completed.returncode == 0, completed.stderr
            assert completed.stdout == ""
        else:
            assert dc._fetch(url, "host", "test-token") is None
        elapsed = time.monotonic() - started
        cleanup_s = min(dc._WORKER_CLEANUP_S, deadline / 5)
        assert deadline - cleanup_s - 0.2 <= elapsed < deadline + 0.5
        assert disconnected.wait(2.0), "deadline must close the in-flight response"
        assert received == ["/v1/hosts/host/credentials/databricks"]
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2.0)
        assert not server_thread.is_alive()


@pytest.mark.parametrize("reader", ["fetch", "cli"])
async def test_broker_deadline_includes_blocking_dns_and_worker_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _credential_env: dict[str, str],
    reader: str,
) -> None:
    """A stuck resolver cannot extend the production deadline or leave a worker."""
    marker = tmp_path / "dns-worker"
    injection = tmp_path / "injection"
    injection.mkdir()
    injection.joinpath("sitecustomize.py").write_text(
        "import json, os, socket, sys, time\n"
        "def blocked_dns(*args, **kwargs):\n"
        "    with open(os.environ['DNS_WORKER_MARKER'], 'w') as output:\n"
        "        json.dump({'pid': os.getpid(), 'argv': sys.argv}, output)\n"
        f"    time.sleep({dc._TIMEOUT_S + 1.5!r})\n"
        "    raise socket.gaierror(socket.EAI_AGAIN, 'synthetic DNS delay')\n"
        "socket.getaddrinfo = blocked_dns\n"
    )
    env = {
        **_credential_env,
        "DNS_WORKER_MARKER": str(marker),
        "NO_PROXY": "*",
        "PYTHONPATH": os.pathsep.join((str(injection), _credential_env["PYTHONPATH"])),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    def blocked_dns(*_args, **_kwargs):
        marker.write_text(json.dumps({"pid": os.getpid(), "argv": sys.argv}))
        time.sleep(dc._TIMEOUT_S + 1.5)
        raise socket.gaierror(socket.EAI_AGAIN, "synthetic DNS delay")

    monkeypatch.setattr(socket, "getaddrinfo", blocked_dns)
    spawned = []
    original_popen = subprocess.Popen

    def record_process(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", record_process)
    coords = tmp_path / dc._SIDECAR_NAME
    coords.write_text(
        json.dumps(
            {
                "server": "https://broker.example.test",
                "host_id": "synthetic-host",
                "host_token": "synthetic-launch-token",
                "workspace_host": "https://workspace.example.test",
            }
        )
    )
    started = time.monotonic()
    if reader == "cli":
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "omnigent.host.databricks_credential",
                "token",
                "--coords",
                str(coords),
            ],
            env=env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=dc._TIMEOUT_S + 5,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == result.stderr == ""
    else:
        # The synchronous API must also work while its caller owns an active loop.
        assert (
            dc._fetch("https://broker.example.test", "synthetic-host", "synthetic-launch-token")
            is None
        )
    elapsed = time.monotonic() - started
    assert dc._TIMEOUT_S == 15.0
    assert 13.0 <= elapsed < dc._TIMEOUT_S + (0.5 if reader == "cli" else 0)
    state = json.loads(marker.read_text())
    worker_pid = state["pid"]
    assert not psutil.pid_exists(worker_pid), "DNS worker must be killed and reaped before return"
    assert "synthetic-launch-token" not in str(state["argv"])
    assert spawned and all(process.poll() is not None for process in spawned)
    assert all("synthetic-launch-token" not in str(process.args) for process in spawned)


@pytest.mark.parametrize("reply", ["credential", "malformed", "list", "nonzero"])
def test_broker_supervisor_validates_private_worker_reply(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], reply: str
) -> None:
    original_popen = subprocess.Popen
    spawned = []
    script = (
        "import json, sys; payload = json.load(sys.stdin); "
        "sys.stderr.write(payload[2]); "
        + {
            "credential": "print(json.dumps({'token': payload[2]}))",
            "malformed": "print('invalid JSON')",
            "list": "print('[]')",
            "nonzero": "sys.exit(1)",
        }[reply]
    )

    def worker(_command, **kwargs):
        process = original_popen([sys.executable, "-c", script], **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", worker)
    result = dc._fetch("https://broker.example.test", "host", "synthetic-launch-token")
    assert result == ({"token": "synthetic-launch-token"} if reply == "credential" else None)
    assert spawned[0].poll() is not None
    assert "synthetic-launch-token" not in str(spawned[0].args)
    output = capsys.readouterr()
    assert output.out == output.err == ""


@pytest.mark.parametrize(
    ("initial_status", "expected_requests", "expected_output"),
    [(503, 2, "synthetic-bearer\n"), (401, 1, "")],
    ids=["transient-outage", "rejected-credential"],
)
def test_token_cli_retries_outage_but_preserves_rejection(
    tmp_path: Path, initial_status: int, expected_requests: int, expected_output: str
) -> None:
    """Cross real CLI/TCP boundaries with production retry policy and synthetic credentials."""
    workspace = "https://workspace.example.test"
    received: list[tuple[str, str | None]] = []

    class BrokerHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            received.append((self.path, self.headers.get("X-Omnigent-Host-Token")))
            status = initial_status if len(received) == 1 else 200
            payload = (
                {"connected": True, "token": "synthetic-bearer", "workspace_host": workspace}
                if status == 200
                else {"error": "synthetic broker failure"}
            )
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), BrokerHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        home = tmp_path / "home"
        home.mkdir()
        sidecar = tmp_path / dc._SIDECAR_NAME
        sidecar.write_text(
            json.dumps(
                {
                    "server": f"http://127.0.0.1:{server.server_port}",
                    "host_id": "synthetic-host",
                    "host_token": "synthetic-launch-token",
                    "workspace_host": workspace,
                }
            )
        )
        sidecar.chmod(0o600)
        repo = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "omnigent.host.databricks_credential",
                "token",
                "--coords",
                str(sidecar),
                "--workspace",
                workspace,
            ],
            cwd=tmp_path,
            env={
                "HOME": str(home),
                "DATABRICKS_CONFIG_FILE": str(home / ".databrickscfg"),
                "OMNIGENT_CONFIG_HOME": str(home / ".omnigent"),
                "OMNIGENT_DATA_DIR": str(home / ".omnigent"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": os.pathsep.join(
                    str(repo / path) for path in (".", "sdks/python-client", "sdks/ui")
                ),
            },
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        assert (
            received
            == [("/v1/hosts/synthetic-host/credentials/databricks", "synthetic-launch-token")]
            * expected_requests
        )
        assert result.stdout == expected_output
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
