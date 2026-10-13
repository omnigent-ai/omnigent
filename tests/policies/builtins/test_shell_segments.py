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


@pytest.mark.parametrize(
    "first",
    [
        r"echo \"",
        r"echo \'",
        r'echo "a\"b"',
        r'echo "a\\"',
        r"echo 'a\'",
        r"echo one\;two",
        r"echo one\&two",
        r"echo one\|two",
        r"echo \(one\)",
        r"echo \{one\}",
    ],
)
def test_escaped_arguments_preserve_command_boundaries(first: str) -> None:
    assert split_command_segments(f"{first} && echo next") == [first, "echo next"]


@pytest.mark.parametrize(
    "command,expected",
    [
        ("echo dir/{one,two}; echo next", ["echo dir/{one,two}", "echo next"]),
        ("echo {one,two} && echo next", ["echo {one,two}", "echo next"]),
        ("echo ${VALUE}; echo next", ["echo ${VALUE}", "echo next"]),
        ("echo pre{ mid }post", ["echo pre{ mid }post"]),
        ("{ echo one;}; echo next", ["echo one", "echo next"]),
        ("{(echo one);}", ["echo one"]),
        (r"echo \ { text", [r"echo \ { text"]),
        ("echo ''{ text", ["echo ''{ text"]),
    ],
)
def test_braces_require_word_boundaries(command: str, expected: list[str]) -> None:
    assert split_command_segments(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "echo ready # today's status\necho done",
        "# status $(with delimiters\necho done",
        "echo ready;# status\necho done",
        "echo \\\n# status\necho done",
        "echo \\\nready",
        "cat <<DATA\ntoday's status\nDATA\necho done",
        "cat <<'DATA'\nplain text\nDATA",
        "cat <<-DATA\n\tplain text\n\tDATA",
        "cat <<<text",
    ],
)
def test_unsupported_shell_syntax_requires_approval(command: str) -> None:
    with pytest.raises(ValueError, match="require approval"):
        split_command_segments(command)


@pytest.mark.parametrize(
    "command",
    [
        'echo "# text"',
        "echo '# text'",
        r"echo \#text",
        "echo word#suffix",
        'echo "<<DATA"',
        "echo 'line\\\nbreak'",
    ],
)
def test_literal_comment_and_redirection_characters_remain_arguments(command: str) -> None:
    assert split_command_segments(command) == [command]
