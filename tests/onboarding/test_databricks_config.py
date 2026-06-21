"""Unit tests for omnigent.onboarding.databricks_config."""

from __future__ import annotations

import configparser
from pathlib import Path
from unittest.mock import patch

import pytest

from omnigent.onboarding.databricks_config import (
    databricks_sdk_installed,
    get_workspace_url_for_profile,
    normalize_workspace_url,
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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The reported foot-gun: the URL copied from a browser address bar.
        (
            "https://my-ws.cloud.databricks.com/browse?o=1234567890",
            "https://my-ws.cloud.databricks.com",
        ),
        # Path with no query.
        (
            "https://my-ws.cloud.databricks.com/explore/data",
            "https://my-ws.cloud.databricks.com",
        ),
        # Fragment is dropped too.
        (
            "https://my-ws.cloud.databricks.com/#/setting/account",
            "https://my-ws.cloud.databricks.com",
        ),
        # Surrounding whitespace is trimmed before parsing.
        (
            "  https://my-ws.cloud.databricks.com/browse  ",
            "https://my-ws.cloud.databricks.com",
        ),
        # Pre-existing trailing-slash case still collapses.
        ("https://my-ws.cloud.databricks.com/", "https://my-ws.cloud.databricks.com"),
        # Already an origin — returned unchanged.
        ("https://my-ws.cloud.databricks.com", "https://my-ws.cloud.databricks.com"),
    ],
)
def test_normalize_workspace_url_reduces_to_origin(raw: str, expected: str) -> None:
    """A pasted workspace URL is reduced to its bare ``scheme://host`` origin."""
    assert normalize_workspace_url(raw) == expected


def test_normalize_workspace_url_scheme_less_input_only_strips_trailing_slash() -> None:
    """Without a scheme there is no netloc to isolate, so the result matches the
    prior ``rstrip("/")`` behavior — the wizard pre-adds ``https://`` before
    calling, so a scheme is present in practice."""
    assert normalize_workspace_url("my-ws.cloud.databricks.com/") == "my-ws.cloud.databricks.com"
