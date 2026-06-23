"""Unit tests for the cursor-native tool-approval mirror.

Covers the pure pieces a live cursor-agent isn't needed for: parsing an
approval block out of rendered pane text (subject/title/keys/operation type),
rejecting non-approval panes (notably the first-run Workspace-Trust modal,
which uses ``[key]`` brackets rather than the ``(key)`` parentheses of a
tool-approval prompt), dedup-hash stability, and the elicitation-id format.

The live tmux + cursor-agent path (detect → POST → keystroke) is exercised by
``tests/e2e/test_cursor_native_cli_e2e.py``, not here.
"""

from __future__ import annotations

import pytest

from omnigent.cursor_native_permissions import (
    cursor_permission_elicitation_id,
    parse_cursor_approval_prompt,
)

# A faithful capture of cursor-agent's shell approval block (the widget the
# mirror keys on), as rendered by ``tmux capture-pane -p``.
_SHELL_PANE = """ $  echo omnigent_probe > out.txt in .

 Run this command?
 Shell allowlist is empty
  → Run (once) (y)
    Run Everything (shift+tab)
    Skip (esc or n)
"""

# The first-run Workspace-Trust modal — must NOT be mistaken for a tool prompt.
_TRUST_PANE = """  │  ⚠ Workspace Trust Required
  │  Do you trust the contents of this directory?
  │    [a] Trust this workspace
  │    [q] Quit
"""


def test_parse_shell_approval_extracts_subject_title_and_keys() -> None:
    """A shell approval block yields the command, title, and advertised keys."""
    prompt = parse_cursor_approval_prompt(_SHELL_PANE)

    assert prompt is not None
    assert prompt.title == "Run this command?"
    assert prompt.subject == "echo omnigent_probe > out.txt"
    assert prompt.operation_type == "shell"
    assert prompt.accept_key == "y"
    assert prompt.decline_key == "Escape"
    # The card renders the command for the user to review.
    assert "echo omnigent_probe > out.txt" in prompt.preview
    assert prompt.message


@pytest.mark.parametrize(
    "pane",
    [
        pytest.param("", id="empty"),
        pytest.param("idle\n> Add a follow-up", id="idle-input"),
        pytest.param(_TRUST_PANE, id="workspace-trust-modal"),
        pytest.param(
            " Run this command?\n  → Run (once) [y]\n    Skip [n]\n",
            id="bracket-keys-not-parens",
        ),
    ],
)
def test_parse_returns_none_for_non_approval_panes(pane: str) -> None:
    """Non-approval panes (incl. the trust modal) are not parsed as prompts."""
    assert parse_cursor_approval_prompt(pane) is None


def test_block_hash_is_stable_across_identical_captures() -> None:
    """The same prompt seen on two polls dedupes to the same hash and id."""
    first = parse_cursor_approval_prompt(_SHELL_PANE)
    second = parse_cursor_approval_prompt(_SHELL_PANE)

    assert first is not None and second is not None
    assert first.block_hash == second.block_hash


def test_block_hash_differs_for_a_different_command() -> None:
    """A different command produces a different dedup hash (a new prompt)."""
    other_pane = _SHELL_PANE.replace("echo omnigent_probe", "rm -rf build")
    first = parse_cursor_approval_prompt(_SHELL_PANE)
    other = parse_cursor_approval_prompt(other_pane)

    assert first is not None and other is not None
    assert first.block_hash != other.block_hash


def test_elicitation_id_is_deterministic_and_session_scoped() -> None:
    """The elicitation id is stable for a (session, block) and carries both."""
    eid = cursor_permission_elicitation_id("conv_abc", "deadbeef")

    assert eid == cursor_permission_elicitation_id("conv_abc", "deadbeef")
    assert eid != cursor_permission_elicitation_id("conv_xyz", "deadbeef")
    assert "conv_abc" in eid
    assert eid.startswith("elicit_cursor_")
