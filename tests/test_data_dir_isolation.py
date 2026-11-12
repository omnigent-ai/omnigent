"""Regression coverage for pytest runtime-data isolation."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from omnigent import cli
from omnigent.host import local_server


def test_pytest_uses_an_isolated_omnigent_data_dir() -> None:
    """Daemon state used by tests must never resolve under the real home."""
    test_data_dir = Path(os.environ["OMNIGENT_DATA_DIR"]).resolve()
    real_data_dir = (Path.home() / ".omnigent").resolve()

    assert test_data_dir != real_data_dir
    assert local_server._LOCAL_SERVER_PID_PATH.resolve().is_relative_to(test_data_dir)
    assert cli._HOST_PID_PATH.resolve().is_relative_to(test_data_dir)
    assert cli._daemon_registry_dir().resolve().is_relative_to(test_data_dir)


def test_pytest_does_not_inherit_debug_log_delivery_credentials() -> None:
    """Pytest startup also isolates the log sink in processes spawned by tests."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("OMNIGENT_DEBUG_LOG_")
    }
    env.update(
        OMNIGENT_DEBUG_LOG_CLIENT_ID="test-client",
        OMNIGENT_DEBUG_LOG_CLIENT_SECRET="test-secret",
        OMNIGENT_DEBUG_LOG_WORKSPACE_URL="https://workspace.example.test",
        OMNIGENT_DEBUG_LOG_ENDPOINT=(
            "https://123.zerobus.example.test/zerobus/v1/tables/catalog.schema.logs/insert"
        ),
    )
    check = "from omnigent.debug_logging import config_from_env; assert config_from_env() is None"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import tests.conftest; import subprocess, sys; "
            f"{check}; subprocess.run([sys.executable, '-c', {check!r}], check=True)",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
