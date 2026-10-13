"""Command segmentation preserves groups and quoted arguments."""

import pytest

from omnigent.policies.builtins._shell import UnsupportedShellSyntax, split_command_segments


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
    "command,completed",
    [
        ("echo ready # today's status\necho done", ["echo ready", "echo done"]),
        ("# status $(with delimiters\necho done", ["echo done"]),
        ("echo ready;# status\necho done", ["echo ready", "echo done"]),
        ("echo \\\n# status\necho done", ["echo done"]),
        ("echo \\\nready", []),
        ("cat <<DATA\ntoday's status\nDATA\necho done", ["echo done"]),
        ("cat <<'DATA'\nplain text\nDATA", []),
        ("cat <<-DATA\n\tplain text\n\tDATA", []),
        ("cat <<<text", []),
        ("echo $(echo one)", []),
        ('echo "$(echo one)"', []),
        ("echo `echo one`", []),
        ('echo "`echo one`"', []),
        ("echo $'text'", []),
        ('echo $"text"', []),
    ],
)
@pytest.mark.parametrize("prefix", ["", "echo first; "])
def test_unsupported_shell_syntax_requires_approval(
    command: str, completed: list[str], prefix: str
) -> None:
    with pytest.raises(UnsupportedShellSyntax, match=r"requires? approval") as error:
        split_command_segments(prefix + command)
    expected = ["echo first", *completed] if prefix else completed
    assert error.value.completed_segments == expected


@pytest.mark.parametrize(
    "command,completed",
    [
        ("echo $(echo one); echo next", ["echo next"]),
        ('echo "$(echo one)"; echo next', ["echo next"]),
        ("echo $(echo $(echo one)); echo next", ["echo next"]),
        ("echo $(echo ')'); echo next", ["echo next"]),
        ("echo $(echo ready # unmatched )\n); echo next", ["echo next"]),
        ("echo `echo one`; echo next", ["echo next"]),
        ("echo $'one\\'two'; echo next", ["echo next"]),
        ('echo $"one;two"; echo next', ["echo next"]),
        ("echo \\\nready; echo next", ["echo next"]),
        ("cat <<<text; echo next", ["echo next"]),
        ("cat <<'DATA'; echo same\nquoted ) body\nDATA\necho next", ["echo same", "echo next"]),
        ("cat <<-DATA\n\tplain text\n\tDATA\necho next", ["echo next"]),
        ("cat <<ONE <<TWO\nfirst\nONE\nsecond\nTWO\necho next", ["echo next"]),
        ("echo $(echo one", []),
        ("echo $'unclosed", []),
        ("cat <<DATA\nunterminated body", []),
        ("cat <<E'O'F\nopaque body", []),
        ("echo $(case x in x) echo ready;; esac); echo next", []),
        ("echo $(cat <<DATA\nopaque body\nDATA\n); echo next", []),
    ],
)
def test_opaque_regions_preserve_only_completed_segments(
    command: str, completed: list[str]
) -> None:
    with pytest.raises(UnsupportedShellSyntax) as error:
        split_command_segments("echo first; " + command)
    assert error.value.completed_segments == ["echo first", *completed]


@pytest.mark.parametrize(
    "command",
    [
        'echo "# text"',
        "echo '# text'",
        r"echo \#text",
        "echo word#suffix",
        'echo "<<DATA"',
        "echo 'line\\\nbreak'",
        "echo '$(text)'",
        "echo '`text`'",
        r'echo "\$(text)"',
        r"echo \`text\`",
    ],
)
def test_literal_comment_and_redirection_characters_remain_arguments(command: str) -> None:
    assert split_command_segments(command) == [command]


@pytest.mark.parametrize("opening,closing", [("$(", ")"), ("`", "`")])
def test_literal_substitution_delimiters_preserve_command_boundaries(
    opening: str, closing: str
) -> None:
    first, last = f"echo '{opening}'", f"echo '{closing}'"
    assert split_command_segments(f"{first}; echo middle; {last}") == [first, "echo middle", last]
