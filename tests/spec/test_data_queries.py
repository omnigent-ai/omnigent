"""Parsing the agent spec's top-level ``data_queries:`` block."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.spec.data_queries import parse_data_queries
from omnigent.spec.parser import parse
from omnigent.spec.types import DataQueriesSpec, DataQueryLocator, DataQueryRule


def test_absent_block_parses_to_none(tmp_path: Path) -> None:
    """A spec without the block keeps ``data_queries`` unset, so only built-ins apply."""
    (tmp_path / "config.yaml").write_text(yaml.dump({"spec_version": 1, "name": "plain"}))
    assert parse(tmp_path).data_queries is None


def test_bundle_block_parses_every_locator_shape(tmp_path: Path) -> None:
    """An MCP rule and a shell rule reach the spec with their locators in order."""
    config = {
        "spec_version": 1,
        "name": "analyst",
        "data_queries": {
            "builtins": False,
            "rules": [
                {
                    "name": "warehouse_sql",
                    "mcp": {"server": "warehouse", "tool": "execute_sql"},
                    "sql": {"argument": "statement"},
                    "source": {"arguments": ["catalog", "schema"]},
                    "row_count": {"result": "manifest.total_row_count"},
                },
                {
                    "name": "mycli",
                    "shell": "mycli query",
                    "value_flags": ["--profile"],
                    "sql": {"positional": 0},
                    "source": [{"flag": ["--db", "-d"]}, {"value": "default"}],
                },
            ],
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))

    assert parse(tmp_path).data_queries == DataQueriesSpec(
        builtins=False,
        rules=(
            DataQueryRule(
                name="warehouse_sql",
                mcp_server="warehouse",
                mcp_tool="execute_sql",
                sql=(DataQueryLocator(argument="statement"),),
                source=(DataQueryLocator(arguments=("catalog", "schema")),),
                row_count=(DataQueryLocator(result="manifest.total_row_count"),),
            ),
            DataQueryRule(
                name="mycli",
                shell_prefix=("mycli", "query"),
                value_flags=("--profile",),
                sql=(DataQueryLocator(positional=0),),
                source=(DataQueryLocator(flag=("--db", "-d")), DataQueryLocator(value="default")),
            ),
        ),
    )


def test_builtins_default_on_and_rules_optional() -> None:
    """``builtins`` defaults to on; a block may only set it."""
    assert parse_data_queries({}) == DataQueriesSpec()
    assert parse_data_queries({"builtins": False}) == DataQueriesSpec(builtins=False)


def test_shell_prefix_accepts_a_word_list() -> None:
    """``shell: [bq, query]`` and ``shell: "bq query"`` are the same prefix."""
    as_list = parse_data_queries(
        {"rules": [{"name": "q", "shell": ["bq", "query"], "sql": {"positional": 0}}]}
    )
    as_text = parse_data_queries(
        {"rules": [{"name": "q", "shell": "bq query", "sql": {"positional": 0}}]}
    )
    assert as_list == as_text
    assert as_list is not None and as_list.rules[0].shell_prefix == ("bq", "query")


_MCP = {"name": "q", "mcp": {"tool": "run_sql"}}
_SHELL = {"name": "q", "shell": "mycli"}


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        pytest.param(["rules"], r"data_queries: must be a mapping", id="not-a-mapping"),
        pytest.param({"rule": []}, r"unknown key\(s\) \['rule'\]", id="typo-top-level-key"),
        pytest.param({"builtins": "no"}, r"builtins: must be a boolean", id="builtins-string"),
        pytest.param({"rules": {"name": "q"}}, r"rules: must be a list", id="rules-mapping"),
        pytest.param({"rules": ["q"]}, r"rules\[0\]: must be a mapping", id="rule-string"),
        pytest.param(
            {"rules": [{**_MCP, "sql": {"argument": "q"}, "sqll": {}}]},
            r"rules\[0\]: unknown key\(s\) \['sqll'\]",
            id="typo-rule-key",
        ),
        pytest.param(
            {"rules": [{"mcp": {"tool": "t"}, "sql": {"argument": "q"}}]},
            r"rules\[0\]\.name: must be a name",
            id="missing-name",
        ),
        pytest.param(
            {"rules": [{**_MCP, "name": "has space", "sql": {"argument": "q"}}]},
            r"rules\[0\]\.name",
            id="name-with-space",
        ),
        pytest.param(
            {"rules": [{**_MCP, "sql": {"argument": "q"}}, {**_MCP, "sql": {"argument": "q"}}]},
            r"rules\[1\]\.name: duplicate rule name 'q'",
            id="duplicate-name",
        ),
        pytest.param(
            {"rules": [{"name": "q", "sql": {"argument": "q"}}]},
            r"needs exactly one of 'mcp'",
            id="no-matcher",
        ),
        pytest.param(
            {"rules": [{**_MCP, "shell": "psql", "sql": {"argument": "q"}}]},
            r"needs exactly one of 'mcp'",
            id="both-matchers",
        ),
        pytest.param(
            {"rules": [{"name": "q", "mcp": {}, "sql": {"argument": "q"}}]},
            r"mcp: needs 'server', 'tool' or both",
            id="empty-mcp",
        ),
        pytest.param(
            {"rules": [{"name": "q", "mcp": {"tools": "x"}, "sql": {"argument": "q"}}]},
            r"mcp: unknown key\(s\) \['tools'\]",
            id="typo-mcp-key",
        ),
        pytest.param(
            {"rules": [{"name": "q", "mcp": "run_sql", "sql": {"argument": "q"}}]},
            r"mcp: must be a mapping",
            id="mcp-string",
        ),
        pytest.param({"rules": [_MCP]}, r"needs a 'sql' or 'source' locator", id="no-locator"),
        pytest.param(
            {"rules": [{**_SHELL, "shell": "", "sql": {"positional": 0}}]},
            r"shell: must have 1 to 4 words",
            id="empty-prefix",
        ),
        pytest.param(
            {"rules": [{**_SHELL, "shell": "psql -c", "sql": {"positional": 0}}]},
            r"word '-c' must be a command or subcommand name",
            id="flag-in-prefix",
        ),
        pytest.param(
            {"rules": [{**_SHELL, "shell": "/usr/bin/psql", "sql": {"positional": 0}}]},
            r"must be a bare command name",
            id="path-in-prefix",
        ),
        pytest.param(
            {"rules": [{**_SHELL, "shell": "psql 'unterminated", "sql": {"positional": 0}}]},
            r"is not a valid command prefix",
            id="unbalanced-quote",
        ),
        pytest.param(
            {"rules": [{**_MCP, "value_flags": ["-h"], "sql": {"argument": "q"}}]},
            r"value_flags: applies only to shell rules",
            id="value-flags-on-mcp",
        ),
        pytest.param(
            {"rules": [{**_SHELL, "value_flags": ["h"], "sql": {"positional": 0}}]},
            r"'h' is not a flag",
            id="value-flag-without-dash",
        ),
        pytest.param(
            {"rules": [{**_MCP, "sql": {"flag": "-c"}}]},
            r"'flag' can't locate a mcp rule's sql",
            id="flag-on-mcp",
        ),
        pytest.param(
            {"rules": [{**_SHELL, "sql": {"argument": "query"}}]},
            r"'argument' can't locate a shell rule's sql",
            id="argument-on-shell",
        ),
        pytest.param(
            {"rules": [{**_MCP, "sql": {"result": "statement"}}]},
            r"'result' can't locate a mcp rule's sql",
            id="sql-from-result",
        ),
        pytest.param(
            {"rules": [{**_SHELL, "shell": "sudo psql", "sql": {"flag": "-c"}}]},
            r"first word 'sudo' is a wrapper or shell",
            id="wrapper-prefix",
        ),
        pytest.param(
            {"rules": [{**_SHELL, "shell": "bash", "sql": {"flag": "-c"}}]},
            r"first word 'bash' is a wrapper or shell",
            id="shell-prefix",
        ),
        pytest.param(
            {"rules": [{"name": "q", "mcp": {"server": " warehouse"}, "source": {"value": "x"}}]},
            r"mcp\.server: must be a non-empty name without surrounding spaces",
            id="padded-server",
        ),
        pytest.param(
            {"rules": [{**_MCP, "sql": {"value": "SELECT 1"}}]},
            r"'value' can't locate a mcp rule's sql",
            id="literal-sql",
        ),
        pytest.param(
            {"rules": [{**_MCP, "sql": {"argument": "q"}, "row_count": {"argument": "n"}}]},
            r"'argument' can't locate a mcp rule's row_count",
            id="row-count-from-arguments",
        ),
        pytest.param(
            {"rules": [{**_MCP, "sql": {"argument": "q", "result": "sql"}}]},
            r"sql: must be a mapping with exactly one of",
            id="two-keys-in-locator",
        ),
        pytest.param(
            {"rules": [{**_MCP, "sql": []}]},
            r"sql: must not be an empty list",
            id="empty-locator-list",
        ),
        pytest.param(
            {"rules": [{**_MCP, "sql": {"argument": "params..sql"}}]},
            r"'params\.\.sql' is not a dotted path",
            id="empty-path-segment",
        ),
        pytest.param(
            {"rules": [{**_MCP, "sql": {"argument": ""}}]},
            r"'' is not a dotted path",
            id="empty-argument-path",
        ),
        pytest.param(
            {"rules": [{**_MCP, "source": {"arguments": []}}]},
            r"arguments: must be a non-empty list",
            id="empty-arguments",
        ),
        pytest.param(
            {"rules": [{**_SHELL, "sql": {"positional": -1}}]},
            r"positional: must be a non-negative integer",
            id="negative-positional",
        ),
        pytest.param(
            {"rules": [{**_SHELL, "sql": {"positional": True}}]},
            r"positional: must be a non-negative integer",
            id="boolean-positional",
        ),
        pytest.param(
            {"rules": [{**_SHELL, "sql": {"flag": []}}]},
            r"flag: must name at least one flag",
            id="no-flag-names",
        ),
        pytest.param(
            {"rules": [{**_SHELL, "sql": {"positional": 0}, "source": {"value": " "}}]},
            r"value: must be a non-empty string",
            id="blank-source-value",
        ),
        pytest.param(
            {"rules": [{**_SHELL, "sql": {"positional": 0}}] * 201},
            r"at most 200 rules",
            id="too-many-rules",
        ),
    ],
)
def test_malformed_block_fails_with_its_path(raw: object, message: str) -> None:
    """Every malformed shape fails the load with ``INVALID_INPUT`` naming where it is."""
    with pytest.raises(OmnigentError, match=message) as exc:
        parse_data_queries(raw)
    assert exc.value.code == ErrorCode.INVALID_INPUT


def test_malformed_block_fails_the_bundle_load(tmp_path: Path) -> None:
    """A bundle with a broken block doesn't load with the rule silently dropped."""
    config = {"spec_version": 1, "name": "typo", "data_queries": {"rules": [{"name": "q"}]}}
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    with pytest.raises(OmnigentError, match=r"data_queries\.rules\[0\]"):
        parse(tmp_path)
