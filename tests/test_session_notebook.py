"""Exporting a session as a Jupyter notebook (nbformat 4.5)."""

from __future__ import annotations

import base64
import json
import time
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
    ("name", "arguments", "source"),
    [
        # Claude Code's tool, with Python fed on stdin.
        (
            "Bash",
            {"command": "python3 - <<'EOF'\nimport pandas as pd\nprint(pd.__version__)\nEOF"},
            "import pandas as pd\nprint(pd.__version__)",
        ),
        # Codex wraps each command in a login shell.
        ("shell", {"command": "/bin/zsh -lc 'python3 -c \"print(42)\"'"}, "print(42)"),
        ("run_shell_command", {"command": "ls -la"}, "%%bash\nls -la"),
        ("Bash", {"command": "/bin/bash -lc 'pwd'"}, "%%bash\npwd"),
        # Codex can send the command as an argv list.
        ("shell", {"command": ["bash", "-lc", "git status"]}, "%%bash\ngit status"),
        # A redirect after the heredoc, or an unquoted delimiter the shell
        # expands, changes what runs, so the cell keeps the whole command.
        (
            "Bash",
            {"command": "python3 - <<'EOF' > out.txt\nprint(1)\nEOF"},
            "%%bash\npython3 - <<'EOF' > out.txt\nprint(1)\nEOF",
        ),
        (
            "Bash",
            {"command": "python3 - <<EOF\nprint('$HOME')\nEOF"},
            "%%bash\npython3 - <<EOF\nprint('$HOME')\nEOF",
        ),
        # Blank lines after the delimiter don't change what runs.
        ("Bash", {"command": "python3 - <<'EOF'\nprint(1)\nEOF\n\n"}, "print(1)"),
        # A second heredoc after the Python one is more shell, not Python.
        (
            "Bash",
            {"command": "python3 - <<'EOF'\nprint(1)\nEOF\ncat <<'EOF'\nhi\nEOF"},
            "%%bash\npython3 - <<'EOF'\nprint(1)\nEOF\ncat <<'EOF'\nhi\nEOF",
        ),
        # The same for ``-c``: a redirect, or a program the shell expands.
        (
            "Bash",
            {"command": "python3 -c 'print(1)'>out.txt"},
            "%%bash\npython3 -c 'print(1)'>out.txt",
        ),
        (
            "Bash",
            {"command": "python3 -c \"print('$HOME')\""},
            "%%bash\npython3 -c \"print('$HOME')\"",
        ),
        # A command run in its own directory runs there again.
        (
            "exec_command",
            {"cmd": "python3 -c 'print(1)'", "workdir": "/work/my data"},
            "%%bash\ncd '/work/my data' || exit 1\npython3 -c 'print(1)'",
        ),
        # A wrapper is removed only when its single-quoted script is the whole
        # command; the script then runs in the same shell.
        ("Bash", {"command": "bash -c 'cd build'&&pwd"}, "%%bash\nbash -c 'cd build'&&pwd"),
        (
            "shell",
            {"command": "/bin/zsh -lc 'print -r -- hello'"},
            "%%script zsh\nprint -r -- hello",
        ),
        ("Bash", {"command": 'bash -lc "echo $HOME"'}, '%%bash\nbash -lc "echo $HOME"'),
        # An argv list hands the shell its script verbatim, quotes and all.
        ("shell", {"command": ["bash", "-lc", "sed -n '1,5p' f"]}, "%%bash\nsed -n '1,5p' f"),
        (
            "shell",
            {"command": ["/bin/zsh", "-lc", "python3 - <<'PY'\nprint(1)\nPY"]},
            "print(1)",
        ),
        # The shell ends a heredoc only at a line that is exactly the delimiter.
        (
            "Bash",
            {"command": "python3 - <<'EOF'\nprint(42)\nEOF "},
            "%%bash\npython3 - <<'EOF'\nprint(42)\nEOF ",
        ),
        (
            "Bash",
            {"command": "python3 - <<'EOF'\nprint(42)\nEOF\t"},
            "%%bash\npython3 - <<'EOF'\nprint(42)\nEOF\t",
        ),
        # Codex's ``exec_command`` names its command ``cmd``.
        ("exec_command", {"cmd": "make test"}, "%%bash\nmake test"),
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
        "heredoc-then-blank-lines",
        "python-c-with-redirect",
        "python-c-expanded",
        "working-directory",
        "wrapper-then-operator",
        "zsh-script",
        "wrapper-with-expansion",
        "codex-argv-quoted-script",
        "codex-argv-python",
        "heredoc-delimiter-trailing-space",
        "heredoc-delimiter-trailing-tab",
        "exec-command-cmd",
    ],
)
def test_each_harness_shell_command_becomes_the_cell_it_ran(
    name: str, arguments: dict[str, Any], source: str
) -> None:
    notebook = _notebook([_call("c1", name, arguments)])
    assert _cells(notebook) == [("code", source)]


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
    # The shell tool's result for a command it stopped at its timeout.
    timed_out = json.dumps(
        {
            "stdout": "",
            "stderr": "",
            "exit_code": None,
            "timed_out": True,
            "error": "Command timed out after 30 seconds",
            "shell": "/bin/bash",
            "cwd": "/work",
        }
    )
    items = [
        _call("c1", "sys_os_shell", {"command": "python3 plot.py"}),
        _result("c1", json.dumps({"stdout": "", "stderr": "warning\n", "exit_code": 2})),
        _call("c2", "sys_os_shell", {"command": "open chart.png"}),
        _result("c2", image),
        _call("c3", "sys_os_shell", {"command": "sleep 60"}),
        _result("c3", timed_out),
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
    assert with_outputs["cells"][3]["outputs"] == [
        {
            "output_type": "stream",
            "name": "stderr",
            "text": "[Command timed out after 30 seconds]\n",
        }
    ]

    without = _notebook(items, include_outputs=False)
    assert [cell.get("outputs") for cell in without["cells"][1:4]] == [[], [], []]


@pytest.mark.parametrize(
    ("name", "printed"),
    [
        # Claude Code's Bash returns what the command printed, JSON or not.
        ("Bash", {"stdout": "a", "stderr": "", "exit_code": 0}),
        # The shell tool's own result never carries other fields.
        ("sys_os_shell", {"stdout": "a", "stderr": "", "rows": 3}),
    ],
    ids=["another-tools-output", "extra-fields"],
)
def test_output_that_only_looks_like_a_shell_result_stays_text(
    name: str, printed: dict[str, Any]
) -> None:
    output = json.dumps(printed)
    notebook = _notebook(
        [_call("c1", name, {"command": "cat result.json"}), _result("c1", output)]
    )
    assert notebook["cells"][1]["outputs"] == [
        {"output_type": "stream", "name": "stdout", "text": output}
    ]


def test_output_that_only_looks_like_an_image_result_stays_text() -> None:
    printed = json.dumps({"__omnigent_mcp_image_result__": 1, "isError": False, "content": 1})
    notebook = _notebook(
        [_call("c1", "Bash", {"command": "cat fake.json"}), _result("c1", printed)]
    )
    assert notebook["cells"][1]["outputs"] == [
        {"output_type": "stream", "name": "stdout", "text": printed}
    ]


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
            # The Claude bridge's thinking: plain content, no summary.
            {
                "type": "reasoning",
                "model": "claude",
                "summary": [],
                "content": [{"type": "reasoning_text", "text": "Let me think."}],
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
        ("markdown", "*(The model's full reasoning isn't included.)*"),
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
    # A %%sql cell needs the SQL extension and a connection to rerun.
    assert "the JupySQL extension (`%load_ext sql`) connected to the same database" in closing
    shell_only = _notebook([_call("c1", "sys_os_shell", {"command": "ls"})])
    assert "JupySQL" not in shell_only["cells"][-1]["source"]


def test_the_header_link_cannot_break_out_of_its_markdown() -> None:
    notebook = _notebook([], session_url="https://omnigent.example/c/a)[x](<img>")
    [link] = [line for line in notebook["cells"][0]["source"].splitlines() if "Link" in line]
    assert link == "- **Link:** https://omnigent.example/c/a%29%5Bx%5D%28%3Cimg%3E"


def test_dollar_signs_in_text_stay_text_but_code_and_links_keep_them() -> None:
    text = (
        "West made $538.9k (\\$2k fees); see `echo $HOME` and\n```\n$ ls\n```\n"
        "[chart](https://example.com/q?p=$5), <https://example.com/a$b> and "
        "https://example.com/c$d; 见https://example.org价格$5\n\n    echo $HOME"
    )
    notebook = _notebook([_message("assistant", text)])
    [(_, source)] = _cells(notebook)
    dollar = "<span>&#36;</span>"
    assert source == (
        f"**Assistant**\n\nWest made {dollar}538.9k ({dollar}2k fees); "
        "see `echo $HOME` and\n```\n$ ls\n```\n"
        "[chart](https://example.com/q?p=$5), <https://example.com/a$b> and "
        f"https://example.com/c$d; 见https://example.org价格{dollar}5\n\n    echo $HOME"
    )


@pytest.mark.parametrize(
    "text",
    ["](" * 50_000 + "<https://" * 20_000 + "$5", "```\nx\n```\n" * 20_000 + "$5"],
    ids=["unclosed-links", "many-fences"],
)
def test_pathological_text_converts_quickly(text: str) -> None:
    """Protecting links and code can't rescan the text after every match or line."""
    started = time.monotonic()
    _notebook([_message("user", text)])
    assert time.monotonic() - started < 2


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        # Indented under a list item or a paragraph line: text, not code.
        ("- a\n    - West $5k, East $3k", "- a\n    - West {d}5k, East {d}3k"),
        ("1. step\n\n    costing $5 and $6", "1. step\n\n    costing {d}5 and {d}6"),
        ("para\n    $a$ and $b$", "para\n    {d}a{d} and {d}b{d}"),
        # After a blank line, outside a list: an indented code block.
        (
            "intro\n\n    echo $HOME\n    ls $PWD\n\nCosts $5.",
            "intro\n\n    echo $HOME\n    ls $PWD\n\nCosts {d}5.",
        ),
        # A list inside a fence doesn't make the next indented block list text.
        ("```\n- a\n```\n\n    echo $HOME", "```\n- a\n```\n\n    echo $HOME"),
        # A marker in code isn't a list item; a rule or heading ends the paragraph.
        ("x\n\n    - a $5", "x\n\n    - a $5"),
        ("* * *\n\n    echo $HOME", "* * *\n\n    echo $HOME"),
        ("# H\n    echo $HOME", "# H\n    echo $HOME"),
        ("Title\n===\n    echo $HOME", "Title\n===\n    echo $HOME"),
        ("p\r\n\r\n***\r\n    echo $HOME", "p\r\n\r\n***\r\n    echo $HOME"),
    ],
    ids=[
        "nested-bullet",
        "list-continuation",
        "lazy-paragraph",
        "indented-code",
        "after-a-fenced-list",
        "indented-list-marker",
        "after-a-rule",
        "after-a-heading",
        "after-a-setext-heading",
        "crlf-rule",
    ],
)
def test_indented_text_is_code_only_where_markdown_makes_it_code(text: str, kept: str) -> None:
    [(_, source)] = _cells(_notebook([_message("assistant", text)]))
    assert source == "**Assistant**\n\n" + kept.format(d="<span>&#36;</span>")


@pytest.mark.parametrize(
    ("script", "needs_zsh"),
    [("print -r hi", True), ("python3 - <<'PY'\nprint(1)\nPY", False)],
    ids=["zsh-cell", "promoted-to-python"],
)
def test_the_closing_cell_says_zsh_cells_need_zsh(script: str, needs_zsh: bool) -> None:
    notebook = _notebook([_call("c1", "shell", {"command": ["/bin/zsh", "-lc", script]})])
    closing = notebook["cells"][-1]["source"]
    assert ("zsh for the `%%script zsh` cells" in closing) is needs_zsh


def test_a_shell_run_that_failed_to_start_shows_its_error() -> None:
    notebook = _notebook(
        [
            _call("c1", "sys_os_shell", {"command": "ls"}),
            _result("c1", json.dumps({"error": "Failed to run shell command: no shell"})),
            _call("c2", "sys_os_shell", {"command": "pwd"}),
            # The output of another tool with the same keys is still text.
            _call("c3", "Bash", {"command": "cat err.json"}),
            _result("c3", json.dumps({"error": "x"})),
        ]
    )
    assert notebook["cells"][1]["outputs"] == [
        {
            "output_type": "stream",
            "name": "stderr",
            "text": "[Failed to run shell command: no shell]\n",
        }
    ]
    assert notebook["cells"][3]["outputs"] == [
        {"output_type": "stream", "name": "stdout", "text": json.dumps({"error": "x"})}
    ]


def test_the_closing_cell_lists_every_extra_a_rerun_needs_in_one_sentence() -> None:
    notebook = _notebook(
        [
            _call("c1", "shell", {"command": ["/bin/zsh", "-lc", "print -r hi"]}),
            _call("c2", "execute_sql", {"query": "SELECT 1"}),
        ]
    )
    assert (
        "Some cells also need zsh for the `%%script zsh` cells and the JupySQL extension "
        "(`%load_ext sql`) connected to the same database for the `%%sql` cells."
    ) in notebook["cells"][-1]["source"]
