"""Exporting a session as a Jupyter notebook (nbformat 4.5)."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from omnigent.session_notebook import MAX_OUTPUT_CHARS, session_notebook

# The fields ``GET /v1/sessions/{id}`` returns that the header shows.
_SESSION = {
    "id": "conv_abc",
    "title": "Q3 revenue by region",
    "agent_name": "analyst",
    "llm_model": "claude-opus-4-7",
}
_EXPORTED_AT = datetime(2026, 11, 13, 9, 30, tzinfo=UTC)
_PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


def _call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """A ``function_call`` item as ``GET /v1/sessions/{id}/items`` returns it."""
    return {
        "type": "function_call",
        "model": "claude-opus-4-7",
        "name": name,
        "arguments": json.dumps(arguments),
        "call_id": call_id,
    }


def _result(call_id: str, output: str) -> dict[str, Any]:
    return {"type": "function_call_output", "call_id": call_id, "output": output}


def _message(role: str, text: str, **extra: Any) -> dict[str, Any]:
    part = "input_text" if role == "user" else "output_text"
    return {"type": "message", "role": role, "content": [{"type": part, "text": text}], **extra}


def _notebook(items: list[dict[str, Any]], **options: Any) -> dict[str, Any]:
    return session_notebook(_SESSION, items, exported_at=_EXPORTED_AT, **options)


def _cells(notebook: dict[str, Any]) -> list[tuple[str, str]]:
    """Each cell as ``(type, source)``, leaving out the header and any closing cell."""
    cells = [(cell["cell_type"], cell["source"]) for cell in notebook["cells"][1:]]
    if cells and cells[-1][1].startswith("## Reproducing this notebook"):
        cells.pop()
    return cells


def test_header_names_the_session_and_the_notebook_is_nbformat_45() -> None:
    notebook = _notebook([], session_url="https://omnigent.example/c/conv_abc")

    assert (notebook["nbformat"], notebook["nbformat_minor"]) == (4, 5)
    assert notebook["metadata"]["kernelspec"]["language"] == "python"
    assert notebook["metadata"]["omnigent"] == {
        "session_id": "conv_abc",
        "title": "Q3 revenue by region",
    }
    [header] = notebook["cells"]
    assert header["cell_type"] == "markdown"
    assert header["source"].splitlines() == [
        "# Q3 revenue by region",
        "",
        "- **Session:** `conv_abc`",
        "- **Agent:** analyst",
        "- **Model:** claude-opus-4-7",
        "- **Exported:** 2026-11-13 09:30 UTC",
        "- **Link:** https://omnigent.example/c/conv_abc",
    ]
    # Cell ids are unique and match nbformat 4.5's pattern.
    assert header["id"] == "cell-0"


def test_messages_become_markdown_and_shell_runs_become_bash_cells_with_output() -> None:
    notebook = _notebook(
        [
            _message("user", "Which region grew fastest?"),
            _call("c1", "sys_os_shell", {"command": "wc -l sales.csv"}),
            _result(
                "c1", json.dumps({"stdout": "1501 sales.csv\n", "stderr": "", "exit_code": 0})
            ),
            _message("assistant", "West grew fastest."),
        ]
    )

    assert _cells(notebook) == [
        ("markdown", "**User**\n\nWhich region grew fastest?"),
        ("code", "%%bash\nwc -l sales.csv"),
        ("markdown", "**Assistant**\n\nWest grew fastest."),
    ]
    code = notebook["cells"][2]
    assert code["execution_count"] is None
    assert code["outputs"] == [
        {"output_type": "stream", "name": "stdout", "text": "1501 sales.csv\n"}
    ]


@pytest.mark.parametrize(
    ("name", "command", "source"),
    [
        # Claude Code's tool, with Python fed on stdin.
        (
            "Bash",
            "python3 - <<'EOF'\nimport pandas as pd\nprint(pd.__version__)\nEOF",
            "import pandas as pd\nprint(pd.__version__)",
        ),
        # Codex wraps each command in a login shell.
        ("shell", "/bin/zsh -lc 'python3 -c \"print(42)\"'", "print(42)"),
        ("run_shell_command", "ls -la", "%%bash\nls -la"),
        ("Bash", "/bin/bash -lc 'pwd'", "%%bash\npwd"),
        # Codex can send the command as an argv list.
        ("shell", ["bash", "-lc", "git status"], "%%bash\ngit status"),
        # A redirect after the heredoc, or an unquoted delimiter the shell
        # expands, changes what runs, so the cell keeps the whole command.
        (
            "Bash",
            "python3 - <<'EOF' > out.txt\nprint(1)\nEOF",
            "%%bash\npython3 - <<'EOF' > out.txt\nprint(1)\nEOF",
        ),
        (
            "Bash",
            "python3 - <<EOF\nprint('$HOME')\nEOF",
            "%%bash\npython3 - <<EOF\nprint('$HOME')\nEOF",
        ),
        # A second heredoc after the Python one is more shell, not Python.
        (
            "Bash",
            "python3 - <<'EOF'\nprint(1)\nEOF\ncat <<'EOF'\nhi\nEOF",
            "%%bash\npython3 - <<'EOF'\nprint(1)\nEOF\ncat <<'EOF'\nhi\nEOF",
        ),
    ],
    ids=[
        "python-heredoc",
        "codex-python-c",
        "qwen-shell",
        "codex-wrapped-shell",
        "codex-argv-list",
        "heredoc-with-redirect",
        "unquoted-heredoc",
        "heredoc-then-more-shell",
    ],
)
def test_each_harness_shell_command_becomes_the_cell_it_ran(
    name: str, command: str | list[str], source: str
) -> None:
    notebook = _notebook([_call("c1", name, {"command": command})])
    assert _cells(notebook) == [("code", source)]


def test_exec_command_takes_its_command_from_cmd() -> None:
    notebook = _notebook([_call("c1", "exec_command", {"cmd": "make test"})])
    assert _cells(notebook) == [("code", "%%bash\nmake test")]


def test_an_sql_tool_becomes_an_sql_cell_and_other_tools_short_notes() -> None:
    notebook = _notebook(
        [
            _call(
                "c1", "execute_sql", {"query": "SELECT region, SUM(revenue) FROM sales GROUP BY 1"}
            ),
            _call("c2", "web_search", {"query": "quarterly revenue", "limit": 5}),
            # A ``query`` argument alone doesn't make a tool SQL.
            _call("c3", "query_docs", {"query": "how do I deploy?"}),
        ]
    )
    assert _cells(notebook) == [
        ("code", "%%sql\nSELECT region, SUM(revenue) FROM sales GROUP BY 1"),
        ("markdown", "*Tool call: `web_search` (`query`, `limit`).*"),
        ("markdown", "*Tool call: `query_docs` (`query`).*"),
    ]


def test_outputs_carry_stderr_exit_codes_and_images_unless_left_out() -> None:
    image = json.dumps(
        {
            "__omnigent_mcp_image_result__": 1,
            "isError": False,
            "content": [
                {"type": "text", "text": "chart saved"},
                {"type": "image", "data": _PNG, "mimeType": "image/png"},
            ],
        }
    )
    items = [
        _call("c1", "sys_os_shell", {"command": "python3 plot.py"}),
        _result("c1", json.dumps({"stdout": "", "stderr": "warning\n", "exit_code": 2})),
        _call("c2", "sys_os_shell", {"command": "open chart.png"}),
        _result("c2", image),
    ]

    with_outputs = _notebook(items)
    assert with_outputs["cells"][1]["outputs"] == [
        {"output_type": "stream", "name": "stderr", "text": "warning\n"},
        {"output_type": "stream", "name": "stderr", "text": "[exit code: 2]\n"},
    ]
    assert with_outputs["cells"][2]["outputs"] == [
        {"output_type": "stream", "name": "stdout", "text": "chart saved"},
        {"output_type": "display_data", "data": {"image/png": _PNG}, "metadata": {}},
    ]

    without = _notebook(items, include_outputs=False)
    assert [cell.get("outputs") for cell in without["cells"][1:3]] == [[], []]


def test_a_long_output_is_cut_with_a_note() -> None:
    notebook = _notebook(
        [
            _call("c1", "Bash", {"command": "cat big.log"}),
            _result("c1", "x" * (MAX_OUTPUT_CHARS + 50)),
        ]
    )
    [output] = notebook["cells"][1]["outputs"]
    assert output["text"].endswith("\n… (50 more characters)\n")
    assert len(output["text"]) < MAX_OUTPUT_CHARS + 100


def test_nothing_is_dropped_silently() -> None:
    notebook = _notebook(
        [
            {"type": "reasoning", "model": "o3", "summary": [], "encrypted_content": "gAAAA..."},
            {
                "type": "reasoning",
                "model": "o3",
                "summary": [{"type": "summary_text", "text": "Group by region first."}],
            },
            {"type": "routing_decision", "model": "databricks-gpt-5-5", "applied": False},
            _result("c_unknown", "orphaned output"),
            {
                "type": "error",
                "source": "tool",
                "code": "timeout",
                "message": "The query timed out.",
            },
            {
                "type": "slash_command",
                "model": "x",
                "kind": "skill",
                "name": "review",
                "arguments": "PR 12",
            },
            {"type": "compaction", "model": "x"},
            {"type": "compaction", "model": "x", "summary": "Loaded sales.csv."},
            {"type": "terminal_command", "kind": "output", "stdout": "stray\n", "stderr": ""},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "refusal", "refusal": "I can't help with that."}],
            },
            {"type": "native_tool", "item": {"type": "web_search_call"}},
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_image", "file_id": "file_123"}],
            },
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_image", "image_url": "data:image/svg+xml;base64,PHN2Zz4="}
                ],
            },
            _message("user", "internal reminder", is_meta=True),
        ]
    )
    assert _cells(notebook) == [
        ("markdown", "*(The model's encrypted reasoning isn't included.)*"),
        ("markdown", "*Reasoning:*\n\nGroup by region first."),
        ("markdown", "*The model router would have picked `databricks-gpt-5-5`.*"),
        ("markdown", "*(Output of a tool call that isn't in this export.)*"),
        ("markdown", "*Error: The query timed out.*"),
        ("markdown", "*Command: `/review PR 12`*"),
        ("markdown", "*(Earlier conversation was compacted here.)*"),
        ("markdown", "*(Earlier conversation was compacted here.)*\n\nLoaded sales.csv."),
        ("markdown", "*(Terminal output of a command that isn't in this export.)*"),
        ("markdown", "**Assistant**\n\n*Refused: I can't help with that.*"),
        ("markdown", "*Built-in tool used: `web_search_call`.*"),
        (
            "markdown",
            "**User**\n\n*(An image was attached here; it isn't stored in the session.)*",
        ),
        ("markdown", "**User**\n\n*(An image was attached here; the export leaves it out.)*"),
    ]


def test_terminal_commands_typed_by_the_user_get_their_own_outputs_in_order() -> None:
    notebook = _notebook(
        [
            {"type": "terminal_command", "kind": "input", "input": "pwd"},
            {"type": "terminal_command", "kind": "input", "input": "ls"},
            {"type": "terminal_command", "kind": "output", "stdout": "/home\n", "stderr": ""},
            {"type": "terminal_command", "kind": "output", "stdout": "", "stderr": "denied\n"},
        ]
    )
    assert [(cell["source"], cell["outputs"]) for cell in notebook["cells"][1:]] == [
        ("%%bash\npwd", [{"output_type": "stream", "name": "stdout", "text": "/home\n"}]),
        ("%%bash\nls", [{"output_type": "stream", "name": "stderr", "text": "denied\n"}]),
    ]


def test_a_closing_cell_lists_the_tools_a_rerun_may_need() -> None:
    notebook = _notebook(
        [
            _call("c1", "sys_os_shell", {"command": "ls"}),
            _call("c2", "sys_os_shell", {"command": "pwd"}),
            _call("c3", "execute_sql", {"query": "SELECT 1"}),
        ]
    )
    closing = notebook["cells"][-1]["source"]
    assert closing.startswith("## Reproducing this notebook")
    assert "- `execute_sql` (1 call)" in closing
    assert "- `sys_os_shell` (2 calls)" in closing


def test_the_header_link_cannot_break_out_of_its_markdown() -> None:
    notebook = _notebook([], session_url="https://omnigent.example/c/a)[x](<img>")
    [link] = [line for line in notebook["cells"][0]["source"].splitlines() if "Link" in line]
    assert link == "- **Link:** https://omnigent.example/c/a%29%5Bx%5D%28%3Cimg%3E"


def test_dollar_signs_in_text_stay_text_but_code_keeps_them() -> None:
    text = "West made $538.9k (\\$2k fees); see `echo $HOME` and\n```\n$ ls\n```"
    notebook = _notebook([_message("assistant", text)])
    [(_, source)] = _cells(notebook)
    dollar = "<span>&#36;</span>"
    assert source == (
        f"**Assistant**\n\nWest made {dollar}538.9k ({dollar}2k fees); "
        "see `echo $HOME` and\n```\n$ ls\n```"
    )
