"""The shared data-query classifier: built-in SQL CLI patterns and spec rules."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from omnigent.data_queries import DataQuery, classify_tool_call, split_mcp_tool_name
from omnigent.spec.data_queries import parse_data_queries
from omnigent.spec.types import DataQueriesSpec


def _shell(
    command: str | list[str], result: str | None = None, **kwargs: object
) -> DataQuery | None:
    return classify_tool_call("Bash", {"command": command}, result, **kwargs)  # type: ignore[arg-type]


def _spec(*rules: dict[str, object], builtins: bool = True) -> DataQueriesSpec:
    spec = parse_data_queries({"builtins": builtins, "rules": list(rules)})
    assert spec is not None
    return spec


@pytest.mark.parametrize(
    ("command", "rule", "sql", "source"),
    [
        # psql
        ("psql -d analytics -c 'SELECT 1'", "psql", "SELECT 1", "analytics"),
        (
            "psql --dbname=analytics --command=\"SELECT count(*) FROM t WHERE name = 'a b'\"",
            "psql",
            "SELECT count(*) FROM t WHERE name = 'a b'",
            "analytics",
        ),
        (
            "PGPASSWORD=s3cret /opt/homebrew/bin/psql 'postgresql://me:s3cret@db/prod' "
            "-c 'SELECT 1'",
            "psql",
            "SELECT 1",
            "postgresql://me:***@db/prod",
        ),
        (
            "psql 'host=db password=s3cret dbname=shop' -c 'SELECT 1'",
            "psql",
            "SELECT 1",
            "host=db password=*** dbname=shop",
        ),
        (
            "psql 'postgres://me:p@ss/w0rd@db/prod' -c 'SELECT 1'",
            "psql",
            "SELECT 1",
            "postgres://me:***@db/prod",
        ),
        ("psql 'host=db passwd = s3cret' -c 'SELECT 1'", "psql", "SELECT 1", "host=db passwd=***"),
        ("env -i PATH=/bin psql -h db -U me shop -c 'select 1'", "psql", "select 1", "shop"),
        ("psql -tAc 'select 1' shop", "psql", "select 1", "shop"),
        ("psql -c 'select 1' 2>&1", "psql", "select 1", None),
        ("psql -c 'select 1' > out.txt shop", "psql", "select 1", "shop"),
        ("psql -c 'select 1' 2>/dev/null shop", "psql", "select 1", "shop"),
        ('psql -d db -c "it\'s" \\\n  -X', "psql", "it's", "db"),
        ("psql -c 'select 1' -c 'select 2;'", "psql", "select 1;\nselect 2;", None),
        ("sudo -u postgres psql -c \"SELECT 'it''s'\"", "psql", "SELECT 'it''s'", None),
        ('psql -c \'SELECT "Name" FROM "T"\'', "psql", 'SELECT "Name" FROM "T"', None),
        ('psql -c "SELECT \\"Name\\" FROM t"', "psql", 'SELECT "Name" FROM t', None),
        ("psql -c 'SELECT 1; SELECT 2' -- shop", "psql", "SELECT 1; SELECT 2", "shop"),
        ("bash -lc \"psql -c 'select 1'\"", "psql", "select 1", None),
        ("cd /tmp && psql -c 'select 1' | head -5", "psql", "select 1", None),
        ("timeout 30 psql -c 'select 1'", "psql", "select 1", None),
        ("psql -h db \\\n  -d shop \\\n  -c 'select 1'", "psql", "select 1", "shop"),
        ("psql -c 'select a\nfrom t'", "psql", "select a\nfrom t", None),
        # bq query
        ("bq query --use_legacy_sql=false 'SELECT 1'", "bq-query", "SELECT 1", None),
        (
            'bq --project_id my-proj --location=US query --format=json "SELECT * FROM ds.t"',
            "bq-query",
            "SELECT * FROM ds.t",
            "my-proj",
        ),
        ("bq query --dataset_id=ds --nouse_legacy_sql 'SELECT 1'", "bq-query", "SELECT 1", "ds"),
        # duckdb
        ('duckdb warehouse.duckdb -c "SELECT 42"', "duckdb", "SELECT 42", "warehouse.duckdb"),
        ("duckdb -csv -c 'select 1'", "duckdb", "select 1", None),
        ("duckdb -c 'select 1' > out.txt", "duckdb", "select 1", None),
        # sqlite3
        (
            "sqlite3 -header app.db 'SELECT * FROM users;'",
            "sqlite3",
            "SELECT * FROM users;",
            "app.db",
        ),
        ("sqlite3 -cmd '.mode csv' app.db \"select 1\"", "sqlite3", "select 1", "app.db"),
        ("sqlite3 app.db 'select 1' > dump.txt", "sqlite3", "select 1", "app.db"),
        # mysql / mariadb
        ("mysql -h db -u root -pSecret shop -e 'SELECT 1'", "mysql", "SELECT 1", "shop"),
        ("mysql --database=shop --execute='select 1'", "mysql", "select 1", "shop"),
        ("mariadb -D shop -e 'select 1'", "mariadb", "select 1", "shop"),
        # snowsql
        ("snowsql -c prod -q 'select 1'", "snowsql", "select 1", "prod"),
        ('snowsql -d ANALYTICS -w WH -q "select 1"', "snowsql", "select 1", "ANALYTICS"),
        # clickhouse
        (
            'clickhouse client --query "SELECT 1" --database logs',
            "clickhouse-client",
            "SELECT 1",
            "logs",
        ),
        ("clickhouse-client -q 'select 1'", "clickhouse-client", "select 1", None),
        # trino / presto
        (
            "trino --server https://t:8443 --catalog hive --execute 'SELECT 1'",
            "trino",
            "SELECT 1",
            "hive",
        ),
        (
            "presto --server http://p:8080 --execute 'SELECT 1'",
            "presto",
            "SELECT 1",
            "http://p:8080",
        ),
    ],
)
def test_builtin_cli_patterns(command: str, rule: str, sql: str, source: str | None) -> None:
    """Each built-in CLI reads its SQL and source through quoting, wrappers and paths."""
    assert _shell(command) == DataQuery(
        kind="query",
        matcher="shell",
        sql=sql,
        source=source,
        row_count=None,
        rule=rule,
        origin="builtin",
    )


@pytest.mark.parametrize(
    "command",
    [
        "psql --help",
        "psql -d shop",
        "psql -d shop -c",
        "bq query --help 'SELECT 1'",
        "bq ls my_dataset",
        "bq query",
        "duckdb --version",
        "duckdb app.db",
        "sqlite3 app.db",
        "trino --execute 'SELECT 1' --help",
        "mysql -e",
        "snowsql -q '   '",
        "echo psql -c 'SELECT 1'",
        'grep -r "psql -c" .',
        "cat q.sql | psql shop",
        'psql -c "$(cat q.sql)"',
        "xpsql -c 'SELECT 1'",
        "clickhouse server --query 'x'",
        "psql -c 'unterminated",
        "sqlite3 my.db < schema.sql",
        "sqlite3 my.db > dump.txt",
        "bq query < q.sql",
        "mysql -pSecrete shop",
    ],
)
def test_commands_that_run_no_query_are_not_classified(command: str) -> None:
    """Help, version, interactive sessions and look-alikes aren't queries."""
    assert _shell(command) is None


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("Bash", {"command": "psql -c 'select 1'"}),
        ("sys_os_shell", {"command": "psql -c 'select 1'"}),
        ("mcp__omnigent__sys_os_shell", {"command": "psql -c 'select 1'"}),
        ("bash", {"command": "psql -c 'select 1'"}),
        ("exec_command", {"cmd": "psql -c 'select 1'"}),
        ("shell", {"command": ["bash", "-lc", "psql -c 'select 1'"]}),
        ("shell", {"command": ["psql", "-c", "select 1"]}),
    ],
)
def test_every_harness_shell_tool_is_read(name: str, arguments: dict[str, object]) -> None:
    """Harness shell tools, string commands and argv lists all classify the same."""
    query = classify_tool_call(name, arguments)
    assert query is not None and (query.rule, query.sql) == ("psql", "select 1")


def test_only_shell_tools_are_read_as_commands() -> None:
    """A non-shell tool whose argument looks like a command isn't a query."""
    assert classify_tool_call("Write", {"command": "psql -c 'select 1'"}) is None
    assert classify_tool_call("linear__shell", {"command": "psql -c 'select 1'"}) is None


def test_recorded_json_arguments_are_parsed() -> None:
    """Items store arguments as a JSON string; it reads like the mapping."""
    recorded = json.dumps({"command": "duckdb -c 'select 1'"})
    query = classify_tool_call("Bash", recorded)
    assert query is not None and query.sql == "select 1"
    assert classify_tool_call("Bash", "{not json") is None


@pytest.mark.parametrize(
    ("command", "result", "rows"),
    [
        ("psql -c 'select 1'", " ?column?\n----------\n        1\n(1 row)\n", 1),
        ("psql -c 'select 1'", json.dumps({"stdout": "a\n---\n(12 rows)\n", "exit_code": 0}), 12),
        ("psql -c 'select 1'", "no footer here", None),
        ("mysql -e 'select 1' -t", "+---+\n| 1 |\n+---+\n3 rows in set (0.00 sec)\n", 3),
        (
            "bq query --format=json 'select 1'",
            json.dumps({"stdout": json.dumps([{"a": 1}, {"a": 2}]), "exit_code": 0}),
            2,
        ),
        ("duckdb -json -c 'select 1'", json.dumps([{"x": 1}]), 1),
        ("duckdb -json -c 'select 1'", "[1, 2, 3]", None),
        ("sqlite3 -json app.db 'select 1'", "[]", 0),
    ],
)
def test_builtin_row_counts(command: str, result: str, rows: int | None) -> None:
    """Row counts come from psql/mysql footers or a JSON array of records."""
    query = _shell(command, result)
    assert query is not None and query.row_count == rows


def test_spec_rules_take_precedence_over_builtins() -> None:
    """A spec rule for a built-in CLI wins; one that finds no SQL falls through."""
    spec = _spec(
        {
            "name": "team_psql",
            "shell": "psql",
            "sql": {"flag": ["-c"]},
            "source": {"value": "team warehouse"},
        },
        {"name": "never", "shell": "duckdb", "sql": {"flag": "--sql"}},
    )
    psql = _shell("psql -d shop -c 'select 1'", spec=spec)
    assert psql is not None
    assert (psql.rule, psql.origin, psql.source) == ("team_psql", "spec", "team warehouse")
    duckdb = _shell("duckdb -c 'select 1'", spec=spec)
    assert duckdb is not None and (duckdb.rule, duckdb.origin) == ("duckdb", "builtin")


def test_spec_can_turn_the_builtins_off() -> None:
    """``builtins: false`` drops every built-in pattern but keeps the spec's own rules."""
    spec = _spec(
        {"name": "mycli", "shell": "mycli query", "sql": {"positional": 0}}, builtins=False
    )
    assert _shell("psql -c 'select 1'", spec=spec) is None
    query = _shell("mycli --profile=prod query 'select 1'", spec=spec)
    assert query is not None and (query.rule, query.sql) == ("mycli", "select 1")


def test_spec_shell_rule_with_subcommand_and_value_flags() -> None:
    """Flags between prefix words are skipped; declared value flags don't shift positionals."""
    spec = _spec(
        {
            "name": "warehouse_cli",
            "shell": ["wh", "sql", "run"],
            "value_flags": ["--profile"],
            "sql": {"positional": 0},
            "source": [{"flag": "--db"}, {"value": "default"}],
            "row_count": {"result": "rows"},
        },
    )
    query = _shell(
        "wh --profile prod sql run --db=sales 'select 1'",
        json.dumps({"stdout": json.dumps({"rows": [[1], [2]]}), "exit_code": 0}),
        spec=spec,
    )
    assert query == DataQuery(
        kind="query",
        matcher="shell",
        sql="select 1",
        source="sales",
        row_count=2,
        rule="warehouse_cli",
        origin="spec",
    )
    fallback = _shell("wh sql run 'select 1'", spec=spec)
    assert fallback is not None and fallback.source == "default"
    assert _shell("wh sql list", spec=spec) is None


_WAREHOUSE_RULE = {
    "name": "warehouse_sql",
    "mcp": {"server": "warehouse", "tool": "execute_sql"},
    "sql": {"argument": "statement"},
    "source": [{"arguments": ["catalog", "schema"]}, {"argument": "warehouse_id"}],
    "row_count": {"result": "manifest.total_row_count"},
}


@pytest.mark.parametrize(
    "name",
    [
        "mcp__warehouse__execute_sql",
        "warehouse__execute_sql",
        "mcp__omnigent__warehouse__execute_sql",
    ],
)
def test_mcp_rule_reads_arguments_and_result(name: str) -> None:
    """An MCP rule reads SQL and source from arguments and rows from a content-block result."""
    result = json.dumps(
        [{"type": "text", "text": json.dumps({"manifest": {"total_row_count": "12"}})}]
    )
    query = classify_tool_call(
        name,
        {"statement": "SELECT * FROM orders", "catalog": "main", "schema": "sales"},
        result,
        spec=_spec(_WAREHOUSE_RULE),
    )
    assert query == DataQuery(
        kind="query",
        matcher="mcp",
        sql="SELECT * FROM orders",
        source="main.sales",
        row_count=12,
        rule="warehouse_sql",
        origin="spec",
    )


def test_mcp_rule_needs_its_sql_and_its_server() -> None:
    """Another server's tool, or a call without the SQL argument, doesn't match."""
    spec = _spec(_WAREHOUSE_RULE)
    assert classify_tool_call("other__execute_sql", {"statement": "SELECT 1"}, spec=spec) is None
    assert classify_tool_call("warehouse__execute_sql", {"query": "SELECT 1"}, spec=spec) is None
    fallback = classify_tool_call(
        "warehouse__execute_sql", {"statement": "SELECT 1", "warehouse_id": 42}, spec=spec
    )
    assert fallback is not None and fallback.source == "42"


def test_source_only_rules_mark_data_source_reads() -> None:
    """A rule with only a source locator marks every matching call as a source read."""
    spec = _spec(
        {"name": "catalog", "mcp": {"server": "catalog"}, "source": {"argument": "table"}},
        {"name": "bare_tool", "mcp": {"tool": "list_tables"}, "source": {"value": "lake"}},
    )
    described = classify_tool_call("catalog__describe", {"table": "main.sales.orders"}, spec=spec)
    assert described is not None
    assert (described.kind, described.sql, described.source) == (
        "source",
        None,
        "main.sales.orders",
    )
    listed = classify_tool_call("list_tables", {}, spec=spec)
    assert listed is not None and (listed.kind, listed.source) == ("source", "lake")
    unnamed = classify_tool_call("catalog__list", {}, spec=spec)
    assert unnamed is not None and (unnamed.kind, unnamed.source) == ("source", None)


@pytest.mark.parametrize(
    ("result", "rows"),
    [
        (json.dumps({"rows": [{"a": 1}, {"a": 2}, {"a": 3}]}), 3),
        (json.dumps({"rows": 7}), 7),
        (json.dumps({"rows": True}), None),
        (json.dumps({"rows": -1}), None),
        (json.dumps({"structuredContent": {"rows": 4}, "content": []}), 4),
        ("not json", None),
        (None, None),
    ],
)
def test_result_row_count_shapes(result: str | None, rows: int | None) -> None:
    """Row counts accept integers, digit strings and row lists, and nothing else."""
    spec = _spec(
        {
            "name": "q",
            "mcp": {"tool": "run_sql"},
            "sql": {"argument": "q"},
            "row_count": {"result": "rows"},
        }
    )
    query = classify_tool_call("run_sql", {"q": "SELECT 1"}, result, spec=spec)
    assert query is not None and query.row_count == rows


def test_classification_never_runs_the_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Classifying reads the recorded text only; substitutions in it never execute."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("classification must not run anything")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(os, "system", refuse)
    monkeypatch.setattr(os, "popen", refuse)
    marker = tmp_path / "ran"
    query = _shell(f"psql -c 'select 1'; echo `touch {marker}`; psql -c \"$(touch {marker})\"")
    assert query is not None and query.sql == "select 1"
    assert not marker.exists()


def test_split_mcp_tool_name() -> None:
    """Server-prefixed names split; Omnigent's own server is transparent."""
    assert split_mcp_tool_name("mcp__linear__get_issue") == ("linear", "get_issue")
    assert split_mcp_tool_name("linear__get_issue") == ("linear", "get_issue")
    assert split_mcp_tool_name("mcp__omnigent__sys_os_shell") == (None, "sys_os_shell")
    assert split_mcp_tool_name("Bash") == (None, "Bash")


def test_query_to_dict_is_json_safe() -> None:
    """The client-facing shape carries every field."""
    query = _shell("psql -d shop -c 'select 1'", "(2 rows)")
    assert query is not None
    assert json.loads(json.dumps(query.to_dict())) == {
        "kind": "query",
        "matcher": "shell",
        "sql": "select 1",
        "source": "shop",
        "row_count": 2,
        "rule": "psql",
        "origin": "builtin",
    }


@pytest.mark.parametrize(
    ("arguments", "result"),
    [
        pytest.param("[" * 200_000, None, id="deep-json-arguments"),
        pytest.param({"command": "psql -c 'select 1'"}, '{"a":' * 100_000, id="deep-json-output"),
        pytest.param({"command": "sqlite3 db 'select 1'"}, "[" * 200_000, id="deep-json-rows"),
    ],
)
def test_hostile_recorded_values_dont_raise(arguments: object, result: str | None) -> None:
    """Pathological recorded JSON classifies or doesn't; it never raises."""
    classify_tool_call("Bash", arguments, result)  # type: ignore[arg-type]


def test_non_ascii_digits_are_not_row_counts() -> None:
    """``"²"`` passes ``str.isdigit`` but isn't a count."""
    spec = _spec(
        {
            "name": "q",
            "mcp": {"tool": "run_sql"},
            "sql": {"argument": "q"},
            "row_count": {"result": "n"},
        }
    )
    query = classify_tool_call("run_sql", {"q": "SELECT 1"}, json.dumps({"n": "²"}), spec=spec)
    assert query is not None and query.row_count is None
