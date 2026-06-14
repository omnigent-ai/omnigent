"""
Client-side ``coding`` tool set.

Eight coding tools — Read, Write, Edit, Glob, Grep, Bash, LSP,
get_current_time — defined as ``@tool``-decorated Python
functions and surfaced through the ``omnigent_client``
SDK's ``build_tool_handler``. The legacy ``TOOLS`` list and
``execute_tool`` dispatcher are derived from the same
functions so consumers that hand-construct schemas
(``examples/frontends/terminal.py``, ``omnigent chat``'s raw-schema
path) keep working without modification.

Used by ``omnigent chat --tools coding`` and the terminal TUI.
"""

from __future__ import annotations

import glob as glob_mod
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from omnigent_client.tools import build_tool_handler, tool

# Maximum characters returned from any tool execution.
# Prevents TUI freezes when tools produce huge output
# (e.g. globbing a large repo, running verbose commands).
_MAX_OUTPUT_CHARS = 20_000

# Maximum number of file paths returned by Glob.
_MAX_GLOB_RESULTS = 200

# Default Bash timeout in milliseconds (2 minutes).
_DEFAULT_BASH_TIMEOUT_MS = 120_000


def _truncate(output: str) -> str:
    """
    Truncate tool output to ``_MAX_OUTPUT_CHARS``.

    Appends a notice when truncation occurs so the LLM knows
    the output was cut short.

    :param output: Raw tool output string.
    :returns: The output, possibly truncated with a notice.
    """
    if len(output) <= _MAX_OUTPUT_CHARS:
        return output
    return (
        output[:_MAX_OUTPUT_CHARS] + f"\n\n... (truncated — {len(output)} chars total, "
        f"showing first {_MAX_OUTPUT_CHARS})"
    )


# ── @tool functions ──────────────────────────────────────


@tool(strict=False)
def Read(
    file_path: str,
    offset: int | None = None,
    limit: int | None = None,
) -> str:
    """Read the contents of a file. Returns the file text with line numbers.

    Supports text files. Output is truncated at 20,000 chars to
    keep the LLM context manageable.

    Args:
        file_path: Absolute path to the file to read,
            e.g. ``/home/user/project/main.py``.
        offset: Line number to start reading from (1-based).
            Only needed for large files.
        limit: Maximum number of lines to read. Only needed
            for large files.
    """
    try:
        text = Path(file_path).read_text()
    except (OSError, UnicodeDecodeError) as exc:
        return _truncate(f"Error reading {file_path}: {exc}")
    lines = text.splitlines()
    start_line = offset if offset is not None else 1
    line_count = limit if limit is not None else len(lines)
    # Convert to 0-based index for slicing.
    start = max(0, start_line - 1)
    selected = lines[start : start + line_count]
    numbered = [f"{start + i + 1}\t{line}" for i, line in enumerate(selected)]
    return _truncate("\n".join(numbered))


@tool(strict=False)
def Write(file_path: str, content: str) -> str:
    """Create a new file or overwrite an existing file.

    Prefer ``Edit`` for modifying existing files. Creates parent
    directories as needed.

    Args:
        file_path: Absolute path to the file to write.
        content: The full content to write to the file.
    """
    target = Path(file_path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    except OSError as exc:
        return _truncate(f"Error writing {target}: {exc}")
    return _truncate(f"Successfully wrote {target}")


@tool(strict=False)
def Edit(
    file_path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """Make targeted string replacements in an existing file.

    The ``old_string`` must appear exactly once in the file
    unless ``replace_all`` is true.

    Args:
        file_path: Absolute path to the file to edit.
        old_string: The exact text to find and replace.
        new_string: The replacement text.
        replace_all: If true, replace all occurrences of
            ``old_string``. Defaults to false.
    """
    target = Path(file_path)
    try:
        text = target.read_text()
    except OSError as exc:
        return _truncate(f"Error reading {target}: {exc}")
    count = text.count(old_string)
    if count == 0:
        return _truncate(f"Error: old_string not found in {target}")
    if not replace_all and count > 1:
        return _truncate(
            f"Error: old_string appears {count} times (expected 1). "
            f"Use replace_all=true or provide more context."
        )
    result = (
        text.replace(old_string, new_string)
        if replace_all
        else text.replace(old_string, new_string, 1)
    )
    try:
        target.write_text(result)
    except OSError as exc:
        return _truncate(f"Error writing {target}: {exc}")
    replacements = count if replace_all else 1
    return _truncate(f"Replaced {replacements} occurrence(s) in {target}")


@tool(strict=False)
def Glob(pattern: str, path: str | None = None) -> str:
    """Find files matching a glob pattern.

    Returns matching file paths, capped at 200 results to avoid
    freezing on large directories.

    Args:
        pattern: Glob pattern to match,
            e.g. ``**/*.py`` or ``src/**/*.ts``.
        path: Directory to search in. Defaults to the current
            working directory.
    """
    base = path if path is not None else "."
    matches = sorted(glob_mod.glob(os.path.join(base, pattern), recursive=True))
    if not matches:
        return "No files matched."
    total = len(matches)
    if total > _MAX_GLOB_RESULTS:
        truncated = matches[:_MAX_GLOB_RESULTS]
        return _truncate(
            "\n".join(truncated) + f"\n\n... ({total} total matches, "
            f"showing first {_MAX_GLOB_RESULTS})"
        )
    return _truncate("\n".join(matches))


@tool(strict=False)
def Grep(
    pattern: str,
    path: str | None = None,
    glob: str | None = None,
    output_mode: str | None = None,
) -> str:
    """Search file contents using regex. Built on ripgrep.

    Falls back to ``grep -r`` if ripgrep is not installed.

    Args:
        pattern: Regex pattern to search for,
            e.g. ``def main`` or ``import\\s+asyncio``.
        path: File or directory to search in. Defaults to the
            current working directory.
        glob: Glob pattern to filter files,
            e.g. ``*.py`` or ``*.{ts,tsx}``.
        output_mode: One of ``content`` (matching lines),
            ``files_with_matches`` (file paths, default), or
            ``count`` (match counts).
    """
    search_path = path if path is not None else "."
    cmd = ["rg", "-e", pattern, search_path, "--no-heading"]
    if glob is not None:
        cmd.extend(["--glob", glob])
    mode = output_mode if output_mode is not None else "files_with_matches"
    if mode == "files_with_matches":
        cmd.append("--files-with-matches")
    elif mode == "count":
        cmd.append("--count")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return _truncate("Search timed out after 30s")
    except FileNotFoundError:
        # ripgrep not installed — fall back to grep.
        grep_cmd = ["grep", "-r", "-e", pattern, search_path]
        try:
            result = subprocess.run(
                grep_cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except FileNotFoundError:
            return _truncate("Search failed: neither ripgrep nor grep is available.")
        except subprocess.TimeoutExpired:
            return _truncate("Search timed out after 30s")
    if result.returncode > 1:
        return _truncate(f"Search failed (exit {result.returncode}): {result.stderr.strip()}")
    return _truncate(result.stdout.strip() or "No matches found.")


@tool(strict=False)
def Bash(command: str, timeout: int | None = None) -> str:
    """Execute a shell command and return its output.

    Use for running tests, git operations, builds, etc.

    Args:
        command: The shell command to execute,
            e.g. ``pytest tests/ -x`` or ``git status``.
        timeout: Timeout in milliseconds. Defaults to 120000
            (2 minutes).
    """
    timeout_ms = timeout if timeout is not None else _DEFAULT_BASH_TIMEOUT_MS
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_ms / 1000,
        )
        output = result.stdout
        if result.stderr:
            output += "\nSTDERR:\n" + result.stderr
        return _truncate(output.strip() or "(no output)")
    except subprocess.TimeoutExpired:
        return _truncate(f"Command timed out after {timeout_ms}ms")


@tool(strict=False)
def LSP(
    action: str,
    file_path: str,
    line: int | None = None,
    character: int | None = None,
) -> str:
    """Code intelligence via language servers.

    Stub — requires a running language server which this client
    doesn't manage. Returns a not-implemented notice for now.

    Args:
        action: One of ``definition``, ``references``,
            ``hover``, ``symbols``, ``implementations``,
            ``diagnostics``.
        file_path: Absolute path to the file.
        line: 1-based line number of the symbol. Required for
            ``definition`` / ``references`` / ``hover`` /
            ``implementations``.
        character: 0-based character offset within the line.
            Required for ``definition`` / ``references`` /
            ``hover`` / ``implementations``.
    """
    _ = (line, character)  # accepted but unused in this stub
    return _truncate(f"LSP not implemented in this client. Action: {action}, file: {file_path}")


@tool(strict=False)
def get_current_time() -> str:
    """Get the current date and time.

    Returns an ISO-formatted timestamp.
    """
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ── Legacy adapter surface ───────────────────────────────
#
# ``examples/frontends/terminal.py`` and the legacy
# ``omnigent chat`` ``_load_tool_handler`` path reach into modules
# in this package for ``TOOLS`` (a list of OpenAI-format
# schema dicts) and ``execute_tool(name, args) -> str``
# (sync dispatcher). Both are derived from the
# ``@tool``-decorated functions above so there's exactly
# one source of truth.

_TOOL_FNS: list[Callable[..., str]] = [
    Read,
    Write,
    Edit,
    Glob,
    Grep,
    Bash,
    LSP,
    get_current_time,
]

_FN_BY_NAME: dict[str, Callable[..., str]] = {fn.__name__: fn for fn in _TOOL_FNS}

# ``build_tool_handler`` reads the metadata attached to each
# ``@tool``-decorated function and emits OpenAI function-call
# schemas. Build once at import time so the resulting
# ``TOOLS`` list is the same shape consumers used to get from
# the hand-written dict.
TOOLS: list[dict[str, object]] = build_tool_handler(_TOOL_FNS).schemas


def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    """Execute a coding tool by name (legacy sync dispatcher).

    Used by ``examples/frontends/terminal.py`` and ``omnigent chat``'s
    raw-schema path. New consumers should construct a
    :class:`~omnigent_client.tools.ToolHandler` via
    :func:`~omnigent_client.tools.build_tool_handler` against
    the ``@tool`` functions directly.

    :param name: Tool function name, e.g. ``"Read"`` or ``"Bash"``.
    :param arguments: Parsed arguments dict from the LLM's
        function call.
    :returns: The tool's output as a string. Truncation at
        ``_MAX_OUTPUT_CHARS`` is applied inside each tool body.
    """
    fn = _FN_BY_NAME.get(name)
    if fn is None:
        return f"Unknown tool: {name}"
    return str(fn(**arguments))
