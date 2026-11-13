"""Native startup carries author text and stable guidance, without turn metadata."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.runner.app import (
    ResolvedSpec,
    _native_startup_instructions_from_spec,
)
from omnigent.runner.native.orchestration import _opencode_session_instructions_from_spec
from omnigent.runtime.prompt import WORKSPACE_IMAGE_INSTRUCTION
from omnigent.spec.types import AgentSpec, ExecutorSpec


def _spec(instructions: str | None) -> AgentSpec:
    """Build a minimal agent spec carrying *instructions*."""
    return AgentSpec(
        spec_version=1,
        name="claude_code",
        instructions=instructions,
        executor=ExecutorSpec(),
    )


@pytest.mark.parametrize(
    ("instructions", "expected"),
    [
        ("Be a concise assistant.", f"Be a concise assistant.\n\n{WORKSPACE_IMAGE_INSTRUCTION}"),
        (None, WORKSPACE_IMAGE_INSTRUCTION),
        ("   \n  ", WORKSPACE_IMAGE_INSTRUCTION),
    ],
    ids=["present", "absent", "whitespace-only"],
)
def test_native_startup_instructions_from_spec(instructions: str | None, expected: str) -> None:
    """Author text is preserved and every startup receives image guidance."""
    assert _native_startup_instructions_from_spec(_spec(instructions)) == expected


def test_native_startup_instructions_from_spec_none_spec() -> None:
    """A missing spec still receives stable image guidance."""
    assert _native_startup_instructions_from_spec(None) == WORKSPACE_IMAGE_INSTRUCTION


@pytest.mark.parametrize("has_spec", [False, True])
def test_opencode_startup_carries_image_guidance(has_spec: bool) -> None:
    """OpenCode's session snapshot carries guidance even without authored text."""
    result = _opencode_session_instructions_from_spec(_spec(None) if has_spec else None)
    assert result is not None
    assert result.count(WORKSPACE_IMAGE_INSTRUCTION) == 1


def test_native_startup_instructions_from_spec_resolved_wrapper() -> None:
    """A ResolvedSpec wrapper unwraps to the same instructions text."""
    wrapped = ResolvedSpec(spec=_spec("Be a concise assistant."), workdir=Path("/tmp"))
    assert _native_startup_instructions_from_spec(wrapped) == (
        f"Be a concise assistant.\n\n{WORKSPACE_IMAGE_INSTRUCTION}"
    )


def test_native_startup_instructions_from_spec_never_returns_stripped_form() -> None:
    """The original resolved text is preserved verbatim, not the stripped form."""
    padded = "  Keep leading/trailing whitespace exactly.  "
    assert _native_startup_instructions_from_spec(_spec(padded)) == (
        f"{padded}\n\n{WORKSPACE_IMAGE_INSTRUCTION}"
    )


# ─── Composed startup instructions (stable text, notes, memory guidance) ─


@pytest.fixture
def memory_guidance(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    """Stub the memory index fetch and record each call."""
    calls: list[tuple[object, ...]] = []

    async def _guidance(*args: object) -> str:
        calls.append(args)
        return "MEMORY GUIDANCE"

    monkeypatch.setattr("omnigent.runner.native.orchestration.memory_startup_guidance", _guidance)
    return calls


@pytest.mark.asyncio
async def test_startup_instructions_join_stable_text_notes_and_memory_in_order(
    memory_guidance: list[tuple[object, ...]],
) -> None:
    from omnigent.runner.native.orchestration import _native_startup_instructions

    spec = _spec("Be a concise assistant.")
    client = object()

    text = await _native_startup_instructions(spec, client, "sess", "ROUTING NOTE", None)

    assert text == (
        f"Be a concise assistant.\n\n{WORKSPACE_IMAGE_INSTRUCTION}"
        "\n\nROUTING NOTE\n\nMEMORY GUIDANCE"
    )
    assert memory_guidance == [(client, "sess", spec)]


@pytest.mark.asyncio
async def test_startup_instructions_without_a_spec_ask_the_server_nothing() -> None:
    """With no spec there are no memory tools, so the launch makes no memory request."""
    from omnigent.runner.native.orchestration import _native_startup_instructions

    class _NoCalls:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"unexpected server call: {name}")

    assert await _native_startup_instructions(
        None, _NoCalls(), "sess"
    ) == _native_startup_instructions_from_spec(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("instructions", ["  Keep this whitespace.  ", None])
async def test_startup_instructions_without_memory_are_unchanged(
    monkeypatch: pytest.MonkeyPatch, instructions: str | None
) -> None:
    """With memory unavailable the result is the pre-memory value, byte for byte."""
    from omnigent.runner.native.orchestration import _native_startup_instructions

    async def _no_memory(*_args: object) -> None:
        return None

    monkeypatch.setattr("omnigent.runner.native.orchestration.memory_startup_guidance", _no_memory)
    spec = _spec(instructions)

    assert await _native_startup_instructions(
        spec, None, "sess"
    ) == _native_startup_instructions_from_spec(spec)
