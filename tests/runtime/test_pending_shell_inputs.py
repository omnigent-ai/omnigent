"""A shell mirror settles its own web input without inferring lost prompts."""

from collections.abc import Iterator

import pytest

from omnigent.runtime import pending_inputs


@pytest.fixture(autouse=True)
def clean_queue() -> Iterator[None]:
    pending_inputs.reset_for_tests()
    yield
    pending_inputs.reset_for_tests()


def record(text: str) -> str:
    return pending_inputs.record("conv_shell", [{"type": "input_text", "text": text}])


def queued_ids() -> list[str]:
    return [entry["pending_id"] for entry in pending_inputs.snapshot_for("conv_shell")]


@pytest.mark.parametrize("text", ["!ls -la", "! ls -la", "  !  ls   -la  "])
def test_shell_input_settles_only_its_matching_entry(text: str) -> None:
    older = record("still in flight")
    shell = record(text)
    later = record("next prompt")

    drained = pending_inputs.resolve_shell_command("conv_shell", "ls -la")

    assert drained is not None and drained.pending_id == shell
    assert queued_ids() == [older, later]


@pytest.mark.parametrize("text,command", [("ls", "ls"), ("!ls", "pwd"), ("!", "  ")])
def test_unmatched_shell_input_leaves_the_queue_unchanged(text: str, command: str) -> None:
    pending = record(text)

    assert pending_inputs.resolve_shell_command("conv_shell", command) is None
    assert queued_ids() == [pending]
    assert pending_inputs.resolve_shell_command("other_conversation", command) is None


def test_identical_commands_settle_in_queue_order_and_skip_held_entries() -> None:
    first = record("!echo hello")
    second = record("! echo hello")

    held = pending_inputs.resolve_shell_command("conv_shell", "echo hello", hold=True)
    following = pending_inputs.resolve_shell_command("conv_shell", "echo hello")

    assert held is not None and held.pending_id == first
    assert following is not None and following.pending_id == second
    assert pending_inputs.resolve_shell_command("conv_shell", "echo hello") is None
    pending_inputs.restore("conv_shell", held)
    assert queued_ids() == [first]
    retried = pending_inputs.resolve_shell_command("conv_shell", "echo hello", hold=True)
    assert retried is not None and retried.pending_id == first
    pending_inputs.release("conv_shell", retried)
    assert queued_ids() == []


def test_shell_match_strips_only_one_bang() -> None:
    plain = record("!echo hello")
    negated = record("!! echo hello")

    drained = pending_inputs.resolve_shell_command("conv_shell", "! echo hello")

    assert drained is not None and drained.pending_id == negated
    assert queued_ids() == [plain]
