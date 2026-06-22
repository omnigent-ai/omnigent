"""
Unit tests for the D7 migration of ``omnigent.client_tools.coding``
from raw ``TOOLS`` dict + ``execute_tool`` dispatcher to
``@tool``-decorated functions.

The migration's contract:

1. Eight ``@tool``-decorated functions are exported via the
   module-level ``_TOOL_FNS`` list (so ``omnigent chat`` /
   ``build_tool_handler`` consumers can pick them up).
2. The legacy ``TOOLS`` list still exposes OpenAI-format
   schemas — derived from the ``@tool`` metadata, not
   hand-rolled — so ``examples/frontends/terminal.py`` (which
   reads ``tool_set.TOOLS`` directly) keeps working.
3. The legacy ``execute_tool(name, args)`` sync dispatcher
   still works — same source of truth as the @tool functions
   so a tool's behavior is identical whether invoked through
   ``execute_tool`` or via ``build_tool_handler``.
4. Schemas preserve optional-vs-required semantics (``T |
   None = None`` parameters are NOT in ``required``) — this
   is why the migration uses ``@tool(strict=False)``; strict
   mode would force every property into ``required``.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from omnigent.client_tools.coding import (
    _TOOL_FNS,
    LSP,
    TOOLS,
    Bash,
    Edit,
    Glob,
    Grep,
    Read,
    Write,
    execute_tool,
    get_current_time,
)


def _schema_by_name(name: str) -> dict[str, object]:
    """Return the schema dict for the named tool (test helper)."""
    for s in TOOLS:
        fn = s.get("function") if isinstance(s, dict) else None
        if isinstance(fn, dict) and fn.get("name") == name:
            return s
    raise KeyError(
        f"no tool named {name!r} in TOOLS; got {[s['function']['name'] for s in TOOLS]}"
    )  # type: ignore[index]


def test_tool_fns_lists_all_eight_tools() -> None:
    """
    ``_TOOL_FNS`` must list every tool the module exports as a
    function. Catches a regression where a new tool is added
    but not appended to the list — its schema would silently
    not appear in ``TOOLS`` and ``omnigent chat`` consumers would
    miss it.
    """
    names = [fn.__name__ for fn in _TOOL_FNS]
    assert names == [
        "Read",
        "Write",
        "Edit",
        "Glob",
        "Grep",
        "Bash",
        "LSP",
        "get_current_time",
    ], f"_TOOL_FNS is out of sync with the @tool definitions; got {names}"


def test_tools_schema_count_matches_tool_fns() -> None:
    """
    ``TOOLS`` is derived from ``_TOOL_FNS`` via
    ``build_tool_handler``. Lengths must match — a mismatch
    means the SDK's handler-build silently dropped one.
    """
    assert len(TOOLS) == len(_TOOL_FNS), (
        f"TOOLS has {len(TOOLS)} entries but _TOOL_FNS has "
        f"{len(_TOOL_FNS)}; build_tool_handler dropped one."
    )


def test_required_params_match_signatures() -> None:
    """
    ``@tool(strict=False)`` keeps optional params (``T | None =
    None``) out of ``required``. If the migration accidentally
    used ``strict=True``, every param ends up in ``required``
    and the LLM is forced to send values for every optional
    arg — broken UX (e.g., calling Read would force offset+limit).
    """
    cases: dict[str, list[str]] = {
        "Read": ["file_path"],
        "Write": ["file_path", "content"],
        "Edit": ["file_path", "old_string", "new_string"],
        "Glob": ["pattern"],
        "Grep": ["pattern"],
        "Bash": ["command"],
        "LSP": ["action", "file_path"],
        "get_current_time": [],
    }
    for tool_name, expected_required in cases.items():
        schema = _schema_by_name(tool_name)
        params = schema["function"]["parameters"]  # type: ignore[index]
        assert isinstance(params, dict)
        actual = sorted(params.get("required") or [])
        assert actual == sorted(expected_required), (
            f"Tool {tool_name!r}: required mismatch. "
            f"Expected {sorted(expected_required)}, got {actual}. "
            f"Likely cause: the @tool decorator was given strict=True "
            f"(forces every param into required), or a parameter's "
            f"default was removed."
        )


def test_execute_tool_dispatches_to_function() -> None:
    """
    Legacy ``execute_tool(name, args)`` must produce the same
    result as calling the underlying ``@tool`` function
    directly. Catches any drift between the dispatcher's
    routing logic and the actual function bindings.
    """
    direct = get_current_time()
    via_dispatcher = execute_tool("get_current_time", {})
    # Both call ``time.strftime`` so the timestamps will
    # differ by sub-second; just check the date prefix.
    assert direct[:10] == via_dispatcher[:10], (
        f"execute_tool('get_current_time') routed to a different "
        f"implementation than calling the function directly. "
        f"direct={direct!r}, via_dispatcher={via_dispatcher!r}"
    )


def test_execute_tool_unknown_returns_error_string() -> None:
    """
    Unknown tool names return a string error rather than
    raising — preserves the legacy behavior so terminal.py's
    error handling doesn't need to change.
    """
    assert execute_tool("nonexistent", {}) == "Unknown tool: nonexistent"


def test_read_handles_offset_and_limit() -> None:
    """
    ``Read`` slices at offset/limit when provided — proves
    optional args are honored when passed but defaulted when
    not. Catches a regression where the migration's None
    handling was wrong (e.g., treating ``offset=None`` as 0
    instead of 1).
    """
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tmp:
        tmp.write("a\nb\nc\nd\ne\n")
        path = tmp.name
    try:
        full = Read(file_path=path)
        assert full == "1\ta\n2\tb\n3\tc\n4\td\n5\te"
        sliced = Read(file_path=path, offset=2, limit=2)
        assert sliced == "2\tb\n3\tc"
    finally:
        Path(path).unlink()


def test_write_then_read_round_trip() -> None:
    """End-to-end: ``Write`` then ``Read`` returns the same content."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = str(Path(tmpdir) / "subdir" / "out.txt")
        msg = Write(file_path=path, content="hello\nworld")
        assert "wrote" in msg.lower()
        # Read returns line-numbered output.
        assert "1\thello" in Read(file_path=path)
        assert "2\tworld" in Read(file_path=path)


def test_edit_replace_once() -> None:
    """``Edit`` replaces a single occurrence by default."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tmp:
        tmp.write("foo bar foo")
        path = tmp.name
    try:
        result = Edit(file_path=path, old_string="bar", new_string="baz")
        assert "Replaced 1" in result
        assert Path(path).read_text() == "foo baz foo"
    finally:
        Path(path).unlink()


def test_edit_rejects_ambiguous_without_replace_all() -> None:
    """``Edit`` with replace_all=False must error on multiple matches."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tmp:
        tmp.write("foo foo")
        path = tmp.name
    try:
        result = Edit(file_path=path, old_string="foo", new_string="bar")
        assert "appears 2 times" in result, (
            f"Expected ambiguity error; got {result!r}. "
            f"Without this guard, Edit would silently replace only "
            f"the first match and the agent would think it edited the "
            f"whole file."
        )
        # File unchanged.
        assert Path(path).read_text() == "foo foo"
    finally:
        Path(path).unlink()


def test_bash_returns_command_output() -> None:
    """Smoke-test that ``Bash`` actually runs the command."""
    out = Bash(command="echo hello")
    assert out == "hello"


def test_lsp_stub_does_not_raise() -> None:
    """``LSP`` is a stub — must return a string, not raise."""
    out = LSP(action="hover", file_path="/tmp/foo.py", line=1, character=0)
    assert "not implemented" in out.lower()


def test_glob_no_matches_returns_friendly_string() -> None:
    """``Glob`` must return a string (not raise / not return None) on no matches."""
    out = Glob(pattern="this-pattern-cannot-match-*-xyz123")
    assert out == "No files matched."


def test_grep_smoke() -> None:
    """``Grep`` smoke-test against this file's known content."""
    out = Grep(pattern="def test_grep_smoke", path=__file__)
    assert __file__ in out, f"Grep should find this test file; got {out!r}"


def test_grep_invalid_regex_returns_error() -> None:
    """``Grep`` with an invalid regex returns an error string, not a crash.

    Both rg and grep exit with code 2 on a bad pattern; the tool must
    surface that rather than silently returning "No matches found."
    """
    out = Grep(pattern="[invalid", path=__file__)
    assert "Search failed" in out, f"Expected error for invalid regex; got {out!r}"


def test_edit_write_error_returns_error_string() -> None:
    """``Edit`` returns an error string (not raise) when write_text raises OSError."""
    from unittest.mock import patch

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tmp:
        tmp.write("hello world")
        path = tmp.name
    try:
        with patch.object(Path, "write_text", side_effect=OSError("disk full")):
            result = Edit(file_path=path, old_string="hello", new_string="goodbye")
        assert "Error writing" in result, f"Expected write-error message; got {result!r}"
    finally:
        Path(path).unlink()
