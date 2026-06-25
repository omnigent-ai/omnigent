"""Regression tests for :func:`omnigent.runner.app._spec_with_workdir_paths`.

The helper rewrites relative local-tool ``path`` values to absolute paths
under the agent's workdir bundle. It must NOT touch omnigent dotted
callables (``type: function`` tools, ``language ==
:data:`OMNIGENT_TOOL_LANGUAGE```): their ``path`` is an importable module
path (e.g. ``pkg.tools.retain``), not a file path. Workdir-joining one
corrupts it into a bogus filesystem path, the import fails with
ModuleNotFoundError, and the whole tool schema list is dropped — leaving
the agent with zero declared tools (issue #379).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from omnigent.runner.app import _spec_with_workdir_paths
from omnigent.spec.omnigent import OMNIGENT_TOOL_LANGUAGE
from omnigent.spec.types import LocalToolInfo


@dataclass
class _FakeAgentSpec:
    """Minimal spec stub carrying just the ``local_tools`` list.

    :param local_tools: List of tool info objects the spec declares.
    """

    local_tools: list[LocalToolInfo] = field(default_factory=list)


def test_dotted_callable_path_left_unchanged(tmp_path: Path) -> None:
    """An ``omnigent-python-callable`` tool's dotted ``path`` is passed
    through verbatim — never workdir-joined."""
    dotted = "hindsight_omnigent.tools.retain"
    spec = _FakeAgentSpec(
        local_tools=[
            LocalToolInfo(
                name="retain",
                path=dotted,
                language=OMNIGENT_TOOL_LANGUAGE,
            )
        ]
    )

    resolved = _spec_with_workdir_paths(spec, tmp_path)

    assert resolved.local_tools[0].path == dotted


def test_python_file_tool_path_is_workdir_joined(tmp_path: Path) -> None:
    """A regular ``python`` file tool with a relative path is still
    resolved against the workdir (no regression for real file tools)."""
    rel = "tools/python/arxiv_search.py"
    spec = _FakeAgentSpec(
        local_tools=[
            LocalToolInfo(
                name="arxiv_search",
                path=rel,
                language="python",
            )
        ]
    )

    resolved = _spec_with_workdir_paths(spec, tmp_path)

    assert resolved.local_tools[0].path == str((tmp_path / rel).resolve())


def test_mixed_tools_only_file_tool_is_joined(tmp_path: Path) -> None:
    """With both kinds present, only the file tool is rewritten; the
    dotted callable is preserved (the bug that dropped the whole list)."""
    dotted = "hindsight_omnigent.tools.retain"
    rel = "tools/python/arxiv_search.py"
    spec = _FakeAgentSpec(
        local_tools=[
            LocalToolInfo(name="retain", path=dotted, language=OMNIGENT_TOOL_LANGUAGE),
            LocalToolInfo(name="arxiv_search", path=rel, language="python"),
        ]
    )

    resolved = _spec_with_workdir_paths(spec, tmp_path)

    by_name = {t.name: t for t in resolved.local_tools}
    assert by_name["retain"].path == dotted
    assert by_name["arxiv_search"].path == str((tmp_path / rel).resolve())
