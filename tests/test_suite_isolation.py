"""The suite never touches the developer's own Omnigent config or live runner."""

from __future__ import annotations

import os
from pathlib import Path

from omnigent.config import global_config_path


def test_user_config_path_is_inside_the_test_data_dir() -> None:
    data_dir = Path(os.environ["OMNIGENT_DATA_DIR"])
    assert global_config_path().is_relative_to(data_dir)


def test_no_inherited_runner_identity() -> None:
    inherited = [
        name
        for name in os.environ
        if name.startswith(("OMNIGENT_RUNNER_", "OMNIGENT_TERMINAL_"))
        or name in ("OMNIGENT_PROCESS_LOG_FILE", "OMNIGENT_HARNESS_TMP_PARENT")
    ]
    assert inherited == []
