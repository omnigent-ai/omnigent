"""Bundle ``instructions:`` reach a native harness session (upstream #3530).

A bundle declaring ``instructions: <sibling file>`` had that text silently
dropped whenever the agent ran on a harness executor (``executor.type:
omnigent`` with a ``*-native`` harness). The runtime path composed it correctly
— :func:`omnigent.runtime.prompt.build_instructions` folded ``spec.instructions``
into the turn's ``system_prompt`` and the executor adapter handed that to the
inner executor — but every native executor's ``run_turn`` opens with
``del tools, system_prompt``, and rightly so: a native harness drives an
already-running CLI that no per-turn system prompt can reach.

Delivery therefore has to happen at LAUNCH, through whichever *additive*
instruction channel the harness exposes. Exactly two native arms have one:

* claude-native — Claude Code's ``--append-system-prompt`` flag
* codex-native — Codex's top-level ``developer_instructions`` config setting

Both slots were ALREADY occupied by Smart Routing's routed-spawn note, so the
central risk in this fix is not delivery but *composition*: writing the spec's
instructions into the slot naively drops the note, and the reverse drops the
instructions. These tests pin the composition contract that both launch sites
depend on.
"""

from __future__ import annotations

from omnigent.runner.native.orchestration import (
    _agent_instructions_from_spec,
    _compose_native_instructions,
)
from omnigent.spec.types import AgentSpec

_INSTRUCTIONS = "# Ferrous Sparrow Protocol\n\nAlways greet in Latin."
_NOTE = "Framework note: a denied spawn is an approved re-route."


def test_instructions_are_read_from_a_bare_agent_spec() -> None:
    """``spec.instructions`` is already resolved text, so this is a field read.

    The spec parser (``_resolve_instructions``) has already turned a
    bundle-relative path into file contents, validated containment, or fallen
    back to inline text — there is no file IO left to do at launch.
    """
    spec = AgentSpec(spec_version=1, name="sparrow", instructions=_INSTRUCTIONS)

    assert _agent_instructions_from_spec(spec) == _INSTRUCTIONS


def test_instructions_are_read_through_a_resolved_spec_wrapper() -> None:
    """The launch sites may hold a ``ResolvedSpec`` rather than a bare spec.

    Mirrors ``_agent_os_env_from_spec``, which unwraps the same two shapes. A
    regression here would drop instructions for every bundle-resolved session
    while bare-spec unit tests kept passing.
    """
    from omnigent.runner.native.orchestration import ResolvedSpec

    spec = AgentSpec(spec_version=1, name="sparrow", instructions=_INSTRUCTIONS)
    resolved = ResolvedSpec(spec=spec, workdir=None)

    assert _agent_instructions_from_spec(resolved) == _INSTRUCTIONS


def test_a_missing_or_blank_spec_contributes_nothing() -> None:
    """Absent, empty and whitespace-only instructions all read as "nothing".

    Whitespace matters because the composer's ``None`` return is what keeps a
    plain session from being handed an empty ``--append-system-prompt``.
    """
    assert _agent_instructions_from_spec(None) is None
    assert _agent_instructions_from_spec(AgentSpec(spec_version=1, name="s")) is None
    assert (
        _agent_instructions_from_spec(AgentSpec(spec_version=1, name="s", instructions="  \n\t "))
        is None
    )


def test_composition_delivers_both_texts_with_framework_policy_last() -> None:
    """The crux: instructions AND the routed-spawn note both survive.

    This is the failure mode that stalled the earlier attempt at this fix — the
    note owned the single additive slot, so the two texts collided and one
    overwrote the other. Ordering follows the repo's existing rule in
    ``append_framework_instructions``: user-authored first, framework last, so
    framework policy has the final word.
    """
    composed = _compose_native_instructions(
        AgentSpec(spec_version=1, name="sparrow", instructions=_INSTRUCTIONS),
        _NOTE,
    )

    assert composed is not None
    assert _INSTRUCTIONS in composed
    assert _NOTE in composed
    assert composed.index(_INSTRUCTIONS) < composed.index(_NOTE)
    # Separated by a blank line so neither text reads as a continuation of the
    # other once the harness concatenates it onto its own system prompt.
    assert composed == f"{_INSTRUCTIONS}\n\n{_NOTE}"


def test_instructions_alone_when_the_session_has_no_framework_note() -> None:
    """A pinned session gets its instructions with no framework framing bolted on.

    Pinned sessions never receive the routed-spawn note (their spawns cannot
    cross harness families), and this is the case #3530 actually reports: before
    the fix the flag was absent entirely, because the note was its only source.
    """
    spec = AgentSpec(spec_version=1, name="sparrow", instructions=_INSTRUCTIONS)

    assert _compose_native_instructions(spec, None) == _INSTRUCTIONS


def test_a_lone_framework_note_passes_through_byte_identically() -> None:
    """No instructions → the note verbatim, NOT a normalized copy of it.

    The launch sites treat "a session contributing nothing extra keeps an argv
    byte-identical to before" as an explicit invariant. The routed-spawn note
    ends in a space (``_mcp_discovery_note``), so routing it through
    ``append_framework_instructions`` — which ``.strip()``s framework entries —
    would shift the argv by a byte and quietly break that invariant. Hence the
    composer's short-circuit.
    """
    note_with_trailing_space = f"{_NOTE} "

    composed = _compose_native_instructions(
        AgentSpec(spec_version=1, name="sparrow"), note_with_trailing_space
    )

    assert composed == note_with_trailing_space


def test_nothing_at_all_composes_to_none() -> None:
    """Neither source → ``None``, so the launch omits the channel entirely.

    Guards the opposite regression from the one above: the fix must not begin
    emitting an empty ``--append-system-prompt`` (or an empty
    ``developer_instructions`` key) on plain sessions.
    """
    assert _compose_native_instructions(None, None) is None
    assert _compose_native_instructions(AgentSpec(spec_version=1, name="s"), None) is None
    assert _compose_native_instructions(None, "", "   ") is None
