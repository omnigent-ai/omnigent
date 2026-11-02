"""Validation of a project's setup command config."""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.project_setup import (
    MAX_COMMAND_CHARS,
    MAX_COMMANDS_PER_LIST,
    ProjectSetup,
    validate_project_setup_config,
)


@pytest.mark.parametrize(
    ("config", "error"),
    [
        ({"setup_commands": "npm ci"}, "must be a list"),
        ({"env_commands": ["ok", "   "]}, "non-empty strings"),
        ({"env_commands": ["x" * (MAX_COMMAND_CHARS + 1)]}, "at most"),
        ({"setup_commands": ["true"] * (MAX_COMMANDS_PER_LIST + 1)}, "at most"),
        ({"parallel_setup": "yes"}, "true or false"),
    ],
    ids=["not-a-list", "blank-line", "line-too-long", "too-many-lines", "non-boolean-flag"],
)
def test_malformed_setup_config_is_rejected(config: dict[str, Any], error: str) -> None:
    with pytest.raises(ValueError, match=error):
        validate_project_setup_config(config)


def test_setup_lines_keep_their_indentation() -> None:
    setup = ProjectSetup.from_mapping({"setup_commands": ["if true; then", "  echo hi  ", "fi"]})

    assert setup is not None
    assert setup.setup_commands == ("if true; then", "  echo hi", "fi")
    assert ProjectSetup.from_mapping({"model": "opus"}) is None
