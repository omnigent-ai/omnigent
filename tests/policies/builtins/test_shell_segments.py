"""Command segmentation preserves groups and quoted arguments."""

import pytest

from omnigent.policies.builtins._shell import split_command_segments


@pytest.mark.parametrize(
    "command,expected",
    [
        ("(echo one)", ["echo one"]),
        ("{ echo one; echo two; }", ["echo one", "echo two"]),
        ("((echo one))", ["echo one"]),
        ("echo 'one;two' && echo three", ["echo 'one;two'", "echo three"]),
        ('echo "(one){two}"; echo three', ['echo "(one){two}"', "echo three"]),
        ("echo one & echo two", ["echo one", "echo two"]),
        ("echo one\necho two", ["echo one", "echo two"]),
        ("echo $(echo one)", ["echo", "echo one"]),
    ],
)
def test_split_command_segments(command: str, expected: list[str]) -> None:
    assert split_command_segments(command) == expected
