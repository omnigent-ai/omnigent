import { describe, expect, it } from "vitest";
import type { RenderItem, ToolState } from "@/lib/renderItems";
import { collectTurnWork, hasTurnWork, looksLikeSql, webUrl } from "@/lib/turnWork";

function tool(
  callId: string,
  name: string,
  args: Record<string, unknown>,
  output: string | null,
  { state = "output-available", duration }: { state?: ToolState; duration?: number } = {},
): Extract<RenderItem, { kind: "tool" }> {
  return {
    kind: "tool",
    itemId: `fc_${callId}`,
    execution: {
      name,
      arguments: args,
      argsSummary: "",
      callId,
      agentName: "agent",
      executedBy: "server",
      output,
    },
    output,
    state,
    startedAt: null,
    duration,
  };
}

const answer: RenderItem = {
  kind: "text",
  itemId: "answer",
  text: "Revenue rose 4%.",
  final: true,
};

describe("collectTurnWork", () => {
  it("lists SQL, shell and code calls as queries in turn order", () => {
    const work = collectTurnWork([
      tool(
        "sql",
        "mcp__databricks__execute_sql",
        { statement: "SELECT region, sum(amount) FROM main.sales.orders GROUP BY 1" },
        JSON.stringify({ manifest: { total_row_count: 4 }, result: { data_array: [[1], [2]] } }),
        { duration: 1.2 },
      ),
      tool("sh", "Bash", { command: "wc -l data.csv", description: "Count rows" }, "42 data.csv\n"),
      tool("py", "run_python", { code: "print(df.describe())" }, "count 42\nmean 3\n"),
      answer,
    ]);

    expect(work.queries.map((query) => [query.language, query.text])).toEqual([
      ["sql", "SELECT region, sum(amount) FROM main.sales.orders GROUP BY 1"],
      ["shell", "wc -l data.csv"],
      ["python", "print(df.describe())"],
    ]);
    const [sql, shell, python] = work.queries;
    expect(sql).toMatchObject({
      label: "Execute SQL",
      server: "databricks",
      status: "completed",
      // The manifest's count wins over the page of rows returned inline.
      resultSize: "4 rows",
      durationS: 1.2,
    });
    expect(shell).toMatchObject({ label: "Shell command", resultSize: "1 line" });
    expect(python).toMatchObject({ label: "Run python", resultSize: "2 lines" });
  });

  it("reads Codex's unwrapped command and reports a non-zero exit as a failure", () => {
    const work = collectTurnWork([
      tool(
        "codex",
        "shell",
        { command: "/bin/bash -lc 'psql -c \"select 1\"'" },
        "psql: error: connection refused\n[exit code: 2]",
      ),
    ]);
    expect(work.queries[0]).toMatchObject({
      text: 'psql -c "select 1"',
      status: "failed",
      error: "Exit 2",
    });
  });

  it("reports a tool's own error output and no result size", () => {
    const work = collectTurnWork([
      tool("bad", "query", { sql: "SELECT * FROM missing" }, "Error: table not found"),
    ]);
    expect(work.queries[0]).toMatchObject({
      status: "failed",
      error: "Error: table not found",
      resultSize: null,
    });
  });

  it("keeps running, cancelled and unrecorded calls distinct from failures", () => {
    const sql = { sql: "SELECT 1" };
    const work = collectTurnWork([
      tool("a", "run_sql", sql, null, { state: "input-available" }),
      tool("b", "run_sql", sql, null, { state: "cancelled" }),
      tool("c", "run_sql", sql, null, { state: "no-output" }),
    ]);
    expect(work.queries.map((query) => query.status)).toEqual([
      "running",
      "cancelled",
      "no-output",
    ]);
    expect(work.queries.every((query) => query.error === null)).toBe(true);
  });

  it("does not mistake a natural-language question for SQL", () => {
    const work = collectTurnWork([
      tool(
        "genie",
        "databricks__query_space",
        { query: "Show the top customers by revenue" },
        "ok",
      ),
      tool("search", "WebSearch", { query: "select the best warehouse from vendors" }, "results"),
      tool("edit", "Edit", { file_path: "a.py", old_string: "x", new_string: "y" }, "updated"),
    ]);
    expect(work.queries).toEqual([]);
  });

  it("names the data source only from the call's arguments", () => {
    const work = collectTurnWork([
      tool(
        "scoped",
        "execute_query",
        {
          query: "select * from orders",
          catalog: "main",
          schema: "sales",
          warehouse_id: "wh-7",
        },
        "[]",
      ),
      tool("plain", "run_sql", { sql: "SELECT * FROM main.sales.orders" }, "[]"),
    ]);
    expect(work.queries[0]!.dataSource).toBe("main.sales (warehouse wh-7)");
    // The table named in the query text is not parsed into a source.
    expect(work.queries[1]!.dataSource).toBeNull();
    expect(work.sources).toEqual([]);
  });

  it("lists files read, tables, URLs and web searches as sources, merging repeats", () => {
    const read = tool("r1", "Read", { file_path: "src/report.py" }, "code");
    const reread = tool("r2", "sys_os_read", { path: "src/report.py" }, "code");
    const table = tool(
      "t",
      "mcp__databricks__describe_table",
      { catalog: "main", schema: "sales", table_name: "orders" },
      "{}",
    );
    const fetch = tool(
      "f",
      "WebFetch",
      { url: "https://example.com/rates", prompt: "rates" },
      "ok",
    );
    const search = tool("s", "web_search", { query: "fed rate 2026" }, "ok");
    const work = collectTurnWork([read, reread, table, fetch, search, answer]);

    expect(work.sources.map((source) => [source.kind, source.value, source.items])).toEqual([
      ["file", "src/report.py", [read, reread]],
      ["table", "main.sales.orders", [table]],
      ["url", "https://example.com/rates", [fetch]],
      ["web_search", "fed rate 2026", [search]],
    ]);
    expect(work.queries).toEqual([]);
  });

  it("leaves out edits, a shell cat and non-web URLs", () => {
    const work = collectTurnWork([
      tool(
        "w",
        "Write",
        { file_path: "out.csv", content: "a,b" },
        "File created successfully at: out.csv",
      ),
      tool("cat", "Bash", { command: "cat notes.txt" }, "hello"),
      tool("u", "open_link", { url: "javascript:alert(1)" }, "ok"),
    ]);
    expect(work.sources).toEqual([]);
    expect(work.queries.map((query) => query.text)).toEqual(["cat notes.txt"]);
  });

  it("reads provider-managed code interpreter and web search calls", () => {
    const work = collectTurnWork([
      {
        kind: "native_tool",
        itemId: "ci_1",
        toolType: "code_interpreter_call",
        label: "Code interpreter",
        data: { code: "1 + 1", status: "completed" },
      },
      {
        kind: "native_tool",
        itemId: "ws_1",
        toolType: "web_search_call",
        label: "Web search",
        data: { action: { type: "search", query: "gdp 2026" } },
      },
      {
        kind: "native_tool",
        itemId: "ws_2",
        toolType: "web_search_call",
        label: "Web search",
        data: { status: "failed", action: { type: "search", query: "failed search" } },
      },
    ]);
    expect(work.queries).toMatchObject([
      { language: "python", text: "1 + 1", status: "completed", resultSize: null },
    ]);
    expect(work.sources).toMatchObject([{ kind: "web_search", value: "gdp 2026" }]);
  });

  it("offers no Sources for a native search that failed", () => {
    const items: RenderItem[] = [
      {
        kind: "native_tool",
        itemId: "ws_failed",
        toolType: "web_search_call",
        label: "Web search",
        data: { status: "failed", action: { type: "search", query: "gdp 2026" } },
      },
    ];
    expect(hasTurnWork(items)).toBe(false);
    expect(collectTurnWork(items).sources).toEqual([]);
  });

  it("is empty for a turn without data work", () => {
    const items = [tool("todo", "TodoWrite", { todos: [] }, "ok"), answer];
    expect(hasTurnWork(items)).toBe(false);
    expect(collectTurnWork(items)).toEqual({ queries: [], sources: [] });
  });

  it("keeps natural-language questions to a query tool out of the SQL list", () => {
    const work = collectTurnWork([
      tool(
        "q1",
        "databricks__query_space",
        { query: "Select the top 5 customers from EMEA" },
        "ok",
      ),
      tool("q2", "ask_query", { query: "Select customers from last quarter who churned" }, "ok"),
      tool("q3", "run_query", { query: "SELECT * FROM orders" }, "[]"),
    ]);
    expect(work.queries.map((query) => query.text)).toEqual(["SELECT * FROM orders"]);
  });

  it("reads SQL only from SQL-named keys of tools that run it", () => {
    const work = collectTurnWork([
      tool(
        "dialect",
        "execute_sql",
        { sql_dialect: "spark", sql_warehouse_id: "wh", query: "SELECT 1" },
        "[]",
      ),
      tool("lint", "lint_sql", { sql: "SELECT * FROM t" }, "ok"),
      tool("format", "format_sql", { sql_text: "select 1" }, "select 1"),
    ]);
    expect(work.queries.map((query) => query.text)).toEqual(["SELECT 1"]);
  });

  it("does not treat a `code` value as code unless the tool runs code", () => {
    const work = collectTurnWork([
      tool("country", "get_country_by_code", { code: "US" }, "{}"),
      tool("flow", "run_workflow", { code: "WF1" }, "{}"),
      tool("oauth", "exchange_auth_code", { code: "secret-code" }, "{}"),
      tool("exec", "execute_code", { code: "x = 1", language: "javascript" }, ""),
      // Runtime-named tools that only inspect or produce code do not run it.
      tool("lint", "lint_python", { code: "print(1)" }, "ok"),
      tool("format", "format_notebook", { code: "print(1)" }, "print(1)"),
      tool("gen", "generate_python_code", { code: "print(1)" }, "ok"),
      tool("save", "write_python_file", { code: "print(1)" }, "ok"),
      tool("explain", "explain_python_code", { code: "print(1)" }, "ok"),
      // A run word in the name means the code still runs.
      tool("both", "generate_and_run_code", { code: "y = 2" }, ""),
    ]);
    expect(work.queries.map((query) => [query.language, query.text])).toEqual([
      ["code", "x = 1"],
      ["code", "y = 2"],
    ]);
  });

  it("lists no source from a call that failed, was cancelled or wrote its target", () => {
    const work = collectTurnWork([
      tool(
        "missing",
        "Read",
        { file_path: "gone.md" },
        "<tool_use_error>File does not exist</tool_use_error>",
      ),
      tool("cancelled", "Read", { file_path: "slow.md" }, null, { state: "cancelled" }),
      tool("forbidden", "WebFetch", { url: "https://example.com/x" }, "Error: 403 Forbidden"),
      tool("drop", "drop_table", { table_name: "main.tmp.scratch" }, "{}"),
      tool("hook", "create_webhook", { url: "https://hooks.example.com/a" }, "{}"),
      tool("ins", "run_sql", { sql: "INSERT INTO main.s.t VALUES (1)", table: "main.s.t" }, "{}"),
      tool(
        "badsql",
        "run_sql",
        { sql: "SELECT * FROM x", table: "main.s.x" },
        "Error: no such table",
      ),
    ]);
    expect(work.sources).toEqual([]);
    expect(work.queries.map((query) => query.status)).toEqual(["completed", "failed"]);
  });

  it("reports a statement API's own failure and pending state", () => {
    const failed = JSON.stringify({
      statement_id: "s1",
      status: { state: "FAILED", error: { message: "TABLE_OR_VIEW_NOT_FOUND" } },
    });
    const pending = JSON.stringify({ statement_id: "s2", status: { state: "PENDING" } });
    const work = collectTurnWork([
      tool("f", "execute_sql", { statement: "SELECT * FROM t" }, failed),
      tool("p", "execute_sql", { statement: "SELECT * FROM t" }, pending),
    ]);
    expect(work.queries).toMatchObject([
      { status: "failed", error: "TABLE_OR_VIEW_NOT_FOUND", resultSize: null },
      { status: "running", error: null, resultSize: null },
    ]);
  });

  it("marks a page of rows as partial and counts rows only for SQL", () => {
    const page = JSON.stringify({ rows: [{ a: 1 }, { a: 2 }], has_more: true });
    const chunk = JSON.stringify({ result: { data_array: [[1]], next_chunk_index: 1 } });
    // A page's own count is not the result's total while more rows follow.
    const countedPage = JSON.stringify({
      row_count: 2,
      rows: [{ a: 1 }, { a: 2 }],
      has_more: true,
    });
    const countedChunk = JSON.stringify({ result: { num_rows: 1, next_chunk_index: 1 } });
    const pagedAbove = JSON.stringify({ has_more: true, result: { row_count: 3 } });
    const total = JSON.stringify({
      manifest: { total_row_count: 40 },
      result: { row_count: 10, next_chunk_index: 1 },
    });
    const work = collectTurnWork([
      tool("page", "run_sql", { sql: "SELECT a FROM t" }, page),
      tool("chunk", "run_sql", { sql: "SELECT a FROM t" }, chunk),
      tool("countedPage", "run_sql", { sql: "SELECT a FROM t" }, countedPage),
      tool("countedChunk", "run_sql", { sql: "SELECT a FROM t" }, countedChunk),
      tool("pagedAbove", "run_sql", { sql: "SELECT a FROM t" }, pagedAbove),
      tool("total", "run_sql", { sql: "SELECT a FROM t" }, total),
      tool("py", "run_python", { code: "print([1, 2, 3])" }, "[1, 2, 3]"),
    ]);
    expect(work.queries.map((query) => query.resultSize)).toEqual([
      "2+ rows",
      "1+ rows",
      "2+ rows",
      "1+ rows",
      "3+ rows",
      "40 rows",
      "1 line",
    ]);
  });

  it("keeps a total exact across pages, but not past a truncation", () => {
    const sql = { sql: "SELECT a FROM t" };
    const sizes = [
      // A total beside one page of rows, or behind a top-level page count.
      { total_row_count: 40, rows: [{ a: 1 }], has_more: true },
      { has_more: true, manifest: { total_row_count: 40 } },
      { row_count: 10, has_more: true, manifest: { total_row_count: 40 } },
      // A limit cut the result, so its total is a floor.
      { manifest: { total_row_count: 100, truncated: true }, result: { row_count: 100 } },
      // A truncation marked on the manifest applies to the result's rows.
      { manifest: { truncated: true }, result: { data_array: [[1], [2]] } },
      // BigQuery reports totals as digit strings and pages with `pageToken`.
      { totalRows: "40", rows: [{ f: [] }], pageToken: "x" },
      { rows: [{ f: [] }, { f: [] }], pageToken: "x" },
    ].map((output, index) => tool(`size${index}`, "run_sql", sql, JSON.stringify(output)));
    expect(collectTurnWork(sizes).queries.map((query) => query.resultSize)).toEqual([
      "40 rows",
      "40 rows",
      "40 rows",
      "100+ rows",
      "2+ rows",
      "40 rows",
      "2+ rows",
    ]);
  });

  it("keys repeated call ids apart", () => {
    const work = collectTurnWork([
      tool("same", "run_sql", { sql: "SELECT 1" }, "[]"),
      tool("same", "run_sql", { sql: "SELECT 2" }, "[]"),
    ]);
    const [first, second] = work.queries;
    expect(first!.key).not.toBe(second!.key);
  });
});

describe("looksLikeSql", () => {
  it.each([
    "SELECT * FROM t",
    "select 1",
    "-- totals\nSELECT a FROM t",
    "/* q */ WITH x AS (SELECT 1) SELECT * FROM x",
    "SHOW TABLES IN main.sales",
    "DESCRIBE TABLE main.sales.orders",
    "describe main.sales.orders",
    "INSERT INTO t VALUES (1)",
  ])("accepts %s", (text) => {
    expect(looksLikeSql(text)).toBe(true);
  });

  it.each([
    "Show me the top customers",
    "Describe the revenue trend",
    "Select the best option",
    "What were sales last week?",
  ])("rejects %s", (text) => {
    expect(looksLikeSql(text)).toBe(false);
  });
});

describe("webUrl", () => {
  it("keeps http(s) URLs and drops other schemes", () => {
    expect(webUrl("https://example.com/a")).toBe("https://example.com/a");
    expect(webUrl("file:///etc/passwd")).toBeNull();
    expect(webUrl("not a url")).toBeNull();
    expect(webUrl(42)).toBeNull();
  });
});
