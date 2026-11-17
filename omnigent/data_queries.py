"""Recognize tool calls that run data queries or read data sources.

One classifier for every client that shows an agent's data work: the Sources
panel, notebook export and SQL cells. Given a recorded tool call (name,
arguments, result) it returns a :class:`DataQuery` or ``None``, using the
agent spec's ``data_queries:`` rules first and then built-in patterns for
common SQL CLIs (``psql -c``, ``bq query``, ``duckdb -c`` and others).

Classification is display metadata only. It reads what a call recorded and
never runs, authorizes, rewrites or sandboxes anything: a query run through a
CLI stays an ordinary shell tool call under the shell tool's policies.
"""

from __future__ import annotations

import functools
import json
import re
import shlex
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Literal

from omnigent.policies.builtins._shell import (
    MAX_SHELL_NESTING,
    SHELL_TOOLS,
    UnsupportedShellSyntax,
    real_invocation_tokens,
    split_command_segments,
    unwrap_shell_command,
)
from omnigent.spec.types import DataQueriesSpec, DataQueryLocator, DataQueryRule

DataQueryKind = Literal["query", "source"]
_Origin = Literal["spec", "builtin"]

# Harness shell tools beyond SHELL_TOOLS that carry a ``command``/``cmd`` argument.
_EXTRA_SHELL_TOOLS = frozenset({"exec_command", "run_command"})
# Flags that make a CLI print usage or its version instead of running anything.
_INFORMATIONAL_FLAGS = frozenset({"--help", "--version", "-?"})
# Longest command text classified; longer commands aren't scanned.
_MAX_COMMAND_CHARS = 32_000
_MAX_RESULT_DEPTH = 4
# Envelope fields a result's payload sits under (MCP content, shell envelopes).
_RESULT_ENVELOPE_KEYS = ("structuredContent", "content", "stdout", "result", "output", "text")
# ``scheme://user:password@host`` and ``password=...`` in connection strings.
# The password runs to the last ``@`` of the word, so an unencoded ``@`` or ``/`` stays masked.
_URL_PASSWORD = re.compile(r"(://[^/:@\s]*):\S*@")
_KEYWORD_PASSWORD = re.compile(r"(?i)\b(password|passwd|pwd)\s*=\s*(?:'[^']*'|\"[^\"]*\"|\S+)")
# A redirection word: ``>``, ``2>>``, ``&>``, ``<``, ``2>&1``, optionally with its target attached.
_REDIRECTION = re.compile(r"(?:\d+|&)?(?:>>|>&|<&|<>|>\||>|<)(?P<target>.*)", re.DOTALL)
_SHORT_FLAG_BUNDLE = re.compile(r"-[A-Za-z]{2,}")
_SERVER_TOOL = re.compile(r"([A-Za-z0-9](?:[\w-]*?[A-Za-z0-9])?)__([A-Za-z0-9].*)", re.DOTALL)


@dataclass(frozen=True)
class DataQuery:
    """
    A tool call recognized as a data query, or as a read of a data source.

    :param kind: ``"query"`` when the call carries SQL, ``"source"`` when it
        only reads a data source (``list_tables``, ``bq show``).
    :param matcher: ``"mcp"`` for a call matched by tool name, ``"shell"`` for
        a shell command matched by its argv prefix.
    :param sql: The SQL text exactly as the call passed it, or ``None``.
    :param source: The connection, database or table it names, with any
        password redacted, e.g. ``"analytics"``; ``None`` when not stated.
    :param row_count: Rows the result reports, or ``None`` when it doesn't.
    :param rule: Name of the rule that matched, e.g. ``"psql"``.
    :param origin: ``"spec"`` for an agent spec rule, ``"builtin"`` for a
        built-in CLI pattern.
    """

    kind: DataQueryKind
    matcher: Literal["mcp", "shell"]
    sql: str | None
    source: str | None
    row_count: int | None
    rule: str
    origin: _Origin

    def to_dict(self) -> dict[str, object]:
        """Return the JSON-safe form, e.g. ``{"kind": "query", "sql": "SELECT 1", ...}``."""
        return {
            "kind": self.kind,
            "matcher": self.matcher,
            "sql": self.sql,
            "source": self.source,
            "row_count": self.row_count,
            "rule": self.rule,
            "origin": self.origin,
        }


def _shell_rule(
    name: str,
    prefix: tuple[str, ...],
    *,
    sql: tuple[str, ...] | int,
    source: tuple[DataQueryLocator, ...] = (),
    value_flags: tuple[str, ...] = (),
    row_count: tuple[DataQueryLocator, ...] = (),
    bundled_short_flags: bool = False,
) -> DataQueryRule:
    sql_locator = (
        DataQueryLocator(positional=sql) if isinstance(sql, int) else DataQueryLocator(flag=sql)
    )
    return DataQueryRule(
        name=name,
        shell_prefix=prefix,
        value_flags=value_flags,
        sql=(sql_locator,),
        source=source,
        row_count=row_count,
        bundled_short_flags=bundled_short_flags,
    )


def _flag(*names: str) -> DataQueryLocator:
    return DataQueryLocator(flag=names)


def _positional(index: int) -> DataQueryLocator:
    return DataQueryLocator(positional=index)


_PSQL_FOOTER = DataQueryLocator(output_pattern=r"(?m)^\((\d+) rows?\)\s*$")
_MYSQL_FOOTER = DataQueryLocator(output_pattern=r"(?m)^(\d+) rows? in set\b")
# ``--format=json`` output: a JSON array with one element per row.
_JSON_ROWS = DataQueryLocator(result="")
_MYSQL_VALUE_FLAGS = (
    "-h",
    "--host",
    "-P",
    "--port",
    "-u",
    "--user",
    "-S",
    "--socket",
    "-D",
    "--database",
)
_TRINO_VALUE_FLAGS = (
    "--server",
    "--catalog",
    "--schema",
    "--user",
    "--file",
    "-f",
    "--output-format",
    "--session",
    "--source",
    "--client-tags",
    "--access-token",
    "--timezone",
)
_CLICKHOUSE_VALUE_FLAGS = (
    "-h",
    "--host",
    "--port",
    "-u",
    "--user",
    "--password",
    "-d",
    "--database",
    "-f",
    "--format",
    "-C",
    "--config-file",
)

# Built-in patterns for SQL CLIs, matched after the spec's rules. Each needs its
# SQL on the command line, so an interactive session or ``--help`` isn't a query.
BUILTIN_RULES: tuple[DataQueryRule, ...] = (
    _shell_rule(
        "psql",
        ("psql",),
        sql=("-c", "--command"),
        source=(_flag("-d", "--dbname"), _positional(0)),
        value_flags=(
            "-h",
            "--host",
            "-p",
            "--port",
            "-U",
            "--username",
            "-f",
            "--file",
            "-v",
            "--set",
            "--variable",
            "-o",
            "--output",
            "-P",
            "--pset",
            "-F",
            "--field-separator",
            "-R",
            "--record-separator",
            "-T",
            "--table-attr",
            "-L",
            "--log-file",
        ),
        row_count=(_PSQL_FOOTER,),
        bundled_short_flags=True,
    ),
    _shell_rule(
        "bq-query",
        ("bq", "query"),
        sql=0,
        source=(_flag("--dataset_id"), _flag("--project_id")),
        value_flags=(
            "--project_id",
            "--dataset_id",
            "--location",
            "--format",
            "--max_rows",
            "-n",
            "--destination_table",
            "--parameter",
            "--job_id",
            "--api",
        ),
        row_count=(_JSON_ROWS,),
    ),
    _shell_rule(
        "duckdb",
        ("duckdb",),
        sql=("-c", "-s"),
        source=(_positional(0),),
        value_flags=("-cmd", "-init", "-separator", "-nullvalue", "-newline"),
        row_count=(_JSON_ROWS,),
    ),
    _shell_rule(
        "sqlite3",
        ("sqlite3",),
        sql=1,
        source=(_positional(0),),
        value_flags=(
            "-cmd",
            "-init",
            "-separator",
            "-newline",
            "-nullvalue",
            "-vfs",
            "-maxsize",
            "-mmap",
            "-heap",
        ),
        row_count=(_JSON_ROWS,),
    ),
    _shell_rule(
        "mysql",
        ("mysql",),
        sql=("-e", "--execute"),
        source=(_flag("-D", "--database"), _positional(0)),
        value_flags=_MYSQL_VALUE_FLAGS,
        row_count=(_MYSQL_FOOTER,),
    ),
    _shell_rule(
        "mariadb",
        ("mariadb",),
        sql=("-e", "--execute"),
        source=(_flag("-D", "--database"), _positional(0)),
        value_flags=_MYSQL_VALUE_FLAGS,
        row_count=(_MYSQL_FOOTER,),
    ),
    _shell_rule(
        "snowsql",
        ("snowsql",),
        sql=("-q", "--query"),
        source=(_flag("-d", "--dbname"), _flag("-c", "--connection")),
        value_flags=(
            "-a",
            "--accountname",
            "-u",
            "--username",
            "-s",
            "--schemaname",
            "-r",
            "--rolename",
            "-w",
            "--warehouse",
            "-h",
            "--host",
            "-p",
            "--port",
            "-f",
            "--filename",
            "-o",
            "--option",
            "-D",
            "--variable",
        ),
    ),
    _shell_rule(
        "clickhouse-client",
        ("clickhouse", "client"),
        sql=("-q", "--query"),
        source=(_flag("-d", "--database"),),
        value_flags=_CLICKHOUSE_VALUE_FLAGS,
    ),
    _shell_rule(
        "clickhouse-client",
        ("clickhouse-client",),
        sql=("-q", "--query"),
        source=(_flag("-d", "--database"),),
        value_flags=_CLICKHOUSE_VALUE_FLAGS,
    ),
    _shell_rule(
        "trino",
        ("trino",),
        sql=("--execute",),
        source=(_flag("--catalog"), _flag("--server")),
        value_flags=_TRINO_VALUE_FLAGS,
    ),
    _shell_rule(
        "presto",
        ("presto",),
        sql=("--execute",),
        source=(_flag("--catalog"), _flag("--server")),
        value_flags=_TRINO_VALUE_FLAGS,
    ),
)


@functools.cache
def shell_tool_names() -> frozenset[str]:
    """Every harness's shell tool name, e.g. ``sys_os_shell``, ``Bash`` or ``shell``."""
    from omnigent.harness_plugins import harness_capabilities

    declared = {
        capabilities.shell_tool_name
        for capabilities in harness_capabilities().values()
        if capabilities.shell_tool_name
    }
    return SHELL_TOOLS | _EXTRA_SHELL_TOOLS | frozenset(declared)


def split_mcp_tool_name(name: str) -> tuple[str | None, str]:
    """
    Split a tool name into its MCP server and tool.

    ``mcp__warehouse__run_sql`` and ``warehouse__run_sql`` give
    ``("warehouse", "run_sql")``; Omnigent's own server is transparent, so
    ``mcp__omnigent__sys_os_shell`` gives ``(None, "sys_os_shell")``.

    :param name: The recorded tool name.
    :returns: ``(server, tool)``; ``server`` is ``None`` for an unprefixed tool.
    """
    bare = name.removeprefix("mcp__")
    match = _SERVER_TOOL.fullmatch(bare)
    if match is None:
        return None, name
    server, tool = match.group(1), match.group(2)
    if server == "omnigent":
        return split_mcp_tool_name(tool)
    return server, tool


def active_rules(spec: DataQueriesSpec | None) -> tuple[tuple[DataQueryRule, _Origin], ...]:
    """
    The rules a classification tries, in precedence order, with each one's origin.

    :param spec: The agent's ``data_queries:`` block, or ``None`` for none.
    :returns: The spec's rules (origin ``"spec"``), then the built-ins (origin
        ``"builtin"``) unless the spec turned them off.
    """
    spec_origin: _Origin = "spec"
    builtin_origin: _Origin = "builtin"
    spec_rules = tuple((rule, spec_origin) for rule in spec.rules) if spec else ()
    if spec is not None and not spec.builtins:
        return spec_rules
    return spec_rules + tuple((rule, builtin_origin) for rule in BUILTIN_RULES)


def classify_tool_call(
    name: str,
    arguments: Mapping[str, object] | str | None,
    result: str | None = None,
    *,
    spec: DataQueriesSpec | None = None,
) -> DataQuery | None:
    """
    Classify one recorded tool call as a data query, a data-source read, or neither.

    Reads only what the call recorded; nothing is executed.

    :param name: Tool name as recorded, e.g. ``"Bash"`` or ``"mcp__warehouse__run_sql"``.
    :param arguments: The call's arguments, as a mapping or the recorded JSON
        string, e.g. ``{"command": "psql -d analytics -c 'SELECT 1'"}``.
    :param result: The call's recorded output, or ``None`` while it runs.
    :param spec: The agent's ``data_queries:`` block; ``None`` applies the
        built-in patterns only.
    :returns: The first match in precedence order, or ``None``.
    """
    args = _arguments(arguments)
    rules = active_rules(spec)
    server, tool = split_mcp_tool_name(name)
    for rule, origin in rules:
        if not rule.is_shell and _mcp_rule_matches(rule, name, server, tool):
            query = _mcp_query(rule, origin, args, result)
            if query is not None:
                return query
    if not _is_shell_tool(name, server, tool):
        return None
    shell_rules = [(rule, origin) for rule, origin in rules if rule.is_shell]
    if not shell_rules:
        return None
    for argv in _shell_invocations(args):
        for rule, origin in shell_rules:
            query = _shell_query(rule, origin, argv, result)
            if query is not None:
                return query
    return None


def _arguments(arguments: Mapping[str, object] | str | None) -> Mapping[str, object]:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (ValueError, RecursionError):
            return {}
    return arguments if isinstance(arguments, Mapping) else {}


def _is_shell_tool(name: str, server: str | None, tool: str) -> bool:
    names = shell_tool_names()
    return name in names or (server is None and tool in names)


def _mcp_rule_matches(rule: DataQueryRule, name: str, server: str | None, tool: str) -> bool:
    if rule.mcp_server is not None and rule.mcp_server != server:
        return False
    return rule.mcp_tool is None or rule.mcp_tool in (tool, name)


def _mcp_query(
    rule: DataQueryRule, origin: _Origin, args: Mapping[str, object], result: str | None
) -> DataQuery | None:
    documents = _ResultDocuments(result)
    sql = _first(rule.sql, lambda locator: _mcp_text(locator, args, documents))
    if rule.sql and sql is None:
        return None
    source = _first(rule.source, lambda locator: _mcp_text(locator, args, documents))
    return _query(rule, origin, "mcp", sql, source, documents)


def _shell_query(
    rule: DataQueryRule, origin: _Origin, argv: list[str], result: str | None
) -> DataQuery | None:
    parsed = _parse_argv(rule, argv)
    if parsed is None:
        return None
    flags, positionals = parsed
    if any(name in _INFORMATIONAL_FLAGS for name, _ in flags):
        return None

    def locate(locator: DataQueryLocator) -> str | None:
        if locator.flag:
            values = [value for name, value in flags if name in locator.flag and value.strip()]
            return _join_statements(values) if values else None
        if locator.positional is not None:
            if locator.positional < len(positionals) and positionals[locator.positional].strip():
                return positionals[locator.positional]
            return None
        return locator.value

    sql = _first(rule.sql, locate)
    if rule.sql and sql is None:
        return None
    source = _first(rule.source, locate)
    return _query(rule, origin, "shell", sql, source, _ResultDocuments(result))


def _query(
    rule: DataQueryRule,
    origin: _Origin,
    matcher: Literal["mcp", "shell"],
    sql: str | None,
    source: str | None,
    documents: _ResultDocuments,
) -> DataQuery:
    row_count = None
    for locator in rule.row_count:
        row_count = documents.row_count(locator)
        if row_count is not None:
            break
    return DataQuery(
        kind="query" if sql is not None else "source",
        matcher=matcher,
        sql=sql,
        source=_redact(source) if source is not None else None,
        row_count=row_count,
        rule=rule.name,
        origin=origin,
    )


def _first(
    locators: tuple[DataQueryLocator, ...], locate: Callable[[DataQueryLocator], str | None]
) -> str | None:
    for locator in locators:
        value = locate(locator)
        if value is not None:
            return value
    return None


def _mcp_text(
    locator: DataQueryLocator, args: Mapping[str, object], documents: _ResultDocuments
) -> str | None:
    if locator.argument is not None:
        return _text(_lookup(args, locator.argument))
    if locator.arguments:
        parts = [_text(_lookup(args, path)) for path in locator.arguments]
        joined = ".".join(part.strip() for part in parts if part is not None)
        return joined or None
    if locator.result is not None:
        return documents.text(locator.result)
    return locator.value


def _text(value: object) -> str | None:
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return None


def _lookup(document: object, path: str) -> object:
    """The value at dotted *path* in *document*, or ``None``; ``""`` is the document."""
    if path == "":
        return document
    if isinstance(document, Mapping) and path in document:
        return document[path]
    current = document
    for part in path.split("."):
        if isinstance(current, Mapping):
            current = current.get(part)
        elif isinstance(current, list) and _is_ascii_digits(part) and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


def _is_ascii_digits(text: str) -> bool:
    return text.isascii() and text.isdecimal()


def _join_statements(values: list[str]) -> str:
    """Repeated SQL flags (``psql -c a -c b``) run in order; keep them one script."""
    if len(values) == 1:
        return values[0]
    return "\n".join(
        value if value.rstrip().endswith(";") else f"{value.rstrip()};" for value in values
    )


def _redact(source: str) -> str:
    return _KEYWORD_PASSWORD.sub(r"\1=***", _URL_PASSWORD.sub(r"\1:***@", source))


class _ResultDocuments:
    """A recorded result's JSON documents, from the outer envelope inward, parsed lazily."""

    def __init__(self, result: str | None) -> None:
        self._result = result
        self._documents: list[object] | None = None

    def _all(self) -> list[object]:
        if self._documents is None:
            self._documents = []
            if self._result is not None:
                _collect_documents(self._result, self._documents, 0)
        return self._documents

    def text(self, path: str) -> str | None:
        for document in self._all():
            value = _text(_lookup(document, path))
            if value is not None:
                return value
        return None

    def output_text(self) -> str:
        """The command's stdout when an envelope carries it, else the raw result."""
        for document in self._all():
            if isinstance(document, dict) and isinstance(document.get("stdout"), str):
                return document["stdout"]
        return self._result or ""

    def row_count(self, locator: DataQueryLocator) -> int | None:
        if locator.output_pattern is not None:
            matches = re.findall(locator.output_pattern, self.output_text())
            return int(matches[-1]) if matches else None
        if locator.result is None:
            return None
        for document in self._all():
            count = _count(_lookup(document, locator.result), whole=locator.result == "")
            if count is not None:
                return count
        return None


def _collect_documents(value: object, documents: list[object], depth: int) -> None:
    """Append *value*'s JSON documents to *documents*: itself, then its envelope payloads."""
    if depth > _MAX_RESULT_DEPTH:
        return
    if isinstance(value, str):
        text = value.strip()
        if text[:1] not in ("{", "["):
            return
        try:
            parsed = json.loads(text)
        except (ValueError, RecursionError):
            return
        _collect_documents(parsed, documents, depth + 1)
        return
    documents.append(value)
    if isinstance(value, dict):
        for key in _RESULT_ENVELOPE_KEYS:
            if key in value:
                _collect_documents(value[key], documents, depth + 1)
    elif isinstance(value, list) and value and all(_is_text_block(block) for block in value):
        for block in value:
            _collect_documents(block["text"], documents, depth + 1)


def _is_text_block(block: object) -> bool:
    return (
        isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    )


def _count(value: object, *, whole: bool) -> int | None:
    """A row count: a non-negative integer, a digit string, or a list of rows."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and _is_ascii_digits(value):
        return int(value)
    if isinstance(value, list) and (
        not whole or all(isinstance(row, dict | list) for row in value)
    ):
        # A whole result counts as rows only when it is a list of records.
        return len(value)
    return None


def _shell_invocations(args: Mapping[str, object]) -> Iterator[list[str]]:
    """Each real command a shell call runs, as argv, wrappers and env prefixes removed."""
    command = args.get("command", args.get("cmd"))
    if isinstance(command, list) and command and all(isinstance(word, str) for word in command):
        yield from _argv_invocations(list(command), 0)
    elif isinstance(command, str) and command.strip() and len(command) <= _MAX_COMMAND_CHARS:
        yield from _command_invocations(command, 0)


def _command_invocations(command: str, depth: int) -> Iterator[list[str]]:
    if depth > MAX_SHELL_NESTING:
        return
    command = _join_continuations(command)
    try:
        segments = split_command_segments(command)
    except UnsupportedShellSyntax as exc:
        # Command substitutions and heredocs aren't parsed; the plain segments before them are.
        segments = exc.completed_segments
    for segment in segments:
        try:
            words = shlex.split(segment)
        except ValueError:
            continue
        yield from _argv_invocations(words, depth)


def _argv_invocations(words: list[str], depth: int) -> Iterator[list[str]]:
    argv = real_invocation_tokens(words)
    if not argv:
        return
    inner = unwrap_shell_command(argv)
    if inner is not None:
        yield from _command_invocations(inner, depth + 1)
    elif not argv[0].startswith("-"):
        yield argv


def _join_continuations(command: str) -> str:
    """Drop backslash-newline continuations outside single quotes, as the shell does."""
    if "\\\n" not in command:
        return command
    out: list[str] = []
    in_single = in_double = False
    index = 0
    while index < len(command):
        char = command[index]
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "\\" and not in_single and command.startswith("\n", index + 1):
            index += 2
            continue
        elif char == "\\" and not in_single and index + 1 < len(command):
            out.append(command[index : index + 2])
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _parse_argv(
    rule: DataQueryRule, argv: list[str]
) -> tuple[list[tuple[str, str]], list[str]] | None:
    """
    Match *argv* against a shell rule's prefix and split it into flags and positionals.

    :returns: ``(flags, positionals)`` where *flags* lists ``(name, value)`` in
        order (``""`` for a flag without a value) and *positionals* are the
        words after the prefix, or ``None`` when the prefix doesn't match.
    """
    if argv[0].rsplit("/", 1)[-1] != rule.shell_prefix[0]:
        return None
    takes_value = set(rule.value_flags)
    for locator in (*rule.sql, *rule.source):
        takes_value.update(locator.flag)
    flags: list[tuple[str, str]] = []
    positionals: list[str] = []
    end_of_options = False
    index = 1
    while index < len(argv):
        word = argv[index]
        index += 1
        redirection = _REDIRECTION.fullmatch(word)
        if redirection is not None:
            # The shell's, not the command's: skip it and a separate target word.
            if not redirection.group("target"):
                index += 1
        elif end_of_options or word == "-" or not word.startswith("-"):
            positionals.append(word)
        elif word == "--":
            end_of_options = True
        elif word.startswith("--") and "=" in word:
            name, _, value = word.partition("=")
            flags.append((name, value))
        elif word in takes_value and index < len(argv):
            flags.append((word, argv[index]))
            index += 1
        elif (
            rule.bundled_short_flags
            and _SHORT_FLAG_BUNDLE.fullmatch(word)
            and f"-{word[-1]}" in takes_value
            and index < len(argv)
        ):
            # ``psql -tAc 'SELECT 1'``: the bundle's last flag takes the next word.
            flags.extend((f"-{letter}", "") for letter in word[1:-1])
            flags.append((f"-{word[-1]}", argv[index]))
            index += 1
        else:
            flags.append((word, ""))
    subcommands = rule.shell_prefix[1:]
    if tuple(positionals[: len(subcommands)]) != subcommands:
        return None
    return flags, positionals[len(subcommands) :]
