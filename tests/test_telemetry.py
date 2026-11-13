"""Unit tests for the usage telemetry helpers."""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from omnigent.telemetry.surface import (
    CLIENT_SURFACE_LABEL_KEY,
    CLIENT_SURFACES,
    classify_surface,
    resolve_client_surface,
    resolve_session_surface,
)

# ── classify_surface ────────────────────────────────────────────────────────


def test_classify_surface_none() -> None:
    """``None`` UA → ``"unknown"``."""
    assert classify_surface(None) == "unknown"


def test_classify_surface_electron() -> None:
    """Electron UA → ``"desktop"``."""
    assert classify_surface("Mozilla/5.0 (Macintosh) Electron/28.0") == "desktop"


def test_classify_surface_iphone() -> None:
    """iPhone UA → ``"ios"``."""
    assert classify_surface("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)") == "ios"


def test_classify_surface_ipad() -> None:
    """iPad UA → ``"ios"``."""
    assert classify_surface("Mozilla/5.0 (iPad; CPU OS 17_0)") == "ios"


def test_classify_surface_android() -> None:
    """Android UA → ``"android"``."""
    assert classify_surface("Mozilla/5.0 (Linux; Android 14) Mobile Safari/537.36") == "android"


@pytest.mark.parametrize(
    ("user_agent", "surface"),
    [
        ("omnigent-cli/0.22.0 python-httpx/0.28.1", "cli"),
        ("omnigent-runner/0.22.0 python-httpx/0.28.1", "runner"),
        ("omnigent-host/0.22.0 python-httpx/0.28.1", "host"),
        ("omnigent-harness/0.22.0 (claude-sdk) python-httpx/0.28.1", "harness"),
        ("omnigent-client/0.22.0 python-httpx/0.28.1", "sdk"),
        ("omnigent-slack/0.22.0 python-httpx/0.28.1", "slack"),
    ],
)
def test_classify_surface_omnigent_components(user_agent: str, surface: str) -> None:
    """Omnigent's own clients are named by their ``omnigent-<component>/`` token."""
    assert classify_surface(user_agent) == surface


@pytest.mark.parametrize(
    ("user_agent", "surface"),
    [
        # Main parity: bare httpx/requests and empty UAs are the CLI, host and runner
        # releases 0.17-0.21, which send no component token.
        ("", "cli"),
        ("python-httpx/0.28.1", "cli"),
        ("python-httpx/0.27.0", "cli"),
        ("python-requests/2.32.3", "cli"),
        ("Python-urllib/3.12", "script"),
        ("node", "script"),
        ("Bun/1.1.38", "script"),
        ("porthole-mcp/1.0", "script"),
        ("curl/8.5.0", "script"),
        ("testclient", "script"),
        ("Python/3.12 websockets/14.2", "script"),
    ],
)
def test_classify_surface_main_parity(user_agent: str, surface: str) -> None:
    """Bare httpx/requests and empty stay ``cli`` as on main; other non-browsers are ``script``."""
    assert classify_surface(user_agent) == surface


def test_classify_surface_regular_browser() -> None:
    """Regular browser UA → ``"web"``."""
    assert (
        classify_surface(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36"
        )
        == "web"
    )


# ── is_disabled ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("surface", sorted(CLIENT_SURFACES))
def test_client_surface_header_overrides_transport_user_agent(surface: str) -> None:
    assert (
        resolve_client_surface({"x-omnigent-client": surface, "user-agent": "python-httpx/0.28"})
        == surface
    )


@pytest.mark.parametrize("header", [None, "unrecognised", "desktop\r\ninjected: value"])
def test_client_surface_invalid_header_uses_user_agent(header: str | None) -> None:
    headers = {"user-agent": "omnigent-runner/0.22.0 python-httpx/0.28"}
    if header is not None:
        headers["x-omnigent-client"] = header
    assert resolve_client_surface(headers) == "runner"


def test_session_surface_header_stores_only_user_surfaces() -> None:
    """A user surface in the header is stored; an internal one is not."""
    assert resolve_session_surface({"x-omnigent-client": "ios"}) == "ios"
    headers = {"x-omnigent-client": "runner", "user-agent": "curl/8"}
    assert resolve_session_surface(headers) == "web"


@pytest.mark.parametrize(
    "headers",
    [
        {"x-omnigent-client": "sdk"},
        {"x-omnigent-client": "slack"},
        {"user-agent": "omnigent-client/0.22.0 python-httpx/0.28.1"},
        {"user-agent": "omnigent-slack/0.1.0 python-httpx/0.28.1"},
    ],
)
def test_sdk_and_slack_sessions_store_cli(headers: dict[str, str]) -> None:
    """SDK and Slack sessions store ``cli``, the label older runners accept."""
    assert resolve_session_surface(headers) == "cli"


def test_session_surface_inherits_parent_before_user_agent() -> None:
    parent = {CLIENT_SURFACE_LABEL_KEY: "desktop"}
    assert (
        resolve_session_surface({"user-agent": "python-httpx/0.28"}, parent_labels=parent)
        == "desktop"
    )
    assert resolve_session_surface({"x-omnigent-client": "ios"}, parent_labels=parent) == "ios"


@pytest.mark.parametrize("component", ["runner", "host", "harness"])
def test_internal_recreate_keeps_requested_surface(component: str) -> None:
    """A runner/host/harness re-creating a session (native /clear) keeps the copied surface."""
    headers = {"user-agent": client_user_agent_for(component)}
    requested = {CLIENT_SURFACE_LABEL_KEY: "desktop"}
    assert resolve_session_surface(headers, requested_labels=requested) == "desktop"
    # A bogus copied value falls back to what main gave the request: the CLI.
    bogus = {CLIENT_SURFACE_LABEL_KEY: "evil"}
    assert resolve_session_surface(headers, requested_labels=bogus) == "cli"


@pytest.mark.parametrize("component", ["runner", "host", "harness"])
def test_internal_session_takes_originating_surface(component: str) -> None:
    """An internal component creating a session under another surface takes that surface."""
    headers = {"user-agent": client_user_agent_for(component)}
    assert resolve_session_surface(headers, source_labels={CLIENT_SURFACE_LABEL_KEY: "web"}) == (
        "web"
    )
    # An internal component is never stored, so a source labelled with one is ignored.
    internal_source = {CLIENT_SURFACE_LABEL_KEY: component}
    assert resolve_session_surface(headers, source_labels=internal_source) == "cli"


def test_internal_session_precedence_parent_then_source_then_body() -> None:
    headers = {"user-agent": client_user_agent_for("runner")}
    parent = {CLIENT_SURFACE_LABEL_KEY: "desktop"}
    source = {CLIENT_SURFACE_LABEL_KEY: "web"}
    body = {CLIENT_SURFACE_LABEL_KEY: "ios"}
    assert resolve_session_surface(headers, parent_labels=parent, source_labels=source) == (
        "desktop"
    )
    assert resolve_session_surface(headers, source_labels=source, requested_labels=body) == "web"
    assert resolve_session_surface(headers, requested_labels=body) == "ios"


@pytest.mark.parametrize(
    ("user_agent", "surface"),
    [("omnigent-cli/0.22.0", "cli"), ("python-httpx/0.28.1", "cli"), (None, "unknown")],
)
def test_external_create_ignores_originating_surface_labels(
    user_agent: str | None, surface: str
) -> None:
    """Only Omnigent's internal components may carry a surface over from another session."""
    headers = {"user-agent": user_agent} if user_agent else {}
    labels = {CLIENT_SURFACE_LABEL_KEY: "desktop"}
    assert (
        resolve_session_surface(headers, requested_labels=labels, source_labels=labels) == surface
    )


def test_runner_spawned_child_session_keeps_parent_surface() -> None:
    """A runner creating a child session doesn't relabel a desktop session as runner."""
    parent = {CLIENT_SURFACE_LABEL_KEY: "desktop"}
    headers = {"user-agent": client_user_agent_for("runner")}
    assert resolve_session_surface(headers, parent_labels=parent) == "desktop"


# ── client identity User-Agent ──────────────────────────────────────────────


def client_user_agent_for(component: str, detail: str | None = None) -> str:
    """Return the User-Agent a process of *component* would send."""
    import os

    from omnigent.telemetry.request_headers import (
        CLIENT_COMPONENT_ENV_VAR,
        client_user_agent,
        set_client_component,
    )

    saved = os.environ.get(CLIENT_COMPONENT_ENV_VAR)
    try:
        set_client_component(component, detail)
        return client_user_agent()
    finally:
        if saved is None:
            os.environ.pop(CLIENT_COMPONENT_ENV_VAR, None)
        else:
            os.environ[CLIENT_COMPONENT_ENV_VAR] = saved


def test_client_user_agent_defaults_to_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from omnigent.telemetry.request_headers import CLIENT_COMPONENT_ENV_VAR, client_user_agent
    from omnigent.version import VERSION

    monkeypatch.delenv(CLIENT_COMPONENT_ENV_VAR, raising=False)
    assert client_user_agent() == f"omnigent-cli/{VERSION} python-httpx/{httpx.__version__}"


@pytest.mark.parametrize(
    ("component", "detail", "surface"),
    [
        ("cli", None, "cli"),
        ("host", None, "host"),
        ("runner", None, "runner"),
        ("harness", "claude-sdk", "harness"),
    ],
)
def test_client_user_agent_round_trips_through_classifier(
    component: str, detail: str | None, surface: str
) -> None:
    user_agent = client_user_agent_for(component, detail)
    assert user_agent.startswith(f"omnigent-{component}/")
    assert " python-httpx/" in user_agent
    assert classify_surface(user_agent) == surface


def test_client_component_is_inherited_by_child_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hook or forwarder a runner spawns identifies as that runner."""
    import subprocess
    import sys

    from omnigent.telemetry.request_headers import CLIENT_COMPONENT_ENV_VAR, set_client_component

    # setenv first so monkeypatch restores the variable set_client_component writes.
    monkeypatch.setenv(CLIENT_COMPONENT_ENV_VAR, "cli")
    set_client_component("runner")
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "from omnigent.telemetry.request_headers import client_user_agent;"
            "print(client_user_agent())",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert classify_surface(child.stdout.strip()) == "runner"


def test_client_user_agent_names_harness_and_strips_unsafe_detail() -> None:
    assert " (claude-sdk) " in client_user_agent_for("harness", "claude-sdk")
    # Header-injection characters in the qualifier are dropped, not sent.
    assert " (claudesdkx) " in client_user_agent_for("harness", "claude sdk\r\nx")


def test_set_client_component_rejects_unknown_component() -> None:
    from omnigent.telemetry.request_headers import set_client_component

    with pytest.raises(ValueError, match="unknown client component"):
        set_client_component("serverless")


def test_client_user_agent_unknown_component_falls_back_to_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.telemetry.request_headers import CLIENT_COMPONENT_ENV_VAR, client_user_agent

    monkeypatch.setenv(CLIENT_COMPONENT_ENV_VAR, "evil/x")
    assert client_user_agent().startswith("omnigent-cli/")
    assert "(" not in client_user_agent()


async def test_server_request_headers_carry_component_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared server-request header builder names the calling component.

    Through ``open_server_client`` (request hooks included) a caller's
    User-Agent in any casing is replaced by the component's, never duplicated.
    """
    import httpx

    from omnigent.cli_auth import databricks_request_headers, open_server_client
    from omnigent.telemetry.request_headers import CLIENT_COMPONENT_ENV_VAR

    monkeypatch.setenv(CLIENT_COMPONENT_ENV_VAR, "runner")
    headers = databricks_request_headers("http://127.0.0.1:6767")
    assert classify_surface(headers["User-Agent"]) == "runner"

    runner_ua = client_user_agent_for("runner")
    sent: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers.get_list("User-Agent"))
        return httpx.Response(200)

    for name in ("User-Agent", "user-agent", "USER-AGENT", "uSeR-aGeNt"):
        async with open_server_client(
            "http://127.0.0.1:6767",
            headers={name: "caller-agent/1.0"},
            transport=httpx.MockTransport(handler),
        ) as client:
            await client.get("http://127.0.0.1:6767/health")
        assert sent[-1] == [runner_ua], name


async def test_sdk_client_sends_sdk_user_agent_unless_overridden() -> None:
    from omnigent_client import OmnigentClient

    url = "http://127.0.0.1:1/health"
    async with OmnigentClient(base_url="http://127.0.0.1:1") as client:
        request = client._http.build_request("GET", url)
        assert request.headers.get_list("User-Agent") == [request.headers["User-Agent"]]
        assert classify_surface(request.headers["User-Agent"]) == "sdk"
    runner_ua = client_user_agent_for("runner")
    for name in ("User-Agent", "user-agent", "USER-AGENT", "uSeR-aGeNt"):
        async with OmnigentClient(
            base_url="http://127.0.0.1:1", headers={name: runner_ua}
        ) as client:
            request = client._http.build_request("GET", url)
            assert request.headers.get_list("User-Agent") == [runner_ua], name


@pytest.fixture(autouse=True)
def _reset_is_disabled_cache():
    """Reset the is_disabled() cache before each test so env patches take effect."""
    import omnigent.telemetry.client as _mod

    _mod._IS_DISABLED_CACHE[0] = None
    yield
    _mod._IS_DISABLED_CACHE[0] = None


def test_is_disabled_omnigent_analytics_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """``OMNIGENT_ANALYTICS=0`` disables telemetry."""
    monkeypatch.setenv("OMNIGENT_ANALYTICS", "0")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    from omnigent.telemetry.client import is_disabled

    assert is_disabled() is True


def test_is_disabled_do_not_track(monkeypatch: pytest.MonkeyPatch) -> None:
    """``DO_NOT_TRACK=1`` disables telemetry."""
    monkeypatch.delenv("OMNIGENT_ANALYTICS", raising=False)
    monkeypatch.delenv("OMNIGENT_DISABLE_TELEMETRY", raising=False)
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    from omnigent.telemetry.client import is_disabled

    assert is_disabled() is True


def test_is_disabled_ci_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CI=true`` disables telemetry."""
    monkeypatch.delenv("OMNIGENT_ANALYTICS", raising=False)
    monkeypatch.delenv("OMNIGENT_DISABLE_TELEMETRY", raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.setenv("CI", "true")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    from omnigent.telemetry.client import is_disabled

    assert is_disabled() is True


def test_is_disabled_github_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    """``GITHUB_ACTIONS=true`` disables telemetry."""
    monkeypatch.delenv("OMNIGENT_ANALYTICS", raising=False)
    monkeypatch.delenv("OMNIGENT_DISABLE_TELEMETRY", raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    from omnigent.telemetry.client import is_disabled

    assert is_disabled() is True


def test_is_disabled_none_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """When none of the opt-out vars are set, telemetry is enabled."""
    _ci_vars = [
        "OMNIGENT_ANALYTICS",
        "OMNIGENT_DISABLE_TELEMETRY",
        "DO_NOT_TRACK",
        "CI",
        "GITHUB_ACTIONS",
        "PYTEST_CURRENT_TEST",
        "CIRCLECI",
        "JENKINS_URL",
        "TRAVIS",
        "GITLAB_CI",
        "TF_BUILD",
        "BITBUCKET_BUILD_NUMBER",
        "CODEBUILD_BUILD_ARN",
        "BUILDKITE",
        "TEAMCITY_VERSION",
    ]
    for var in _ci_vars:
        monkeypatch.delenv(var, raising=False)
    from omnigent.telemetry.client import is_disabled

    assert is_disabled() is False


# ── is_disabled — DISABLE_TELEMETRY alias ───────────────────────────────────


def test_is_disabled_disable_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """``DISABLE_TELEMETRY=true`` disables telemetry."""
    monkeypatch.delenv("OMNIGENT_ANALYTICS", raising=False)
    monkeypatch.setenv("DISABLE_TELEMETRY", "true")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    from omnigent.telemetry.client import is_disabled

    assert is_disabled() is True


def test_is_disabled_omnigent_disable_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """``OMNIGENT_DISABLE_TELEMETRY=1`` disables telemetry."""
    monkeypatch.delenv("OMNIGENT_ANALYTICS", raising=False)
    monkeypatch.setenv("OMNIGENT_DISABLE_TELEMETRY", "1")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    from omnigent.telemetry.client import is_disabled

    assert is_disabled() is True


# ── is_disabled — config.yaml ────────────────────────────────────────────────


_ALL_OPT_OUT_VARS = [
    "OMNIGENT_ANALYTICS",
    "DISABLE_TELEMETRY",
    "OMNIGENT_DISABLE_TELEMETRY",
    "DO_NOT_TRACK",
    "CI",
    "GITHUB_ACTIONS",
    "PYTEST_CURRENT_TEST",
    "CIRCLECI",
    "JENKINS_URL",
    "TRAVIS",
    "GITLAB_CI",
    "TF_BUILD",
    "BITBUCKET_BUILD_NUMBER",
    "CODEBUILD_BUILD_ARN",
    "BUILDKITE",
    "TEAMCITY_VERSION",
]


def test_is_disabled_config_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``telemetry: false`` in config.yaml disables telemetry."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("telemetry: false\n", encoding="utf-8")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    for var in _ALL_OPT_OUT_VARS:
        monkeypatch.delenv(var, raising=False)
    from omnigent.telemetry.client import is_disabled

    assert is_disabled() is True


def test_is_disabled_config_yaml_telemetry_true(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``telemetry: true`` in config.yaml does NOT disable telemetry."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("telemetry: true\n", encoding="utf-8")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    for var in _ALL_OPT_OUT_VARS:
        monkeypatch.delenv(var, raising=False)
    from omnigent.telemetry.client import is_disabled

    assert is_disabled() is False


# ── init_client — server_config ──────────────────────────────────────────────


def test_init_client_server_config_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """``init_client(config={'telemetry': False})`` skips client creation."""
    import omnigent.telemetry.client as _mod

    for var in [
        "OMNIGENT_ANALYTICS",
        "DISABLE_TELEMETRY",
        "OMNIGENT_DISABLE_TELEMETRY",
        "DO_NOT_TRACK",
        "CI",
        "GITHUB_ACTIONS",
        "PYTEST_CURRENT_TEST",
        "CIRCLECI",
        "JENKINS_URL",
        "TRAVIS",
        "GITLAB_CI",
        "TF_BUILD",
        "BITBUCKET_BUILD_NUMBER",
        "CODEBUILD_BUILD_ARN",
        "BUILDKITE",
        "TEAMCITY_VERSION",
    ]:
        monkeypatch.delenv(var, raising=False)

    original_client = _mod._CLIENT
    try:
        monkeypatch.setattr(_mod, "_CLIENT", None)
        _mod.init_client(config={"telemetry": False})
        assert _mod._CLIENT is None
    finally:
        monkeypatch.setattr(_mod, "_CLIENT", original_client)


# ── _fetch_remote_config — rollout percentage ─────────────────────────────────


@pytest.mark.parametrize(
    ("rollout", "sample", "included"),
    [
        (0, 0.0, False),
        (50, 0.499, True),
        (50, 0.5, False),
        (100, 0.999, True),
    ],
)
def test_fetch_remote_config_respects_rollout_percentage_boundaries(
    rollout: int,
    sample: float,
    included: bool,
) -> None:
    """Rollout percentages include exactly their half-open share of samples."""
    import omnigent.telemetry.client as _mod

    config = {
        "omnigent_version": _mod.VERSION,
        "ingestion_url": "https://telemetry.example.test",
        "rollout_percentage": rollout,
    }
    with (
        patch("urllib.request.urlopen") as urlopen,
        patch.object(_mod.random, "random", return_value=sample) as random_sample,
    ):
        response = urlopen.return_value.__enter__.return_value
        response.read.return_value = json.dumps(config).encode("utf-8")
        result = _mod._fetch_remote_config()

    random_sample.assert_called_once_with()
    assert (result is not None) is included


def test_fetch_remote_config_accepts_default_version() -> None:
    """A config with omnigent_version='default' is accepted for any client version."""
    import omnigent.telemetry.client as _mod

    config = {
        "omnigent_version": "default",
        "ingestion_url": "https://telemetry.example.test",
        "rollout_percentage": 100,
    }
    with patch("urllib.request.urlopen") as urlopen:
        response = urlopen.return_value.__enter__.return_value
        response.read.return_value = json.dumps(config).encode("utf-8")
        result = _mod._fetch_remote_config()

    assert result is not None


# ── get_installation_id ──────────────────────────────────────────────────────


def test_get_installation_id_creates_uuid(tmp_path: Path) -> None:
    """First call generates a valid UUID and writes it to disk."""
    import omnigent.telemetry.installation_id as _mod

    telemetry_file = tmp_path / "telemetry.json"

    with (
        patch.object(_mod, "_cache_initialized", False),
        patch.object(_mod, "_cache", None),
        patch.object(_mod, "_CACHE_LOCK", threading.RLock()),
        patch(
            "omnigent.telemetry.installation_id._telemetry_file_path", return_value=telemetry_file
        ),
    ):
        result = _mod.get_installation_id()

    assert result is not None
    uuid.UUID(result)  # raises if invalid
    assert telemetry_file.exists()
    data = json.loads(telemetry_file.read_text())
    assert data["installation_id"] == result


def test_get_installation_id_reads_existing(tmp_path: Path) -> None:
    """If the file already exists, the stored ID is returned."""
    import omnigent.telemetry.installation_id as _mod

    existing_id = str(uuid.uuid4())
    telemetry_file = tmp_path / "telemetry.json"
    telemetry_file.write_text(
        json.dumps({"installation_id": existing_id, "schema_version": 1}),
        encoding="utf-8",
    )

    with (
        patch.object(_mod, "_cache_initialized", False),
        patch.object(_mod, "_cache", None),
        patch.object(_mod, "_CACHE_LOCK", threading.RLock()),
        patch(
            "omnigent.telemetry.installation_id._telemetry_file_path", return_value=telemetry_file
        ),
    ):
        result = _mod.get_installation_id()

    assert result == existing_id


def test_get_installation_id_cache(tmp_path: Path) -> None:
    """Second call returns the same value from the in-memory cache."""
    import omnigent.telemetry.installation_id as _mod

    telemetry_file = tmp_path / "telemetry.json"

    with (
        patch.object(_mod, "_cache_initialized", False),
        patch.object(_mod, "_cache", None),
        patch.object(_mod, "_CACHE_LOCK", threading.RLock()),
        patch(
            "omnigent.telemetry.installation_id._telemetry_file_path", return_value=telemetry_file
        ),
    ):
        first = _mod.get_installation_id()
        # Reset only the path patch; cache flags remain as set by first call.
        second = _mod.get_installation_id()

    assert first == second


def test_get_installation_id_corrupted_file(tmp_path: Path) -> None:
    """Corrupted JSON on disk returns ``None`` gracefully."""
    import omnigent.telemetry.installation_id as _mod

    telemetry_file = tmp_path / "telemetry.json"
    telemetry_file.write_text("not valid json{{{{", encoding="utf-8")

    with (
        patch.object(_mod, "_cache_initialized", False),
        patch.object(_mod, "_cache", None),
        patch.object(_mod, "_CACHE_LOCK", threading.RLock()),
        patch(
            "omnigent.telemetry.installation_id._telemetry_file_path", return_value=telemetry_file
        ),
        # Make _write_to_disk fail so we get None back rather than a fresh ID.
        patch(
            "omnigent.telemetry.installation_id._write_to_disk", side_effect=OSError("disk full")
        ),
    ):
        result = _mod.get_installation_id()

    # Corruption + write failure: either None or a freshly generated UUID.
    # What must NOT happen is an exception propagating to the caller.
    assert result is None or (isinstance(result, str) and len(result) > 0)


# ── host_installation_id ──────────────────────────────────────────────────────


def test_host_hello_frame_roundtrip_with_installation_id() -> None:
    """``HostHelloFrame`` with ``installation_id`` survives encode/decode."""
    from unittest.mock import patch

    from omnigent.host.frames import HostHelloFrame, decode_host_frame, encode_host_frame
    from omnigent.runtime import telemetry as _telemetry_mod

    frame = HostHelloFrame(
        version="0.1.0",
        frame_protocol_version=1,
        name="test-host",
        installation_id="abc-123",
    )
    with (
        patch.object(_telemetry_mod, "record_message_payload"),
        patch.object(_telemetry_mod, "inject_trace_context"),
    ):
        wire = encode_host_frame(frame)
    decoded = decode_host_frame(wire)
    assert isinstance(decoded, HostHelloFrame)
    assert decoded.installation_id == "abc-123"


def test_host_hello_frame_roundtrip_none_installation_id() -> None:
    """``HostHelloFrame`` with ``installation_id=None`` survives encode/decode."""
    from unittest.mock import patch

    from omnigent.host.frames import HostHelloFrame, decode_host_frame, encode_host_frame
    from omnigent.runtime import telemetry as _telemetry_mod

    frame = HostHelloFrame(
        version="0.1.0",
        frame_protocol_version=1,
        name="test-host",
        installation_id=None,
    )
    with (
        patch.object(_telemetry_mod, "record_message_payload"),
        patch.object(_telemetry_mod, "inject_trace_context"),
    ):
        wire = encode_host_frame(frame)
    decoded = decode_host_frame(wire)
    assert isinstance(decoded, HostHelloFrame)
    assert decoded.installation_id is None


def test_host_registry_get_host_installation_id_unregistered() -> None:
    """``get_host_installation_id`` returns ``None`` when host is not registered."""
    from omnigent.server.host_registry import HostRegistry

    registry = HostRegistry()
    assert registry.get_host_installation_id("host_nonexistent") is None


def test_host_registry_get_host_installation_id_registered() -> None:
    """``get_host_installation_id`` returns the ID from the hello frame."""
    from unittest.mock import AsyncMock

    from omnigent.host.frames import HostHelloFrame
    from omnigent.server.host_registry import HostRegistry

    registry = HostRegistry()
    hello = HostHelloFrame(
        version="0.1.0",
        frame_protocol_version=1,
        name="test-host",
        installation_id="inst-xyz",
    )
    ws = AsyncMock()

    # Use register() directly — it doesn't need an event loop.
    conn = registry.register(host_id="host_abc", ws=ws, hello=hello, owner=None)
    assert conn is not None
    assert registry.get_host_installation_id("host_abc") == "inst-xyz"


# ── PolicyRegisteredEvent ────────────────────────────────────────────────────


def test_build_record_policy_registered_event_admin_scope() -> None:
    """``_build_record`` serialises admin-scope ``PolicyRegisteredEvent`` correctly.

    ``installation_id``, ``session_id``, and ``anon_user_id`` are promoted to
    top-level ``data`` fields; ``handler``, ``policy_type``, and ``scope`` go
    into ``params``.  Admin policies have ``session_id=None`` which becomes
    an empty string in the wire format.
    """
    import omnigent.telemetry.client as _mod
    from omnigent.telemetry.events import PolicyRegisteredEvent

    event = PolicyRegisteredEvent(
        installation_id="inst-abc",
        handler="omnigent.policies.builtins.safety.ask_on_os_tools",
        policy_type="python",
        scope="admin",
        session_id=None,
        anon_user_id="deadbeef01234567",
    )
    record = _mod._build_record(event)
    data = record["data"]

    assert data["event_name"] == "PolicyRegisteredEvent"
    assert data["installation_id"] == "inst-abc"
    assert data["anon_user_id"] == "deadbeef01234567"
    # session_id=None → empty string sentinel in wire format
    assert data["session_id"] == ""
    # Event-specific fields live in params, not at top level.
    params = json.loads(data["params"]) if data["params"] else {}
    assert params["handler"] == "omnigent.policies.builtins.safety.ask_on_os_tools"
    assert params["policy_type"] == "python"
    assert params["scope"] == "admin"
    # Promoted fields must not leak into params.
    assert "installation_id" not in params
    assert "session_id" not in params
    assert "anon_user_id" not in params


def test_build_record_policy_registered_event_session_scope() -> None:
    """``_build_record`` serialises session-scope ``PolicyRegisteredEvent`` correctly.

    ``session_id`` is a real string for session-scoped policies and must appear
    as the top-level ``session_id`` field, not inside ``params``.
    """
    import omnigent.telemetry.client as _mod
    from omnigent.telemetry.events import PolicyRegisteredEvent

    event = PolicyRegisteredEvent(
        installation_id="inst-xyz",
        handler="https://example.com/policies/eval",
        policy_type="url",
        scope="session",
        session_id="sess_abc123",
        anon_user_id=None,
    )
    record = _mod._build_record(event)
    data = record["data"]

    assert data["event_name"] == "PolicyRegisteredEvent"
    assert data["session_id"] == "sess_abc123"
    assert data["anon_user_id"] is None
    params = json.loads(data["params"]) if data["params"] else {}
    assert params["scope"] == "session"
    assert params["handler"] == "https://example.com/policies/eval"
    assert params["policy_type"] == "url"
    assert "session_id" not in params


def test_build_record_promotes_host_installation_id() -> None:
    """``_build_record`` lifts ``host_installation_id`` to top-level data."""
    import omnigent.telemetry.client as _mod
    from omnigent.telemetry.events import SessionCreatedEvent

    event = SessionCreatedEvent(
        installation_id="server-inst-id",
        session_id="sess_001",
        agent_id=None,
        harness="claude-native",
        surface="web",
        anon_user_id=None,
        host_installation_id="host-inst-abc",
        is_fork=False,
        is_sub_agent=False,
    )
    record = _mod._build_record(event)
    data = record["data"]
    assert data["host_installation_id"] == "host-inst-abc"
    params = json.loads(data["params"]) if data["params"] else {}
    assert "host_installation_id" not in params


def test_session_created_event_agent_name_polly() -> None:
    """``agent_name`` is set for polly sessions."""
    from omnigent.telemetry.events import SessionCreatedEvent

    event = SessionCreatedEvent(
        installation_id=None,
        session_id="sess_polly",
        agent_id="ag_001",
        harness="claude-sdk",
        surface="web",
        anon_user_id=None,
        host_installation_id=None,
        is_fork=False,
        is_sub_agent=False,
        agent_name="polly",
    )
    assert event.agent_name == "polly"


def test_session_created_event_agent_name_none_for_custom() -> None:
    """``agent_name`` defaults to ``None`` for custom (non-built-in) agents."""
    from omnigent.telemetry.events import SessionCreatedEvent

    event = SessionCreatedEvent(
        installation_id=None,
        session_id="sess_custom",
        agent_id="ag_002",
        harness="claude-sdk",
        surface="web",
        anon_user_id=None,
        host_installation_id=None,
        is_fork=False,
        is_sub_agent=False,
    )
    assert event.agent_name is None


# ── _resolve_harness ─────────────────────────────────────────────────────────


def test_resolve_harness_none_when_conv_is_none() -> None:
    """``None`` conversation → ``None``."""
    from omnigent.server.routes.sessions import _resolve_harness

    assert _resolve_harness(None) is None


def test_resolve_harness_uses_harness_override() -> None:
    """``conv.harness_override`` is returned immediately, no store lookup."""
    from unittest.mock import MagicMock

    from omnigent.server.routes.sessions import _resolve_harness

    conv = MagicMock()
    conv.harness_override = "claude-sdk"
    assert _resolve_harness(conv) == "claude-sdk"


def test_resolve_harness_none_when_agent_store_uninitialized() -> None:
    """``_agent_store is None`` (runtime not started) → ``None``."""
    from unittest.mock import MagicMock, patch

    from omnigent.server.routes.sessions import _resolve_harness

    conv = MagicMock()
    conv.harness_override = None
    conv.agent_id = "ag_abc"

    with patch("omnigent.runtime._globals._agent_store", None):
        assert _resolve_harness(conv) is None


def test_resolve_harness_none_when_agent_not_in_store() -> None:
    """Agent id present but not found in the store → ``None``."""
    from unittest.mock import MagicMock, patch

    from omnigent.server.routes.sessions import _resolve_harness

    conv = MagicMock()
    conv.harness_override = None
    conv.agent_id = "ag_missing"

    mock_store = MagicMock()
    mock_store.get.return_value = None

    with patch("omnigent.runtime._globals._agent_store", mock_store):
        assert _resolve_harness(conv) is None


def test_resolve_harness_sdk_via_config_key() -> None:
    """Executor with ``config["harness"] = "claude-sdk"`` → ``"claude-sdk"``."""
    from unittest.mock import MagicMock, patch

    from omnigent.server.routes.sessions import _resolve_harness

    conv = MagicMock()
    conv.harness_override = None
    conv.agent_id = "ag_sdk"
    conv.sub_agent_name = None

    executor = MagicMock()
    executor.config = {"harness": "claude-sdk"}
    executor.type = "omnigent"

    spec = MagicMock()
    spec.executor = executor
    spec.sub_agents = []

    loaded = MagicMock()
    loaded.spec = spec

    mock_store = MagicMock()
    mock_store.get.return_value = MagicMock(
        id="ag_sdk", bundle_location="ag_sdk/bundle", session_id=None
    )

    mock_cache = MagicMock()
    mock_cache.load.return_value = loaded

    with (
        patch("omnigent.runtime._globals._agent_store", mock_store),
        patch("omnigent.runtime.get_agent_cache", return_value=mock_cache),
    ):
        assert _resolve_harness(conv) == "claude-sdk"


def test_resolve_harness_sdk_via_executor_type() -> None:
    """Executor with no ``config["harness"]`` falls back to ``executor.type``."""
    from unittest.mock import MagicMock, patch

    from omnigent.server.routes.sessions import _resolve_harness

    conv = MagicMock()
    conv.harness_override = None
    conv.agent_id = "ag_sdk2"
    conv.sub_agent_name = None

    executor = MagicMock()
    executor.config = {}  # no harness key
    executor.type = "claude-sdk"

    spec = MagicMock()
    spec.executor = executor
    spec.sub_agents = []

    loaded = MagicMock()
    loaded.spec = spec

    mock_store = MagicMock()
    mock_store.get.return_value = MagicMock(
        id="ag_sdk2", bundle_location="ag_sdk2/bundle", session_id=None
    )

    mock_cache = MagicMock()
    mock_cache.load.return_value = loaded

    with (
        patch("omnigent.runtime._globals._agent_store", mock_store),
        patch("omnigent.runtime.get_agent_cache", return_value=mock_cache),
    ):
        assert _resolve_harness(conv) == "claude-sdk"


def test_resolve_harness_returns_none_on_exception() -> None:
    """Any exception inside the resolver degrades to ``None`` (never raises)."""
    from unittest.mock import MagicMock, patch

    from omnigent.server.routes.sessions import _resolve_harness

    conv = MagicMock()
    conv.harness_override = None
    conv.agent_id = "ag_boom"

    mock_store = MagicMock()
    mock_store.get.side_effect = OSError("disk read error")

    with patch("omnigent.runtime._globals._agent_store", mock_store):
        assert _resolve_harness(conv) is None


# ── anon_user_id ─────────────────────────────────────────────────────────────


def test_anon_user_id_none_user() -> None:
    """No authenticated user → no anonymous id."""
    from omnigent.telemetry.anon import anon_user_id

    assert anon_user_id(None, "inst-abc") is None


def test_anon_user_id_salted_by_installation() -> None:
    """The same user hashes differently per installation."""
    import hashlib

    from omnigent.telemetry.anon import anon_user_id

    got = anon_user_id("alice@example.com", "inst-abc")
    assert got == hashlib.sha256(b"inst-abc:alice@example.com").hexdigest()[:16]
    assert got != anon_user_id("alice@example.com", "inst-xyz")


def test_anon_user_id_without_installation_id() -> None:
    """Missing installation id → the user id is hashed unsalted."""
    import hashlib

    from omnigent.telemetry.anon import anon_user_id

    assert (
        anon_user_id("alice@example.com", None)
        == hashlib.sha256(b"alice@example.com").hexdigest()[:16]
    )


def test_current_anon_user_id_hashes_the_request_user() -> None:
    """The request-scoped user bound by the server middleware is hashed."""
    from omnigent.debug_logging import current_user_id_scope
    from omnigent.telemetry.anon import anon_user_id, current_anon_user_id

    with (
        patch(
            "omnigent.telemetry.installation_id.get_installation_id",
            return_value="inst-abc",
        ),
        current_user_id_scope("alice@example.com"),
    ):
        got = current_anon_user_id()
    assert got == anon_user_id("alice@example.com", "inst-abc")


def test_current_anon_user_id_none_without_a_request_user() -> None:
    """No bound request user (single-user mode, or outside a request) → ``None``."""
    from omnigent.debug_logging import USER_ID_ENV_VAR, current_user_id_scope
    from omnigent.telemetry.anon import current_anon_user_id

    with (
        patch.dict("os.environ", {USER_ID_ENV_VAR: ""}),
        current_user_id_scope(None),
    ):
        assert current_anon_user_id() is None


# ── TurnEndEvent / NativeSessionUsageEvent ───────────────────────────────────


def test_build_record_turn_end_event_promotes_anon_user_id() -> None:
    """``anon_user_id`` / ``host_installation_id`` are top-level on ``TurnEndEvent``."""
    import omnigent.telemetry.client as _mod
    from omnigent.telemetry.events import TurnEndEvent

    event = TurnEndEvent(
        installation_id="inst-abc",
        session_id="sess_abc123",
        anon_user_id="deadbeef01234567",
        host_installation_id="host-inst-abc",
        status="completed",
        latency_ms=1234.5,
        model="claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=20,
        cost_usd=0.5,
    )
    data = _mod._build_record(event)["data"]

    assert data["event_name"] == "TurnEndEvent"
    assert data["anon_user_id"] == "deadbeef01234567"
    assert data["host_installation_id"] == "host-inst-abc"
    params = json.loads(data["params"]) if data["params"] else {}
    assert params["status"] == "completed"
    assert "anon_user_id" not in params
    assert "host_installation_id" not in params


def test_build_record_native_session_usage_event_promotes_anon_user_id() -> None:
    """``anon_user_id`` / ``host_installation_id`` are top-level on the usage event."""
    import omnigent.telemetry.client as _mod
    from omnigent.telemetry.events import NativeSessionUsageEvent

    event = NativeSessionUsageEvent(
        installation_id="inst-abc",
        session_id="sess_abc123",
        anon_user_id="deadbeef01234567",
        host_installation_id="host-inst-abc",
        input_tokens=1000,
        output_tokens=200,
        cost_usd=1.25,
        model="claude-opus-4-5",
    )
    data = _mod._build_record(event)["data"]

    assert data["event_name"] == "NativeSessionUsageEvent"
    assert data["anon_user_id"] == "deadbeef01234567"
    assert data["host_installation_id"] == "host-inst-abc"
    params = json.loads(data["params"]) if data["params"] else {}
    assert params["cost_usd"] == 1.25
    assert "anon_user_id" not in params
    assert "host_installation_id" not in params


# ── telemetry_request_headers ────────────────────────────────────────────────


def test_telemetry_request_headers_carries_this_machines_installation_id() -> None:
    """A client stamps its installation ID so the server needs no lookup."""
    from omnigent.telemetry.request_headers import (
        INSTALLATION_ID_HEADER,
        telemetry_request_headers,
    )

    with (
        patch("omnigent.telemetry.client.is_disabled", return_value=False),
        patch(
            "omnigent.telemetry.installation_id.get_installation_id",
            return_value="inst-machine-1",
        ),
    ):
        assert telemetry_request_headers() == {INSTALLATION_ID_HEADER: "inst-machine-1"}


def test_telemetry_request_headers_empty_when_telemetry_is_disabled() -> None:
    """An opted-out client sends no installation ID at all."""
    from omnigent.telemetry.request_headers import telemetry_request_headers

    with (
        patch("omnigent.telemetry.client.is_disabled", return_value=True),
        patch(
            "omnigent.telemetry.installation_id.get_installation_id",
            return_value="inst-machine-1",
        ),
    ):
        assert telemetry_request_headers() == {}


def test_telemetry_request_headers_empty_without_an_installation_id() -> None:
    """No installation ID (unwritable data dir) means no header, not an empty one."""
    from omnigent.telemetry.request_headers import telemetry_request_headers

    with (
        patch("omnigent.telemetry.client.is_disabled", return_value=False),
        patch("omnigent.telemetry.installation_id.get_installation_id", return_value=None),
    ):
        assert telemetry_request_headers() == {}


def test_telemetry_request_headers_survives_a_failing_lookup() -> None:
    """The header builder must never raise into the request path."""
    from omnigent.telemetry.request_headers import telemetry_request_headers

    with patch("omnigent.telemetry.client.is_disabled", side_effect=RuntimeError("boom")):
        assert telemetry_request_headers() == {}


def test_server_request_headers_fold_in_the_installation_id() -> None:
    """Every request opened through the shared builder carries the header."""
    from omnigent.cli_auth import databricks_request_headers
    from omnigent.telemetry.request_headers import INSTALLATION_ID_HEADER

    with (
        patch("omnigent.telemetry.client.is_disabled", return_value=False),
        patch(
            "omnigent.telemetry.installation_id.get_installation_id",
            return_value="inst-machine-1",
        ),
    ):
        headers = databricks_request_headers("http://127.0.0.1:8000")

    assert headers[INSTALLATION_ID_HEADER] == "inst-machine-1"


def test_parse_installation_id_header_accepts_a_uuid() -> None:
    """A well-formed client-sent ID is taken as-is."""
    from omnigent.telemetry.request_headers import parse_installation_id_header

    value = str(uuid.uuid4())
    assert parse_installation_id_header(f" {value} ") == value


@pytest.mark.parametrize(
    "raw",
    [None, "", "not-a-uuid", "x" * 5000, "1234"],
)
def test_parse_installation_id_header_rejects_junk(raw: str | None) -> None:
    """The header is attacker-controlled, so only a UUID reaches telemetry."""
    from omnigent.telemetry.request_headers import parse_installation_id_header

    assert parse_installation_id_header(raw) is None


def test_isolated_test_runtime_disables_usage_telemetry_without_ci_markers(tmp_path: Path) -> None:
    """Sanitized test environments suppress analytics before spawning a server."""
    import omnigent.telemetry.client as telemetry
    from tests._helpers.live_server import isolated_process_env

    env = isolated_process_env(tmp_path, {}, {"PATH": "/usr/bin"})
    with (
        patch.dict("os.environ", env, clear=True),
        patch.object(telemetry, "TelemetryClient") as client,
    ):
        telemetry.init_client()
        assert telemetry.get_client() is None
        client.assert_not_called()
