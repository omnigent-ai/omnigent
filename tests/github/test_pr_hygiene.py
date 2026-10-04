from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).parents[2] / ".github" / "scripts" / "pr-template" / "hygiene.py"
_SPEC = importlib.util.spec_from_file_location("pr_hygiene", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
pr_hygiene = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = pr_hygiene
_SPEC.loader.exec_module(pr_hygiene)


def test_visible_word_count_ignores_template_comments_and_link_targets() -> None:
    body = (
        "<!-- " + "hidden " * 700 + "-->\n"
        "## Summary\n\n[Readable label](https://example.com/long/internal/path)\n"
    )

    assert pr_hygiene.visible_word_count(body) == 3
    assert pr_hygiene.check_hygiene(body, "") == []
    assert any(
        "601 visible words" in warning for warning in pr_hygiene.check_hygiene("word " * 601, "")
    )


def test_repeated_long_sentence_is_flagged_once() -> None:
    sentence = (
        "The fix prevents duplicate updates when a retry resumes after the "
        "original attempt has already completed."
    )
    body = f"## Summary\n\n{sentence}\n\n## Demo\n\n{sentence}\n"

    warnings = pr_hygiene.check_hygiene(body, "")

    assert len(warnings) == 1
    assert "Repeated PR prose at lines 3, 7" in warnings[0]


def test_long_added_comment_blocks_ignore_markdown_and_code_lines() -> None:
    diff = (
        "diff --git a/app.py b/app.py\n"
        "+++ b/app.py\n"
        "@@ -0,0 +1,7 @@\n"
        "+# First safety constraint.\n"
        "+# Second safety constraint.\n"
        "+# Third safety constraint.\n"
        "+# Fourth safety constraint.\n"
        "+value = 1  # This inline comment is not a block.\n"
        "+# Short note.\n"
        "+value = 2\n"
        "diff --git a/README.md b/README.md\n"
        "+++ b/README.md\n"
        "@@ -0,0 +1,4 @@\n"
        "+# Heading\n"
        "+# Another heading\n"
        "+# Yet another heading\n"
        "+# Final heading\n"
    )

    assert pr_hygiene.added_comment_blocks(diff) == [pr_hygiene.CommentBlock("app.py", 1, 4)]


def test_multiline_js_comment_and_python_docstring_are_reviewed() -> None:
    diff = (
        "diff --git a/ui.ts b/ui.ts\n"
        "+++ b/ui.ts\n"
        "@@ -0,0 +1,5 @@\n"
        "+/*\n"
        "+ * A reason.\n"
        "+ * Another reason.\n"
        "+ * One more reason.\n"
        "+ */\n"
        "diff --git a/worker.py b/worker.py\n"
        "+++ b/worker.py\n"
        "@@ -0,0 +1,5 @@\n"
        '+"""\n'
        "+Explain the behavior.\n"
        "+Explain the edge case.\n"
        "+Explain the fallback.\n"
        '+"""\n'
    )

    assert pr_hygiene.added_comment_blocks(diff) == [
        pr_hygiene.CommentBlock("ui.ts", 1, 5),
        pr_hygiene.CommentBlock("worker.py", 1, 5),
    ]


def test_cli_checks_committed_diff_and_keeps_warnings_advisory(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)

    git("init", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    (repo / "app.py").write_text("value = 0\n", encoding="utf-8")
    git("add", "app.py")
    git("commit", "-m", "base")
    git("checkout", "-b", "fix")
    (repo / "app.py").write_text(
        "# Safety rationale one.\n"
        "# Safety rationale two.\n"
        "# Safety rationale three.\n"
        "# Safety rationale four.\n"
        "value = 1\n",
        encoding="utf-8",
    )
    git("add", "app.py")
    git("commit", "-m", "fix")
    body_file = repo / "pr-body.md"
    body_file.write_text("word " * 601, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--base", "main", "--body-file", str(body_file)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "601 visible words" in result.stdout
    assert "app.py:1 (4 lines" in result.stdout
    assert (repo / "app.py").read_text(encoding="utf-8").startswith("# Safety rationale")
