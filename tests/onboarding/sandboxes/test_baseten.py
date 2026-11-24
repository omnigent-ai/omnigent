"""Tests for :mod:`omnigent.onboarding.sandboxes.baseten`."""

from __future__ import annotations

import re
import signal
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import click
import httpx
import pytest

pytest.importorskip("baseten.sandbox", reason="the optional Baseten SDK is not installed")

from baseten.sandbox import (
    ImageBuilder,
    SandboxAPIError,
    SandboxEnvValue,
    SandboxExpirationPolicyMaxAge,
    SandboxLifecycle,
    SandboxProcessExecEventExit,
    SandboxProcessExecEventOutput,
    SandboxProcessInfo,
)

import omnigent.onboarding.sandboxes.baseten as launcher_module
from omnigent.onboarding.sandboxes.base import DEFAULT_HOST_IMAGE, SandboxCapabilityError
from omnigent.onboarding.sandboxes.baseten import (
    API_KEY_ENV_VAR,
    HOST_IMAGE_ENV_VAR,
    REGION_ENV_VAR,
    SANDBOX_ENV_PASSTHROUGH_ENV_VAR,
    TEAM_ID_ENV_VAR,
    BasetenSandboxLauncher,
    default_image_name,
    default_source_image,
    managed_token_ttl_s,
    parse_ttl_seconds,
)

API_KEY = "test-api-key-value"
IMAGE = "team-image:v1"
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
RELEASE_SOURCE_IMAGE = "ghcr.io/omnigent-ai/omnigent-host:v0.24.0"
RELEASE_IMAGE_NAME = "omnigent-host-v0-24-0"


def _api_error(status: int, message: str | None = None) -> SandboxAPIError:
    return SandboxAPIError(status=status, code=None, message=message, details=None, body=None)


def _process_info(
    *, status: str = "running", exit_code: Any = 0, stdout: str = "", stderr: str = ""
) -> SandboxProcessInfo:
    return SandboxProcessInfo(
        pid="42",
        name="process",
        command="command",
        status=status,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        logs="",
        working_dir="/",
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        completed_at=None,
        keep_alive=None,
        max_restarts=None,
        restart_count=None,
        restart_on_failure=None,
        stdin=None,
    )


def _out(
    text: str, stream: Literal["stdout", "stderr"] = "stdout"
) -> SandboxProcessExecEventOutput:
    return SandboxProcessExecEventOutput(stream=stream, text=text)


def _exit(exit_code: Any = 0, status: str = "completed") -> SandboxProcessExecEventExit:
    return SandboxProcessExecEventExit(process=_process_info(status=status, exit_code=exit_code))


# Marks the point where a fake stream blocks until the process is killed.
_AWAIT_KILL = object()


class _FakeStream:
    """Exec event stream: yields its events, then raises ``error`` when set."""

    def __init__(
        self, events: list[object], error: BaseException | None, killed: threading.Event
    ) -> None:
        self.events = events
        self.error = error
        self.killed = killed
        self.close_calls = 0

    def __iter__(self) -> Iterator[object]:
        for event in self.events:
            if event is _AWAIT_KILL:
                # Like an open connection: nothing more arrives until the kill.
                self.killed.wait(timeout=5)
                continue
            yield event
        if self.error is not None:
            raise self.error

    def close(self) -> None:
        self.close_calls += 1


class _FakeProcessAPI:
    """Record process API calls and stream canned events."""

    def __init__(self) -> None:
        self.events: list[object] = [_exit()]
        self.stream_error: BaseException | None = None
        self.start_errors: list[Exception] = []
        self.stream_calls: list[dict[str, Any]] = []
        self.streams: list[_FakeStream] = []
        self.exec_calls: list[dict[str, Any]] = []
        self.exec_status = "running"
        self.exec_error: Exception | None = None
        self.kills: list[str] = []
        self.kill_error: Exception | None = None
        self.killed = threading.Event()
        self.list_errors: list[Exception] = []
        self.list_calls = 0

    def exec_stream(self, **kwargs: Any) -> _FakeStream:
        self.stream_calls.append(kwargs)
        if self.start_errors:
            raise self.start_errors.pop(0)
        stream = _FakeStream(list(self.events), self.stream_error, self.killed)
        self.streams.append(stream)
        return stream

    def exec(self, **kwargs: Any) -> SandboxProcessInfo:
        self.exec_calls.append(kwargs)
        if self.exec_error is not None:
            raise self.exec_error
        return _process_info(status=self.exec_status)

    def kill(self, *, identifier: str) -> None:
        self.kills.append(identifier)
        self.killed.set()
        if self.kill_error is not None:
            raise self.kill_error

    def list(self) -> list[object]:
        self.list_calls += 1
        if self.list_errors:
            raise self.list_errors.pop(0)
        return []


class _FakeFS:
    def __init__(self) -> None:
        self.writes: list[tuple[str, bytes]] = []
        self.error: Exception | None = None

    def write_bytes(self, *, path: str, content: bytes) -> None:
        if self.error is not None:
            raise self.error
        self.writes.append((path, content))


class _FakeSandbox:
    def __init__(self) -> None:
        self.process = _FakeProcessAPI()
        self.fs = _FakeFS()


class _FakeImages:
    """Team image API; ``status=None`` means the image does not exist."""

    def __init__(self) -> None:
        self.status: str | None = "BUILT"
        self.get_info_error: Exception | None = None
        self.list_error: Exception | None = None
        self.get_info_calls: list[str] = []
        self.pushes: list[tuple[str, object, timedelta]] = []
        self.waits: list[tuple[str, timedelta]] = []
        self.list_library_calls = 0

    def list_library(self) -> list[object]:
        self.list_library_calls += 1
        if self.list_error is not None:
            raise self.list_error
        return []

    def get_info(self, *, name: str) -> SimpleNamespace:
        self.get_info_calls.append(name)
        if self.get_info_error is not None:
            raise self.get_info_error
        if self.status is None:
            raise _api_error(404, "image not found")
        return SimpleNamespace(name=name, status=self.status)

    def push(self, *, name: str, builder: object, timeout: timedelta) -> None:
        self.pushes.append((name, builder, timeout))

    def wait_built(self, *, name: str, timeout: timedelta) -> None:
        self.waits.append((name, timeout))


@dataclass
class _Record:
    """A sandbox record; ``get_info`` reports its statuses in turn, the last one sticky."""

    labels: dict[str, str] = field(default_factory=dict)
    statuses: list[str | Exception] = field(
        default_factory=lambda: list[str | Exception](["DEPLOYED"])
    )

    def next_status(self) -> str:
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if isinstance(status, Exception):
            raise status
        return status


class _FakeClient:
    """The ``SandboxClient`` surface the launcher uses, recording each call."""

    def __init__(self) -> None:
        self.images = _FakeImages()
        self.records: dict[str, _Record] = {}
        self.sandboxes: dict[str, _FakeSandbox] = {}
        self.created: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.get_info_calls: list[str] = []
        self.deploy_statuses: list[str | Exception] = ["DEPLOYED"]
        self.after_delete: list[str | Exception] = ["DELETING", "TERMINATED"]
        self.create_error: Exception | None = None
        # A create that commits remotely before its error reaches the caller.
        self.create_commits = False
        self.delete_error: Exception | None = None
        self.get_info_error: Exception | None = None

    def create(self, **kwargs: Any) -> None:
        self.created.append(kwargs)
        if self.create_error is not None and not self.create_commits:
            raise self.create_error
        self.records[kwargs["name"]] = _Record(
            labels=dict(kwargs["labels"]), statuses=list(self.deploy_statuses)
        )
        if self.create_error is not None:
            raise self.create_error

    def get_info(self, *, name: str) -> SimpleNamespace:
        self.get_info_calls.append(name)
        if self.get_info_error is not None:
            raise self.get_info_error
        record = self.records.get(name)
        if record is None:
            raise _api_error(404, "sandbox not found")
        status = record.next_status()
        url = f"https://{name}.sandbox.example.test" if status == "DEPLOYED" else ""
        return SimpleNamespace(name=name, status=status, url=url, labels=record.labels)

    def get(self, *, info: SimpleNamespace) -> _FakeSandbox:
        return self.sandboxes.setdefault(info.name, _FakeSandbox())

    def delete(self, *, name: str) -> None:
        self.deleted.append(name)
        if self.delete_error is not None:
            raise self.delete_error
        record = self.records.get(name)
        if record is None:
            raise _api_error(404, "sandbox not found")
        record.statuses = list(self.after_delete)


class _FakeClock:
    """Stands in for the module's ``time``: sleeping advances the clock instantly."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    fake = _FakeClock()
    monkeypatch.setattr(launcher_module, "time", fake)
    return fake


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, clock: _FakeClock) -> _FakeClient:
    fake = _FakeClient()
    monkeypatch.setattr(launcher_module, "_client", lambda: fake)
    monkeypatch.setenv(API_KEY_ENV_VAR, API_KEY)
    for name in (
        TEAM_ID_ENV_VAR,
        HOST_IMAGE_ENV_VAR,
        REGION_ENV_VAR,
        SANDBOX_ENV_PASSTHROUGH_ENV_VAR,
    ):
        monkeypatch.delenv(name, raising=False)
    return fake


@pytest.fixture
def sandbox(client: _FakeClient) -> _FakeSandbox:
    """A deployed sandbox named ``sb``."""
    client.records["sb"] = _Record()
    return client.sandboxes.setdefault("sb", _FakeSandbox())


@pytest.fixture
def release_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher_module.metadata, "version", lambda _name: "0.24.0")


def test_capabilities_match_exec_provider_contract() -> None:
    launcher = BasetenSandboxLauncher(image=IMAGE)
    capabilities = launcher.capabilities

    assert capabilities.cli_bootstrap is True
    assert capabilities.managed_launch is True
    assert capabilities.local_port_forward is False
    assert capabilities.resume_stopped is False
    assert capabilities.programmatic_terminate is True
    assert capabilities.file_copy is True
    assert capabilities.streaming_exec is True
    assert capabilities.foreground_exec is True
    with pytest.raises(SandboxCapabilityError):
        launcher.forward_local_port("sb", 8022)
    assert "--force-reinstall" in launcher.wheel_install_command("/tmp/wheels.tgz")


@pytest.mark.parametrize(
    ("ttl", "expected"),
    [
        (None, 24 * 3600),
        ("30m", 1800),
        ("24h", 24 * 3600),
        ("7d", 7 * 24 * 3600),
        ("2w", 14 * 24 * 3600),
        ("1h30m", 5400),
        (" 12H ", 12 * 3600),
        ("1.5h", 5400),
    ],
)
def test_parse_ttl_seconds_reads_durations(ttl: str | None, expected: int) -> None:
    assert parse_ttl_seconds(ttl) == expected


@pytest.mark.parametrize(
    "ttl", ["", "forever", "24", "h", "24hh", "1y", "-1h", "0h", "0m0s", "90s", "4m59s"]
)
def test_parse_ttl_seconds_rejects_unusable_durations(ttl: str) -> None:
    with pytest.raises(ValueError):
        parse_ttl_seconds(ttl)


def test_managed_token_ttl_outlives_sandbox_max_age() -> None:
    for ttl in (None, "30m", "7d"):
        assert managed_token_ttl_s(ttl) == parse_ttl_seconds(ttl) + 3600
    assert managed_token_ttl_s() == 25 * 3600


@pytest.mark.parametrize(
    ("version", "tag"),
    [
        ("0.24.0", "v0.24.0"),
        ("0.25.0rc2", "v0.25.0rc2"),
        ("0.25.0.dev20261124", "v0.25.0.dev20261124"),
        # Source checkouts have no published image of their own.
        ("0.24.0.dev0", "latest"),
        ("0.24.0+g1234abc", "latest"),
    ],
)
def test_default_source_image_follows_installed_release(
    monkeypatch: pytest.MonkeyPatch, version: str, tag: str
) -> None:
    monkeypatch.setattr(launcher_module.metadata, "version", lambda _name: version)

    assert default_source_image() == f"ghcr.io/omnigent-ai/omnigent-host:{tag}"


def test_default_source_image_without_installed_package(monkeypatch: pytest.MonkeyPatch) -> None:
    def _missing(name: str) -> str:
        raise launcher_module.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(launcher_module.metadata, "version", _missing)

    assert default_source_image() == DEFAULT_HOST_IMAGE


@pytest.mark.parametrize(
    ("source", "name"),
    [
        (RELEASE_SOURCE_IMAGE, RELEASE_IMAGE_NAME),
        ("ghcr.io/omnigent-ai/omnigent-host:latest", "omnigent-host-latest"),
        ("ghcr.io/omnigent-ai/omnigent-host:v0.25.0rc2", "omnigent-host-v0-25-0rc2"),
        (
            "ghcr.io/omnigent-ai/omnigent-host:v0.25.0.dev20261124",
            "omnigent-host-v0-25-0-dev20261124",
        ),
        ("ghcr.io/omnigent-ai/omnigent-host", "omnigent-host-latest"),
    ],
)
def test_default_image_name_slugs_the_tag(source: str, name: str) -> None:
    assert default_image_name(source) == name


@pytest.mark.parametrize("api_key", [None, "   "])
def test_client_requires_api_key(monkeypatch: pytest.MonkeyPatch, api_key: str | None) -> None:
    monkeypatch.setattr(launcher_module, "_CLIENTS", {})
    if api_key is None:
        monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(API_KEY_ENV_VAR, api_key)

    with pytest.raises(click.ClickException, match=f"Set {API_KEY_ENV_VAR}"):
        launcher_module._client()


def test_client_passes_team_and_caches_one_client_per_key_and_team(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed: list[dict[str, str | None]] = []

    class _RecordingClient:
        def __init__(self, *, api_key: str, team_id: str | None) -> None:
            constructed.append({"api_key": api_key, "team_id": team_id})

    monkeypatch.setattr("baseten.sandbox.SandboxClient", _RecordingClient)
    monkeypatch.setattr(launcher_module, "_CLIENTS", {})
    monkeypatch.setenv(API_KEY_ENV_VAR, API_KEY)
    monkeypatch.setenv(TEAM_ID_ENV_VAR, " team-alpha ")

    first = launcher_module._client()
    assert launcher_module._client() is first
    monkeypatch.setenv(TEAM_ID_ENV_VAR, "team-beta")
    second = launcher_module._client()
    monkeypatch.delenv(TEAM_ID_ENV_VAR)
    launcher_module._client()

    assert second is not first
    assert constructed == [
        {"api_key": API_KEY, "team_id": "team-alpha"},
        {"api_key": API_KEY, "team_id": "team-beta"},
        {"api_key": API_KEY, "team_id": None},
    ]


def test_client_reports_missing_optional_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "baseten.sandbox", None)

    with pytest.raises(click.ClickException, match=r"omnigent\[baseten\]"):
        launcher_module._client()


def test_prepare_rejects_credentials_without_leaking_key(client: _FakeClient) -> None:
    client.images.list_error = _api_error(401, f"invalid key {API_KEY}")

    with pytest.raises(click.ClickException, match="rejected the sandbox credentials") as exc:
        BasetenSandboxLauncher(image=IMAGE).prepare()

    assert API_KEY not in str(exc.value)
    assert "[redacted]" in str(exc.value)
    assert client.images.get_info_calls == []


@pytest.mark.usefixtures("release_version")
def test_prepare_reuses_built_default_image(client: _FakeClient) -> None:
    launcher = BasetenSandboxLauncher()

    launcher.prepare()
    launcher.provision("sb")

    assert client.images.get_info_calls == [RELEASE_IMAGE_NAME], "resolved once, then cached"
    assert client.images.pushes == []
    assert client.images.waits == []
    assert client.created[0]["image"] == f"{RELEASE_IMAGE_NAME}:latest"


@pytest.mark.usefixtures("release_version")
@pytest.mark.parametrize("status", [None, "FAILED"], ids=["missing", "failed-build"])
def test_prepare_builds_default_image_from_registry(
    client: _FakeClient, monkeypatch: pytest.MonkeyPatch, status: str | None
) -> None:
    monkeypatch.setattr(
        ImageBuilder, "from_registry", staticmethod(lambda source: f"builder:{source}")
    )
    client.images.status = status

    BasetenSandboxLauncher().prepare()

    assert client.images.pushes == [
        (RELEASE_IMAGE_NAME, f"builder:{RELEASE_SOURCE_IMAGE}", timedelta(minutes=30))
    ]
    assert client.images.waits == []


@pytest.mark.usefixtures("release_version")
@pytest.mark.parametrize("status", ["BUILDING", "UPLOADING"])
def test_prepare_waits_for_default_image_build_in_progress(
    client: _FakeClient, status: str
) -> None:
    client.images.status = status

    BasetenSandboxLauncher().prepare()

    assert client.images.waits == [(RELEASE_IMAGE_NAME, timedelta(minutes=30))]
    assert client.images.pushes == []


@pytest.mark.parametrize(
    ("ctor_image", "env_image"),
    [(IMAGE, None), (None, IMAGE), (IMAGE, "other-image:v9")],
    ids=["constructor", "environment", "constructor-wins"],
)
def test_configured_image_skips_default_image_build(
    client: _FakeClient,
    monkeypatch: pytest.MonkeyPatch,
    ctor_image: str | None,
    env_image: str | None,
) -> None:
    if env_image is not None:
        monkeypatch.setenv(HOST_IMAGE_ENV_VAR, env_image)
    launcher = BasetenSandboxLauncher(image=ctor_image)

    launcher.prepare()
    launcher.provision("sb")

    assert client.images.list_library_calls == 1
    assert client.images.get_info_calls == []
    assert client.images.pushes == []
    assert client.created[0]["image"] == IMAGE


@pytest.mark.usefixtures("release_version")
def test_prepare_reports_image_lookup_failure(client: _FakeClient) -> None:
    client.images.get_info_error = _api_error(500, f"registry down for {API_KEY}")

    with pytest.raises(
        click.ClickException, match="Could not prepare the Omnigent host image"
    ) as exc:
        BasetenSandboxLauncher().prepare()
    assert API_KEY not in str(exc.value)


def test_provision_forwards_config_to_create(
    client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODEL_KEY", "model-secret")
    monkeypatch.setenv(REGION_ENV_VAR, "env-region")
    launcher = BasetenSandboxLauncher(
        image=IMAGE, env=["MODEL_KEY"], region="test-region", memory_mb=8192, ttl="2h"
    )

    assert launcher.provision("sb") == "sb"

    created = dict(client.created[0])
    labels = created.pop("labels")
    assert created == {
        "name": "sb",
        "image": IMAGE,
        "memory": 8192,
        "region": "test-region",
        "envs": {"MODEL_KEY": SandboxEnvValue(value="model-secret", secret=True)},
        "lifecycle": SandboxLifecycle(
            expiration_policies=[SandboxExpirationPolicyMaxAge(after=timedelta(hours=2))]
        ),
    }
    assert labels.keys() == {"managed-by", "omnigent-create-attempt"}
    assert labels["managed-by"] == "omnigent"
    assert re.fullmatch(r"[0-9a-f]{32}", labels["omnigent-create-attempt"])


def test_provision_defaults(client: _FakeClient) -> None:
    BasetenSandboxLauncher(image=IMAGE).provision("sb")

    created = client.created[0]
    assert created["memory"] == 4096
    assert created["region"] is None
    assert created["envs"] is None
    assert created["lifecycle"] == SandboxLifecycle(
        expiration_policies=[SandboxExpirationPolicyMaxAge(after=timedelta(hours=24))]
    )


def test_provision_reads_region_and_env_passthrough_from_environment(
    client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REGION_ENV_VAR, "env-region")
    monkeypatch.setenv(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, " FIRST_KEY , SECOND_KEY ,")
    monkeypatch.setenv("FIRST_KEY", "one")
    monkeypatch.setenv("SECOND_KEY", "two")

    BasetenSandboxLauncher(image=IMAGE).provision("sb")

    assert client.created[0]["region"] == "env-region"
    assert client.created[0]["envs"] == {
        "FIRST_KEY": SandboxEnvValue(value="one", secret=True),
        "SECOND_KEY": SandboxEnvValue(value="two", secret=True),
    }


def test_provision_waits_until_deployed_and_api_answers(client: _FakeClient) -> None:
    client.deploy_statuses = ["DEPLOYING", "DEPLOYING", "DEPLOYED"]
    sandbox = client.sandboxes["sb"] = _FakeSandbox()
    sandbox.process.list_errors = [_api_error(503, "waking")]
    launcher = BasetenSandboxLauncher(image=IMAGE)

    assert launcher.provision("sb") == "sb"
    assert client.get_info_calls == ["sb"] * 3
    assert sandbox.process.list_calls == 2

    launcher.run("sb", "true")
    assert client.get_info_calls == ["sb"] * 3, "provision caches the deployed handle"


@pytest.mark.parametrize("via_environment", [False, True])
def test_provision_refuses_to_inject_api_key(
    client: _FakeClient, monkeypatch: pytest.MonkeyPatch, via_environment: bool
) -> None:
    if via_environment:
        monkeypatch.setenv(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, API_KEY_ENV_VAR)
        launcher = BasetenSandboxLauncher(image=IMAGE)
    else:
        launcher = BasetenSandboxLauncher(image=IMAGE, env=[API_KEY_ENV_VAR])

    with pytest.raises(click.ClickException, match="must not inject the Baseten API key"):
        launcher.provision("sb")
    assert client.created == []


def test_provision_rejects_unset_passthrough_variable(
    client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MISSING_KEY", raising=False)

    with pytest.raises(click.ClickException, match="'MISSING_KEY' but it is not set"):
        BasetenSandboxLauncher(image=IMAGE, env=["MISSING_KEY"]).provision("sb")
    assert client.created == []


def test_provision_rejects_invalid_ttl(client: _FakeClient) -> None:
    with pytest.raises(click.ClickException, match="Invalid Baseten sandbox ttl"):
        BasetenSandboxLauncher(image=IMAGE, ttl="forever").provision("sb")
    assert client.created == []


def test_provision_leaves_namesake_after_rejected_create(client: _FakeClient) -> None:
    client.records["sb"] = _Record()
    client.create_error = _api_error(409, "sandbox already exists")

    with pytest.raises(click.ClickException, match="already exists"):
        BasetenSandboxLauncher(image=IMAGE).provision("sb")
    assert client.get_info_calls == []
    assert client.deleted == []


@pytest.mark.parametrize(
    "error",
    [_api_error(503, "upstream timeout"), httpx.ReadTimeout("response lost")],
    ids=["5xx", "transport"],
)
def test_provision_deletes_sandbox_an_ambiguous_create_made(
    client: _FakeClient, error: Exception
) -> None:
    client.create_error = error
    client.create_commits = True

    with pytest.raises(click.ClickException, match="creation failed"):
        BasetenSandboxLauncher(image=IMAGE).provision("sb")
    assert client.deleted == ["sb"]


def test_provision_spares_namesake_from_an_earlier_attempt(client: _FakeClient) -> None:
    client.records["omnigent-host"] = _Record(
        labels={"managed-by": "omnigent", "omnigent-create-attempt": "earlier-attempt"}
    )
    client.create_error = _api_error(503, "upstream timeout")

    with pytest.raises(click.ClickException, match="upstream timeout"):
        BasetenSandboxLauncher(image=IMAGE).provision("omnigent-host")
    assert client.get_info_calls == ["omnigent-host"]
    assert client.deleted == []


def test_provision_rides_out_transient_control_plane_errors(client: _FakeClient) -> None:
    client.deploy_statuses = ["DEPLOYING", _api_error(503, "unavailable"), "DEPLOYED"]

    assert BasetenSandboxLauncher(image=IMAGE).provision("sb") == "sb"
    assert client.get_info_calls == ["sb"] * 3
    assert client.deleted == []


def test_provision_ambiguous_failure_with_nothing_created(client: _FakeClient) -> None:
    client.create_error = httpx.ConnectError("connection refused")

    with pytest.raises(click.ClickException, match="connection refused") as exc:
        BasetenSandboxLauncher(image=IMAGE).provision("sb")
    assert client.deleted == []
    assert "Cleanup also failed" not in str(exc.value)


@pytest.mark.parametrize(
    ("statuses", "message"),
    [(["DEPLOYING", "FAILED"], "is FAILED"), (["DEPLOYING"], "still DEPLOYING after 300s")],
    ids=["failed", "deploy-timeout"],
)
def test_provision_terminates_sandbox_that_never_deploys(
    client: _FakeClient, statuses: list[str | Exception], message: str
) -> None:
    client.deploy_statuses = statuses

    with pytest.raises(click.ClickException, match=message):
        BasetenSandboxLauncher(image=IMAGE).provision("sb")
    assert client.deleted == ["sb"]


def test_provision_terminates_sandbox_whose_api_rejects_readiness(client: _FakeClient) -> None:
    sandbox = client.sandboxes["sb"] = _FakeSandbox()
    sandbox.process.list_errors = [_api_error(400, "bad request")]

    with pytest.raises(click.ClickException, match=r"creation failed.*bad request"):
        BasetenSandboxLauncher(image=IMAGE).provision("sb")
    assert client.deleted == ["sb"]


def test_provision_reports_cleanup_failure(client: _FakeClient) -> None:
    client.deploy_statuses = ["FAILED"]
    client.delete_error = _api_error(500, f"delete unavailable for {API_KEY}")

    with pytest.raises(click.ClickException, match="Cleanup also failed for sandbox 'sb'") as exc:
        BasetenSandboxLauncher(image=IMAGE).provision("sb")
    assert API_KEY not in str(exc.value)


def test_provision_redacts_api_key_and_injected_values(
    client: _FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MODEL_KEY", "model-secret")
    client.create_error = _api_error(400, f"rejected {API_KEY} with model-secret")

    with pytest.raises(click.ClickException) as exc:
        BasetenSandboxLauncher(image=IMAGE, env=["MODEL_KEY"]).provision("sb")

    message = str(exc.value)
    assert API_KEY not in message
    assert "model-secret" not in message
    assert message.count("[redacted]") == 2


def test_run_splits_output_streams_and_checks_exit(
    sandbox: _FakeSandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    sandbox.process.events = [
        _out("out 1\n"),
        _out("err 1\n", "stderr"),
        _out("out 2\n"),
        _exit(7, "failed"),
    ]
    launcher = BasetenSandboxLauncher(image=IMAGE)

    result = launcher.run("sb", "false", check=False)

    assert (result.returncode, result.stdout, result.stderr) == (7, "out 1\nout 2\n", "err 1\n")
    assert "out 1\nout 2\nerr 1\n" in capsys.readouterr().out
    call = sandbox.process.stream_calls[0]
    assert call["command"] == "false"
    assert call["name"].startswith("omni-command-")
    assert (call["timeout"], call["keep_alive"], call["env"]) == (
        timedelta(minutes=15),
        None,
        None,
    )
    with pytest.raises(click.ClickException, match=r"exit 7"):
        launcher.run("sb", "false")


@pytest.mark.parametrize(
    ("status", "exit_code", "expected"),
    [("completed", 0, 0), ("failed", 2, 2), ("completed", None, 0), ("stopped", None, 1)],
)
def test_run_reads_exit_code_from_exit_event(
    sandbox: _FakeSandbox, status: str, exit_code: int | None, expected: int
) -> None:
    sandbox.process.events = [_exit(exit_code, status)]

    result = BasetenSandboxLauncher(image=IMAGE).run("sb", "true", check=False)

    assert result.returncode == expected


def test_run_reports_stream_that_ends_before_exit(sandbox: _FakeSandbox) -> None:
    sandbox.process.events = [_out("partial\n")]

    with pytest.raises(click.ClickException, match="ended before the command exited"):
        BasetenSandboxLauncher(image=IMAGE).run("sb", "true")


def test_run_accepts_output_up_to_cap(sandbox: _FakeSandbox) -> None:
    sandbox.process.events = [_out("x" * (MAX_OUTPUT_BYTES - 1)), _out("y"), _exit()]

    result = BasetenSandboxLauncher(image=IMAGE).run("sb", "cat big")

    assert len(result.stdout) == MAX_OUTPUT_BYTES
    assert sandbox.process.kills == []


def test_run_kills_process_when_output_exceeds_cap(sandbox: _FakeSandbox) -> None:
    # One three-byte character crosses the byte cap while the character count stays under it.
    sandbox.process.events = [_out("x" * (MAX_OUTPUT_BYTES - 1)), _out("€"), _exit()]

    with pytest.raises(click.ClickException, match=f"exceeded {MAX_OUTPUT_BYTES} bytes"):
        BasetenSandboxLauncher(image=IMAGE).run("sb", "cat big")
    assert sandbox.process.kills == [sandbox.process.stream_calls[0]["name"]]


@pytest.mark.parametrize(
    ("kill_error", "kill_failure_reported"),
    [(None, False), (_api_error(404, "process not found"), False), (_api_error(500), True)],
    ids=["killed", "already-gone", "kill-failed"],
)
def test_run_kills_process_when_output_stream_fails(
    sandbox: _FakeSandbox, kill_error: Exception | None, kill_failure_reported: bool
) -> None:
    sandbox.process.events = [_out("partial\n")]
    sandbox.process.stream_error = httpx.ReadError("connection reset")
    sandbox.process.kill_error = kill_error

    with pytest.raises(click.ClickException, match="remote process was killed") as exc:
        BasetenSandboxLauncher(image=IMAGE).run("sb", "sleep 600")

    assert "connection reset" in str(exc.value)
    assert sandbox.process.kills == [sandbox.process.stream_calls[0]["name"]]
    assert ("Remote kill also failed" in str(exc.value)) is kill_failure_reported


@pytest.mark.parametrize("call", ["run", "exec_foreground"])
def test_ctrl_c_on_stuck_stream_kills_process_and_reraises(
    sandbox: _FakeSandbox, call: str
) -> None:
    # Ctrl-C reaches the main thread while the stream sends nothing until the
    # process is killed.
    sandbox.process.events = [_out("working\n"), _AWAIT_KILL, _exit(-1, "killed")]
    launcher = BasetenSandboxLauncher(image=IMAGE)
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    interrupt = threading.Timer(0.2, signal.raise_signal, (signal.SIGINT,))
    interrupt.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            getattr(launcher, call)("sb", "sleep 600")
    finally:
        interrupt.cancel()
        signal.signal(signal.SIGINT, previous)

    assert sandbox.process.kills == [sandbox.process.stream_calls[0]["name"]]


@pytest.mark.parametrize(
    ("after_kill", "stream_error"),
    [
        pytest.param([], httpx.ReadError("stream closed"), id="stream-drops"),
        pytest.param(
            [_exit(137, "killed")],
            None,
            id="stream-reports-exit",
        ),
    ],
)
def test_run_timeout_kills_process_and_reports_timeout(
    sandbox: _FakeSandbox,
    monkeypatch: pytest.MonkeyPatch,
    after_kill: list[object],
    stream_error: Exception | None,
) -> None:
    monkeypatch.setattr(launcher_module, "_COMMAND_TIMEOUT_S", 0)
    monkeypatch.setattr(launcher_module, "_COMMAND_KILL_GRACE_S", 0.05)
    # The real SDK stream cannot be closed from the watchdog thread, so only
    # the kill ends it: by dropping the connection or by reporting the exit.
    sandbox.process.events = [_out("started\n"), _AWAIT_KILL, *after_kill]
    sandbox.process.stream_error = stream_error

    with pytest.raises(click.ClickException, match="timed out"):
        BasetenSandboxLauncher(image=IMAGE).run("sb", "sleep 600")
    assert sandbox.process.stream_calls[0]["name"] in sandbox.process.kills


@pytest.mark.parametrize(
    ("exit_event", "message"),
    [
        # Baseten's own exec timeout: the process ends as "killed" with exit -1.
        pytest.param(
            _exit(-1, "killed"), "timed out after 900 seconds and was killed", id="baseten-timeout"
        ),
        # A process that kills itself fails; it is not a timeout.
        pytest.param(_exit(-1, "failed"), r"failed .*\(exit -1\)", id="self-killed"),
    ],
)
def test_run_reports_baseten_timeout_kill_as_timeout(
    sandbox: _FakeSandbox, exit_event: object, message: str
) -> None:
    sandbox.process.events = [_out("started\n"), exit_event]

    with pytest.raises(click.ClickException, match=message):
        BasetenSandboxLauncher(image=IMAGE).run("sb", "sleep 1200")
    assert sandbox.process.stream_calls[0]["timeout"] == timedelta(seconds=900)


def test_run_returns_at_local_deadline_when_sandbox_stops_answering(
    sandbox: _FakeSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher_module, "_COMMAND_TIMEOUT_S", 0)
    monkeypatch.setattr(launcher_module, "_COMMAND_KILL_GRACE_S", 0.05)
    # Neither the exit nor the kill gets through, so the stream stays open.
    sandbox.process.events = [_out("started\n"), _AWAIT_KILL]
    kills: list[str] = []

    def _unanswered_kill(*, identifier: str) -> None:
        kills.append(identifier)
        raise _api_error(503, "sandbox unavailable")

    monkeypatch.setattr(sandbox.process, "kill", _unanswered_kill)
    started = time.perf_counter()

    with pytest.raises(click.ClickException, match="timed out") as exc:
        BasetenSandboxLauncher(image=IMAGE).run("sb", "sleep 600")

    assert time.perf_counter() - started < 2, "run waited for the stuck stream"
    assert "Remote kill also failed" in str(exc.value)
    assert kills == [sandbox.process.stream_calls[0]["name"]]


@pytest.mark.parametrize(
    ("streamed", "recorded", "expected"),
    [
        # Replayed one line per event, without newlines.
        (["a", "b"], "a\nb\n", "a\nb\n"),
        # A record cut short does not replace what was streamed.
        (["0123456789\n"], "0123", "0123456789\n"),
    ],
    ids=["replayed-lines", "record-cut-short"],
)
def test_run_output_is_the_fuller_of_stream_and_record(
    sandbox: _FakeSandbox, streamed: list[str], recorded: str, expected: str
) -> None:
    exit_event = SandboxProcessExecEventExit(
        process=_process_info(status="completed", exit_code=0, stdout=recorded)
    )
    sandbox.process.events = [*(_out(text) for text in streamed), exit_event]

    assert BasetenSandboxLauncher(image=IMAGE).run("sb", "printf 'a\\nb\\n'").stdout == expected


def test_run_retries_gateway_error_under_new_process_name(
    sandbox: _FakeSandbox, clock: _FakeClock
) -> None:
    sandbox.process.start_errors = [_api_error(503, "gateway unavailable")]

    result = BasetenSandboxLauncher(image=IMAGE).run("sb", "true")

    assert result.returncode == 0
    first, second = (call["name"] for call in sandbox.process.stream_calls)
    assert second == f"{first}-1", "a start that committed remotely must not collide"
    assert sandbox.process.kills == [], "a 503 from the edge never reached the sandbox"
    assert clock.sleeps == [0.5]


@pytest.mark.parametrize(
    ("kill_error", "retried"),
    [(None, True), (_api_error(404, "process not found"), True), (_api_error(500), False)],
    ids=["stopped", "never-started", "kill-failed"],
)
def test_run_retries_lost_start_only_once_it_is_stopped(
    sandbox: _FakeSandbox, clock: _FakeClock, kill_error: Exception | None, retried: bool
) -> None:
    # A gateway timeout loses the response: the process may be running.
    sandbox.process.start_errors = [_api_error(504, "gateway timeout")]
    sandbox.process.kill_error = kill_error
    launcher = BasetenSandboxLauncher(image=IMAGE)

    if retried:
        assert launcher.run("sb", "true").returncode == 0
    else:
        with pytest.raises(click.ClickException, match="could not start"):
            launcher.run("sb", "true")

    first = sandbox.process.stream_calls[0]["name"]
    assert sandbox.process.kills == [first]
    assert len(sandbox.process.stream_calls) == (2 if retried else 1)


def test_run_gives_up_after_three_start_attempts(sandbox: _FakeSandbox) -> None:
    sandbox.process.start_errors = [_api_error(503, "gateway unavailable")] * 3

    with pytest.raises(click.ClickException, match=r"could not start.*gateway unavailable"):
        BasetenSandboxLauncher(image=IMAGE).run("sb", "true")

    names = [call["name"] for call in sandbox.process.stream_calls]
    assert names == [names[0], f"{names[0]}-1", f"{names[0]}-2"]


def test_run_does_not_retry_rejected_start(sandbox: _FakeSandbox) -> None:
    sandbox.process.start_errors = [_api_error(400, f"bad request for {API_KEY}")]

    with pytest.raises(click.ClickException, match="could not start") as exc:
        BasetenSandboxLauncher(image=IMAGE).run("sb", "true")

    assert len(sandbox.process.stream_calls) == 1
    assert API_KEY not in str(exc.value)


def test_stream_exec_joins_chunks_into_lines(sandbox: _FakeSandbox) -> None:
    sandbox.process.events = [
        _out("ab"),
        _out("c\nde", "stderr"),
        _out("f\n"),
        _out("tail"),
        _exit(),
    ]

    process = BasetenSandboxLauncher(image=IMAGE).stream_exec("sb", "omnigent login")

    assert process.lines is process.lines, "repeated access resumes one iterator"
    assert list(process.lines) == ["abc\n", "def\n", "tail"]
    call = sandbox.process.stream_calls[0]
    assert call["command"] == "omnigent login"
    assert call["name"].startswith("omni-stream-")
    assert (call["timeout"], call["keep_alive"], call["env"]) == (
        timedelta(0),
        True,
        {"TERM": "xterm-256color"},
    )


def test_stream_exec_flushes_long_partial_line(
    sandbox: _FakeSandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher_module, "_MAX_PARTIAL_LINE_CHARS", 4)
    sandbox.process.events = [_out("abcdef"), _out("gh\n"), _exit()]

    process = BasetenSandboxLauncher(image=IMAGE).stream_exec("sb", "progress")

    assert list(process.lines) == ["abcdef", "gh\n"]


def test_stream_exec_wait_returns_exit_code_and_keeps_unread_lines(
    sandbox: _FakeSandbox,
) -> None:
    sandbox.process.events = [_out("one\ntwo\n"), _out("three\n"), _exit(3, "failed")]
    process = BasetenSandboxLauncher(image=IMAGE).stream_exec("sb", "exit 3")

    assert next(process.lines) == "one\n"
    assert process.wait() == 3
    assert process.wait() == 3
    assert list(process.lines) == ["two\n", "three\n"]

    process.close()
    assert sandbox.process.kills == [], "a process that exited is not killed"


def test_stream_exec_close_kills_running_process_once(sandbox: _FakeSandbox) -> None:
    sandbox.process.events = [_out("waiting for login\n")]
    process = BasetenSandboxLauncher(image=IMAGE).stream_exec("sb", "omnigent login")

    assert next(process.lines) == "waiting for login\n"
    process.close()
    process.close()

    assert sandbox.process.kills == [sandbox.process.stream_calls[0]["name"]]
    assert sandbox.process.streams[0].close_calls == 1
    assert list(process.lines) == [], "a closed process yields no more lines"


def test_exec_foreground_echoes_output_and_returns_exit_code(
    sandbox: _FakeSandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    sandbox.process.events = [_out("hello\n"), _out("warn\n", "stderr"), _exit(5, "failed")]

    assert BasetenSandboxLauncher(image=IMAGE).exec_foreground("sb", "omnigent setup") == 5

    assert capsys.readouterr().out == "hello\nwarn\n"
    call = sandbox.process.stream_calls[0]
    assert call["name"].startswith("omni-foreground-")
    assert (call["timeout"], call["keep_alive"], call["env"]) == (
        timedelta(0),
        True,
        {"TERM": "xterm-256color"},
    )


def test_run_background_moves_env_prefix_out_of_command(sandbox: _FakeSandbox) -> None:
    result = BasetenSandboxLauncher(image=IMAGE).run_background(
        "sb", "OMNIGENT_HOST_TOKEN=token-value OMNIGENT_HOST_ID='host 1' omnigent host --flag"
    )

    call = sandbox.process.exec_calls[0]
    assert call["env"] == {"OMNIGENT_HOST_TOKEN": "token-value", "OMNIGENT_HOST_ID": "host 1"}
    assert "token-value" not in call["command"]
    assert "omnigent host --flag" in call["command"]
    assert "while :; do" in call["command"], "host crashes must be supervised"
    assert call["command"].endswith("> /tmp/omnigent-host.log 2>&1")
    assert call["name"].startswith("omnigent-host-")
    assert call["keep_alive"] is True
    assert call["timeout"] == timedelta(0)
    assert call["wait_for_completion"] is False
    assert result.returncode == 0


@pytest.mark.parametrize(
    ("command", "env", "remainder"),
    [
        ("omnigent host", None, "omnigent host"),
        (
            "OMNIGENT_HOST_TOKEN='token with space' first && second > output",
            {"OMNIGENT_HOST_TOKEN": "token with space"},
            "first && second > output",
        ),
        ('FIRST="x\\"y" SECOND=z omnigent host', {"FIRST": 'x"y', "SECOND": "z"}, "omnigent host"),
    ],
    ids=["no-prefix", "shell-operators", "escaped-quote"],
)
def test_run_background_parses_env_prefix(
    sandbox: _FakeSandbox, command: str, env: dict[str, str] | None, remainder: str
) -> None:
    BasetenSandboxLauncher(image=IMAGE).run_background("sb", command)

    call = sandbox.process.exec_calls[0]
    assert call["env"] == env
    assert remainder in call["command"]


@pytest.mark.parametrize(
    ("command", "message"),
    [
        ("OMNIGENT_HOST_TOKEN=token-value", "environment assignments only"),
        ("OMNIGENT_HOST_TOKEN='unterminated omnigent host", "invalid environment prefix"),
    ],
)
def test_run_background_rejects_bad_env_prefix(
    sandbox: _FakeSandbox, command: str, message: str
) -> None:
    with pytest.raises(click.ClickException, match=message):
        BasetenSandboxLauncher(image=IMAGE).run_background("sb", command)
    assert sandbox.process.exec_calls == []


@pytest.mark.parametrize("status", ["completed", "failed", "killed", "stopped"])
def test_run_background_rejects_process_that_already_ended(
    sandbox: _FakeSandbox, status: str
) -> None:
    sandbox.process.exec_status = status

    with pytest.raises(click.ClickException, match="failed to start"):
        BasetenSandboxLauncher(image=IMAGE).run_background("sb", "omnigent host")


def test_run_background_redacts_env_values_from_start_error(sandbox: _FakeSandbox) -> None:
    sandbox.process.exec_error = _api_error(500, "rejected token-value")

    with pytest.raises(click.ClickException, match="failed to start") as exc:
        BasetenSandboxLauncher(image=IMAGE).run_background(
            "sb", "OMNIGENT_HOST_TOKEN=token-value omnigent host"
        )
    assert "token-value" not in str(exc.value)
    assert "[redacted]" in str(exc.value)


def test_put_writes_file_bytes(sandbox: _FakeSandbox, tmp_path: Path) -> None:
    source = tmp_path / "payload.bin"
    source.write_bytes(b"\x00\xffpayload")

    BasetenSandboxLauncher(image=IMAGE).put("sb", source, "/tmp/payload.bin")

    assert sandbox.fs.writes == [("/tmp/payload.bin", b"\x00\xffpayload")]


def test_put_reports_write_failure(sandbox: _FakeSandbox, tmp_path: Path) -> None:
    source = tmp_path / "payload.bin"
    source.write_bytes(b"payload")
    sandbox.fs.error = _api_error(507, "disk full")

    with pytest.raises(click.ClickException, match=r"Could not copy a file.*disk full"):
        BasetenSandboxLauncher(image=IMAGE).put("sb", source, "/tmp/payload.bin")


def test_terminate_missing_sandbox_is_a_noop(client: _FakeClient) -> None:
    BasetenSandboxLauncher(image=IMAGE).terminate("sb")

    assert client.deleted == ["sb"]
    assert client.get_info_calls == []


@pytest.mark.parametrize(
    "after_delete",
    [["DELETING", "DELETING", "TERMINATED"], ["DELETING", _api_error(404, "sandbox not found")]],
    ids=["terminated", "gone"],
)
def test_terminate_waits_until_sandbox_is_gone(
    client: _FakeClient, after_delete: list[str | Exception]
) -> None:
    client.records["sb"] = _Record()
    client.after_delete = after_delete

    BasetenSandboxLauncher(image=IMAGE).terminate("sb")

    assert client.deleted == ["sb"]
    assert client.get_info_calls == ["sb"] * len(after_delete)


def test_terminate_rides_out_transient_control_plane_errors(client: _FakeClient) -> None:
    client.records["sb"] = _Record()
    client.after_delete = ["DELETING", _api_error(502, "bad gateway"), "TERMINATED"]

    BasetenSandboxLauncher(image=IMAGE).terminate("sb")

    assert client.get_info_calls == ["sb"] * 3


def test_terminate_times_out_with_last_status(client: _FakeClient) -> None:
    client.records["sb"] = _Record()
    client.after_delete = ["DELETING"]

    with pytest.raises(
        click.ClickException, match="did not terminate within 120 seconds; last status: DELETING"
    ):
        BasetenSandboxLauncher(image=IMAGE).terminate("sb")


def test_terminate_reports_delete_failure_without_leaking_key(client: _FakeClient) -> None:
    client.delete_error = _api_error(500, f"delete failed for {API_KEY}")

    with pytest.raises(click.ClickException, match="Could not delete Baseten sandbox 'sb'") as exc:
        BasetenSandboxLauncher(image=IMAGE).terminate("sb")
    assert API_KEY not in str(exc.value)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("DEPLOYED", True),
        ("TERMINATED", False),
        ("FAILED", False),
        ("DELETING", False),
        ("ARCHIVED", False),
        ("DEACTIVATED", None),
        ("DEPLOYING", None),
    ],
)
def test_is_running_maps_sandbox_status(
    client: _FakeClient, status: str, expected: bool | None
) -> None:
    client.records["sb"] = _Record(statuses=[status])

    assert BasetenSandboxLauncher(image=IMAGE).is_running("sb") is expected


def test_is_running_missing_sandbox_is_false(client: _FakeClient) -> None:
    assert BasetenSandboxLauncher(image=IMAGE).is_running("sb") is False


def test_is_running_surfaces_provider_outage(client: _FakeClient) -> None:
    client.get_info_error = _api_error(503, "service unavailable")

    with pytest.raises(click.ClickException, match="service unavailable"):
        BasetenSandboxLauncher(image=IMAGE).is_running("sb")


@pytest.mark.parametrize("status", ["TERMINATED", "FAILED"])
def test_attach_rejects_stopped_sandbox(client: _FakeClient, status: str) -> None:
    client.records["sb"] = _Record(statuses=[status])

    with pytest.raises(click.ClickException, match=f"is {status.lower()}"):
        BasetenSandboxLauncher(image=IMAGE).attach("sb")
    assert "sb" not in client.sandboxes


def test_attach_rejects_missing_sandbox(client: _FakeClient) -> None:
    with pytest.raises(click.ClickException, match="was not found"):
        BasetenSandboxLauncher(image=IMAGE).attach("sb")


def test_attach_retries_until_sandbox_api_answers(
    sandbox: _FakeSandbox, clock: _FakeClock
) -> None:
    sandbox.process.list_errors = [_api_error(503, "waking"), httpx.ConnectError("waking")]

    BasetenSandboxLauncher(image=IMAGE).attach("sb")

    assert sandbox.process.list_calls == 3
    assert clock.sleeps == [0.5, 1.0]


def test_attach_does_not_retry_rejected_readiness_check(sandbox: _FakeSandbox) -> None:
    sandbox.process.list_errors = [_api_error(401, f"bad key {API_KEY}")]

    with pytest.raises(click.ClickException, match="Could not attach") as exc:
        BasetenSandboxLauncher(image=IMAGE).attach("sb")

    assert sandbox.process.list_calls == 1
    assert API_KEY not in str(exc.value)


def test_attach_gives_up_at_readiness_timeout(sandbox: _FakeSandbox, clock: _FakeClock) -> None:
    sandbox.process.list_errors = [_api_error(503, "waking")] * 100

    with pytest.raises(click.ClickException, match="Could not attach"):
        BasetenSandboxLauncher(image=IMAGE).attach("sb")

    assert sum(clock.sleeps) == pytest.approx(90.0)
