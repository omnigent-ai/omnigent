"""Runner Databricks credential resolution must not refresh the CLI token eagerly.

A runner that re-authenticates through its ``databricks-cli`` profile must
only read profile/host configuration while resolving credentials and mint a
bearer when a request first needs one. If the SDK ``Config`` built inside
resolution runs ``databricks auth token`` immediately, runners sharing a
profile refresh independently at startup and a failing CLI aborts resolution.

Usage::

    python -m pytest tests/e2e/test_databricks_runner_auth_eager_cli_refresh.py -v
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from tests.e2e.test_databricks_cli_upgrade_token_refresh import (
    _localhost_env,
    _start_server,
    _terminate,
)

_WORKSPACE_HOST = "https://adb-1111222233334444.15.azuredatabricks.net"
_FAKE_CLI_TTL_ENV = "OMNIGENT_TEST_FAKE_CLI_TTL_S"
_RUNNER_TIMEOUT_S = 120.0

# Stand-in for the Databricks CLI: logs every invocation to the stage's
# order log and mints a token unless the ``invalid-grant`` marker exists.
_FAKE_CLI = """#!/usr/bin/env python3
import json, os, sys, time
from datetime import datetime, timedelta
stage = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
with open(os.path.join(stage, "order.log"), "a") as fh:
    stamp = f"{time.time():.6f}\\tcli\\tpid={os.getpid()}\\tppid={os.getppid()}"
    fh.write(f"{stamp}\\targv={' '.join(sys.argv[1:])}\\n")
args = sys.argv[1:]
if "--help" in args:
    print("      --force-refresh      Force a token refresh even if the cached token is valid.")
    sys.exit(0)
if args[:1] in (["--version"], ["version"]):
    print("Databricks CLI v1.15.0")
    sys.exit(0)
if args[:2] != ["auth", "token"]:
    sys.exit(2)
if os.path.exists(os.path.join(stage, "invalid-grant")):
    sys.stderr.write('Error: oauth2: "invalid_grant" "Refresh token is invalid or expired"\\n')
    sys.exit(1)
ttl = int(os.environ.get("__TTL_ENV__", "3600"))
expiry = (datetime.now() + timedelta(seconds=ttl)).strftime("%Y-%m-%dT%H:%M:%S")
token = {"access_token": f"fake-cli-token-{os.getpid()}", "token_type": "Bearer"}
token["expiry"] = expiry
print(json.dumps(token))
""".replace("__TTL_ENV__", _FAKE_CLI_TTL_ENV)

# A second runner process: construct the runner auth factory at a shared instant
# and log its credential-resolution window.
_RUNNER_SNIPPET = """
import os, sys, time
log, server_url, start_at = sys.argv[1:4]
import databricks.sdk.config as sdk_config
if hasattr(sdk_config, "get_host_metadata"):
    from databricks.sdk.oauth import HostMetadata
    sdk_config.get_host_metadata = lambda _host: HostMetadata(oidc_endpoint="")
import omnigent.inner.databricks_executor as executor
from omnigent.runner._entry import _make_auth_token_factory
def mark(event):
    with open(log, "a") as fh:
        fh.write(f"{time.time():.6f}\\tevent\\t{event}\\tpid={os.getpid()}\\n")
original_resolve = executor._resolve_databricks_auth
def resolve(profile=None, *, host=None):
    mark("resolve_start")
    try:
        return original_resolve(profile, host=host)
    finally:
        mark("resolve_done")
executor._resolve_databricks_auth = resolve
while time.time() < float(start_at):
    time.sleep(0.001)
mark("factory_start")
factory = _make_auth_token_factory(server_url)
mark("factory_done" if factory is not None else "factory_none")
"""


@dataclass(frozen=True)
class _Stage:
    root: Path

    @property
    def log(self) -> Path:
        return self.root / "order.log"

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def bin(self) -> Path:
        return self.root / "bin"

    def mark(self, event: str) -> None:
        with self.log.open("a") as fh:
            fh.write(f"{time.time():.6f}\tevent\t{event}\tpid={os.getpid()}\n")

    def rows(self) -> list[tuple[float, str, str]]:
        rows: list[tuple[float, str, str]] = []
        if not self.log.exists():
            return rows
        for line in self.log.read_text().splitlines():
            ts, kind, rest = line.split("\t", 2)
            rows.append((float(ts), kind, rest))
        rows.sort()
        return rows

    def cli_spawns(self) -> list[tuple[float, str]]:
        return [
            (ts, rest)
            for ts, kind, rest in self.rows()
            if kind == "cli" and "argv=auth token" in rest
        ]

    def first(self, prefix: str, *, after: float = 0.0) -> float | None:
        for ts, kind, rest in self.rows():
            if kind == "event" and rest.startswith(prefix) and ts >= after:
                return ts
        return None

    def exact(self, event: str) -> float | None:
        """Time of the first event row equal to *event*, e.g. ``"resolve_done\\tpid=12"``."""
        for ts, kind, rest in self.rows():
            if kind == "event" and rest == event:
                return ts
        return None

    def cli_spawns_between(self, start: str, *ends: str) -> list[str]:
        """CLI refreshes logged after *start* and before the first of *ends*."""
        start_ts = self.first(start)
        if start_ts is None:
            return []
        end_candidates = [
            ts for end in ends if (ts := self.first(end, after=start_ts)) is not None
        ]
        end_ts = min(end_candidates) if end_candidates else float("inf")
        return [rest for ts, rest in self.cli_spawns() if start_ts <= ts <= end_ts]

    def render(self) -> str:
        rows = self.rows()
        if not rows:
            return "<empty order log>"
        base = rows[0][0]
        return "\n".join(f"+{ts - base:7.3f}s {kind:5} {rest}" for ts, kind, rest in rows)


def _stage_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    pointer_server_url: str | None = None,
) -> _Stage:
    """Stage a databricks-cli profile and the fake CLI for the runner under test."""
    stage = _Stage(tmp_path / "stage")
    stage.bin.mkdir(parents=True)
    stage.home.mkdir(parents=True)
    cli = stage.bin / "databricks"
    # The SDK only accepts a ``databricks`` executable larger than 1 MiB.
    cli.write_text(_FAKE_CLI + ("# " + "x" * 1022 + "\n") * 1100)
    cli.chmod(0o755)
    (stage.home / ".databrickscfg").write_text(
        f"[DEFAULT]\nhost = {_WORKSPACE_HOST}\nauth_type = databricks-cli\n\n"
        f"[example]\nhost = {_WORKSPACE_HOST}\nauth_type = databricks-cli\n"
    )
    monkeypatch.setenv("HOME", str(stage.home))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(stage.home / ".omnigent"))
    monkeypatch.setenv("PATH", f"{stage.bin}{os.pathsep}{os.environ['PATH']}")
    for name in list(os.environ):
        if name.startswith(("DATABRICKS", "OMNIGENT_RUNNER")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("RUNNER_SERVER_URL", raising=False)
    monkeypatch.delenv(_FAKE_CLI_TTL_ENV, raising=False)

    import databricks.sdk.config as sdk_config

    if hasattr(sdk_config, "get_host_metadata"):
        from databricks.sdk.oauth import HostMetadata

        monkeypatch.setattr(
            sdk_config, "get_host_metadata", lambda _host: HostMetadata(oidc_endpoint="")
        )

    if pointer_server_url is not None:
        from omnigent.cli_auth import store_databricks_auth

        store_databricks_auth(pointer_server_url, _WORKSPACE_HOST)
    return stage


def _instrument(monkeypatch: pytest.MonkeyPatch, stage: _Stage) -> None:
    """Log resolver, SDK Config construction and bearer requests into the order log."""
    import databricks.sdk.config as sdk_config

    import omnigent.inner.databricks_executor as executor

    original_init = sdk_config.Config.__init__

    def init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        stage.mark("sdk_config_init_start")
        try:
            original_init(self, *args, **kwargs)
        except BaseException as exc:
            stage.mark(f"sdk_config_init_error:{type(exc).__name__}")
            raise
        stage.mark("sdk_config_init_done")

    monkeypatch.setattr(sdk_config.Config, "__init__", init)

    original_resolve = executor._resolve_databricks_auth

    def resolve(profile=None, *, host=None):  # type: ignore[no-untyped-def]
        stage.mark(f"resolve_start:profile={profile!r}:host={host!r}")
        try:
            result = original_resolve(profile, host=host)
        except BaseException as exc:
            stage.mark(f"resolve_error:{type(exc).__name__}:{exc}")
            raise
        stage.mark("resolve_done")
        return result

    monkeypatch.setattr(executor, "_resolve_databricks_auth", resolve)

    original_headers = executor._DatabricksBearerAuth._authenticate_headers

    def headers(self):  # type: ignore[no-untyped-def]
        stage.mark("bearer_requested")
        return original_headers(self)

    monkeypatch.setattr(executor._DatabricksBearerAuth, "_authenticate_headers", headers)


def _resolve_runner_auth(stage: _Stage, server_url: str):  # type: ignore[no-untyped-def]
    from omnigent.runner._entry import _make_auth_token_factory

    stage.mark("factory_start")
    factory = _make_auth_token_factory(server_url)
    stage.mark("factory_done" if factory is not None else "factory_none")
    return factory


def _first_authenticated_request(  # type: ignore[no-untyped-def]
    stage: _Stage, server_url: str, factory
) -> tuple[httpx.Response, str | None]:
    """GET /v1/me through the runner's auth; also return the ``Authorization`` header it sent."""
    from omnigent.cli_auth import open_server_client
    from omnigent.runner._entry import _RunnerDatabricksAuth

    sent: list[str | None] = []

    async def _record(request: httpx.Request) -> None:
        sent.append(request.headers.get("Authorization"))

    async def _get() -> httpx.Response:
        client = open_server_client(
            server_url,
            auth=_RunnerDatabricksAuth(factory, server_url=server_url),
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
        )
        client.event_hooks["request"].append(_record)
        try:
            stage.mark("first_request_start")
            response = await client.get("/v1/me")
            stage.mark(f"first_request_done:{response.status_code}")
            return response
        finally:
            await client.aclose()

    response = asyncio.run(_get())
    return response, sent[0] if sent else None


@pytest.fixture(scope="module")
def live_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    proc, base_url = _start_server(tmp_path_factory.mktemp("server"))
    try:
        yield base_url
    finally:
        _terminate(proc)


def test_profile_selector_defers_cli_refresh_until_bearer_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live_server: str
) -> None:
    stage = _stage_profile(tmp_path, monkeypatch)
    _instrument(monkeypatch, stage)

    factory = _resolve_runner_auth(stage, live_server)
    assert factory is not None, (
        f"runner auth factory did not resolve the profile\n{stage.render()}"
    )
    response, authorization = _first_authenticated_request(stage, live_server, factory)
    assert response.status_code == 200, response.text
    token = factory()
    assert token and token.startswith("fake-cli-token-"), token
    assert authorization == f"Bearer {token}", authorization

    during_resolution = stage.cli_spawns_between("resolve_start", "resolve_done", "resolve_error")
    assert not during_resolution, (
        "Bug reproduced: credential resolution itself ran the Databricks CLI refresh "
        f"{during_resolution} before any request asked for a bearer.\n{stage.render()}"
    )


def test_host_selector_defers_cli_refresh_until_bearer_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live_server: str
) -> None:
    stage = _stage_profile(tmp_path, monkeypatch, pointer_server_url=live_server)
    _instrument(monkeypatch, stage)

    factory = _resolve_runner_auth(stage, live_server)
    assert factory is not None, (
        f"runner auth factory did not resolve the workspace host\n{stage.render()}"
    )
    assert stage.first(f"resolve_start:profile=None:host={_WORKSPACE_HOST!r}") is not None, (
        f"the pointer record did not route resolution through the host selector\n{stage.render()}"
    )
    response, authorization = _first_authenticated_request(stage, live_server, factory)
    assert response.status_code == 200, response.text
    token = factory()
    assert token and token.startswith("fake-cli-token-"), token
    assert authorization == f"Bearer {token}", authorization

    during_resolution = stage.cli_spawns_between("resolve_start", "resolve_done", "resolve_error")
    assert not during_resolution, (
        "Bug reproduced: host-selector credential resolution ran the Databricks CLI refresh "
        f"{during_resolution} before any request asked for a bearer.\n{stage.render()}"
    )


def test_runners_sharing_a_profile_do_not_refresh_during_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live_server: str
) -> None:
    stage = _stage_profile(tmp_path, monkeypatch)
    start_at = time.time() + 1.0
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _RUNNER_SNIPPET, str(stage.log), live_server, str(start_at)],
            env=_localhost_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    for proc in procs:
        _out, err = proc.communicate(timeout=_RUNNER_TIMEOUT_S)
        assert proc.returncode == 0, err

    resolving_runners = {
        rest.split("pid=")[1]
        for _ts, kind, rest in stage.rows()
        if kind == "event" and rest.startswith("resolve_start")
    }
    assert len(resolving_runners) == 2, (
        f"both runners must resolve Databricks credentials\n{stage.render()}"
    )
    # The factory's credential probe is a runner's first bearer request, so only
    # CLI spawns inside each runner's resolution window count as eager.
    eager_by_runner: dict[str, list[tuple[float, str]]] = {}
    for ts, kind, rest in stage.rows():
        if kind != "cli" or "argv=auth token" not in rest:
            continue
        runner_pid = rest.split("ppid=")[1].split("\t")[0]
        start = stage.exact(f"resolve_start\tpid={runner_pid}")
        done = stage.exact(f"resolve_done\tpid={runner_pid}")
        if start is not None and done is not None and start <= ts <= done:
            eager_by_runner.setdefault(runner_pid, []).append((ts, rest))
    gap_ms = ""
    if len(eager_by_runner) == 2:
        first, second = (spawns[0][0] for spawns in eager_by_runner.values())
        gap_ms = f" {abs(first - second) * 1000:.0f} ms apart"
    assert not eager_by_runner, (
        f"Bug reproduced: {len(eager_by_runner)} runner(s) sharing one profile each ran their own "
        f"`databricks auth token` refresh during credential resolution{gap_ms}, with nothing "
        f"serializing them.\n{stage.render()}"
    )


def test_failing_cli_refresh_does_not_abort_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live_server: str
) -> None:
    stage = _stage_profile(tmp_path, monkeypatch)
    (stage.root / "invalid-grant").touch()
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "example")
    _instrument(monkeypatch, stage)

    factory = _resolve_runner_auth(stage, live_server)

    resolve_errors = [
        rest
        for _ts, kind, rest in stage.rows()
        if kind == "event" and rest.startswith("resolve_error")
    ]
    during_resolution = stage.cli_spawns_between("resolve_start", "resolve_done", "resolve_error")
    assert not resolve_errors and not during_resolution, (
        "Bug reproduced: the SDK auth step inside credential resolution ran the CLI "
        f"{len(during_resolution)} time(s) and its failure aborted resolution "
        f"({resolve_errors}) instead of surfacing when a bearer is requested.\n{stage.render()}"
    )
    # Resolution completed, the broken CLI ran only for the factory's bearer
    # probe afterwards, and that probe found no usable credential.
    assert stage.first("resolve_done") is not None, stage.render()
    assert stage.cli_spawns_between("resolve_done", "factory_none"), (
        f"the factory probe never asked the CLI for a bearer\n{stage.render()}"
    )
    assert factory is None and stage.first("factory_none") is not None, stage.render()
