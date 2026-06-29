"""
Unit tests for the claude-native terminal-resolved elicitation fast path.

When a permission prompt is answered (in Claude's native TUI or the web
UI) the gated tool runs and its result is mirrored back as a
``function_call_output``. ``_drive_terminal_resolved_elicitation`` recovers
the tool identity by ``call_id`` (cached from the earlier mirrored
``function_call``) and ``_signal_terminal_resolved_harness_elicitation``
resolves the parked prompt it belongs to.

Correlation is exact-only on ``(tool_name, tool_input)``: Claude Code's
``PermissionRequest`` payload carries no ``tool_use_id``, so the id can
never tie a parked prompt to its output, and both inputs are unmodified
JSON round-trips of the same data so exact equality is the signal.

These tests pin that contract and guard the regression that prompted it:
a result must NEVER resolve a same-named prompt with a *different* input.
That used to happen via a ``len(candidates) == 1`` fallback — approving
``Bash{ls}`` in the web UI un-parked it, then mirroring its own output
found the lone remaining ``Bash{pwd}`` sibling and wrongly cleared it.

State isolation: ``_harness_parked_elicitations`` is reset between tests by
the ``_reset_elicitation_state`` autouse fixture in
``tests/server/conftest.py``; the ``_recent_mirrored_tool_calls`` cache is
cleared per test below.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from omnigent.entities.conversation import (
    ConversationItem,
    FunctionCallData,
    FunctionCallOutputData,
)
from omnigent.server._elicitation_registry import (
    _harness_parked_elicitations,
    _ParkedHarnessElicitation,
)
from omnigent.server.routes import sessions as sessions_route

SESSION = "conv_test"


@pytest.fixture(autouse=True)
def _clear_mirrored_tool_calls() -> Iterator[None]:
    """Clear the call-id cache so call_ids don't leak across tests."""
    sessions_route._recent_mirrored_tool_calls.clear()
    yield
    sessions_route._recent_mirrored_tool_calls.clear()


def _park(
    elicitation_id: str,
    tool_name: str | None,
    tool_input: dict[str, Any] | None,
    *,
    session_id: str = SESSION,
) -> _ParkedHarnessElicitation:
    """Register a parked prompt in the registry and return it."""
    parked = _ParkedHarnessElicitation(
        session_id=session_id,
        tool_name=tool_name,
        tool_input=tool_input,
        resolved_elsewhere=asyncio.Event(),
    )
    _harness_parked_elicitations[elicitation_id] = parked
    return parked


def _function_call_item(call_id: str, name: str, tool_input: dict[str, Any]) -> ConversationItem:
    """Build a mirrored ``function_call`` item as the forwarder posts it."""
    return ConversationItem(
        id=f"item_{call_id}_fc",
        type="function_call",
        status="completed",
        response_id="resp_test",
        created_at=0,
        data=FunctionCallData(
            agent="claude",
            name=name,
            # The forwarder serializes the JSONL tool_use.input exactly
            # this way (see claude_native_bridge); mirror that here.
            arguments=json.dumps(tool_input, separators=(",", ":")),
            call_id=call_id,
        ),
    )


def _function_call_output_item(call_id: str, output: str = "ok") -> ConversationItem:
    """Build a mirrored ``function_call_output`` item for ``call_id``."""
    return ConversationItem(
        id=f"item_{call_id}_fco",
        type="function_call_output",
        status="completed",
        response_id="resp_test",
        created_at=0,
        data=FunctionCallOutputData(call_id=call_id, output=output),
    )


def test_exact_input_match_resolves_only_that_prompt() -> None:
    """
    Among same-named prompts, only the one whose ``tool_input`` matches
    the result exactly is resolved; its sibling stays pending.
    """
    a = _park("e_a", "Bash", {"command": "ls"})
    b = _park("e_b", "Bash", {"command": "pwd"})

    sessions_route._signal_terminal_resolved_harness_elicitation(
        SESSION, "Bash", {"command": "ls"}
    )

    assert a.resolved_elsewhere.is_set()
    assert not b.resolved_elsewhere.is_set()


def test_no_input_prompt_resolved_by_empty_mirrored_output() -> None:
    """
    A prompt parked with no input (``tool_input=None`` — its hook payload
    carried no ``tool_input``) is resolved by its mirrored result, whose
    parsed arguments normalize to ``{}``. The park side spells "no input"
    as ``None`` and the mirror side as ``{}``; both canonicalize to ``{}``
    so they compare equal. Without that, ``None == {}`` is ``False`` and —
    with no count-based fallback — the prompt would orphan until the hook
    timeout.
    """
    a = _park("e_a", "Bash", None)

    sessions_route._signal_terminal_resolved_harness_elicitation(SESSION, "Bash", {})

    assert a.resolved_elsewhere.is_set()


def test_no_input_prompt_not_cleared_by_result_with_input() -> None:
    """
    Canonicalizing ``None`` to ``{}`` must not over-match: a no-input
    prompt is left pending by a same-named result that carried real input
    (they describe different calls), even as the lone candidate.
    """
    a = _park("e_a", "Bash", None)

    sessions_route._signal_terminal_resolved_harness_elicitation(
        SESSION, "Bash", {"command": "ls"}
    )

    assert not a.resolved_elsewhere.is_set()


def test_lone_same_name_prompt_with_different_input_is_not_resolved(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Regression: a result must not resolve a lone same-named prompt whose
    input differs. The removed ``len(candidates) == 1`` fallback used to
    clear it; now a non-match resolves nothing and logs (at debug) the
    skipped path.
    """
    a = _park("e_a", "Bash", {"command": "ls"})

    with caplog.at_level(logging.DEBUG, logger="omnigent.server.routes.sessions"):
        sessions_route._signal_terminal_resolved_harness_elicitation(
            SESSION, "Bash", {"command": "pwd"}
        )

    assert not a.resolved_elsewhere.is_set()
    assert "matched no parked prompt by input" in caplog.text


def test_approved_prompt_output_does_not_clear_sibling() -> None:
    """
    The exact scenario reported: two same-named prompts pending, the
    user approves one in the web UI (which un-parks it), and that tool's
    own mirrored output must not clear the still-pending sibling.
    """
    _park("e_a", "Bash", {"command": "ls"})
    b = _park("e_b", "Bash", {"command": "pwd"})

    # Web approval of A returns and un-parks it (sessions.py finally block).
    del _harness_parked_elicitations["e_a"]

    # A's own tool now runs; its mirrored output arrives for ``ls``.
    sessions_route._signal_terminal_resolved_harness_elicitation(
        SESSION, "Bash", {"command": "ls"}
    )

    assert not b.resolved_elsewhere.is_set()


def test_different_tool_name_is_noop() -> None:
    """A result for a different tool never touches a parked prompt."""
    a = _park("e_a", "Bash", {"command": "ls"})

    sessions_route._signal_terminal_resolved_harness_elicitation(
        SESSION, "Read", {"file_path": "x"}
    )

    assert not a.resolved_elsewhere.is_set()


def test_different_session_is_noop() -> None:
    """A result in another session never touches this session's prompt."""
    a = _park("e_a", "Bash", {"command": "ls"})

    sessions_route._signal_terminal_resolved_harness_elicitation(
        "conv_other", "Bash", {"command": "ls"}
    )

    assert not a.resolved_elsewhere.is_set()


def test_already_resolved_candidate_is_skipped() -> None:
    """
    A prompt already marked resolved is filtered out, so an identical
    input resolves the next still-pending same-named prompt instead.
    """
    a = _park("e_a", "Bash", {"command": "ls"})
    a.resolved_elsewhere.set()
    b = _park("e_b", "Bash", {"command": "ls"})

    sessions_route._signal_terminal_resolved_harness_elicitation(
        SESSION, "Bash", {"command": "ls"}
    )

    assert b.resolved_elsewhere.is_set()


def test_drive_mirrored_output_resolves_matching_parked_prompt() -> None:
    """
    End-to-end server path: a mirrored ``function_call`` caches the tool
    identity by ``call_id`` and the matching ``function_call_output``
    resolves the exact-input prompt, leaving the sibling pending.
    """
    a = _park("e_a", "Bash", {"command": "ls"})
    b = _park("e_b", "Bash", {"command": "pwd"})

    sessions_route._drive_terminal_resolved_elicitation(
        SESSION, _function_call_item("toolu_a", "Bash", {"command": "ls"})
    )
    sessions_route._drive_terminal_resolved_elicitation(
        SESSION, _function_call_output_item("toolu_a")
    )

    assert a.resolved_elsewhere.is_set()
    assert not b.resolved_elsewhere.is_set()


def test_drive_output_without_known_call_id_is_noop() -> None:
    """
    A ``function_call_output`` whose ``call_id`` was never mirrored as a
    ``function_call`` recovers no identity, so nothing is resolved.
    """
    a = _park("e_a", "Bash", {"command": "ls"})

    sessions_route._drive_terminal_resolved_elicitation(
        SESSION, _function_call_output_item("toolu_unknown")
    )

    assert not a.resolved_elsewhere.is_set()
