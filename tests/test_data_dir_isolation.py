"""Regression coverage for pytest runtime-data isolation."""

from __future__ import annotations

import os
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
