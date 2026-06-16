from __future__ import annotations

from omnigent.runner.app import _is_relative_tool_file, _spec_with_workdir_paths
from omnigent.spec.types import AgentSpec, ExecutorSpec, LocalToolInfo


def test_is_relative_tool_file_distinguishes_dotted_callable_from_file() -> None:
    # Dotted callable import paths (``type: function`` tools) are not workdir-relative files.
    assert _is_relative_tool_file("hindsight_omnigent.tools.retain") is False
    assert _is_relative_tool_file("examples._shared.tool_functions.search_web") is False
    # Relative source files are (whether nested or at the bundle root).
    assert _is_relative_tool_file("tools/python/arxiv_search.py") is True
    assert _is_relative_tool_file("arxiv_search.py") is True
    assert _is_relative_tool_file("mytool.ts") is True
    # Absolute paths are left untouched.
    assert _is_relative_tool_file("/abs/tools/x.py") is False


def test_spec_with_workdir_paths_preserves_dotted_callable_paths(tmp_path) -> None:
    # Regression for #378: a single-file YAML agent's ``type: function`` tool carries a dotted
    # callable import path. Workdir resolution must not prepend the bundle dir to it (which
    # corrupts the import and silently drops the whole tool set); only real relative file
    # paths get resolved against the workdir.
    callable_tool = LocalToolInfo(
        name="retain", path="hindsight_omnigent.tools.retain", language="python"
    )
    file_tool = LocalToolInfo(name="arxiv", path="tools/arxiv.py", language="python")
    spec = AgentSpec(
        spec_version=1,
        executor=ExecutorSpec(type="claude_sdk"),
        local_tools=[callable_tool, file_tool],
    )

    resolved = _spec_with_workdir_paths(spec, tmp_path)

    by_name = {t.name: t for t in resolved.local_tools}
    # The dotted callable path is left untouched ...
    assert by_name["retain"].path == "hindsight_omnigent.tools.retain"
    # ... while a genuine relative file path is resolved against the workdir.
    assert by_name["arxiv"].path == str((tmp_path / "tools/arxiv.py").resolve())
