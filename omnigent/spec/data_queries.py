"""Parse the agent spec's top-level ``data_queries:`` block.

The block declares which tool calls run queries or read data sources, so
clients can show them (Sources, notebook export, SQL cells) without guessing
from tool names. It is strict on purpose: a typo'd key would otherwise drop a
rule silently and hide the queries it was written to surface.

Example::

    data_queries:
      builtins: true
      rules:
        - name: warehouse_sql
          mcp: {server: warehouse, tool: execute_sql}
          sql: {argument: statement}
          source: {arguments: [catalog, schema]}
          row_count: {result: manifest.total_row_count}
        - name: mycli
          shell: mycli query
          value_flags: [--profile]
          sql: {positional: 0}
          source: {flag: --db}
"""

from __future__ import annotations

import re
import shlex
from typing import Literal

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.policies.builtins._shell import SHELL_INTERPRETERS, real_invocation_tokens
from omnigent.spec.types import DataQueriesSpec, DataQueryLocator, DataQueryRule

_BLOCK_KEYS = frozenset({"builtins", "rules"})
_RULE_KEYS = frozenset({"name", "mcp", "shell", "value_flags", "sql", "source", "row_count"})
_MCP_KEYS = frozenset({"server", "tool"})
_RULE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_FLAG = re.compile(r"--?[A-Za-z0-9?][A-Za-z0-9_.?-]*")
_MAX_RULES = 200
_MAX_PREFIX_WORDS = 4

_Field = Literal["sql", "source", "row_count"]
_Matcher = Literal["mcp", "shell"]

# Locator keys each value accepts, per matcher. SQL comes from the call itself,
# so a call is a query before it returns; row counts only from what it returned.
_LOCATOR_KEYS: dict[tuple[_Field, _Matcher], frozenset[str]] = {
    ("sql", "mcp"): frozenset({"argument"}),
    ("sql", "shell"): frozenset({"flag", "positional"}),
    ("source", "mcp"): frozenset({"argument", "arguments", "result", "value"}),
    ("source", "shell"): frozenset({"flag", "positional", "value"}),
    ("row_count", "mcp"): frozenset({"result"}),
    ("row_count", "shell"): frozenset({"result"}),
}


def _invalid(path: str, message: str) -> OmnigentError:
    return OmnigentError(f"{path}: {message}", code=ErrorCode.INVALID_INPUT)


def parse_data_queries(raw: object) -> DataQueriesSpec | None:
    """
    Parse the top-level ``data_queries:`` block.

    Shared by the native parser and the single-file YAML adapter so both
    reject the same input.

    :param raw: The raw ``data_queries:`` value, or ``None`` if absent, e.g.
        ``{"builtins": False, "rules": [{"name": "q", "mcp": {"tool": "run_sql"},
        "sql": {"argument": "query"}}]}``.
    :returns: The parsed block, or ``None`` when *raw* is ``None``.
    :raises OmnigentError: ``INVALID_INPUT`` naming the offending path when
        the block is malformed.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _invalid("data_queries", f"must be a mapping, got {type(raw).__name__}")
    unknown = sorted(str(key) for key in raw if key not in _BLOCK_KEYS)
    if unknown:
        raise _invalid("data_queries", f"unknown key(s) {unknown}; expected {sorted(_BLOCK_KEYS)}")
    builtins = raw.get("builtins", True)
    if not isinstance(builtins, bool):
        raise _invalid("data_queries.builtins", "must be a boolean")
    raw_rules = raw.get("rules", [])
    if raw_rules is None:
        raw_rules = []
    if not isinstance(raw_rules, list):
        raise _invalid("data_queries.rules", f"must be a list, got {type(raw_rules).__name__}")
    if len(raw_rules) > _MAX_RULES:
        raise _invalid("data_queries.rules", f"at most {_MAX_RULES} rules are allowed")
    rules: list[DataQueryRule] = []
    seen: set[str] = set()
    for index, raw_rule in enumerate(raw_rules):
        rule = _parse_rule(raw_rule, f"data_queries.rules[{index}]")
        if rule.name in seen:
            raise _invalid(
                f"data_queries.rules[{index}].name", f"duplicate rule name {rule.name!r}"
            )
        seen.add(rule.name)
        rules.append(rule)
    return DataQueriesSpec(rules=tuple(rules), builtins=builtins)


def _parse_rule(raw: object, path: str) -> DataQueryRule:
    if not isinstance(raw, dict):
        raise _invalid(path, f"must be a mapping, got {type(raw).__name__}")
    unknown = sorted(str(key) for key in raw if key not in _RULE_KEYS)
    if unknown:
        raise _invalid(path, f"unknown key(s) {unknown}; expected {sorted(_RULE_KEYS)}")
    name = raw.get("name")
    if not isinstance(name, str) or _RULE_NAME.fullmatch(name) is None:
        raise _invalid(
            f"{path}.name",
            "must be a name of letters, digits, '_', '.' or '-' (at most 64 characters)",
        )
    has_mcp, has_shell = "mcp" in raw, "shell" in raw
    if has_mcp == has_shell:
        raise _invalid(path, "needs exactly one of 'mcp' (a tool) or 'shell' (an argv prefix)")
    matcher: _Matcher = "mcp" if has_mcp else "shell"
    mcp_server = mcp_tool = None
    shell_prefix: tuple[str, ...] = ()
    if matcher == "mcp":
        mcp_server, mcp_tool = _parse_mcp(raw["mcp"], f"{path}.mcp")
        if "value_flags" in raw:
            raise _invalid(f"{path}.value_flags", "applies only to shell rules")
        value_flags: tuple[str, ...] = ()
    else:
        shell_prefix = _parse_shell_prefix(raw["shell"], f"{path}.shell")
        value_flags = _parse_flags(raw.get("value_flags", []), f"{path}.value_flags")
    if raw.get("sql") is None and raw.get("source") is None:
        raise _invalid(path, "needs a 'sql' or 'source' locator")
    return DataQueryRule(
        name=name,
        mcp_server=mcp_server,
        mcp_tool=mcp_tool,
        shell_prefix=shell_prefix,
        value_flags=value_flags,
        sql=_parse_locators(raw.get("sql"), "sql", matcher, f"{path}.sql"),
        source=_parse_locators(raw.get("source"), "source", matcher, f"{path}.source"),
        row_count=_parse_locators(raw.get("row_count"), "row_count", matcher, f"{path}.row_count"),
    )


def _parse_mcp(raw: object, path: str) -> tuple[str | None, str | None]:
    if not isinstance(raw, dict):
        raise _invalid(path, "must be a mapping with 'server' and/or 'tool'")
    unknown = sorted(str(key) for key in raw if key not in _MCP_KEYS)
    if unknown:
        raise _invalid(path, f"unknown key(s) {unknown}; expected {sorted(_MCP_KEYS)}")
    server, tool = raw.get("server"), raw.get("tool")
    for key, value in (("server", server), ("tool", tool)):
        if value is not None and (
            not isinstance(value, str) or not value or value != value.strip()
        ):
            raise _invalid(f"{path}.{key}", "must be a non-empty name without surrounding spaces")
    if server is None and tool is None:
        raise _invalid(path, "needs 'server', 'tool' or both")
    return server, tool


def _parse_shell_prefix(raw: object, path: str) -> tuple[str, ...]:
    if isinstance(raw, str):
        try:
            words = shlex.split(raw)
        except ValueError as exc:
            raise _invalid(path, f"is not a valid command prefix ({exc})") from None
    elif isinstance(raw, list) and all(isinstance(word, str) for word in raw):
        words = list(raw)
    else:
        raise _invalid(path, "must be a command prefix string or a list of words")
    if not words or not 1 <= len(words) <= _MAX_PREFIX_WORDS:
        raise _invalid(path, f"must have 1 to {_MAX_PREFIX_WORDS} words")
    for word in words:
        if not word or any(char.isspace() for char in word) or word.startswith("-"):
            raise _invalid(path, f"word {word!r} must be a command or subcommand name, not a flag")
    if "/" in words[0] or "=" in words[0]:
        raise _invalid(path, f"first word {words[0]!r} must be a bare command name")
    if _is_wrapper(words[0]):
        raise _invalid(
            path,
            f"first word {words[0]!r} is a wrapper or shell, which matching reads through; "
            "start the prefix at the command it runs",
        )
    return tuple(words)


def _is_wrapper(word: str) -> bool:
    """Whether shell matching strips or unwraps *word* (``env``, ``sudo``, ``bash -c``)."""
    argv = [word, "command"]
    return real_invocation_tokens(argv) != argv or word in SHELL_INTERPRETERS or word == "eval"


def _parse_flags(raw: object, path: str) -> tuple[str, ...]:
    flags = [raw] if isinstance(raw, str) else raw
    if not isinstance(flags, list) or not all(isinstance(flag, str) for flag in flags):
        raise _invalid(path, "must be a flag or a list of flags")
    for flag in flags:
        if _FLAG.fullmatch(flag) is None:
            raise _invalid(path, f"{flag!r} is not a flag like '-c' or '--command'")
    return tuple(flags)


def _parse_locators(
    raw: object, field: _Field, matcher: _Matcher, path: str
) -> tuple[DataQueryLocator, ...]:
    if raw is None:
        return ()
    entries = raw if isinstance(raw, list) else [raw]
    if not entries:
        raise _invalid(path, "must not be an empty list")
    return tuple(
        _parse_locator(
            entry, field, matcher, f"{path}[{index}]" if isinstance(raw, list) else path
        )
        for index, entry in enumerate(entries)
    )


def _parse_locator(raw: object, field: _Field, matcher: _Matcher, path: str) -> DataQueryLocator:
    allowed = _LOCATOR_KEYS[(field, matcher)]
    if not isinstance(raw, dict) or len(raw) != 1:
        raise _invalid(path, f"must be a mapping with exactly one of {sorted(allowed)}")
    ((key, value),) = raw.items()
    if key not in allowed:
        raise _invalid(
            path, f"{key!r} can't locate a {matcher} rule's {field}; use one of {sorted(allowed)}"
        )
    if key == "argument":
        return DataQueryLocator(argument=_parse_path(value, f"{path}.argument", allow_root=False))
    if key == "arguments":
        if not isinstance(value, list) or not value:
            raise _invalid(f"{path}.arguments", "must be a non-empty list of argument paths")
        return DataQueryLocator(
            arguments=tuple(
                _parse_path(part, f"{path}.arguments[{index}]", allow_root=False)
                for index, part in enumerate(value)
            )
        )
    if key == "result":
        return DataQueryLocator(result=_parse_path(value, f"{path}.result", allow_root=True))
    if key == "flag":
        flags = _parse_flags(value, f"{path}.flag")
        if not flags:
            raise _invalid(f"{path}.flag", "must name at least one flag")
        return DataQueryLocator(flag=flags)
    if key == "positional":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _invalid(f"{path}.positional", "must be a non-negative integer")
        return DataQueryLocator(positional=value)
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{path}.value", "must be a non-empty string")
    return DataQueryLocator(value=value)


def _parse_path(raw: object, path: str, *, allow_root: bool) -> str:
    if not isinstance(raw, str):
        raise _invalid(path, "must be a dotted path string")
    if raw == "" and allow_root:
        return raw
    if not raw or any(not part for part in raw.split(".")):
        raise _invalid(path, f"{raw!r} is not a dotted path like 'params.sql'")
    return raw
