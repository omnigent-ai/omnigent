"""Bounded Git argv parsing, separate from pflag-style forge CLIs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GitArguments:
    """Preserve value ownership, repeated options, and the operand separator."""

    options: tuple[tuple[str, str], ...]
    operands: tuple[str, ...]

    def values(self, name: str) -> list[str]:
        return [value for option, value in self.options if option == name]

    def has(self, name: str) -> bool:
        return any(option == name for option, _ in self.options)


def parse_git_arguments(
    args: list[str],
    options: dict[str, tuple[str, bool]],
    optional_values: frozenset[str] = frozenset(),
) -> GitArguments | None:
    """Consume known options once; optional values require the long ``=`` form."""
    parsed: list[tuple[str, str]] = []
    operands: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        index += 1
        if token == "--":
            operands.extend(args[index:])
            break
        if not token.startswith("-") or token == "-":
            operands.append(token)
            continue
        long_option = token.startswith("--")
        remainder = token if long_option else token[1:]
        while remainder:
            if long_option:
                flag, equal, value = remainder.partition("=")
                remainder = ""
            else:
                flag, remainder = "-" + remainder[0], remainder[1:]
                equal, value = "", remainder
            spec = options.get(flag)
            if spec is None:
                return None
            name, takes_value = spec
            if takes_value:
                if not equal and (long_option or not remainder):
                    if index == len(args):
                        return None
                    value = args[index]
                    index += 1
                remainder = ""
            elif name in optional_values and long_option:
                value = value if equal else ""
            elif equal:
                return None
            else:
                value = ""
            parsed.append((name, value))
    return GitArguments(tuple(parsed), tuple(operands))
