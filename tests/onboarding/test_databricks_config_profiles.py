"""Tests for profile listing in ``omnigent.onboarding.databricks_config``.

Complements ``test_databricks_config.py`` (host lookup, URL normalization) with
``list_databricks_profiles``, unreadable-config handling, and the SDK presence probe.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.onboarding import databricks_config
from omnigent.onboarding.databricks_config import (
    databricks_sdk_installed,
    list_databricks_profiles,
)


@pytest.fixture()
def cfg_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / ".databrickscfg"
    monkeypatch.setattr(databricks_config, "_DATABRICKSCFG_PATH", path)
    return path


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
