"""The helper wheel's release checks and the ``find_binary()`` it ships."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import sysconfig
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VERSION = "0.23.0"
_HELPER = "omnigent-workspace-helper"


def _load(name: str, path: Path) -> ModuleType:
    # Loaded by path: ``scripts`` collides with the ``tests/scripts`` package,
    # and the helper's package isn't installed in the repo's environment.
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _load(
    "_check_workspace_helper_wheel_under_test",
    _REPO_ROOT / "scripts" / "check_workspace_helper_wheel.py",
)
package = _load(
    "_omnigent_workspace_helper_under_test",
    _REPO_ROOT
    / "rust"
    / "workspace-helper"
    / "python"
    / "omnigent_workspace_helper"
    / "__init__.py",
)


def _wheel(
    directory: Path, tag: str, version: str = _VERSION, extra: tuple[str, ...] = ()
) -> Path:
    path = directory / f"omnigent_workspace_helper-{version}-py3-none-{tag}.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for entry in (
            "omnigent_workspace_helper/__init__.py",
            "omnigent_workspace_helper/py.typed",
            f"omnigent_workspace_helper-{version}.data/scripts/{_HELPER}",
            f"omnigent_workspace_helper-{version}.dist-info/METADATA",
            f"omnigent_workspace_helper-{version}.dist-info/RECORD",
            *extra,
        ):
            archive.writestr(entry, "")
    return path


def test_a_wheel_with_the_right_tag_version_and_files_passes(tmp_path: Path) -> None:
    assert (
        check.check_contents(
            _wheel(tmp_path, "manylinux_2_28_x86_64"), "manylinux_2_28_x86_64", _VERSION
        )
        == []
    )
    assert (
        check.check_contents(_wheel(tmp_path, "macosx_11_0_arm64"), "macosx_arm64", _VERSION) == []
    )


def test_a_wheel_with_the_wrong_tag_version_or_files_fails(tmp_path: Path) -> None:
    musl = _wheel(tmp_path, "musllinux_1_2_x86_64")
    assert check.check_contents(musl, "manylinux_2_28_x86_64", _VERSION) == [
        f"{musl.name} is tagged musllinux_1_2_x86_64, not manylinux_2_28_x86_64"
    ]
    stale = _wheel(tmp_path, "manylinux_2_28_x86_64", version="0.22.0")
    problems = check.check_contents(stale, "manylinux_2_28_x86_64", _VERSION)
    assert problems[:2] == [
        f"{stale.name} has version 0.22.0, not 0.23.0",
        f"{stale.name} has no {_HELPER} executable",
    ]
    extra = _wheel(tmp_path, "macosx_11_0_arm64", extra=("omnigent_workspace_helper/extra.so",))
    assert check.check_contents(extra, "macosx_arm64", _VERSION) == [
        f"{extra.name} unexpectedly contains omnigent_workspace_helper/extra.so"
    ]
    other = tmp_path / "omnigent_workspace_helper-0.23.0-cp312-cp312-manylinux_2_28_x86_64.whl"
    other.touch()
    assert check.check_contents(other, "manylinux_2_28_x86_64", _VERSION) == [
        f"{other.name} isn't a omnigent_workspace_helper py3-none wheel"
    ]


def test_a_release_needs_exactly_one_wheel_per_platform(tmp_path: Path) -> None:
    for tag in (
        "manylinux_2_28_x86_64",
        "manylinux_2_28_aarch64",
        "musllinux_1_2_x86_64",
        "musllinux_1_2_aarch64",
        "macosx_10_12_x86_64",
        "macosx_11_0_arm64",
    ):
        _wheel(tmp_path, tag)
    # Other distributions and versions don't count.
    (tmp_path / "omnigent-0.23.0-py3-none-any.whl").touch()
    _wheel(tmp_path, "manylinux_2_28_x86_64", version="0.22.0")
    assert check.check_complete(tmp_path, _VERSION) == []

    (tmp_path / "omnigent_workspace_helper-0.23.0-py3-none-macosx_11_0_arm64.whl").unlink()
    _wheel(tmp_path, "macosx_14_0_x86_64")
    assert check.check_complete(tmp_path, _VERSION) == [
        "2 macosx_x86_64 wheels for 0.23.0, expected 1",
        "0 macosx_arm64 wheels for 0.23.0, expected 1",
    ]


def _fake_helper(directory: Path, answer: dict[str, object], exit_code: int = 0) -> Path:
    binary = directory / _HELPER
    binary.write_text(
        f"#!/bin/sh\necho '{json.dumps(answer)}'\necho failed >&2\nexit {exit_code}\n"
    )
    binary.chmod(0o755)
    return binary


@pytest.fixture
def installed_helper(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Install a stand-in ``omnigent_workspace_helper`` whose binary prints ``answer``."""

    def install(answer: dict[str, object], exit_code: int = 0) -> Path:
        binary = _fake_helper(tmp_path, answer, exit_code)
        module = ModuleType("omnigent_workspace_helper")
        module.find_binary = lambda: binary  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "omnigent_workspace_helper", module)
        return binary

    return install


def test_the_installed_helper_must_report_the_release_and_serve_only_workspace_calls(
    installed_helper,
) -> None:
    installed_helper({"version": _VERSION, "capabilities": ["ListDirectory", "ReadFile"]})
    assert check.check_installed(_VERSION) == []

    binary = installed_helper(
        {"version": "dev", "capabilities": ["ListDirectory", "pty", "InspectSession"]}
    )
    assert check.check_installed(_VERSION) == [
        f"{binary} reports version 'dev', not '0.23.0'",
        f"{binary} doesn't serve ReadFile",
        f"{binary} serves InspectSession, pty",
    ]

    binary = installed_helper({}, exit_code=3)
    assert check.check_installed(_VERSION) == [f"{binary} --version exited 3: failed"]


def test_the_command_line_reports_problems_as_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert check.main(["complete", str(tmp_path), "--version", _VERSION]) == 1
    assert (
        "::error::0 manylinux_2_28_x86_64 wheels for 0.23.0, expected 1" in capsys.readouterr().out
    )
    wheel = _wheel(tmp_path, "musllinux_1_2_aarch64")
    assert (
        check.main(
            ["contents", str(wheel), "--platform", "musllinux_1_2_aarch64", "--version", _VERSION]
        )
        == 0
    )


@pytest.fixture
def schemes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Point the environment's and the user scheme's scripts directories into ``tmp_path``."""
    directories = {"environment": tmp_path / "venv" / "bin", "user": tmp_path / "user" / "bin"}
    user_scheme = sysconfig.get_preferred_scheme("user")

    def get_path(name: str, scheme: str | None = None) -> str:
        assert name == "scripts"
        return str(directories["user" if scheme == user_scheme else "environment"])

    monkeypatch.setattr(package.sysconfig, "get_path", get_path)
    return directories


def _executable(directory: Path, mode: int = 0o755) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (_HELPER + (sysconfig.get_config_var("EXE") or ""))
    path.write_text("#!/bin/sh\n")
    path.chmod(mode)
    return path


def test_find_binary_prefers_the_environment_then_the_user_scheme(
    schemes: dict[str, Path],
) -> None:
    user = _executable(schemes["user"])
    assert package.find_binary() == user
    environment = _executable(schemes["environment"])
    assert package.find_binary() == environment


def test_find_binary_skips_a_file_that_isnt_executable(schemes: dict[str, Path]) -> None:
    _executable(schemes["environment"], mode=0o644)
    user = _executable(schemes["user"])
    assert package.find_binary() == user


def test_find_binary_finds_a_target_install_beside_the_package(
    monkeypatch: pytest.MonkeyPatch, schemes: dict[str, Path], tmp_path: Path
) -> None:
    # `pip install --target DIR` puts the package in DIR and its scripts in DIR/bin.
    target = tmp_path / "target"
    (target / "omnigent_workspace_helper").mkdir(parents=True)
    monkeypatch.setattr(
        package, "__file__", str(target / "omnigent_workspace_helper" / "__init__.py")
    )
    binary = _executable(target / "bin")
    assert package.find_binary() == binary


def test_find_binary_names_where_it_looked_when_nothing_is_installed(
    schemes: dict[str, Path],
) -> None:
    with pytest.raises(FileNotFoundError) as raised:
        package.find_binary()
    message = str(raised.value)
    assert f"{_HELPER} is not installed for {sys.executable}" in message
    assert str(schemes["environment"]) in message
    assert str(schemes["user"]) in message
    assert os.sep + "bin" in message
