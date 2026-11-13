"""``hindsight_*`` builtins are deprecated in favour of the memory_* tools.

A spec that still declares them keeps them and loses ``tool_groups.memory``, so
the model is never shown two memory systems.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.spec.parser import parse
from omnigent.tools.manager import ToolManager


def _spec(tmp_path: Path, **extra: object):
    config = {"spec_version": 1, "name": "legacy-agent", **extra}
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    return parse(tmp_path)


def _tool_names(spec) -> set[str]:
    return {s["function"]["name"] for s in ToolManager(spec).get_tool_schemas()}


_HINDSIGHT = {"builtins": [{"name": "hindsight_recall", "api_key": "k"}]}


def test_a_spec_without_hindsight_gets_the_memory_tools(tmp_path: Path) -> None:
    spec = _spec(tmp_path)

    assert spec.tool_groups.memory is True
    assert "memory_write" in _tool_names(spec)


def test_declaring_hindsight_turns_the_memory_group_off_and_keeps_hindsight(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="omnigent.spec.parser"):
        spec = _spec(tmp_path, tools=_HINDSIGHT)

    assert spec.tool_groups.memory is False
    names = _tool_names(spec)
    assert "hindsight_recall" in names
    assert not {n for n in names if n.startswith("memory_")}
    assert "deprecated hindsight_*" in caplog.text and "legacy-agent" in caplog.text


@pytest.mark.parametrize("tool", ["hindsight_retain", "hindsight_recall", "hindsight_reflect"])
def test_each_hindsight_tool_triggers_the_rule(tmp_path: Path, tool: str) -> None:
    spec = _spec(tmp_path, tools={"builtins": [{"name": tool, "api_key": "k"}]})

    assert spec.tool_groups.memory is False


def test_explicit_memory_false_alongside_hindsight_is_fine_and_quiet(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="omnigent.spec.parser"):
        spec = _spec(tmp_path, tools=_HINDSIGHT, tool_groups={"memory": False})

    assert spec.tool_groups.memory is False
    assert "deprecated" not in caplog.text


def test_explicit_memory_true_alongside_hindsight_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(OmnigentError) as excinfo:
        _spec(tmp_path, tools=_HINDSIGHT, tool_groups={"memory": True})

    assert excinfo.value.code == ErrorCode.INVALID_INPUT
    assert "two memory systems" in str(excinfo.value)
    assert "--memory-backend hindsight" in str(excinfo.value)


def test_other_builtins_do_not_affect_the_memory_group(tmp_path: Path) -> None:
    spec = _spec(tmp_path, tools={"builtins": [{"name": "web_fetch"}]})

    assert spec.tool_groups.memory is True


def test_memory_group_is_a_known_tool_group_and_typos_still_fail(tmp_path: Path) -> None:
    assert _spec(tmp_path, tool_groups={"memory": False}).tool_groups.memory is False

    with pytest.raises(OmnigentError, match="unknown group"):
        _spec(tmp_path, tool_groups={"memories": False})
