"""Tests for the Node.js version gate in the web UI build (``setup.py``).

The web UI toolchain and the runtime harness CLIs are pinned to Node 22
LTS. Building on an off-version Node (e.g. Node 25) otherwise fails deep
inside corepack/pnpm with ``ERR_VM_DYNAMIC_IMPORT_CALLBACK_MISSING``, so
``_require_node_22`` fails fast with an actionable message instead.
"""

from __future__ import annotations

import importlib.util
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


def _run_node_version_check(module: ModuleType, version_output: str) -> None:
    """Invoke ``_require_node_22`` with ``node --version`` stubbed."""
    completed = mock.Mock(stdout=version_output)
    with (
        mock.patch("shutil.which", return_value="/usr/bin/node"),
        mock.patch.object(module.subprocess, "run", return_value=completed),
    ):
        module._require_node_22()


def test_node_22_passes() -> None:
    module = _load_setup_module()
    # Should not raise for the supported major version.
    _run_node_version_check(module, "v22.14.0\n")


def test_node_25_fails_with_clear_message() -> None:
    module = _load_setup_module()
    with pytest.raises(SystemExit) as excinfo:
        _run_node_version_check(module, "v25.2.1\n")
    message = str(excinfo.value)
    assert "Node 22 LTS is required but found Node 25.2.1" in message
    assert "OMNIGENT_SKIP_WEB_UI=true" in message


def test_older_node_fails() -> None:
    module = _load_setup_module()
    with pytest.raises(SystemExit) as excinfo:
        _run_node_version_check(module, "v20.11.0\n")
    assert "Node 22 LTS is required but found Node 20.11.0" in str(excinfo.value)


def test_unparseable_version_fails() -> None:
    module = _load_setup_module()
    with pytest.raises(SystemExit) as excinfo:
        _run_node_version_check(module, "not-a-version\n")
    assert "Node 22 LTS is required" in str(excinfo.value)


def test_node_missing_fails() -> None:
    module = _load_setup_module()
    with mock.patch("shutil.which", return_value=None):
        with pytest.raises(SystemExit) as excinfo:
            module._require_node_22()
    message = str(excinfo.value)
    assert "Node.js not found on PATH" in message
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
            module._require_node_22()
    message = str(excinfo.value)
    assert "could not determine the Node.js version" in message
    assert "OMNIGENT_SKIP_WEB_UI=true" in message
