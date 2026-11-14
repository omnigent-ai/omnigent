"""Export a session as a Jupyter notebook.

Messages become Markdown cells and what the agent ran becomes code cells: a
shell command as a ``%%bash`` cell, Python run through the shell as a Python
cell, and a query to an SQL tool as a ``%%sql`` cell. Tool calls that aren't
code, and content the export can't show, become short Markdown notes rather
than being dropped. The notebook is nbformat 4.5 JSON, written directly.
"""

from __future__ import annotations

import base64
import json
import re
import shlex
import urllib.parse
from collections import deque
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

from omnigent.runtime.mcp_tool_result import decode_mcp_image_result

#: Largest tool output kept in a cell, in characters; longer output is cut with a note.
MAX_OUTPUT_CHARS = 100_000
#: Largest image kept as a cell output, in base64 characters.
MAX_IMAGE_CHARS = 2_000_000

# ``bash -lc '…'``, as Codex wraps each command: the shell and its literal script.
_SHELL_WRAPPER = re.compile(
    r"\A\s*(?:(?:/[\w.+-]+)*/)?(?P<shell>bash|sh|zsh)\s+-(?:c|lc|ic)\s+'(?P<script>[^']*)'\s*\Z"
)
_SHELL_MAGICS = {"bash": "%%bash", "sh": "%%sh", "zsh": "%%script zsh"}
_SHELL_SCRIPT_FLAGS = {"-c", "-lc", "-ic"}
# Codex's and other harnesses' shell tools beyond each harness's declared one.
_EXTRA_SHELL_TOOLS = frozenset({"exec_command", "run_command"})
# ``python3 - <<'EOF'`` … ``EOF``: Python fed on stdin. Only a quoted delimiter
# (no shell expansion) with nothing else on its line runs the body as written,
# and the shell ends the heredoc only at a line that is exactly the delimiter.
_PYTHON_HEREDOC = re.compile(
    r"\A\s*(?:python3?)\s+-\s*<<\s*(['\"])(\w+)\1[ \t]*\n(?P<body>.*?)\n\2\n*\Z",
    re.DOTALL,
)
# ``python3 -c '…'``: only a quoted program the shell passes through unchanged
# (single quotes, or double quotes with nothing to expand) and nothing after it.
_PYTHON_C = re.compile(
    r"""\A\s*(?:(?:/[\w.+-]+)*/)?python3?\s+-c\s+(?:'(?P<single>[^']*)'|"(?P<double>[^"$`\\]*)")\s*\Z"""
)
# Argument names shell tools use for the directory a command runs in.
_WORKING_DIRECTORY_KEYS = ("workdir", "cwd", "directory")
_SQL_ARGUMENT_KEYS = ("sql", "query", "statement")
# Tools whose result is the built-in shell envelope, and that envelope's fields.
_SHELL_RESULT_TOOLS = frozenset({"sys_os_shell"})
_SHELL_RESULT_KEYS = frozenset(
    {"stdout", "stderr", "exit_code", "timed_out", "error", "shell", "cwd"}
)
_INLINE_IMAGE_TYPES = ("png", "jpeg", "gif", "webp")
# URL characters kept as is in the header link; ``<>()[]`` and spaces are encoded.
_URL_SAFE = ":/?#@!$&'*+,;=%~-._"


def _shell_tool_names() -> frozenset[str]:
    """Every harness's shell tool, e.g. ``sys_os_shell``, ``Bash`` or ``run_shell_command``."""
    from omnigent.harness_plugins import harness_capabilities
    from omnigent.policies.builtins._shell import SHELL_TOOLS

    plugin_names = {
        capabilities.shell_tool_name
        for capabilities in harness_capabilities().values()
        if capabilities.shell_tool_name
    }
    return SHELL_TOOLS | plugin_names | _EXTRA_SHELL_TOOLS


def _shell_command(arguments: Mapping[str, Any]) -> tuple[str, str] | None:
    """
    The cell magic and script for what a shell tool ran: ``command`` or ``cmd``,
    a string or an argv list, e.g. ``("%%bash", "ls")``.
    """
    command = arguments.get("command", arguments.get("cmd"))
    if isinstance(command, list) and command and all(isinstance(part, str) for part in command):
        # ``["bash", "-lc", script]`` hands the shell its script verbatim.
        shell = command[0].rsplit("/", 1)[-1]
        if len(command) == 3 and shell in _SHELL_MAGICS and command[1] in _SHELL_SCRIPT_FLAGS:
            return (_SHELL_MAGICS[shell], command[2]) if command[2].strip() else None
        command = shlex.join(command)
    if not isinstance(command, str) or not command.strip():
        return None
    return _unwrap_shell(command)


def _unwrap_shell(command: str) -> tuple[str, str]:
    """
    ``/bin/zsh -lc 'pwd'`` → ``("%%script zsh", "pwd")``: a login-shell wrapper's
    script and the cell magic that runs it in the same shell.

    Only a single-quoted script with nothing after it is unwrapped, since its
    text reaches the shell unchanged; anything else is kept whole.
    """
    wrapped = _SHELL_WRAPPER.match(command)
    if wrapped is None:
        return "%%bash", command
    return _SHELL_MAGICS[wrapped.group("shell")], wrapped.group("script")


def _python_source(command: str) -> str | None:
    """The Python a shell command runs inline, or ``None`` when it runs something else.

    Covers ``python3 - <<'EOF'`` heredocs and ``python3 -c '…'``.
    """
    heredoc = _PYTHON_HEREDOC.match(command)
    if heredoc:
        body = heredoc.group("body")
        # A delimiter line inside the body ends the heredoc early: more shell follows.
        if any(line == heredoc.group(2) for line in body.splitlines()):
            return None
        return body
    inline = _PYTHON_C.match(command)
    if inline:
        return (
            inline.group("single")
            if inline.group("single") is not None
            else inline.group("double")
        )
    return None


def _sql_query(name: str, arguments: Mapping[str, Any]) -> str | None:
    """The query an SQL tool ran, e.g. ``execute_sql(query=…)``, or ``None``."""
    if "sql" not in name.lower():
        return None
    for key in _SQL_ARGUMENT_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


# Where ``$`` is literal: fenced and inline code, link destinations, autolinks
# and bare URLs (ASCII only, so a URL ends where unspaced text resumes).
_MARKDOWN_CODE = re.compile(
    r"```.*?```|~~~.*?~~~|`[^`\n]*`"
    r"|\]\([^()\s\]]*\)|<https?://[^<>\s]*>|https?://[A-Za-z0-9\-._~:/?#@!$&'*+,;=%]+",
    re.DOTALL,
)
_LIST_ITEM = re.compile(r" {0,3}(?:[-*+]|\d+[.)])(?:\s|\Z)")
_HEADING = re.compile(r" {0,3}#{1,6}(?:\s|\Z)")
_THEMATIC_BREAK = re.compile(r" {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*\Z")
# The ``===`` or ``---`` line under a paragraph that makes it a heading.
_SETEXT_UNDERLINE = re.compile(r" {0,3}(?:=+|-+)[ \t]*\Z")
_DOLLAR = re.compile(r"\\?\$")
# JupyterLab typesets ``\$`` and ``&#36;`` as math too; an entity in its own
# element is the form both it and nbconvert show as a plain dollar sign.
_LITERAL_DOLLAR = "<span>&#36;</span>"


def _indented_code_spans(text: str, fences: list[tuple[int, int]]) -> Iterator[tuple[int, int]]:
    """
    Spans of *text*'s indented code blocks, as CommonMark reads them: indented
    lines that start a block (after a blank line, a heading, a rule or a fence)
    outside a list. An indented line in a list or a paragraph is not code.

    :param fences: Spans of *text*'s fenced code blocks, whose lines are skipped.
    """
    offset = 0
    in_code = in_list = previous_blank = False
    can_start = True
    # Fences are in text order, so one cursor walks them alongside the lines.
    fence = 0
    for raw in text.split("\n"):
        end = offset + len(raw)
        line = raw.removesuffix("\r")
        while fence < len(fences) and fences[fence][1] <= offset:
            fence += 1
        if fence < len(fences) and fences[fence][0] <= offset:
            # The block after a fence may be indented code.
            in_code = in_list = False
            can_start = True
        elif not line.strip():
            previous_blank = can_start = True
        elif line.startswith(("    ", "\t")):
            in_code = in_code or (can_start and not in_list)
            if in_code:
                yield offset, end
            previous_blank = can_start = False
        else:
            in_code = False
            boundary = bool(
                _HEADING.match(line)
                or _THEMATIC_BREAK.match(line)
                or (not previous_blank and not in_list and _SETEXT_UNDERLINE.match(line))
            )
            in_list = not boundary and (
                bool(_LIST_ITEM.match(line)) or (in_list and not previous_blank)
            )
            can_start = boundary
            previous_blank = False
        offset = end + 1


def _markdown_text(text: str) -> str:
    """*text* for a Markdown cell, ``$`` outside code kept from rendering as LaTeX."""
    literal = bytearray(len(text))
    spans = [match.span() for match in _MARKDOWN_CODE.finditer(text)]
    fences = [(start, end) for start, end in spans if text.startswith(("```", "~~~"), start)]
    for start, end in [*spans, *_indented_code_spans(text, fences)]:
        literal[start:end] = b"\x01" * (end - start)
    return _DOLLAR.sub(
        lambda match: match.group() if literal[match.start()] else _LITERAL_DOLLAR, text
    )


def _message_text(content: Iterable[Mapping[str, Any]]) -> str:
    """A message's content parts as Markdown: text as is, images inline or noted."""
    parts: list[str] = []
    for part in content:
        part_type = part.get("type")
        if part_type in {"input_text", "output_text", "text"}:
            parts.append(_markdown_text(str(part.get("text", ""))))
        elif part_type == "refusal":
            parts.append(f"*Refused: {_markdown_text(str(part.get('refusal', '')))}*")
        elif part_type in {"input_image", "output_image"}:
            url = part.get("image_url")
            if (
                isinstance(url, str)
                and url.startswith(tuple(f"data:image/{kind};" for kind in _INLINE_IMAGE_TYPES))
                and len(url) <= MAX_IMAGE_CHARS
            ):
                parts.append(f"![image]({url})")
            elif isinstance(url, str) and url.startswith("data:"):
                parts.append("*(An image was attached here; the export leaves it out.)*")
            else:
                parts.append("*(An image was attached here; it isn't stored in the session.)*")
        elif part_type in {"input_file", "output_file"}:
            label = part.get("filename") or part.get("file_id") or "a file"
            parts.append(f"*(Attached file: {label})*")
    return "\n\n".join(part for part in parts if part.strip())


def _cut(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n… ({len(text) - MAX_OUTPUT_CHARS:,} more characters)\n"


def _stream(name: str, text: str) -> dict[str, Any]:
    return {"output_type": "stream", "name": name, "text": _cut(text)}


def _shell_result(parsed: object) -> dict[str, Any] | None:
    """The built-in shell tool's result, or ``None`` for anything shaped otherwise."""
    if not isinstance(parsed, dict) or not parsed.keys() <= _SHELL_RESULT_KEYS:
        return None
    streams = {"stdout", "stderr"} <= parsed.keys() and all(
        isinstance(parsed[name], str) for name in ("stdout", "stderr")
    )
    # A failure to run at all carries only the error, or only the timeout.
    failed = not ({"stdout", "stderr"} & parsed.keys()) and bool(
        parsed.get("error") or parsed.get("timed_out")
    )
    return parsed if streams or failed else None


def _tool_outputs(output: str, tool: str) -> list[dict[str, Any]]:
    """A tool result as notebook outputs: stdout and stderr, text, or images."""
    try:
        parsed = json.loads(output)
    except (TypeError, ValueError):
        parsed = None
    # Another tool's output that happens to look like the envelope stays text.
    shell = _shell_result(parsed) if tool in _SHELL_RESULT_TOOLS else None
    if shell is not None:
        outputs = []
        if shell.get("stdout"):
            outputs.append(_stream("stdout", shell["stdout"]))
        if shell.get("stderr"):
            outputs.append(_stream("stderr", shell["stderr"]))
        exit_code = shell.get("exit_code")
        error = shell.get("error")
        if shell.get("timed_out"):
            outputs.append(_stream("stderr", f"[{error or 'Command timed out'}]\n"))
        elif isinstance(exit_code, int) and exit_code != 0:
            outputs.append(_stream("stderr", f"[exit code: {exit_code}]\n"))
        elif exit_code is None and error:
            outputs.append(_stream("stderr", f"[{error}]\n"))
        return outputs
    # Only the exact image envelope; text that merely looks like one stays text.
    image_result = decode_mcp_image_result(parsed)
    if image_result is not None:
        outputs = []
        for block in image_result.content:
            if block.get("type") == "text" and block.get("text"):
                outputs.append(_stream("stdout", str(block["text"])))
            elif block.get("type") == "image":
                outputs.append(_image_output(str(block.get("mimeType", "")), block.get("data")))
        return outputs
    return [_stream("stdout", output)] if output else []


def _image_output(mime_type: str, data: object) -> dict[str, Any]:
    if mime_type in {"image/png", "image/jpeg"} and isinstance(data, str):
        if len(data) <= MAX_IMAGE_CHARS:
            try:
                base64.b64decode(data, validate=True)
            except ValueError:
                pass
            else:
                return {"output_type": "display_data", "data": {mime_type: data}, "metadata": {}}
    return _stream("stdout", f"[{mime_type or 'an'} image the export doesn't include]\n")


def _arguments(raw: object) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class _NotebookBuilder:
    """Collects cells in session order, pairing each code cell with its output."""

    def __init__(self, include_outputs: bool) -> None:
        self.include_outputs = include_outputs
        self.cells: list[dict[str, Any]] = []
        self._code_cells_by_call: dict[str, dict[str, Any]] = {}
        # Each call's tool, by call id, so its output is read in that tool's shape.
        self._calls_seen: dict[str, str] = {}
        self._pending_terminal: deque[dict[str, Any]] = deque()
        self._shell_tools = _shell_tool_names()
        self.tools_used: dict[str, int] = {}
        self.magics_used: set[str] = set()

    def markdown(self, source: str) -> None:
        self.cells.append(
            {
                "cell_type": "markdown",
                "id": f"cell-{len(self.cells)}",
                "metadata": {},
                "source": source,
            }
        )

    def code(self, source: str, call_id: str | None = None) -> dict[str, Any]:
        cell: dict[str, Any] = {
            "cell_type": "code",
            "id": f"cell-{len(self.cells)}",
            "metadata": {},
            "execution_count": None,
            "source": source,
            "outputs": [],
        }
        self.cells.append(cell)
        if call_id:
            self._code_cells_by_call[call_id] = cell
        return cell

    def function_call(self, item: Mapping[str, Any]) -> None:
        name = str(item.get("name", ""))
        self.tools_used[name] = self.tools_used.get(name, 0) + 1
        arguments = _arguments(item.get("arguments"))
        call_id = item.get("call_id")
        if call_id:
            self._calls_seen[str(call_id)] = name
        shell = _shell_command(arguments) if name in self._shell_tools else None
        if shell is not None:
            magic, command = shell
            directory = next(
                (
                    value
                    for key in _WORKING_DIRECTORY_KEYS
                    if isinstance(value := arguments.get(key), str) and value.strip()
                ),
                None,
            )
            python = _python_source(command) if directory is None else None
            if python is not None:
                self.code(python, call_id)
            elif directory is not None:
                # The cell's own shell process keeps the ``cd`` in this cell;
                # without the directory the command doesn't run at all.
                self.code(f"{magic}\ncd {shlex.quote(directory)} || exit 1\n{command}", call_id)
                self.magics_used.add(magic)
            else:
                self.code(f"{magic}\n{command}", call_id)
                self.magics_used.add(magic)
            return
        query = _sql_query(name, arguments)
        if query is not None:
            self.code(f"%%sql\n{query}", call_id)
            self.magics_used.add("%%sql")
            return
        summary = ", ".join(f"`{key}`" for key in arguments) or "no arguments"
        self.markdown(f"*Tool call: `{name}` ({summary}).*")

    def function_call_output(self, item: Mapping[str, Any]) -> None:
        call_id = str(item.get("call_id", ""))
        if call_id not in self._calls_seen:
            self.markdown("*(Output of a tool call that isn't in this export.)*")
            return
        cell = self._code_cells_by_call.get(call_id)
        if cell is not None and self.include_outputs:
            tool = self._calls_seen[call_id]
            cell["outputs"].extend(_tool_outputs(str(item.get("output", "")), tool))

    def terminal_command(self, item: Mapping[str, Any]) -> None:
        if item.get("kind") == "input":
            if item.get("input"):
                self._pending_terminal.append(self.code(f"%%bash\n{item['input']}"))
            return
        if not self._pending_terminal:
            self.markdown("*(Terminal output of a command that isn't in this export.)*")
            return
        cell = self._pending_terminal.popleft()
        if not self.include_outputs:
            return
        if item.get("stdout"):
            cell["outputs"].append(_stream("stdout", str(item["stdout"])))
        if item.get("stderr"):
            cell["outputs"].append(_stream("stderr", str(item["stderr"])))


def session_notebook(
    session: Mapping[str, Any],
    items: Iterable[Mapping[str, Any]],
    *,
    include_outputs: bool = True,
    session_url: str | None = None,
    exported_at: datetime | None = None,
    more_items: bool = False,
) -> dict[str, Any]:
    """
    Convert a session's items into a Jupyter notebook (nbformat 4.5).

    :param session: The session, as ``GET /v1/sessions/{id}`` returns it, e.g.
        ``{"id": "conv_abc", "title": "Q3 revenue", "agent_name": "analyst",
        "llm_model": "databricks-claude-opus-4-8"}``.
    :param items: Its items in order, as ``GET /v1/sessions/{id}/items`` returns them.
    :param include_outputs: Whether code cells keep the outputs the agent saw.
    :param session_url: A link back to the session, shown in the header cell.
    :param exported_at: When the export ran; now when omitted.
    :param more_items: Whether the session has items past *items*, which the
        header then says are left out.
    :returns: The notebook as a JSON-serializable dict.
    """
    builder = _NotebookBuilder(include_outputs)
    title = str(session.get("title") or session.get("id") or "Omnigent session")
    header = [f"# {_markdown_text(title)}", ""]
    details = [
        ("Session", f"`{session.get('id')}`" if session.get("id") else None),
        ("Agent", session.get("agent_name")),
        ("Model", session.get("llm_model")),
        ("Exported", (exported_at or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC")),
        # Encoded so the link can't close the Markdown around it.
        ("Link", urllib.parse.quote(session_url, safe=_URL_SAFE) if session_url else None),
    ]
    header.extend(f"- **{label}:** {value}" for label, value in details if value)
    if more_items:
        header.append("- **Note:** the session continues past this notebook's last cell.")
    builder.markdown("\n".join(header))

    for item in items:
        item_type = item.get("type")
        if item_type == "message":
            if item.get("is_meta"):
                continue
            text = _message_text(item.get("content") or [])
            if text:
                speaker = "User" if item.get("role") == "user" else "Assistant"
                builder.markdown(f"**{speaker}**\n\n{text}")
        elif item_type == "function_call":
            builder.function_call(item)
        elif item_type == "function_call_output":
            builder.function_call_output(item)
        elif item_type == "terminal_command":
            builder.terminal_command(item)
        elif item_type == "reasoning":
            summary = "\n\n".join(
                str(block.get("text", ""))
                for block in item.get("summary") or []
                if isinstance(block, Mapping) and str(block.get("text", "")).strip()
            )
            if summary:
                builder.markdown(f"*Reasoning:*\n\n{_markdown_text(summary)}")
            elif item.get("encrypted_content"):
                builder.markdown("*(The model's encrypted reasoning isn't included.)*")
            elif item.get("content"):
                builder.markdown("*(The model's full reasoning isn't included.)*")
        elif item_type == "routing_decision":
            verb = "picked" if item.get("applied", True) else "would have picked"
            builder.markdown(f"*The model router {verb} `{item.get('model', '')}`.*")
        elif item_type == "error":
            builder.markdown(f"*Error: {_markdown_text(str(item.get('message', '')))}*")
        elif item_type == "slash_command":
            arguments = f" {item['arguments']}" if item.get("arguments") else ""
            builder.markdown(f"*Command: `/{item.get('name', '')}{arguments}`*")
        elif item_type == "compaction":
            summary = str(item.get("summary") or "").strip()
            note = "*(Earlier conversation was compacted here.)*"
            builder.markdown(f"{note}\n\n{_markdown_text(summary)}" if summary else note)
        elif item_type == "native_tool":
            native_type = (item.get("item") or {}).get("type", "a built-in tool")
            builder.markdown(f"*Built-in tool used: `{native_type}`.*")
        # ``resource_event`` items are lifecycle metadata, not conversation content.

    if builder.tools_used:
        tools = "\n".join(
            f"- `{name}` ({count} call{'s' if count != 1 else ''})"
            for name, count in sorted(builder.tools_used.items())
        )
        needs = [
            note
            for magic, note in (
                ("%%script zsh", "zsh for the `%%script zsh` cells"),
                (
                    "%%sql",
                    "the JupySQL extension (`%load_ext sql`) connected to the same database "
                    "for the `%%sql` cells",
                ),
            )
            if magic in builder.magics_used
        ]
        also = f" Some cells also need {' and '.join(needs)}." if needs else ""
        builder.markdown(
            "## Reproducing this notebook\n\n"
            "The code cells repeat what the agent ran. Running them again may need the "
            f"same workspace files, credentials and data sources.{also} The session used "
            f"these tools:\n\n{tools}"
        )

    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"},
            "language_info": {"name": "python"},
            "omnigent": {"session_id": session.get("id"), "title": title},
        },
        "cells": builder.cells,
    }
