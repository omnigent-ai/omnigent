"""Tool-specific Rich renderers for the terminal formatter.

The stream protocol currently exposes tool results as raw strings. Most
built-in tools, however, return stable JSON envelopes. This module keeps the
knowledge of those envelopes out of ``_formatter.py`` and provides a small,
extensible registry that can turn known envelopes into semantic terminal
views while preserving the generic panel fallback for unknown tools.

Each tool envelope is modelled as a Pydantic ``BaseModel`` with automatic
validation and coercion.  Renderers call ``model.model_validate(obj)``
early and then use typed attribute access rather than ad-hoc
``obj.get("key")`` lookups.
"""

from __future__ import annotations

import difflib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from omnigent_client import NativeToolBlock, ToolExecution
from pydantic import BaseModel, Field, ValidationError
from rich import box
from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text


@dataclass(frozen=True)
class TerminalToolRenderTheme:
    """Styling/configuration supplied by ``RichBlockFormatter``."""

    accent: str
    muted: str
    warning: str
    error: str
    success: str
    code_theme: str
    max_result_lines: int
    max_result_chars: int


@dataclass(frozen=True)
class ParsedToolOutput:
    """Best-effort parse of a raw tool result string."""

    raw: str
    json_value: Any | None = None
    is_json: bool = False


def parse_tool_output(raw: str) -> ParsedToolOutput:
    """Parse JSON object/array/scalar tool output when possible."""
    stripped = raw.lstrip()
    if not stripped:
        return ParsedToolOutput(raw=raw)
    try:
        return ParsedToolOutput(raw=raw, json_value=json.loads(stripped), is_json=True)
    except ValueError:
        return ParsedToolOutput(raw=raw)


def prettify_tool_output(raw: str) -> str:
    """Pretty-print JSON object/array output; preserve all other output."""
    parsed = parse_tool_output(raw)
    if parsed.is_json and isinstance(parsed.json_value, (dict, list)):
        return json.dumps(parsed.json_value, indent=2, ensure_ascii=False)
    return raw


ToolRenderer = Callable[
    [ToolExecution, ParsedToolOutput, TerminalToolRenderTheme],
    RenderableType | None,
]
NativeToolRenderer = Callable[[NativeToolBlock, TerminalToolRenderTheme], RenderableType | None]


class TerminalToolRendererRegistry:
    """Dispatches tool/native-tool blocks to specialized renderers.

    ``register()`` supports aliases so provider-specific names (``Bash``) and
    built-in names (``sys_os_shell``) can share an implementation. Unknown
    names simply return ``None`` so the caller can use its generic fallback.
    """

    def __init__(self) -> None:
        self._tool_renderers: dict[str, ToolRenderer] = {}
        self._native_renderers: dict[str, NativeToolRenderer] = {}

    def register(self, *names: str) -> Callable[[ToolRenderer], ToolRenderer]:
        def decorator(renderer: ToolRenderer) -> ToolRenderer:
            for name in names:
                self._tool_renderers[_normalize_tool_name(name)] = renderer
            return renderer

        return decorator

    def register_native(
        self, *tool_types: str
    ) -> Callable[[NativeToolRenderer], NativeToolRenderer]:
        def decorator(renderer: NativeToolRenderer) -> NativeToolRenderer:
            for tool_type in tool_types:
                self._native_renderers[_normalize_tool_name(tool_type)] = renderer
            return renderer

        return decorator

    def render_tool(
        self,
        ex: ToolExecution,
        parsed: ParsedToolOutput,
        theme: TerminalToolRenderTheme,
    ) -> RenderableType | None:
        renderer = self._tool_renderers.get(_normalize_tool_name(ex.name))
        if renderer is None:
            return None
        return renderer(ex, parsed, theme)

    def render_native(
        self,
        block: NativeToolBlock,
        theme: TerminalToolRenderTheme,
    ) -> RenderableType | None:
        renderer = self._native_renderers.get(_normalize_tool_name(block.tool_type))
        if renderer is None:
            return None
        return renderer(block, theme)


def _native_call_line(label: str, detail: str, theme: TerminalToolRenderTheme) -> Text:
    return Text.from_markup(
        f"   [{theme.accent}]⏵ {label}[/{theme.accent}][dim]({_escape_markup(detail)})[/dim]"
    )


def _normalize_tool_name(name: str) -> str:
    return name.strip().lower().replace("-", "_")


DEFAULT_TOOL_RENDERERS = TerminalToolRendererRegistry()


# ── Tool output payload models ────────────────────────────────────────
#
# Pydantic v2 BaseModel for each tool's JSON envelope.  Renderers call
# ``Model.model_validate(obj)`` to parse and coerce; ``ValidationError``
# is caught and treated as "shape mismatch → fall through to generic."


class FileReadResult(BaseModel, frozen=True):
    """Parsed payload for ``sys_os_read`` / ``read`` tool output."""

    path: str = ""
    content: str
    offset: int = 1
    returned_lines: int | None = None
    total_lines: int | None = None
    limit: int | None = None


class ShellResult(BaseModel, frozen=True):
    """Parsed payload for ``sys_os_shell`` / ``bash`` / ``shell`` tool output."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    cwd: str = ""
    shell: str = ""
    error: str = ""


class TerminalReadResult(BaseModel, frozen=True):
    """Parsed payload for ``sys_terminal_read`` / ``terminal_read`` tool output."""

    terminal: str = ""
    screen: str
    scrollback_lines: int | None = None


class TerminalListEntry(BaseModel, frozen=True):
    """A single entry in ``sys_terminal_list`` / ``terminal_list`` output."""

    terminal: str = ""
    session: str = ""
    running: bool | None = None
    command: str = ""
    has_os_env: bool | None = None


class TaskEntry(BaseModel, frozen=True):
    """A single task in ``list_tasks`` output."""

    task_id: str = ""
    kind: str = ""
    status: str = ""
    tool_name: str = ""
    sub_agent: dict[str, Any] | None = None
    created_at: str = ""

    @property
    def target(self) -> str:
        if self.tool_name:
            return self.tool_name
        if self.sub_agent and isinstance(self.sub_agent, Mapping):
            name = self.sub_agent.get("name")
            return str(name) if name is not None else ""
        return ""


class TaskListResult(BaseModel, frozen=True):
    """Parsed payload for ``list_tasks`` tool output."""

    tasks: list[TaskEntry]


class StatusToolFields(BaseModel, frozen=True):
    """Union of all fields across status-tool JSON envelopes.

    Status tools (write, edit, terminal_launch, timer_set, …) share a
    single renderer but have tool-specific JSON shapes.  This model
    captures the superset of fields so the renderer uses typed access.
    """

    error: str = ""
    path: str = ""
    bytes_written: int = 0
    created: bool = False
    replacements: int = 0
    terminal: str = ""
    session: str = ""
    status: str = ""
    notify_when_idle: bool = False
    seconds: str = ""
    repeat: bool = False
    note: str = ""
    timer_id: str = ""
    tool_name: str = ""
    task_id: str = ""
    cancelled: str = ""
    filename: str = ""
    content_type: str = ""
    file_id: str = ""
    download_bytes: int = Field(default=0, alias="bytes")

    model_config = {"populate_by_name": True, "extra": "allow"}


class WebSearchAction(BaseModel, frozen=True):
    """Parsed payload for ``web_search_call`` native tool block's ``action``."""

    type: str = Field(alias="type", default="")
    query: str = ""
    url: str = ""

    model_config = {"populate_by_name": True}


class McpCallData(BaseModel, frozen=True):
    """Parsed payload for ``mcp_call`` / ``mcp_list_tools`` native tool block."""

    name: str = ""


# ── Registered renderers ───────────────────────────────────────────────


@DEFAULT_TOOL_RENDERERS.register("sys_os_read", "read")
def _render_file_read(
    ex: ToolExecution,
    parsed: ParsedToolOutput,
    theme: TerminalToolRenderTheme,
) -> RenderableType | None:
    obj = _json_object(parsed)
    if obj is None:
        return None
    error_panel = _render_error_if_present(obj, theme)
    if error_panel is not None:
        return error_panel

    try:
        result = FileReadResult.model_validate(obj)
    except ValidationError:
        return None

    path = result.path or _as_str(ex.arguments.get("path") or ex.arguments.get("file_path"))
    content_lines = len(result.content.splitlines())
    offset = result.offset
    returned = result.returned_lines if result.returned_lines is not None else content_lines
    total = result.total_lines if result.total_lines is not None else returned
    limit = result.limit if result.limit is not None else returned

    title = f"read {_display_path(path)}"
    subtitle = f"lines {offset}-{max(offset, offset + returned - 1)}"
    if total:
        subtitle += f" of {total}"
    if limit and returned >= limit and total and offset + returned - 1 < total:
        subtitle += " · truncated"

    language = _language_for_path(path)
    body = _truncate_text(result.content, theme)
    syntax = Syntax(
        body.visible,
        language,
        theme=theme.code_theme,
        line_numbers=True,
        start_line=offset,
        word_wrap=False,
    )
    renderables: list[RenderableType] = [syntax]
    if body.note:
        renderables.append(Text(body.note, style=theme.muted))
    return _panel(Group(*renderables), theme, title=title, subtitle=subtitle)


@DEFAULT_TOOL_RENDERERS.register("sys_os_shell", "bash", "shell")
def _render_shell(
    ex: ToolExecution,
    parsed: ParsedToolOutput,
    theme: TerminalToolRenderTheme,
) -> RenderableType | None:
    obj = _json_object(parsed)
    if obj is None:
        return None

    has_shell_fields = any(key in obj for key in ("stdout", "stderr", "exit_code", "timed_out"))
    if not has_shell_fields:
        return _render_error_if_present(obj, theme)

    try:
        result = ShellResult.model_validate(obj)
    except ValidationError:
        return _render_error_if_present(obj, theme)

    if result.timed_out:
        status = "timed out"
        border = theme.warning
    elif result.exit_code == 0:
        status = "exit 0"
        border = theme.success
    elif result.exit_code is not None:
        status = f"exit {result.exit_code}"
        border = theme.error
    else:
        status = "shell"
        border = theme.accent

    cmd = _as_str(ex.arguments.get("command") or ex.args_summary)
    title = f"shell · {status}"
    subtitle_bits = [
        _display_path(result.cwd) if result.cwd else "",
        os.path.basename(result.shell) if result.shell else "",
    ]
    subtitle = " · ".join(bit for bit in subtitle_bits if bit)

    sections: list[RenderableType] = []
    if cmd:
        sections.append(Text(f"$ {cmd}", style=theme.muted))
    if result.error and (result.timed_out or result.exit_code not in (None, 0)):
        sections.append(
            Text(result.error, style=theme.error if not result.timed_out else theme.warning)
        )
    if result.stdout:
        sections.append(_section("stdout", result.stdout, theme, style=""))
    if result.stderr:
        sections.append(_section("stderr", result.stderr, theme, style=theme.warning))
    if not result.stdout and not result.stderr and not result.error:
        sections.append(Text("(no output)", style=theme.muted))

    return _panel(Group(*sections), theme, title=title, subtitle=subtitle, border_style=border)


@DEFAULT_TOOL_RENDERERS.register("sys_terminal_read", "terminal_read")
def _render_terminal_read(
    ex: ToolExecution,
    parsed: ParsedToolOutput,
    theme: TerminalToolRenderTheme,
) -> RenderableType | None:
    obj = _json_object(parsed)
    if obj is None:
        return None
    error_panel = _render_error_if_present(obj, theme)
    if error_panel is not None:
        return error_panel

    try:
        result = TerminalReadResult.model_validate(obj)
    except ValidationError:
        return None

    terminal = result.terminal or _as_str(ex.arguments.get("terminal"))
    session = _as_str(ex.arguments.get("session"))
    name = terminal
    if session and session not in terminal:
        name = f"{terminal}:{session}" if terminal else session
    subtitle = (
        f"{result.scrollback_lines} scrollback lines"
        if result.scrollback_lines is not None
        else ""
    )
    truncated = _truncate_text(result.screen, theme)
    body = Text(truncated.visible or "(empty terminal screen)", style="dim")
    renderables: list[RenderableType] = [body]
    if truncated.note:
        renderables.append(Text(truncated.note, style=theme.muted))
    return _panel(Group(*renderables), theme, title=f"terminal {name}".strip(), subtitle=subtitle)


@DEFAULT_TOOL_RENDERERS.register("sys_terminal_list", "terminal_list")
def _render_terminal_list(
    ex: ToolExecution,
    parsed: ParsedToolOutput,
    theme: TerminalToolRenderTheme,
) -> RenderableType | None:
    if not isinstance(parsed.json_value, list):
        return None

    entries: list[TerminalListEntry] = []
    for item in parsed.json_value:
        if not isinstance(item, Mapping):
            return None
        try:
            entries.append(TerminalListEntry.model_validate(item))
        except ValidationError:
            return None

    table = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style=theme.muted)
    table.add_column("terminal")
    table.add_column("session")
    table.add_column("running")
    table.add_column("command")
    table.add_column("os env")
    for entry in entries:
        table.add_row(
            entry.terminal,
            entry.session,
            _yes_no(entry.running),
            entry.command,
            _yes_no(entry.has_os_env),
        )
    return _panel(table, theme, title="terminals")


@DEFAULT_TOOL_RENDERERS.register("list_tasks")
def _render_task_list(
    ex: ToolExecution,
    parsed: ParsedToolOutput,
    theme: TerminalToolRenderTheme,
) -> RenderableType | None:
    obj = _json_object(parsed)
    if obj is None:
        return None

    try:
        result = TaskListResult.model_validate(obj)
    except ValidationError:
        return None

    if not result.tasks:
        return _status_panel(
            "no tasks",
            "No matching background tasks.",
            theme,
            border_style=theme.muted,
        )

    table = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style=theme.muted)
    table.add_column("task")
    table.add_column("kind")
    table.add_column("status")
    table.add_column("target")
    table.add_column("created")
    for task in result.tasks:
        table.add_row(
            task.task_id,
            task.kind,
            task.status,
            task.target,
            task.created_at,
        )
    return _panel(table, theme, title="background tasks")


@DEFAULT_TOOL_RENDERERS.register(
    "sys_os_write",
    "write",
    "sys_os_edit",
    "edit",
    "sys_terminal_launch",
    "terminal_launch",
    "sys_terminal_send",
    "terminal_send",
    "sys_terminal_close",
    "terminal_close",
    "sys_timer_set",
    "timer_set",
    "sys_timer_cancel",
    "timer_cancel",
    "sys_call_async",
    "sys_cancel_async",
    "sys_cancel_task",
    "sys_files_upload",
    "sys_files_download",
)
def _render_status_tool(
    ex: ToolExecution,
    parsed: ParsedToolOutput,
    theme: TerminalToolRenderTheme,
) -> RenderableType | None:
    obj = _json_object(parsed)
    if obj is None:
        if parsed.raw.startswith("Error:"):
            return _status_panel(ex.name, parsed.raw, theme, border_style=theme.error)
        return None
    # A runner download returns the file's own content, even a JSON document;
    # only the in-process tool returns saved-file metadata with a path.
    if ex.name == "sys_files_download" and "path" not in obj:
        return None

    try:
        fields = StatusToolFields.model_validate(obj)
    except ValidationError:
        return None

    # Overlay argument-derived fallbacks that aren't in the JSON output.
    path = fields.path or _as_str(ex.arguments.get("path") or ex.arguments.get("file_path"))
    terminal = fields.terminal or _as_str(ex.arguments.get("terminal"))
    session = fields.session or _as_str(ex.arguments.get("session"))

    if fields.error:
        return _status_panel(
            _human_tool_name(ex.name), fields.error, theme, border_style=theme.error
        )

    title, message = _status_title_message(
        ex, fields, path=path, terminal=terminal, session=session
    )
    if _normalize_tool_name(ex.name) in {"sys_os_edit", "edit"}:
        edit_diff = _render_edit_diff_panel(
            ex,
            fields,
            path=path,
            title=title,
            message=message,
            theme=theme,
        )
        if edit_diff is not None:
            return edit_diff
    return _status_panel(title, message, theme, border_style=theme.success)


@DEFAULT_TOOL_RENDERERS.register_native("web_search_call")
def _render_native_web_search(
    block: NativeToolBlock,
    theme: TerminalToolRenderTheme,
) -> RenderableType | None:
    raw_action = block.data.get("action")
    if not isinstance(raw_action, Mapping):
        return None
    try:
        action = WebSearchAction.model_validate(raw_action)
    except ValidationError:
        return None
    if action.type == "search":
        return _native_call_line("web search", action.query, theme)
    if action.type == "open_page":
        return _native_call_line("open page", action.url, theme)
    return None


@DEFAULT_TOOL_RENDERERS.register_native("mcp_call", "mcp_list_tools")
def _render_native_mcp(
    block: NativeToolBlock,
    theme: TerminalToolRenderTheme,
) -> RenderableType | None:
    try:
        data = McpCallData.model_validate(block.data)
    except ValidationError:
        return None
    name = data.name or block.label
    return _native_call_line("MCP", name, theme)


# ── Helper builders ────────────────────────────────────────────────────


@dataclass(frozen=True)
class _TruncatedText:
    visible: str
    note: str


def _json_object(parsed: ParsedToolOutput) -> Mapping[str, Any] | None:
    if parsed.is_json and isinstance(parsed.json_value, Mapping):
        return parsed.json_value
    return None


def _as_str(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _as_int(value: object, *, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _yes_no(value: object) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return ""


def _display_path(path: str) -> str:
    if not path:
        return ""
    try:
        return os.path.relpath(path) if os.path.isabs(path) else path
    except ValueError:
        return path


def _language_for_path(path: str) -> str:
    suffix = os.path.splitext(path)[1].lower().lstrip(".")
    aliases = {
        "py": "python",
        "js": "javascript",
        "jsx": "jsx",
        "ts": "typescript",
        "tsx": "tsx",
        "md": "markdown",
        "toml": "toml",
        "yaml": "yaml",
        "yml": "yaml",
        "json": "json",
        "sh": "bash",
        "zsh": "bash",
        "rs": "rust",
    }
    return aliases.get(suffix, suffix or "text")


def _escape_markup(text: str) -> str:
    return text.replace("[", "\\[").replace("]", "\\]")


def _truncate_text(text: str, theme: TerminalToolRenderTheme) -> _TruncatedText:
    lines = text.split("\n")
    omitted_lines = max(0, len(lines) - theme.max_result_lines)
    visible = "\n".join(lines[: theme.max_result_lines]) if omitted_lines else text
    omitted_chars = max(0, len(visible) - theme.max_result_chars)
    if omitted_chars:
        visible = visible[: theme.max_result_chars]
    notes: list[str] = []
    if omitted_lines:
        notes.append(f"… {omitted_lines} more lines")
    if omitted_chars:
        notes.append(f"… {omitted_chars} more chars")
    return _TruncatedText(visible=visible, note=" · ".join(notes))


def _panel(
    renderable: RenderableType,
    theme: TerminalToolRenderTheme,
    *,
    title: str,
    subtitle: str = "",
    border_style: str | None = None,
) -> Padding:
    safe_title = _escape_markup(title[:100])
    safe_subtitle = _escape_markup(subtitle[:100]) if subtitle else ""
    return Padding(
        Panel(
            renderable,
            title=f"[dim]{safe_title}[/dim]",
            title_align="left",
            subtitle=f"[dim]{safe_subtitle}[/dim]" if safe_subtitle else None,
            subtitle_align="right",
            border_style=border_style or theme.accent,
            box=box.ROUNDED,
            padding=(0, 1),
        ),
        (0, 1, 0, 3),
    )


def _render_edit_diff_panel(
    ex: ToolExecution,
    fields: StatusToolFields,
    *,
    path: str,
    title: str,
    message: str,
    theme: TerminalToolRenderTheme,
) -> RenderableType | None:
    diff = _diff_for_edit_arguments(ex.arguments, path=path)
    if not diff:
        return None

    body = _truncate_text(diff.rstrip("\n"), theme)
    renderables: list[RenderableType] = [
        Text(message, style=theme.muted),
        Syntax(
            body.visible,
            "diff",
            theme=theme.code_theme,
            line_numbers=False,
            word_wrap=False,
        ),
    ]
    if body.note:
        renderables.append(Text(body.note, style=theme.muted))
    plural = "s" if fields.replacements != 1 else ""
    subtitle = (
        f"{fields.replacements} replacement{plural} · {_bytes_message(fields.bytes_written)}"
    )
    return _panel(
        Group(*renderables),
        theme,
        title=title,
        subtitle=subtitle,
        border_style=theme.success,
    )


def _diff_for_edit_arguments(arguments: Mapping[str, Any], *, path: str) -> str:
    edits = _edit_pairs_from_arguments(arguments)
    if not edits:
        return ""

    display_path = _display_path(
        path or _as_str(arguments.get("path") or arguments.get("file_path"))
    )
    chunks: list[str] = []
    for index, (old_text, new_text) in enumerate(edits, start=1):
        old_lines = _diff_lines(old_text)
        new_lines = _diff_lines(new_text)
        fromfile = display_path or "before"
        tofile = display_path or "after"
        if len(edits) > 1:
            fromfile = f"{fromfile} (edit {index})"
            tofile = f"{tofile} (edit {index})"
        chunk = "".join(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=fromfile,
                tofile=tofile,
            )
        )
        if chunk:
            chunks.append(chunk)
    return "\n".join(chunks)


def _diff_lines(text: str) -> list[str]:
    if text == "":
        return []
    lines = text.splitlines(keepends=True)
    if not lines:
        return [text]
    if lines[-1].endswith(("\n", "\r")):
        return lines
    # difflib.unified_diff emits malformed-looking output when the final
    # line has no terminator and lineterm="". Add one for display only; the
    # edit arguments remain unchanged.
    return [*lines[:-1], f"{lines[-1]}\n"]


def _edit_pairs_from_arguments(arguments: Mapping[str, Any]) -> list[tuple[str, str]]:
    edits_arg = arguments.get("edits")
    if isinstance(edits_arg, list):
        pairs: list[tuple[str, str]] = []
        for item in edits_arg:
            if not isinstance(item, Mapping):
                continue
            old_text = item.get("oldText")
            new_text = item.get("newText")
            if isinstance(old_text, str) and isinstance(new_text, str):
                pairs.append((old_text, new_text))
        return pairs

    old_text = arguments.get("oldText")
    new_text = arguments.get("newText")
    if isinstance(old_text, str) and isinstance(new_text, str):
        return [(old_text, new_text)]
    return []


def _section(
    label: str, text: str, theme: TerminalToolRenderTheme, *, style: str
) -> RenderableType:
    truncated = _truncate_text(text.rstrip("\n"), theme)
    header = Text(label, style=theme.muted)
    body = Text(truncated.visible, style=style or None)
    parts: list[RenderableType] = [header, body]
    if truncated.note:
        parts.append(Text(truncated.note, style=theme.muted))
    return Group(*parts)


def _status_panel(
    title: str,
    message: str,
    theme: TerminalToolRenderTheme,
    *,
    border_style: str,
) -> Padding:
    return _panel(
        Text(message or title, style="dim"),
        theme,
        title=title,
        border_style=border_style,
    )


def _render_error_if_present(
    obj: Mapping[str, Any], theme: TerminalToolRenderTheme
) -> RenderableType | None:
    error = _as_str(obj.get("error"))
    if not error:
        return None
    return _status_panel("tool error", error, theme, border_style=theme.error)


def _human_tool_name(name: str) -> str:
    normalized = _normalize_tool_name(name)
    return normalized.removeprefix("sys_").replace("_", " ")


def _status_title_message(
    ex: ToolExecution,
    fields: StatusToolFields,
    *,
    path: str,
    terminal: str,
    session: str,
) -> tuple[str, str]:
    name = _normalize_tool_name(ex.name)
    if name in {"sys_os_write", "write"}:
        action = "created" if fields.created else "wrote"
        return f"{action} {_display_path(path)}".strip(), _bytes_message(fields.bytes_written)
    if name in {"sys_os_edit", "edit"}:
        plural = "s" if fields.replacements != 1 else ""
        bytes_written = _bytes_message(fields.bytes_written)
        message = f"{fields.replacements} replacement{plural} · {bytes_written}"
        return f"edited {_display_path(path)}".strip(), message
    if "terminal_launch" in name:
        notify = " · idle notifications on" if fields.notify_when_idle else ""
        return f"terminal {terminal}:{session}", f"{fields.status or 'launched'}{notify}"
    if "terminal_send" in name:
        return "sent to terminal", "status: sent"
    if "terminal_close" in name:
        return f"terminal {terminal}:{session}", fields.status or "closed"
    if "timer_set" in name:
        repeat = " · repeating" if fields.repeat else ""
        return "timer scheduled", f"{fields.seconds}s{repeat}" + (
            f" · {fields.note}" if fields.note else ""
        )
    if "timer_cancel" in name:
        return "timer", f"{fields.status or 'cancelled'} · {fields.timer_id}"
    if name in {"sys_call_async"}:
        message = f"{fields.tool_name} · {fields.task_id}"
        return "async task started", message
    if "cancel" in name:
        message = f"{fields.task_id} · cancelled={fields.cancelled}"
        return "task cancellation", message
    if name == "sys_files_upload":
        message = " · ".join(
            bit for bit in (fields.filename, fields.content_type, fields.file_id) if bit
        )
        return "uploaded file", message
    if name == "sys_files_download":
        message = " · ".join(
            bit
            for bit in (
                fields.filename,
                _bytes_message(fields.download_bytes),
                _display_path(path),
            )
            if bit
        )
        return "downloaded file", message
    return _human_tool_name(ex.name), json.dumps(fields.model_dump(), ensure_ascii=False)


def _bytes_message(value: object) -> str:
    size = _as_int(value, default=0)
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} bytes"
