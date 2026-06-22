"""Unit tests for omnigent.onboarding.databricks_config."""

from __future__ import annotations

import configparser
from pathlib import Path
from unittest.mock import patch

from omnigent.onboarding.databricks_config import (
    databricks_sdk_installed,
    get_workspace_url_for_profile,
)

_WORKSPACE_URL = "https://example.databricks.com"


def test_get_workspace_url_for_profile_reads_databrickscfg(tmp_path: Path) -> None:
    """Resolves a profile name to its host from ~/.databrickscfg."""
    cfg = configparser.ConfigParser()
    cfg["test-profile"] = {"host": _WORKSPACE_URL, "token": "tok"}
    cfg_path = tmp_path / ".databrickscfg"
    with open(cfg_path, "w") as f:
        cfg.write(f)

    with patch("omnigent.onboarding.databricks_config._DATABRICKSCFG_PATH", cfg_path):
        url = get_workspace_url_for_profile("test-profile")

    assert url == _WORKSPACE_URL


def test_get_workspace_url_for_profile_strips_trailing_slash(tmp_path: Path) -> None:
    """Host values with a trailing slash are normalized."""
    cfg = configparser.ConfigParser()
    cfg["test-profile"] = {"host": _WORKSPACE_URL + "/", "token": "tok"}
    cfg_path = tmp_path / ".databrickscfg"
    with open(cfg_path, "w") as f:
        cfg.write(f)

    with patch("omnigent.onboarding.databricks_config._DATABRICKSCFG_PATH", cfg_path):
        url = get_workspace_url_for_profile("test-profile")

    assert url == _WORKSPACE_URL


def test_get_workspace_url_for_profile_returns_none_when_file_absent(
    tmp_path: Path,
) -> None:
    """Returns None when ~/.databrickscfg does not exist."""
    with patch(
        "omnigent.onboarding.databricks_config._DATABRICKSCFG_PATH",
        tmp_path / "nonexistent",
    ):
        assert get_workspace_url_for_profile("test-profile") is None


def test_get_workspace_url_for_profile_returns_none_for_missing_profile(
    tmp_path: Path,
) -> None:
    """Returns None when the named profile is not in ~/.databrickscfg."""
    cfg = configparser.ConfigParser()
    cfg["other"] = {"host": "https://example-other.cloud.databricks.com"}
    cfg_path = tmp_path / ".databrickscfg"
    with open(cfg_path, "w") as f:
        cfg.write(f)

    with patch("omnigent.onboarding.databricks_config._DATABRICKSCFG_PATH", cfg_path):
        assert get_workspace_url_for_profile("test-profile") is None


def test_get_workspace_url_for_profile_does_not_use_default_for_missing_profile(
    tmp_path: Path,
) -> None:
    """A typo'd profile must not silently resolve to the DEFAULT workspace."""
    cfg = configparser.ConfigParser()
    cfg["DEFAULT"] = {"host": _WORKSPACE_URL}
    cfg["other"] = {"host": "https://example-other.cloud.databricks.com"}
    cfg_path = tmp_path / ".databrickscfg"
    with open(cfg_path, "w") as f:
        cfg.write(f)

    with patch("omnigent.onboarding.databricks_config._DATABRICKSCFG_PATH", cfg_path):
        assert get_workspace_url_for_profile("test-profile") is None


def test_get_workspace_url_for_profile_reads_explicit_default_profile(
    tmp_path: Path,
) -> None:
    """The DEFAULT section is only used when the caller asks for DEFAULT."""
    cfg = configparser.ConfigParser()
    cfg["DEFAULT"] = {"host": _WORKSPACE_URL}
    cfg_path = tmp_path / ".databrickscfg"
    with open(cfg_path, "w") as f:
        cfg.write(f)

    with patch("omnigent.onboarding.databricks_config._DATABRICKSCFG_PATH", cfg_path):
        url = get_workspace_url_for_profile("DEFAULT")

    assert url == _WORKSPACE_URL


def test_get_workspace_url_for_profile_reads_lowercase_default_profile(
    tmp_path: Path,
) -> None:
    """The Databricks SDK treats ``default`` as the DEFAULT profile name."""
    cfg = configparser.ConfigParser()
    cfg["DEFAULT"] = {"host": _WORKSPACE_URL}
    cfg_path = tmp_path / ".databrickscfg"
    with open(cfg_path, "w") as f:
        cfg.write(f)

    with patch("omnigent.onboarding.databricks_config._DATABRICKSCFG_PATH", cfg_path):
        url = get_workspace_url_for_profile("default")

    assert url == _WORKSPACE_URL


def test_databricks_sdk_installed_true_in_dev_env() -> None:
    """``databricks_sdk_installed`` finds the SDK in the dev environment.

    The dev/CI install carries ``databricks-sdk`` (via the ``all`` extra),
    so the helper must report it present. A failure means the helper probes
    the wrong module path (e.g. a typo'd ``find_spec`` target), which would
    make the add-provider menu and ``setup --internal-beta`` claim the
    Databricks extra is missing even on installs that have it.
    """
    assert databricks_sdk_installed() is True
