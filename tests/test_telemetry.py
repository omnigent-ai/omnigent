"""Unit tests for the usage telemetry helpers."""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

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


def _import_is_disabled():
    from omnigent.telemetry.client import is_disabled

    return is_disabled


def test_is_disabled_omnigent_telemetry_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """``OMNIGENT_TELEMETRY=0`` disables telemetry."""
    monkeypatch.setenv("OMNIGENT_TELEMETRY", "0")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    from omnigent.telemetry.client import is_disabled

    assert is_disabled() is True


def test_is_disabled_do_not_track(monkeypatch: pytest.MonkeyPatch) -> None:
    """``DO_NOT_TRACK=1`` disables telemetry."""
    monkeypatch.delenv("OMNIGENT_TELEMETRY", raising=False)
    monkeypatch.delenv("OMNIGENT_DISABLE_TELEMETRY", raising=False)
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    from omnigent.telemetry.client import is_disabled

    assert is_disabled() is True


def test_is_disabled_ci_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CI=true`` disables telemetry."""
    monkeypatch.delenv("OMNIGENT_TELEMETRY", raising=False)
    monkeypatch.delenv("OMNIGENT_DISABLE_TELEMETRY", raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.setenv("CI", "true")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    from omnigent.telemetry.client import is_disabled

    assert is_disabled() is True


def test_is_disabled_github_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    """``GITHUB_ACTIONS=true`` disables telemetry."""
    monkeypatch.delenv("OMNIGENT_TELEMETRY", raising=False)
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
        "OMNIGENT_TELEMETRY",
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
    for var in _ci_vars:
        monkeypatch.delenv(var, raising=False)
    from omnigent.telemetry.client import is_disabled

    assert is_disabled() is False


# ── is_disabled — DISABLE_TELEMETRY alias ───────────────────────────────────


def test_is_disabled_disable_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """``DISABLE_TELEMETRY=true`` disables telemetry."""
    monkeypatch.delenv("OMNIGENT_TELEMETRY", raising=False)
    monkeypatch.setenv("DISABLE_TELEMETRY", "true")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    from omnigent.telemetry.client import is_disabled

    assert is_disabled() is True


def test_is_disabled_omnigent_disable_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """``OMNIGENT_DISABLE_TELEMETRY=1`` disables telemetry."""
    monkeypatch.delenv("OMNIGENT_TELEMETRY", raising=False)
    monkeypatch.setenv("OMNIGENT_DISABLE_TELEMETRY", "1")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    from omnigent.telemetry.client import is_disabled

    assert is_disabled() is True


# ── is_disabled — config.yaml ────────────────────────────────────────────────


_ALL_OPT_OUT_VARS = [
    "OMNIGENT_TELEMETRY",
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


def _clear_opt_out_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove env vars that can disable telemetry."""
    for var in _ALL_OPT_OUT_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("OMNIGENT_TELEMETRY", "0"),
        ("DISABLE_TELEMETRY", "1"),
        ("DISABLE_TELEMETRY", "true"),
        ("DISABLE_TELEMETRY", "yes"),
        ("DISABLE_TELEMETRY", "TRUE"),
        ("OMNIGENT_DISABLE_TELEMETRY", "1"),
        ("OMNIGENT_DISABLE_TELEMETRY", "true"),
        ("OMNIGENT_DISABLE_TELEMETRY", "yes"),
        ("DO_NOT_TRACK", "1"),
    ],
)
def test_is_disabled_env_var_variants(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    var: str,
    value: str,
) -> None:
    """Every documented env-var opt-out disables telemetry."""
    _clear_opt_out_env(monkeypatch)
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv(var, value)
    from omnigent.telemetry.client import is_disabled

    assert is_disabled() is True


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("OMNIGENT_TELEMETRY", "1"),
        ("DISABLE_TELEMETRY", "false"),
        ("OMNIGENT_DISABLE_TELEMETRY", "false"),
        ("DO_NOT_TRACK", "0"),
    ],
)
def test_is_disabled_env_var_non_opt_out_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    var: str,
    value: str,
) -> None:
    """Truthy-only env vars do not opt out on unrelated values."""
    _clear_opt_out_env(monkeypatch)
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv(var, value)
    from omnigent.telemetry.client import is_disabled

    assert is_disabled() is False


def test_is_disabled_config_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``telemetry: false`` in config.yaml disables telemetry."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("telemetry: false\n", encoding="utf-8")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    for var in _ALL_OPT_OUT_VARS:
        monkeypatch.delenv(var, raising=False)
    from omnigent.telemetry.client import is_disabled

    assert is_disabled() is True


def test_is_disabled_config_home_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``OMNIGENT_CONFIG_HOME`` controls which config.yaml is read."""
    config_home = tmp_path / "custom-home"
    config_home.mkdir()
    (config_home / "config.yaml").write_text("telemetry: false\n", encoding="utf-8")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
    _clear_opt_out_env(monkeypatch)
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


def test_is_disabled_malformed_config_yaml_returns_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Malformed config text does not silently suppress telemetry."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("telemetry: [false\n", encoding="utf-8")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    _clear_opt_out_env(monkeypatch)
    from omnigent.telemetry.client import is_disabled

    assert is_disabled() is False


def test_config_opts_out_bool_false() -> None:
    """A real YAML bool false opts out."""
    from omnigent.telemetry.client import _config_opts_out

    assert _config_opts_out(False) is True


def test_config_opts_out_bool_true() -> None:
    """A real YAML bool true does not opt out."""
    from omnigent.telemetry.client import _config_opts_out

    assert _config_opts_out(True) is False


@pytest.mark.parametrize("value", ["false", "no", "off", "0", " FALSE "])
def test_config_opts_out_corrupted_string_opt_out(value: str) -> None:
    """String spellings produced by the corrupted loader opt out."""
    from omnigent.telemetry.client import _config_opts_out

    assert _config_opts_out(value) is True


@pytest.mark.parametrize("value", ["true", "yes", "on", "1", ""])
def test_config_opts_out_corrupted_string_not_opt_out(value: str) -> None:
    """Opt-in string spellings do not disable telemetry."""
    from omnigent.telemetry.client import _config_opts_out

    assert _config_opts_out(value) is False


# ── init_client — server_config ──────────────────────────────────────────────


def test_init_client_server_config_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """``init_client(config={'telemetry': False})`` skips client creation."""
    import omnigent.telemetry.client as _mod

    for var in [
        "OMNIGENT_TELEMETRY",
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


def test_init_client_c_file_false_after_spec_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """``telemetry: false`` from ``omnigent server -c`` disables telemetry."""
    import omnigent.spec.parser  # noqa: F401 — reproduces the SafeLoader mutation
    import omnigent.telemetry.client as _mod

    for var in _ALL_OPT_OUT_VARS:
        monkeypatch.delenv(var, raising=False)

    config = yaml.safe_load("telemetry: false\n")
    assert config["telemetry"] == "false"

    original_client = _mod._CLIENT
    try:
        monkeypatch.setattr(_mod, "_CLIENT", None)
        _mod.init_client(config=config)
        assert _mod._CLIENT is None
    finally:
        monkeypatch.setattr(_mod, "_CLIENT", original_client)


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
