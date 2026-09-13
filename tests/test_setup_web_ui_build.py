"""Tests for the web UI build prerequisites in ``setup.py``."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest


def _load_setup_module() -> ModuleType:
    """Load repo-root ``setup.py`` without running the real ``setup()``.

    ``setup.py`` calls ``setup(...)`` at module scope, which would drive
    setuptools off pytest's argv; patch it to a no-op during load so we
    can exercise the module's helpers in isolation.
    """
    path = Path(__file__).resolve().parents[1] / "setup.py"
    spec = importlib.util.spec_from_file_location("_omnigent_setup_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    setuptools = ModuleType("setuptools")
    setuptools.setup = mock.Mock()  # type: ignore[attr-defined]
    setuptools_command = ModuleType("setuptools.command")
    setuptools_build_py = ModuleType("setuptools.command.build_py")
    setuptools_build_py.build_py = type("build_py", (), {})  # type: ignore[attr-defined]
    with mock.patch.dict(
        sys.modules,
        {
            "setuptools": setuptools,
            "setuptools.command": setuptools_command,
            "setuptools.command.build_py": setuptools_build_py,
        },
    ):
        spec.loader.exec_module(module)
    return module


def _run_node_version_check(module: ModuleType, version_output: str) -> str:
    """Invoke the Node.js gate with ``node --version`` stubbed."""
    completed = mock.Mock(stdout=version_output)
    with (
        mock.patch("shutil.which", return_value="/usr/bin/node"),
        mock.patch.object(module.subprocess, "run", return_value=completed),
    ):
        return module._require_supported_node()


def test_node_22_13_passes() -> None:
    module = _load_setup_module()
    assert _run_node_version_check(module, "v22.13.0\n") == "22.13.0"


@pytest.mark.parametrize("version", ["v23.0.0\n", "v24.14.0\n", "v25.2.1\n"])
def test_newer_node_versions_pass(version: str) -> None:
    module = _load_setup_module()
    assert _run_node_version_check(module, version) == version.strip().lstrip("v")


@pytest.mark.parametrize("version", ["v22.12.0\n", "v20.20.1\n"])
def test_older_node_fails(version: str) -> None:
    module = _load_setup_module()
    with pytest.raises(SystemExit) as excinfo:
        _run_node_version_check(module, version)
    message = str(excinfo.value)
    assert "Node.js 22.13.0 or newer is required" in message
    assert version.strip().lstrip("v") in message
    assert "https://nodejs.org/en/download" in message
    assert "OMNIGENT_SKIP_WEB_UI=true" in message


def test_unparseable_version_fails() -> None:
    module = _load_setup_module()
    with pytest.raises(SystemExit) as excinfo:
        _run_node_version_check(module, "not-a-version\n")
    message = str(excinfo.value)
    assert "could not parse Node.js version not-a-version" in message
    assert "Node.js 22.13.0 or newer is required" in message
    assert "https://nodejs.org/en/download" in message
    assert "OMNIGENT_SKIP_WEB_UI=true" in message


def test_node_missing_fails() -> None:
    module = _load_setup_module()
    with mock.patch("shutil.which", return_value=None):
        with pytest.raises(SystemExit) as excinfo:
            module._require_supported_node()
    message = str(excinfo.value)
    assert "Node.js not found on PATH" in message
    assert "Node.js 22.13.0 or newer is required" in message
    assert "https://nodejs.org/en/download" in message
    assert "OMNIGENT_SKIP_WEB_UI=true" in message


def test_node_version_probe_failure_fails() -> None:
    module = _load_setup_module()
    with (
        mock.patch("shutil.which", return_value="/usr/bin/node"),
        mock.patch.object(
            module.subprocess,
            "run",
            side_effect=OSError("boom"),
        ),
    ):
        with pytest.raises(SystemExit) as excinfo:
            module._require_supported_node()
    message = str(excinfo.value)
    assert "could not determine the Node.js version" in message
    assert "Node.js 22.13.0 or newer is required" in message
    assert "https://nodejs.org/en/download" in message
    assert "OMNIGENT_SKIP_WEB_UI=true" in message


def test_skip_web_ui_bypasses_node_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_setup_module()
    monkeypatch.setenv("OMNIGENT_SKIP_WEB_UI", "true")
    require_node = mock.Mock()
    with mock.patch.object(module, "_require_supported_node", require_node):
        module._GenerateBuildInfo._build_web_ui(object())
    require_node.assert_not_called()


def test_newer_node_build_failure_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_setup_module()
    monkeypatch.delenv("OMNIGENT_SKIP_WEB_UI", raising=False)
    monkeypatch.setenv("OMNIGENT_BUILD_WEB_UI", "1")
    failure = module.subprocess.CalledProcessError(
        1,
        ["pnpm", "install"],
        stderr="ERR_PNPM_UNSUPPORTED_ENGINE: use Node.js 22.13.0 or newer",
    )
    with (
        mock.patch.object(module, "_require_supported_node", return_value="25.2.1"),
        mock.patch("shutil.which", return_value="/usr/bin/pnpm"),
        mock.patch.object(module.subprocess, "run", side_effect=failure),
        pytest.raises(SystemExit) as excinfo,
    ):
        module._GenerateBuildInfo._build_web_ui(object())
    message = str(excinfo.value)
    assert "web UI build failed on Node.js 25.2.1" in message
    assert "ERR_PNPM_UNSUPPORTED_ENGINE" in message
    assert "upgrade to Node.js 22.13.0 or newer" in message
    assert "OMNIGENT_SKIP_WEB_UI=true" in message
    assert excinfo.value.__cause__ is failure


def test_real_node_floor_matches_pinned_pnpm() -> None:
    """Exercise the setup gate and pinned pnpm with real boundary binaries.

    Set both environment variables to opt in. Keeping this test explicit avoids
    downloading Node during ordinary pytest runs while providing a real
    toolchain check that cannot pass from mocked ``node --version`` output.
    """
    node_paths = {
        "22.12.0": os.environ.get("OMNIGENT_TEST_NODE_22_12"),
        "22.13.0": os.environ.get("OMNIGENT_TEST_NODE_22_13"),
    }
    if not any(node_paths.values()):
        pytest.skip(
            "set OMNIGENT_TEST_NODE_22_12 and OMNIGENT_TEST_NODE_22_13 "
            "to run the real toolchain boundary check"
        )
    assert all(node_paths.values()), "both real Node boundary binaries are required"

    root = Path(__file__).resolve().parents[1]
    package_manager = json.loads((root / "package.json").read_text())["packageManager"]
    assert package_manager == "pnpm@11.15.1"
    module = _load_setup_module()

    for expected_version, raw_node_path in node_paths.items():
        assert raw_node_path is not None
        node_path = Path(raw_node_path).resolve()
        assert node_path.is_file(), f"Node binary not found: {node_path}"
        version_result = subprocess.run(
            [node_path, "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert version_result.stdout.strip() == f"v{expected_version}"

        with mock.patch("shutil.which", return_value=str(node_path)):
            if expected_version == "22.12.0":
                with pytest.raises(SystemExit):
                    module._require_supported_node()
            else:
                assert module._require_supported_node() == expected_version

        corepack_path = node_path.parent / "corepack"
        assert corepack_path.is_file(), f"Corepack not found: {corepack_path}"
        env = {
            **os.environ,
            "COREPACK_DEFAULT_TO_LATEST": "0",
            "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
            "PATH": f"{node_path.parent}{os.pathsep}{os.environ['PATH']}",
        }
        pnpm_result = subprocess.run(
            [corepack_path, "pnpm", "--version"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        output = f"{pnpm_result.stdout}\n{pnpm_result.stderr}"
        if expected_version == "22.12.0":
            assert pnpm_result.returncode != 0
            assert "requires at least Node.js v22.13" in output
        else:
            assert pnpm_result.returncode == 0, output
            assert "11.15.1" in pnpm_result.stdout
