from pathlib import Path

import pytest

from omnigent.server.skill_settings import (
    read_skill_trust,
    resolve_skill_data_dir,
    skill_trust_path,
    write_skill_trust,
)


def test_fresh_data_dir_defaults_to_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "fresh"
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(data_dir))

    assert resolve_skill_data_dir() == data_dir
    assert skill_trust_path() == data_dir / "skill_trust"
    assert read_skill_trust() == "current"


def test_skill_trust_is_isolated_across_data_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(first))
    write_skill_trust("all-host")

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(second))
    assert read_skill_trust() == "current"
    write_skill_trust("current")

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(first))
    assert read_skill_trust() == "all-host"
    assert (first / "skill_trust").read_text() == "all-host\n"
    assert (second / "skill_trust").read_text() == "current\n"


def test_data_dir_overrides_admin_credentials_volume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "runtime-data"
    admin_volume = tmp_path / "admin-volume"
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(data_dir))
    monkeypatch.setenv(
        "OMNIGENT_ADMIN_CREDENTIALS_PATH",
        str(admin_volume / "admin-credentials"),
    )

    assert resolve_skill_data_dir() == data_dir


def test_admin_credentials_volume_is_fallback_without_data_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_volume = tmp_path / "admin-volume"
    monkeypatch.delenv("OMNIGENT_DATA_DIR", raising=False)
    monkeypatch.setenv(
        "OMNIGENT_ADMIN_CREDENTIALS_PATH",
        str(admin_volume / "admin-credentials"),
    )

    assert resolve_skill_data_dir() == admin_volume
