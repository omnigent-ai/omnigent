"""Argument construction shared by SDK and native Pi launches."""

import pytest

from omnigent.pi_prompt import pi_prompt_args


@pytest.mark.parametrize(
    ("instructions", "mode", "expected"),
    [
        (None, "append", []),
        ("", "append", []),
        ("  Keep whitespace.  ", "append", ["--append-system-prompt", "  Keep whitespace.  "]),
        ("/session/role.md", "append", ["--append-system-prompt", "/session/role.md"]),
        (
            "/session/role.md",
            "replace",
            ["--system-prompt", "/session/role.md", "--append-system-prompt", ""],
        ),
    ],
)
def test_pi_prompt_args(instructions: str | None, mode: str, expected: list[str]) -> None:
    assert pi_prompt_args(instructions, mode) == expected


@pytest.mark.parametrize(
    ("instructions", "mode", "error"),
    [
        ("Role", "invalid", "must be 'append' or 'replace'"),
        (None, "replace", "requires non-empty instructions"),
        ("", "replace", "requires non-empty instructions"),
        (" \n ", "replace", "requires non-empty instructions"),
    ],
)
def test_pi_prompt_args_rejects_invalid_prompt(
    instructions: str | None, mode: str, error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        pi_prompt_args(instructions, mode)
