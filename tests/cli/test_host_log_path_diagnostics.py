"""Diagnostics must name the configured data dir, not the default tree.

Host and server state live under :func:`omnigent.process_logging.data_dir`,
which honors ``OMNIGENT_DATA_DIR``. A failure message that hardcodes
``~/.omnigent/logs/...`` sends a reader with a relocated data dir to an empty
directory and hides the logs that would explain the failure.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from omnigent.process_logging import DATA_DIR_ENV_VAR

# Every diagnostic that points a reader at a host or server log tree. Each is
# checked for the hardcoded literal rather than exercised, because reaching
# them needs a real daemon spawn.
_LOG_TREE_DIAGNOSTICS = (
    ("omnigent.cli", "_discover_local_server_url"),
    ("omnigent.cli", "_run_background_host"),
    ("omnigent.chat", "_unreachable_server_message"),
)


@pytest.mark.parametrize(("module_name", "func_name"), _LOG_TREE_DIAGNOSTICS)
def test_diagnostic_does_not_hardcode_the_default_log_tree(
    module_name: str,
    func_name: str,
) -> None:
    """No log-tree diagnostic embeds the default ``~/.omnigent`` path.

    :param module_name: Module holding the diagnostic, e.g. ``"omnigent.cli"``.
    :param func_name: Function whose source is scanned.
    """
    import importlib

    module = importlib.import_module(module_name)
    source = inspect.getsource(getattr(module, func_name))

    assert "~/.omnigent/logs/" not in source, (
        f"{module_name}.{func_name} hardcodes the default log tree; "
        "use process_log_dir_reference(destination) so the path follows "
        f"${DATA_DIR_ENV_VAR}"
    )


def test_unreachable_local_server_message_names_the_relocated_log_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The loopback connection error points at the configured server log dir.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Pytest temp dir, used as the runtime data dir.
    """
    from omnigent.chat import _unreachable_server_message

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "elsewhere")
    monkeypatch.setenv(DATA_DIR_ENV_VAR, str(tmp_path / "data"))

    message = _unreachable_server_message("http://127.0.0.1:6767")

    assert str(tmp_path / "data" / "logs" / "server") in message
    assert "~/.omnigent" not in message
