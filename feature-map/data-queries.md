# Data queries

An agent author declares which of the agent's tool calls run data queries or
read data sources with a top-level `data_queries:` spec block, and Omnigent also
recognizes common SQL CLIs run through the shell tool (`psql -c`, `bq query`,
`duckdb -c`, `sqlite3`, `mysql -e`, `snowsql -q`, `clickhouse client --query`,
`trino --execute`). Clients read each session's classified calls (the SQL, the
data source and the row count) from one read-only API, which the Sources panel,
notebook export and SQL cells build on. Classification is display metadata: the
call itself stays an ordinary tool call under its own policies and sandbox.

## Sub-features

- `spec-rules`: `data_queries.rules` match an MCP tool (server and/or tool name)
  or a shell command by its argv prefix, and locate the SQL, source and row count
  in arguments, argv flags or positionals, or result fields.
- `builtin-clis`: built-in patterns for the SQL CLIs above apply after the spec's
  rules; `data_queries.builtins: false` turns them off.
- `not-a-query`: `--help`, `--version`, interactive sessions, SQL read from a file
  or heredoc and commands built with `$(...)` are not classified.
- `strict-spec`: an unknown key, a wrong locator for the matcher or a malformed
  value fails the agent load with the offending path, in a bundle and a
  single-file YAML alike.
- `api`: `GET /v1/sessions/{id}/data_queries` lists classified calls oldest
  first, each with its call and output item IDs and its turn (`response_id`);
  pages resume from `next_after`. It needs read access to the session.
- `policy-unchanged`: a classified CLI query is still gated by the shell tool's
  policies and sandbox, and nothing is executed to classify it.

## How to get to it (user POV)

- Agent YAML: add a `data_queries:` block to a `config.yaml` bundle or a
  single-file agent YAML (see `docs/AGENT_YAML_SPEC.md#data-queries`).
- REST API: `GET /v1/sessions/{id}/data_queries` from any client with read access.
- Web UI: none yet; the Sources panel, notebook export and SQL cells read this
  API in follow-up changes.

## Driving it with the repro environment

Preconditions: none for the component and server tests; the manual check needs
a running instance from the verify-omnigent skill and an agent with `os_env`.

- Spec parsing and malformed specs: `tests/spec/test_data_queries.py::test_malformed_block_fails_with_its_path` and `tests/spec/test_omnigent_adapter.py::test_compat_yaml_data_queries_reach_the_spec`.
- Every built-in CLI pattern, quoting and wrappers: `tests/test_data_queries.py::test_builtin_cli_patterns`; non-queries: `tests/test_data_queries.py::test_commands_that_run_no_query_are_not_classified`; precedence and opt-out: `tests/test_data_queries.py::test_spec_rules_take_precedence_over_builtins` and `tests/test_data_queries.py::test_spec_can_turn_the_builtins_off`.
- The API through the real route and stores, including paging and the opt-out: `tests/server/integration/test_sessions_data_queries.py::test_session_data_queries_classify_spec_and_builtin_calls` and `tests/server/integration/test_sessions_data_queries.py::test_session_data_queries_respect_a_builtins_opt_out`; read access: `tests/server/integration/test_sessions_permissions.py::test_get_session_items_with_read_access`.
- Shell policy still applies: `tests/runtime/policies/test_data_query_shell_policy.py::test_shell_policy_still_gates_a_classified_cli_query`.
- Manual: in a session whose agent has `os_env`, ask the agent to run `sqlite3 /tmp/demo.db "select 1"` through its shell tool, then `curl` the session's `/data_queries`; the entry shows rule `sqlite3`, the SQL and source `/tmp/demo.db`, and the shell call's approval card (if a policy asks) appeared as usual.

## Gotchas

- The Sources button, notebook cells and SQL cells don't read this API yet, so
  nothing changes in the chat UI.
- Row counts come only from what the result states: a `row_count` result path,
  `psql`'s `(N rows)` footer, `mysql`'s `N rows in set`, or a JSON array of rows
  (`bq --format=json`, `duckdb -json`). Plain table output has no count.
- Short flags take their value as the next word (`-c 'SELECT 1'`); an attached
  `-c'SELECT 1'` isn't read, and only `psql` reads bundles such as `-tAc`. A flag
  a rule doesn't list in `value_flags` can shift positional words.
- Only the first query in a chained command is reported for a call.
