"""Unit tests for the usage telemetry helpers."""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from omnigent.telemetry.surface import classify_surface

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


def test_classify_surface_python_httpx() -> None:
    """python-httpx UA → ``"cli"``."""
    assert classify_surface("python-httpx/0.27.0") == "cli"


def test_classify_surface_empty_string() -> None:
    """Empty string → ``"cli"``."""
    assert classify_surface("") == "cli"


def test_classify_surface_regular_browser() -> None:
    """Regular browser UA → ``"web"``."""
    assert (
        classify_surface(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36"
        )
        == "web"
    )


# ── is_disabled ─────────────────────────────────────────────────────────────


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
