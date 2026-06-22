"""Unit tests for the terminal tool renderer module.

Tests the public API surface extracted from ``_formatter.py`` into
``_tool_renderers.py``: parsing helpers, the renderer registry dispatch,
the built-in renderers registered on ``DEFAULT_TOOL_RENDERERS``, and
the Pydantic payload models that back them.

All renderer tests go through the public ``render_tool()`` /
``render_native()`` methods — never calling ``_render_*`` functions
directly.
"""

from __future__ import annotations

import io
import json

import pytest
from omnigent_client import NativeToolBlock, ToolExecution
from omnigent_ui_sdk.terminal._tool_renderers import (
    DEFAULT_TOOL_RENDERERS,
    FileReadResult,
    McpCallData,
    ShellResult,
    StatusToolFields,
    TaskEntry,
    TaskListResult,
    TerminalListEntry,
    TerminalReadResult,
    TerminalToolRendererRegistry,
    TerminalToolRenderTheme,
    WebSearchAction,
    parse_tool_output,
    prettify_tool_output,
)
from pydantic import ValidationError
from rich.console import Console
from rich.text import Text

# ── Helpers ────────────────────────────────────────────────────────────


def _theme(**overrides: object) -> TerminalToolRenderTheme:
    """Build a ``TerminalToolRenderTheme`` with sensible defaults."""
    defaults = {
        "accent": "cyan",
        "muted": "dim",
        "warning": "yellow",
        "error": "red",
        "success": "green",
        "code_theme": "monokai",
        "max_result_lines": 30,
        "max_result_chars": 2000,
    }
    defaults.update(overrides)
    return TerminalToolRenderTheme(**defaults)  # type: ignore[arg-type]


def _tool_ex(
    name: str,
    output: str | None = None,
    *,
    arguments: dict[str, object] | None = None,
    args_summary: str = "",
) -> ToolExecution:
    """Build a ``ToolExecution`` with minimal boilerplate."""
    return ToolExecution(
        name=name,
        arguments=arguments or {},
        args_summary=args_summary,
        call_id="c1",
        agent_name="test",
        executed_by="server",
        output=output,
    )


def _native_block(tool_type: str, label: str = "", **data: object) -> NativeToolBlock:
    """Build a ``NativeToolBlock`` with minimal boilerplate."""
    return NativeToolBlock(tool_type=tool_type, label=label or tool_type, data=dict(data))


def _render_to_text(renderable: object, *, width: int = 200) -> str:
    """Render a Rich renderable to plain text (no ANSI)."""
    buf = io.StringIO()
    console = Console(
        file=buf,
        force_terminal=False,
        color_system=None,
        width=width,
        legacy_windows=False,
    )
    console.print(renderable)
    return buf.getvalue()


# ── parse_tool_output ──────────────────────────────────────────────────


def test_parse_tool_output_json_object() -> None:
    """JSON object string → is_json=True, json_value is a dict."""
    parsed = parse_tool_output('{"key": "value"}')
    assert parsed.is_json is True, (
        "Expected is_json=True for a valid JSON object string. "
        "If False, the parser failed to recognize JSON."
    )
    assert parsed.json_value == {"key": "value"}, (
        "Expected the parsed dict to match the input. "
        "If None, json.loads was not called or raised."
    )
    assert parsed.raw == '{"key": "value"}'


def test_parse_tool_output_json_array() -> None:
    """JSON array string → is_json=True, json_value is a list."""
    parsed = parse_tool_output("[1, 2, 3]")
    assert parsed.is_json is True
    assert parsed.json_value == [1, 2, 3]


def test_parse_tool_output_json_scalar() -> None:
    """JSON scalar (number) → is_json=True, json_value is the scalar."""
    parsed = parse_tool_output("42")
    assert parsed.is_json is True
    assert parsed.json_value == 42


def test_parse_tool_output_empty_string() -> None:
    """Empty string → is_json=False, json_value=None."""
    parsed = parse_tool_output("")
    assert parsed.is_json is False
    assert parsed.json_value is None


def test_parse_tool_output_invalid_json() -> None:
    """Invalid JSON → is_json=False, json_value=None, raw preserved."""
    raw = '{"broken": "json'
    parsed = parse_tool_output(raw)
    assert parsed.is_json is False
    assert parsed.json_value is None
    assert parsed.raw == raw


def test_parse_tool_output_leading_whitespace() -> None:
    """Leading whitespace before valid JSON is tolerated."""
    parsed = parse_tool_output("  \n  [1]")
    assert parsed.is_json is True
    assert parsed.json_value == [1]


# ── prettify_tool_output ───────────────────────────────────────────────


def test_prettify_json_object() -> None:
    """JSON object → indented with ensure_ascii=False."""
    raw = json.dumps({"border": "─"})
    # Python's default json.dumps escapes non-ASCII
    assert "\\u2500" in raw, "sanity: input must contain ASCII escape"
    result = prettify_tool_output(raw)
    assert "─" in result, (
        "Expected the real Unicode character after prettify. "
        "If \\u2500 appears instead, ensure_ascii=False was not used."
    )
    assert "\\u2500" not in result
    assert '"border"' in result


def test_prettify_json_array() -> None:
    """JSON array → indented."""
    raw = json.dumps([1, 2, 3])
    result = prettify_tool_output(raw)
    # Indented means each element on its own line.
    assert "1" in result
    assert "\n" in result


def test_prettify_json_scalar_passthrough() -> None:
    """JSON scalar is not reformatted — no structure to expand."""
    assert prettify_tool_output("42") == "42"
    assert prettify_tool_output('"hello"') == '"hello"'


def test_prettify_plain_text_passthrough() -> None:
    """Non-JSON text is returned unchanged."""
    raw = "just plain text, nothing to parse"
    assert prettify_tool_output(raw) == raw


def test_prettify_invalid_json_passthrough() -> None:
    """Broken JSON is returned unchanged, not raised."""
    raw = '{"unterminated'
    assert prettify_tool_output(raw) == raw


# ── TerminalToolRendererRegistry ───────────────────────────────────────


def test_registry_unknown_tool_returns_none() -> None:
    """Dispatch for an unregistered tool name returns None (caller uses fallback)."""
    registry = TerminalToolRendererRegistry()
    ex = _tool_ex("unknown_tool", output='{"ok": true}')
    parsed = parse_tool_output(ex.output or "")
    result = registry.render_tool(ex, parsed, _theme())
    assert result is None, (
        "Expected None for an unregistered tool name so the caller "
        "can fall through to the generic panel."
    )


def test_registry_unknown_native_returns_none() -> None:
    """Dispatch for an unregistered native tool type returns None."""
    registry = TerminalToolRendererRegistry()
    block = _native_block("unknown_native_type")
    result = registry.render_native(block, _theme())
    assert result is None


def test_registry_register_and_dispatch() -> None:
    """A registered renderer is called when the tool name matches."""
    registry = TerminalToolRendererRegistry()
    call_log: list[str] = []

    @registry.register("my_tool")
    def _renderer(ex, parsed, theme):
        call_log.append(ex.name)
        return Text("custom render")

    ex = _tool_ex("my_tool", output="data")
    parsed = parse_tool_output("data")
    result = registry.render_tool(ex, parsed, _theme())
    assert result is not None
    assert call_log == ["my_tool"], (
        f"Expected the registered renderer to be called exactly once. Got call_log={call_log}."
    )


def test_registry_name_normalization() -> None:
    """Tool names are normalized: lowercased and hyphens become underscores."""
    registry = TerminalToolRendererRegistry()

    @registry.register("My-Tool")
    def _renderer(ex, parsed, theme):
        return Text("found")

    # Dispatch with different casing/hyphens should still match.
    ex = _tool_ex("MY_TOOL", output="x")
    parsed = parse_tool_output("x")
    result = registry.render_tool(ex, parsed, _theme())
    assert result is not None, (
        "Expected normalization to match 'MY_TOOL' against registered 'My-Tool'. "
        "If None, name normalization is broken."
    )


def test_registry_register_aliases() -> None:
    """Multiple names registered to the same renderer all dispatch correctly."""
    registry = TerminalToolRendererRegistry()

    @registry.register("alias_a", "alias_b")
    def _renderer(ex, parsed, theme):
        return Text("aliased")

    for name in ("alias_a", "alias_b"):
        ex = _tool_ex(name, output="x")
        parsed = parse_tool_output("x")
        result = registry.render_tool(ex, parsed, _theme())
        assert result is not None, f"Alias '{name}' should dispatch to the registered renderer."


def test_registry_register_native_and_dispatch() -> None:
    """A registered native renderer is called for matching tool_type."""
    registry = TerminalToolRendererRegistry()

    @registry.register_native("custom_native")
    def _renderer(block, theme):
        return Text(f"native: {block.label}")

    block = _native_block("custom_native", label="my label")
    result = registry.render_native(block, _theme())
    rendered = _render_to_text(result)
    assert "my label" in rendered


# ── DEFAULT_TOOL_RENDERERS: file read ──────────────────────────────────


def test_render_file_read_json_envelope() -> None:
    """sys_os_read with a JSON envelope renders a syntax-highlighted panel."""
    output = json.dumps(
        {
            "path": "/tmp/example.py",
            "content": "def hello():\n    print('hi')\n",
            "offset": 1,
            "returned_lines": 2,
            "total_lines": 2,
        }
    )
    ex = _tool_ex("sys_os_read", output=output, arguments={"path": "/tmp/example.py"})
    parsed = parse_tool_output(output)
    result = DEFAULT_TOOL_RENDERERS.render_tool(ex, parsed, _theme())
    assert result is not None, (
        "Expected a specialized panel for sys_os_read with a valid JSON envelope. "
        "If None, the renderer didn't match or the envelope structure changed."
    )
    rendered = _render_to_text(result)
    assert "example.py" in rendered, "Panel title should contain the file path."
    assert "def hello" in rendered, "Panel body should contain the file content."


def test_render_file_read_alias() -> None:
    """'read' is an alias for sys_os_read and dispatches the same renderer."""
    output = json.dumps({"path": "/tmp/f.txt", "content": "data"})
    ex = _tool_ex("read", output=output, arguments={"file_path": "/tmp/f.txt"})
    parsed = parse_tool_output(output)
    result = DEFAULT_TOOL_RENDERERS.render_tool(ex, parsed, _theme())
    assert result is not None, "'read' should be a registered alias."


def test_render_file_read_error_envelope() -> None:
    """sys_os_read with an error field renders an error panel."""
    output = json.dumps({"error": "File not found"})
    ex = _tool_ex("sys_os_read", output=output)
    parsed = parse_tool_output(output)
    result = DEFAULT_TOOL_RENDERERS.render_tool(ex, parsed, _theme())
    assert result is not None
    rendered = _render_to_text(result)
    assert "File not found" in rendered


def test_render_file_read_non_json_returns_none() -> None:
    """sys_os_read with non-JSON output returns None (generic fallback)."""
    ex = _tool_ex("sys_os_read", output="plain text, not JSON")
    parsed = parse_tool_output("plain text, not JSON")
    result = DEFAULT_TOOL_RENDERERS.render_tool(ex, parsed, _theme())
    assert result is None, (
        "Non-JSON output should return None so the formatter uses the generic panel."
    )


# ── DEFAULT_TOOL_RENDERERS: shell ──────────────────────────────────────


def test_render_shell_success() -> None:
    """sys_os_shell with exit_code=0 renders a success-bordered panel."""
    output = json.dumps(
        {
            "stdout": "hello world\n",
            "stderr": "",
            "exit_code": 0,
            "cwd": "/tmp",
        }
    )
    ex = _tool_ex("sys_os_shell", output=output, arguments={"command": "echo hello world"})
    parsed = parse_tool_output(output)
    result = DEFAULT_TOOL_RENDERERS.render_tool(ex, parsed, _theme())
    assert result is not None
    rendered = _render_to_text(result)
    assert "hello world" in rendered
    assert "exit 0" in rendered


def test_render_shell_failure() -> None:
    """sys_os_shell with non-zero exit_code renders an error-bordered panel."""
    output = json.dumps(
        {
            "stdout": "",
            "stderr": "command not found\n",
            "exit_code": 127,
        }
    )
    ex = _tool_ex("bash", output=output, arguments={"command": "nonexistent"})
    parsed = parse_tool_output(output)
    result = DEFAULT_TOOL_RENDERERS.render_tool(ex, parsed, _theme())
    assert result is not None
    rendered = _render_to_text(result)
    assert "exit 127" in rendered
    assert "command not found" in rendered


def test_render_shell_timeout() -> None:
    """sys_os_shell with timed_out=True renders a warning-bordered panel."""
    output = json.dumps(
        {
            "stdout": "partial",
            "stderr": "",
            "exit_code": None,
            "timed_out": True,
        }
    )
    ex = _tool_ex("shell", output=output, arguments={"command": "sleep 999"})
    parsed = parse_tool_output(output)
    result = DEFAULT_TOOL_RENDERERS.render_tool(ex, parsed, _theme())
    assert result is not None
    rendered = _render_to_text(result)
    assert "timed out" in rendered


def test_render_shell_no_output() -> None:
    """Shell with empty stdout/stderr renders '(no output)'."""
    output = json.dumps({"stdout": "", "stderr": "", "exit_code": 0})
    ex = _tool_ex("sys_os_shell", output=output, arguments={"command": "true"})
    parsed = parse_tool_output(output)
    result = DEFAULT_TOOL_RENDERERS.render_tool(ex, parsed, _theme())
    rendered = _render_to_text(result)
    assert "(no output)" in rendered


def test_render_shell_non_shell_json_returns_none() -> None:
    """JSON without shell fields (stdout/stderr/exit_code) returns None."""
    output = json.dumps({"unrelated": "data"})
    ex = _tool_ex("sys_os_shell", output=output)
    parsed = parse_tool_output(output)
    result = DEFAULT_TOOL_RENDERERS.render_tool(ex, parsed, _theme())
    assert result is None


# ── DEFAULT_TOOL_RENDERERS: terminal_read ──────────────────────────────


def test_render_terminal_read() -> None:
    """sys_terminal_read with a screen field renders a terminal panel."""
    output = json.dumps(
        {
            "terminal": "zsh",
            "screen": "$ ls\nfile1.txt\nfile2.txt",
            "scrollback_lines": 100,
        }
    )
    ex = _tool_ex("sys_terminal_read", output=output, arguments={"terminal": "zsh"})
    parsed = parse_tool_output(output)
    result = DEFAULT_TOOL_RENDERERS.render_tool(ex, parsed, _theme())
    assert result is not None
    rendered = _render_to_text(result)
    assert "terminal" in rendered.lower()
    assert "file1.txt" in rendered


def test_render_terminal_read_error() -> None:
    """sys_terminal_read with an error renders the error, not screen."""
    output = json.dumps({"error": "Terminal not found"})
    ex = _tool_ex("sys_terminal_read", output=output)
    parsed = parse_tool_output(output)
    result = DEFAULT_TOOL_RENDERERS.render_tool(ex, parsed, _theme())
    rendered = _render_to_text(result)
    assert "Terminal not found" in rendered


# ── DEFAULT_TOOL_RENDERERS: terminal_list ──────────────────────────────


def test_render_terminal_list() -> None:
    """sys_terminal_list with a JSON array renders a table."""
    output = json.dumps(
        [
            {
                "terminal": "zsh",
                "session": "s1",
                "running": True,
                "command": "vim",
                "has_os_env": False,
            },
            {
                "terminal": "bash",
                "session": "s2",
                "running": False,
                "command": "",
                "has_os_env": True,
            },
        ]
    )
    ex = _tool_ex("sys_terminal_list", output=output)
    parsed = parse_tool_output(output)
    result = DEFAULT_TOOL_RENDERERS.render_tool(ex, parsed, _theme())
    assert result is not None
    rendered = _render_to_text(result)
    assert "zsh" in rendered
    assert "bash" in rendered


def test_render_terminal_list_non_array_returns_none() -> None:
    """terminal_list with non-array JSON returns None."""
    output = json.dumps({"not": "an array"})
    ex = _tool_ex("sys_terminal_list", output=output)
    parsed = parse_tool_output(output)
    result = DEFAULT_TOOL_RENDERERS.render_tool(ex, parsed, _theme())
    assert result is None


# ── DEFAULT_TOOL_RENDERERS: list_tasks ─────────────────────────────────


def test_render_task_list_with_tasks() -> None:
    """list_tasks with tasks renders a table."""
    output = json.dumps(
        {
            "tasks": [
                {
                    "task_id": "t1",
                    "kind": "tool",
                    "status": "running",
                    "tool_name": "bash",
                    "created_at": "12:00",
                },
            ]
        }
    )
    ex = _tool_ex("list_tasks", output=output)
    parsed = parse_tool_output(output)
    result = DEFAULT_TOOL_RENDERERS.render_tool(ex, parsed, _theme())
    assert result is not None
    rendered = _render_to_text(result)
    assert "t1" in rendered
    assert "bash" in rendered


def test_render_task_list_empty() -> None:
    """list_tasks with empty tasks array renders 'no tasks'."""
    output = json.dumps({"tasks": []})
    ex = _tool_ex("list_tasks", output=output)
    parsed = parse_tool_output(output)
    result = DEFAULT_TOOL_RENDERERS.render_tool(ex, parsed, _theme())
    rendered = _render_to_text(result)
    assert "no" in rendered.lower()


# ── DEFAULT_TOOL_RENDERERS: status tools ───────────────────────────────


def test_render_write_tool_success() -> None:
    """sys_os_write with a JSON success envelope renders a status panel."""
    output = json.dumps({"path": "/tmp/out.txt", "bytes_written": 1024, "created": True})
    ex = _tool_ex("sys_os_write", output=output, arguments={"path": "/tmp/out.txt"})
    parsed = parse_tool_output(output)
    result = DEFAULT_TOOL_RENDERERS.render_tool(ex, parsed, _theme())
    assert result is not None
    rendered = _render_to_text(result)
    # "created" because obj.created is True
    assert "created" in rendered.lower()
    assert "1.0 KB" in rendered, "Expected human-readable byte size '1.0 KB' for 1024 bytes."


def test_render_edit_tool_success() -> None:
    """sys_os_edit with replacements renders the count."""
    output = json.dumps({"path": "/tmp/f.py", "replacements": 3, "bytes_written": 512})
    ex = _tool_ex("sys_os_edit", output=output, arguments={"path": "/tmp/f.py"})
    parsed = parse_tool_output(output)
    result = DEFAULT_TOOL_RENDERERS.render_tool(ex, parsed, _theme())
    rendered = _render_to_text(result)
    assert "3 replacements" in rendered
    assert "edited" in rendered.lower()


def test_render_edit_tool_shows_diff_from_old_and_new_text() -> None:
    """sys_os_edit renders a unified diff when edit arguments are available."""
    output = json.dumps({"path": "f.py", "replacements": 1, "bytes_written": 20})
    ex = _tool_ex(
        "sys_os_edit",
        output=output,
        arguments={
            "path": "f.py",
            "oldText": "print('old')\n",
            "newText": "print('new')\n",
        },
    )
    parsed = parse_tool_output(output)
    result = DEFAULT_TOOL_RENDERERS.render_tool(ex, parsed, _theme())
    rendered = _render_to_text(result)
    assert "---" in rendered
    assert "+++" in rendered
    assert "@@" in rendered
    assert "--- f.py" in rendered
    assert "+++ f.py" in rendered
    assert "@@ -1 +1 @@" in rendered
    assert "--- f.py+++ f.py" not in rendered
    assert "+++ f.py@@ -1 +1 @@" not in rendered
    assert "-print('old')" in rendered
    assert "+print('new')" in rendered


def test_render_edit_tool_shows_diff_from_edits_array() -> None:
    """sys_os_edit renders all replacement entries from the edits array."""
    output = json.dumps({"path": "/tmp/f.py", "replacements": 2, "bytes_written": 20})
    ex = _tool_ex(
        "sys_os_edit",
        output=output,
        arguments={
            "path": "/tmp/f.py",
            "edits": [
                {"oldText": "alpha\n", "newText": "beta\n"},
                {"oldText": "one\n", "newText": "two\n"},
            ],
        },
    )
    parsed = parse_tool_output(output)
    result = DEFAULT_TOOL_RENDERERS.render_tool(ex, parsed, _theme())
    rendered = _render_to_text(result)
    assert "-alpha" in rendered
    assert "+beta" in rendered
    assert "-one" in rendered
    assert "+two" in rendered


def test_render_status_tool_error() -> None:
    """A status tool with an error field renders an error panel."""
    output = json.dumps({"error": "Permission denied"})
    ex = _tool_ex("sys_os_write", output=output)
    parsed = parse_tool_output(output)
    result = DEFAULT_TOOL_RENDERERS.render_tool(ex, parsed, _theme())
    rendered = _render_to_text(result)
    assert "Permission denied" in rendered


def test_render_status_tool_raw_error_prefix() -> None:
    """A status tool with 'Error:' prefix in raw text renders error panel."""
    ex = _tool_ex("sys_os_write", output="Error: disk full")
    parsed = parse_tool_output("Error: disk full")
    result = DEFAULT_TOOL_RENDERERS.render_tool(ex, parsed, _theme())
    rendered = _render_to_text(result)
    assert "disk full" in rendered


def test_render_terminal_launch_success() -> None:
    """sys_terminal_launch renders terminal name and status."""
    output = json.dumps(
        {
            "terminal": "zsh",
            "session": "s1",
            "status": "launched",
            "notify_when_idle": True,
        }
    )
    ex = _tool_ex(
        "sys_terminal_launch", output=output, arguments={"terminal": "zsh", "session": "s1"}
    )
    parsed = parse_tool_output(output)
    result = DEFAULT_TOOL_RENDERERS.render_tool(ex, parsed, _theme())
    rendered = _render_to_text(result)
    assert "zsh" in rendered
    assert "launched" in rendered


# ── DEFAULT_TOOL_RENDERERS: native renderers ───────────────────────────


def test_render_native_web_search() -> None:
    """web_search_call with action.type=search renders the query."""
    block = _native_block(
        "web_search_call",
        label="web_search",
        action={"type": "search", "query": "python dataclasses"},
    )
    result = DEFAULT_TOOL_RENDERERS.render_native(block, _theme())
    assert result is not None
    rendered = _render_to_text(result)
    assert "web search" in rendered.lower()
    assert "python dataclasses" in rendered


def test_render_native_web_search_open_page() -> None:
    """web_search_call with action.type=open_page renders the URL."""
    block = _native_block(
        "web_search_call",
        label="web_search",
        action={"type": "open_page", "url": "https://example.com"},
    )
    result = DEFAULT_TOOL_RENDERERS.render_native(block, _theme())
    rendered = _render_to_text(result)
    assert "open page" in rendered.lower()
    assert "example.com" in rendered


def test_render_native_web_search_unknown_action_returns_none() -> None:
    """web_search_call with an unknown action type returns None."""
    block = _native_block(
        "web_search_call",
        label="web_search",
        action={"type": "unknown_action"},
    )
    result = DEFAULT_TOOL_RENDERERS.render_native(block, _theme())
    assert result is None


def test_render_native_mcp_call() -> None:
    """mcp_call renders the tool name."""
    block = _native_block("mcp_call", label="mcp", name="read_database")
    result = DEFAULT_TOOL_RENDERERS.render_native(block, _theme())
    assert result is not None
    rendered = _render_to_text(result)
    assert "MCP" in rendered
    assert "read_database" in rendered


def test_render_native_mcp_list_tools() -> None:
    """mcp_list_tools is a registered alias for the MCP native renderer."""
    block = _native_block("mcp_list_tools", label="mcp_list")
    result = DEFAULT_TOOL_RENDERERS.render_native(block, _theme())
    assert result is not None


# ── Formatter integration ──────────────────────────────────────────────


def test_formatter_uses_specialized_tool_renderer() -> None:
    """RichBlockFormatter dispatches to specialized renderers for known tools."""
    from omnigent_client._blocks import ToolGroup
    from omnigent_ui_sdk.terminal._formatter import RichBlockFormatter

    fmt = RichBlockFormatter(show_tool_output=True)
    output = json.dumps(
        {
            "stdout": "hello\n",
            "stderr": "",
            "exit_code": 0,
        }
    )
    group = ToolGroup(
        executions=[
            ToolExecution(
                name="sys_os_shell",
                arguments={"command": "echo hello"},
                args_summary="echo hello",
                call_id="c1",
                agent_name="test",
                executed_by="server",
                output=output,
            ),
        ]
    )
    items = fmt.format(group)
    # Tool call line + specialized result panel = at least 2 items.
    assert len(items) >= 2, (
        f"Expected at least 2 items (call line + result panel), got {len(items)}."
    )
    rendered = _render_to_text(items[1])
    # The specialized shell renderer shows "exit 0" — the generic
    # panel wouldn't have this label.
    assert "exit 0" in rendered, (
        "Expected 'exit 0' from the specialized shell renderer. "
        "If absent, the formatter fell through to the generic panel."
    )


def test_formatter_result_block_preserves_arguments_for_edit_diff() -> None:
    """Result-only tool blocks still carry call arguments for diff rendering."""
    from omnigent_client._blocks import ToolResultBlock
    from omnigent_ui_sdk.terminal._formatter import RichBlockFormatter

    output = json.dumps({"path": "/tmp/f.py", "replacements": 1, "bytes_written": 20})
    block = ToolResultBlock(
        name="sys_os_edit",
        call_id="c1",
        agent_name="test",
        output=output,
        arguments={
            "path": "/tmp/f.py",
            "oldText": "print('old')\n",
            "newText": "print('new')\n",
        },
    )

    items = RichBlockFormatter(show_tool_output=True).format_tool_result(block)
    rendered = _render_to_text(items[0])
    assert "-print('old')" in rendered
    assert "+print('new')" in rendered


def test_formatter_falls_back_for_unknown_tool() -> None:
    """Unknown tool names fall back to the generic panel."""
    from omnigent_client._blocks import ToolGroup
    from omnigent_ui_sdk.terminal._formatter import RichBlockFormatter

    fmt = RichBlockFormatter(show_tool_output=True)
    group = ToolGroup(
        executions=[
            ToolExecution(
                name="totally_custom_tool",
                arguments={},
                args_summary="",
                call_id="c1",
                agent_name="test",
                executed_by="server",
                output="some output",
            ),
        ]
    )
    items = fmt.format(group)
    assert len(items) >= 2, "Generic fallback should still produce call line + panel."
    rendered = _render_to_text(items[1])
    assert "some output" in rendered


def test_formatter_custom_registry() -> None:
    """RichBlockFormatter accepts a custom tool_renderers registry."""
    from omnigent_client._blocks import ToolGroup
    from omnigent_ui_sdk.terminal._formatter import RichBlockFormatter

    custom = TerminalToolRendererRegistry()

    @custom.register("my_custom")
    def _renderer(ex, parsed, theme):
        return Text("CUSTOM RESULT")

    fmt = RichBlockFormatter(tool_renderers=custom, show_tool_output=True)
    group = ToolGroup(
        executions=[
            ToolExecution(
                name="my_custom",
                arguments={},
                args_summary="",
                call_id="c1",
                agent_name="test",
                executed_by="server",
                output="raw",
            ),
        ]
    )
    items = fmt.format(group)
    # The custom renderer returns Text("CUSTOM RESULT") which replaces
    # the generic panel.
    found = any("CUSTOM RESULT" in _render_to_text(item) for item in items)
    assert found, (
        "Expected the custom renderer's output in the formatted items. "
        "If absent, the custom registry was not used."
    )


def test_formatter_native_tool_specialized() -> None:
    """RichBlockFormatter dispatches native tool blocks to specialized renderers."""
    from omnigent_client._blocks import NativeToolBlock
    from omnigent_ui_sdk.terminal._formatter import RichBlockFormatter

    fmt = RichBlockFormatter()
    block = NativeToolBlock(
        tool_type="web_search_call",
        label="web_search",
        data={"action": {"type": "search", "query": "test query"}},
    )
    items = fmt.format_native_tool(block)
    # Specialized renderer should produce exactly 1 item.
    assert len(items) == 1
    rendered = _render_to_text(items[0])
    assert "web search" in rendered.lower(), (
        "Expected 'web search' from the specialized native renderer. "
        "If '⏵ web_search' appears instead, the generic fallback was used."
    )
    assert "test query" in rendered


# ── _bytes_message through status renderers ────────────────────────────


@pytest.mark.parametrize(
    "bytes_val,expected_fragment",
    [
        (0, "0 bytes"),
        (512, "512 bytes"),
        (1024, "1.0 KB"),
        (2048, "2.0 KB"),
        (1048576, "1.0 MB"),
        (5242880, "5.0 MB"),
    ],
    ids=["zero", "sub-KB", "1KB", "2KB", "1MB", "5MB"],
)
def test_bytes_display(bytes_val: int, expected_fragment: str) -> None:
    """Byte values are displayed in human-readable form via status renderers."""
    output = json.dumps({"path": "/tmp/f", "bytes_written": bytes_val, "created": False})
    ex = _tool_ex("sys_os_write", output=output, arguments={"path": "/tmp/f"})
    parsed = parse_tool_output(output)
    result = DEFAULT_TOOL_RENDERERS.render_tool(ex, parsed, _theme())
    rendered = _render_to_text(result)
    assert expected_fragment in rendered, (
        f"Expected '{expected_fragment}' for {bytes_val} bytes, got: {rendered!r}"
    )


# ── Pydantic model validation ─────────────────────────────────────────
#
# Focused tests that Pydantic validation/coercion works correctly for
# the payload models.  These complement the rendering-level tests above.


class TestPydanticModels:
    """Pydantic model validation and coercion behavior."""

    def test_file_read_requires_content(self) -> None:
        """FileReadResult rejects dicts without a ``content`` string."""
        with pytest.raises(ValidationError):
            FileReadResult.model_validate({"path": "/tmp/f.py"})

    def test_file_read_coerces_offset(self) -> None:
        """FileReadResult coerces string offset to int."""
        result = FileReadResult.model_validate({"content": "x", "offset": "5"})
        assert result.offset == 5

    def test_shell_result_coerces_exit_code(self) -> None:
        """ShellResult coerces string exit_code to int."""
        result = ShellResult.model_validate({"exit_code": "42"})
        assert result.exit_code == 42

    def test_shell_result_defaults(self) -> None:
        """ShellResult fills missing fields with defaults."""
        result = ShellResult.model_validate({})
        assert result.stdout == ""
        assert result.exit_code is None
        assert result.timed_out is False

    def test_terminal_read_requires_screen(self) -> None:
        """TerminalReadResult rejects dicts without ``screen``."""
        with pytest.raises(ValidationError):
            TerminalReadResult.model_validate({"terminal": "zsh"})

    def test_terminal_list_entry_coerces(self) -> None:
        """TerminalListEntry fills defaults for missing optional fields."""
        entry = TerminalListEntry.model_validate({"terminal": "zsh"})
        assert entry.terminal == "zsh"
        assert entry.running is None
        assert entry.command == ""

    def test_task_entry_target_from_tool_name(self) -> None:
        """TaskEntry.target resolves from tool_name."""
        entry = TaskEntry.model_validate({"tool_name": "bash"})
        assert entry.target == "bash"

    def test_task_entry_target_from_sub_agent(self) -> None:
        """TaskEntry.target falls back to sub_agent.name."""
        entry = TaskEntry.model_validate({"sub_agent": {"name": "researcher"}})
        assert entry.target == "researcher"

    def test_task_list_result_requires_tasks_list(self) -> None:
        """TaskListResult rejects dicts without a ``tasks`` list."""
        with pytest.raises(ValidationError):
            TaskListResult.model_validate({"no_tasks": True})

    def test_status_tool_fields_bytes_alias(self) -> None:
        """StatusToolFields reads ``bytes`` key via alias into ``download_bytes``."""
        fields = StatusToolFields.model_validate({"bytes": 4096})
        assert fields.download_bytes == 4096

    def test_status_tool_fields_extra_allowed(self) -> None:
        """StatusToolFields tolerates unknown keys (extra='allow')."""
        fields = StatusToolFields.model_validate({"unknown_key": "ok", "error": "e"})
        assert fields.error == "e"

    def test_web_search_action_type_alias(self) -> None:
        """WebSearchAction reads ``type`` field correctly."""
        action = WebSearchAction.model_validate({"type": "search", "query": "test"})
        assert action.type == "search"
        assert action.query == "test"

    def test_mcp_call_data_defaults(self) -> None:
        """McpCallData defaults name to empty string."""
        data = McpCallData.model_validate({})
        assert data.name == ""
