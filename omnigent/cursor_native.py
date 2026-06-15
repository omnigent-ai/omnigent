"""Native Cursor Agent wrapper for the Omnigent CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_AGENT_NAME = "cursor-native-ui"


def _materialize_cursor_agent_spec(
    tmpdir: Path,
    *,
    model: str | None = None,
) -> Path:
    """
    Write the agent spec used by the Cursor harness.

    :param tmpdir: Temporary directory for the generated YAML file.
    :param model: Optional model id, e.g. ``"gpt-5"`` or ``"auto"``.
    :returns: Path to the generated YAML spec.
    """
    yaml_path = tmpdir / "cursor-native-ui.yaml"
    executor: dict[str, str] = {"harness": "cursor"}
    if model is not None:
        executor["model"] = model
    raw: dict[str, Any] = {
        "name": _AGENT_NAME,
        "prompt": (
            "You are a helpful AI assistant powered by Cursor. "
            "Assist the user with their tasks."
        ),
        "executor": executor,
        "os_env": {
            "type": "caller_process",
            "cwd": ".",
            "sandbox": {"type": "none"},
        },
    }
    yaml_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return yaml_path
