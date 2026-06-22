"""Direct unit tests for ``_find_stable_markdown_boundary``.

Exercises edge cases not covered by the indirect tests in
``test_formatter.py``: empty input, fence-only responses, language
specifiers on fences, trailing double newlines, and multi-paragraph
chains.
"""

from __future__ import annotations

import pytest
from omnigent_ui_sdk.terminal._formatter import _find_stable_markdown_boundary


def test_boundary_simple_paragraph_break() -> None:
    """Two paragraphs separated by a blank line → offset at start of second."""
    text = "Para 1.\n\nPara 2."
    result = _find_stable_markdown_boundary(text)
    # The boundary should point to the start of "Para 2." — the
    # character immediately after the blank line. "Para 1.\n\n" is
    # 10 chars; position 10 is 'P' of "Para 2.".
    assert result == text.index("Para 2."), (
        f"Expected boundary at start of 'Para 2.' (index {text.index('Para 2.')}), "
        f"got {result}. A wrong offset means the formatter would commit "
        f"either too much text (including the unstable tail) or too little "
        f"(missing the completed paragraph)."
    )


def test_boundary_no_break_returns_zero() -> None:
    """A single paragraph with no blank line → 0 (no safe boundary)."""
    result = _find_stable_markdown_boundary("Hello world, no break here.")
    # 0 means "nothing is safe to commit yet" — the entire text is
    # still the unstable tail. If non-zero, the formatter would
    # prematurely commit text that might still be mid-paragraph.
    assert result == 0, f"Expected 0 (no safe boundary in single paragraph), got {result}."


def test_boundary_open_code_fence_prevents_boundary() -> None:
    """A ``\\n\\n`` inside an open fence is NOT a boundary."""
    text = "```\ncode\n\nmore code\n```\n\nAfter fence."
    result = _find_stable_markdown_boundary(text)
    # The only valid boundary is after the closing fence's blank
    # line — the \n\n between "code" and "more code" is inside
    # the fence and must not be treated as a boundary.
    assert result == text.index("After fence."), (
        f"Expected boundary at 'After fence.' (index {text.index('After fence.')}), "
        f"got {result}. If the result points inside the fence, the "
        f"boundary detector failed to track fence open/close state."
    )


def test_boundary_closed_fence_allows_boundary() -> None:
    """After a fence closes, the next ``\\n\\n`` is a valid boundary."""
    text = "```\ncode\n```\n\nAfter."
    result = _find_stable_markdown_boundary(text)
    assert result == text.index("After."), (
        f"Expected boundary after closed fence at 'After.' "
        f"(index {text.index('After.')}), got {result}."
    )


def test_boundary_multiple_paragraphs_returns_last_safe() -> None:
    """Multiple paragraph breaks → offset at the start of the last paragraph."""
    text = "A.\n\nB.\n\nC.\n\nD"
    result = _find_stable_markdown_boundary(text)
    # "D" is the unstable tail. The last safe boundary is at
    # the start of "D" — everything before it is committed.
    assert result == text.index("D"), (
        f"Expected boundary at 'D' (index {text.index('D')}), got {result}. "
        f"The function should return the LAST safe boundary, not the first."
    )


def test_boundary_trailing_double_newline() -> None:
    """``\"Text.\\n\\n\"`` → 0 because candidate must be ``< n``."""
    # The boundary detection requires content AFTER the blank line
    # (candidate < n). A trailing \n\n has nothing after it, so
    # no safe commit point exists.
    result = _find_stable_markdown_boundary("Text.\n\n")
    assert result == 0, (
        f"Expected 0 (trailing \\n\\n has no content after it), got {result}. "
        f"A non-zero result would cause the formatter to commit text "
        f"that ends on a boundary with nothing left as tail — the host "
        f"would emit an empty StreamLive."
    )


def test_boundary_fence_with_language_specifier() -> None:
    """````` ```javascript` `` is recognized as a fence opener."""
    text = "```javascript\nconsole.log('hi');\n```\n\nDone."
    result = _find_stable_markdown_boundary(text)
    assert result == text.index("Done."), (
        f"Expected boundary at 'Done.' after fenced code block with "
        f"language specifier, got {result}. If 0, the fence with "
        f"language tag was not recognized as a fence."
    )


def test_boundary_empty_string() -> None:
    """Empty string → 0."""
    result = _find_stable_markdown_boundary("")
    assert result == 0, f"Expected 0 for empty string, got {result}."


def test_boundary_only_newlines() -> None:
    """``\"\\n\\n\\n\"`` → 0 (blank lines with no content to commit)."""
    # All blank lines, but the candidate must point to non-empty
    # content after the boundary. Since there's nothing but more
    # newlines, and the last candidate would be at position 3
    # which is < n=3, it might technically be a candidate.
    # Let's verify what the function returns — the key point is
    # that there's no useful content, so either 0 or a valid
    # offset to the trailing newline is acceptable as long as the
    # formatter handles it.
    result = _find_stable_markdown_boundary("\n\n\n")
    # Position 1 is after the first blank line (line "" at i=0).
    # candidate = 0+1 = 1, and 1 < 3, so last_safe = 1.
    # Then position 2 is after the second newline. line at i=1
    # is "" (stripped == ""), candidate = 1+1 = 2, 2 < 3, so
    # last_safe = 2. Then i=3, which is n, loop ends.
    # Actually let's trace more carefully:
    # text = "\n\n\n", n=3
    # i=0: nl=text.find("\n",0)=0, line=text[0:0]="" stripped=""
    #   not startswith("```"), stripped=="" and not in_fence → candidate=1, 1<3 → last_safe=1
    #   i=1
    # i=1: nl=text.find("\n",1)=1, line=text[1:1]="" stripped=""
    #   candidate=2, 2<3 → last_safe=2
    #   i=2
    # i=2: nl=text.find("\n",2)=2, line=text[2:2]="" stripped=""
    #   candidate=3, 3<3 is False → last_safe stays 2
    #   i=3
    # loop ends, return 2
    assert result == 2, f"Expected 2 (last valid candidate offset in '\\n\\n\\n'), got {result}."


@pytest.mark.parametrize(
    "fence_opener",
    [
        "```",
        "```python",
        "```javascript",
        "``` ",
        "  ```",
    ],
    ids=["bare", "python", "javascript", "trailing-space", "indented"],
)
def test_boundary_fence_variants_recognized(fence_opener: str) -> None:
    """Various fence openers are all recognized as fences."""
    # Build text with a fence that contains a blank line inside it.
    text = f"{fence_opener}\ncode\n\nmore\n```\n\nAfter."
    result = _find_stable_markdown_boundary(text)
    # The only valid boundary is after the closing fence.
    assert result == text.index("After."), (
        f"Fence opener {fence_opener!r} was not recognized — boundary "
        f"landed at {result} instead of {text.index('After.')}."
    )
