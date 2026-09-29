"""Tests for profile listing and fallback resolution in ``omnigent.onboarding.databricks_config``.

Complements ``test_databricks_config.py`` (host lookup, URL normalization) with
``list_databricks_profiles``, unreadable-config fallbacks, the bundled-profile
fallback, and the SDK presence probe.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from omnigent.onboarding import databricks_config
from omnigent.onboarding.databricks_config import (
    databricks_sdk_installed,
    get_workspace_url_for_profile,
    list_databricks_profiles,
)


@pytest.fixture()
def cfg_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / ".databrickscfg"
    monkeypatch.setattr(databricks_config, "_DATABRICKSCFG_PATH", path)
    return path


def _stub_bundled_profiles(monkeypatch: pytest.MonkeyPatch, *specs: object) -> None:
    stub = types.ModuleType("omnigent.onboarding.internal_beta")
    stub.DEFAULT_PROFILES = specs  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "omnigent.onboarding.internal_beta", stub)


def _spec(name: str, host: object) -> types.SimpleNamespace:
    return types.SimpleNamespace(name=name, host=host)


# ── list_databricks_profiles ─────────────────────────────────


def test_list_profiles_is_empty_without_a_config_file(cfg_path: Path) -> None:
    assert list_databricks_profiles() == []


def test_list_profiles_returns_sections_in_file_order(cfg_path: Path) -> None:
    cfg_path.write_text("[oss]\nhost = https://a\n[team]\nhost = https://b\n", encoding="utf-8")

    assert list_databricks_profiles() == ["oss", "team"]


def test_list_profiles_includes_default_only_when_it_has_keys(cfg_path: Path) -> None:
    cfg_path.write_text("[DEFAULT]\n[oss]\nhost = https://a\n", encoding="utf-8")
    assert list_databricks_profiles() == ["oss"]

    cfg_path.write_text("[DEFAULT]\nhost = https://d\n[oss]\nhost = https://a\n", encoding="utf-8")
    assert list_databricks_profiles() == ["oss", "DEFAULT"]


def test_list_profiles_is_empty_for_unparseable_config(cfg_path: Path) -> None:
    cfg_path.write_text("host = no section header\n", encoding="utf-8")

    assert list_databricks_profiles() == []


# ── get_workspace_url_for_profile fallbacks ──────────────────


def test_workspace_url_ignores_unparseable_config_and_uses_bundled_profile(
    cfg_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path.write_text("host = no section header\n", encoding="utf-8")
    _stub_bundled_profiles(monkeypatch, _spec("oss", "https://oss.example.com/"))

    assert get_workspace_url_for_profile("oss") == "https://oss.example.com"


def test_workspace_url_falls_back_when_profile_has_no_host(
    cfg_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path.write_text("[oss]\ntoken = abc\n", encoding="utf-8")
    _stub_bundled_profiles(monkeypatch, _spec("oss", "https://oss.example.com"))

    assert get_workspace_url_for_profile("oss") == "https://oss.example.com"


def test_workspace_url_prefers_config_over_bundled_profile(
    cfg_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path.write_text("[oss]\nhost = https://mine.example.com/\n", encoding="utf-8")
    _stub_bundled_profiles(monkeypatch, _spec("oss", "https://oss.example.com"))

    assert get_workspace_url_for_profile("oss") == "https://mine.example.com"


def test_workspace_url_is_none_for_unknown_profile_or_bad_bundled_host(
    cfg_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_bundled_profiles(monkeypatch, _spec("oss", 42))

    assert get_workspace_url_for_profile("other") is None
    # A bundled entry without a string host is not usable.
    assert get_workspace_url_for_profile("oss") is None


def test_workspace_url_is_none_when_bundled_catalog_is_absent(
    cfg_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ``None`` in sys.modules makes the import raise, like the OSS build.
    monkeypatch.setitem(sys.modules, "omnigent.onboarding.internal_beta", None)

    assert get_workspace_url_for_profile("oss") is None


# ── databricks_sdk_installed ─────────────────────────────────


def test_sdk_probe_is_false_when_spec_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(databricks_config.importlib.util, "find_spec", lambda name: None)

    assert databricks_sdk_installed() is False


def test_sdk_probe_is_false_when_namespace_package_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raising(name: str) -> None:
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(databricks_config.importlib.util, "find_spec", raising)

    assert databricks_sdk_installed() is False
