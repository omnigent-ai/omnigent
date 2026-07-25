"""Parser tests for the ``tools.allowed`` configuration field."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from omnigent.errors import OmnigentError
from omnigent.spec.parser import parse


def _parse_config(tmp_path: Path, tools: dict[str, object] | None = None):
    config: dict[str, object] = {"spec_version": 1}
    if tools is not None:
        config["tools"] = tools
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    return parse(tmp_path)


def test_allowed_tools_list_parses_to_frozenset(tmp_path: Path) -> None:
    spec = _parse_config(
        tmp_path,
        {"allowed": ["sys_os_read", "sys_session_list"]},
    )

    assert spec.allowed_tools == frozenset({"sys_os_read", "sys_session_list"})


@pytest.mark.parametrize("tools", [None, {}, {"allowed": None}])
def test_allowed_tools_missing_or_null_parses_to_none(
    tmp_path: Path,
    tools: dict[str, object] | None,
) -> None:
    spec = _parse_config(tmp_path, tools)

    assert spec.allowed_tools is None


@pytest.mark.parametrize(
    "raw_allowed",
    ["sys_os_read", ["sys_os_read", 1], {"sys_os_read": True}],
)
def test_allowed_tools_rejects_non_list_of_strings(
    tmp_path: Path,
    raw_allowed: object,
) -> None:
    with pytest.raises(
        OmnigentError,
        match=r"'tools\.allowed' must be a list of strings",
    ):
        _parse_config(tmp_path, {"allowed": raw_allowed})


def test_allowed_tools_warns_for_unrecognized_name(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="omnigent.spec.parser"):
        spec = _parse_config(tmp_path, {"allowed": ["custom_mcp_tool"]})

    assert spec.allowed_tools == frozenset({"custom_mcp_tool"})
    assert "unrecognized built-in names" in caplog.text
