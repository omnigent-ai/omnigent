// "Show your work" for an assistant answer: the queries a turn ran (SQL, shell
// and code) and the data sources it read (files, tables, URLs, web searches),
// read from the turn's own tool calls. Only what a call's name, arguments and
// recorded output state is listed; nothing is inferred from query text.

import type { RenderItem } from "@/lib/renderItems";
import { parseShellToolOutput } from "@/lib/shellToolOutput";
import {
  formatToolTitle,
  humanizeToolName,
  shellToolCall,
  splitToolName,
  stripOmnigentPrefix,
  toolCallCategory,
} from "@/lib/toolTitle";
import {
  type ParsedToolOutput,
  identifierWords,
  isRecord,
  ownValue,
  parseToolOutput,
  toolErrorMessage,
} from "@/lib/toolValue";

export type ToolRenderItem = Extract<RenderItem, { kind: "tool" | "native_tool" }>;
type ToolCallItem = Extract<RenderItem, { kind: "tool" }>;
type NativeToolItem = Extract<RenderItem, { kind: "native_tool" }>;

export type TurnQueryLanguage = "sql" | "shell" | "python" | "code";

/** `completed` means the call finished without reporting a failure. */
export type TurnCallStatus = "completed" | "failed" | "running" | "cancelled" | "no-output";

export interface TurnQuery {
  /** Stable key for the call within the turn. */
  key: string;
  item: ToolRenderItem;
  /** The tool's action label ("Execute SQL", "Shell command"). */
  label: string;
  /** Raw tool name as the call carries it. */
  toolName: string;
  /** MCP server that owns the tool, when another server's tool ran it. */
  server?: string;
  language: TurnQueryLanguage;
  /** The query, command or code exactly as the call passed it. */
  text: string;
  /** Data source the call's arguments name (`main.sales.orders`), or `null`. */
  dataSource: string | null;
  /** `null` when the record does not say (provider-managed calls without a status). */
  status: TurnCallStatus | null;
  error: string | null;
  durationS?: number;
  /** "12 rows" or "3 lines", from the recorded output; `null` without one. */
  resultSize: string | null;
}

export type TurnSourceKind = "file" | "table" | "url" | "web_search";

export interface TurnSource {
  key: string;
  kind: TurnSourceKind;
  /** Path, table name, URL or search text. */
  value: string;
  /** Calls that used this source, in turn order. */
  items: ToolRenderItem[];
}

export interface TurnWork {
  queries: TurnQuery[];
  sources: TurnSource[];
}

interface SourceRef {
  kind: TurnSourceKind;
  value: string;
}

/** A query a call ran, read from its name and arguments alone. */
interface QueryCall {
  label: string;
  server?: string;
  language: TurnQueryLanguage;
  text: string;
  outputFormat?: Parameters<typeof parseShellToolOutput>[1];
}

/**
 * Whether a turn has calls to show behind its "Sources" button. Reads names and
 * arguments only, so it stays cheap on every render; a source whose call then
 * turns out to have failed is dropped by {@link collectTurnWork}.
 */
export function hasTurnWork(items: readonly RenderItem[]): boolean {
  return items.some((item) => {
    if (item.kind === "native_tool") {
      return nativeQueryCall(item) !== null || nativeSearchQuery(item) !== null;
    }
    if (item.kind !== "tool") return false;
    const { name, arguments: args } = item.execution;
    const query = queryCall(name, args);
    if (query !== null) return true;
    return item.state === "output-available" && sourceRefs(name, args, null).length > 0;
  });
}

/** The queries and data sources of a turn's tool calls, in the order they ran. */
export function collectTurnWork(items: readonly RenderItem[]): TurnWork {
  const queries: TurnQuery[] = [];
  const sources = new Map<string, TurnSource>();
  const addSources = (refs: SourceRef[], item: ToolRenderItem) => {
    for (const { kind, value } of refs) {
      const key = `${kind}:${value}`;
      const existing = sources.get(key);
      if (existing === undefined) sources.set(key, { key, kind, value, items: [item] });
      else if (!existing.items.includes(item)) existing.items.push(item);
    }
  };

  items.forEach((item, index) => {
    if (item.kind === "tool") {
      const { name, arguments: args, callId } = item.execution;
      const call = queryCall(name, args);
      const query = call === null ? null : toolQuery(item, `${index}:${callId}`, call);
      // Only a call that finished without a reported failure read its sources.
      const read = query === null ? completedWithoutFailure(item) : query.status === "completed";
      if (query !== null) queries.push(query);
      if (read) addSources(sourceRefs(name, args, call), item);
      return;
    }
    if (item.kind === "native_tool") {
      const call = nativeQueryCall(item);
      if (call !== null) queries.push(nativeQuery(item, `${index}:${item.itemId ?? ""}`, call));
      const search = nativeSearchQuery(item);
      if (search !== null) addSources([{ kind: "web_search", value: search }], item);
    }
  });

  return { queries, sources: [...sources.values()] };
}

function queryCall(name: string, args: Record<string, unknown>): QueryCall | null {
  const { server, tool } = splitToolName(name);
  const owner = server === undefined ? {} : { server };
  const shell = shellToolCall(name, args);
  if (shell !== null) {
    return {
      label: "Shell command",
      language: "shell",
      text: shell.command,
      outputFormat: shell.outputFormat,
      ...owner,
    };
  }
  const label = humanizeToolName(tool, server);
  const sql = sqlArgument(tool, args);
  if (sql !== null) return { label, language: "sql", text: sql, ...owner };
  const code = codeArgument(tool, args);
  return code === null ? null : { label, ...code, ...owner };
}

function toolQuery(item: ToolCallItem, key: string, call: QueryCall): TurnQuery {
  const base = {
    key,
    item,
    toolName: item.execution.name,
    label: call.label,
    language: call.language,
    text: call.text,
    ...(call.server === undefined ? {} : { server: call.server }),
    durationS: item.duration,
    dataSource: argumentDataSource(item.execution.arguments),
  };
  if (item.output === null) return { ...base, ...callStatus(item.state, null), resultSize: null };

  if (call.outputFormat !== undefined) {
    const parsed = parseShellToolOutput(item.output, call.outputFormat);
    let failure: string | null = null;
    if (parsed.timedOut) failure = "Timed out";
    else if (parsed.exitCode !== null && parsed.exitCode !== 0) failure = `Exit ${parsed.exitCode}`;
    const status = callStatus(item.state, failure);
    return { ...base, ...status, resultSize: sizeUnlessFailed(status, lineCount(parsed.text)) };
  }

  const parsed = parseToolOutput(item.output);
  const statement = call.language === "sql" ? statementState(parsed) : null;
  const status =
    statement !== null && item.state === "output-available"
      ? statement
      : callStatus(item.state, toolErrorMessage(parsed));
  const size = call.language === "sql" ? sqlResultSize(parsed) : lineCount(parsed.text);
  return { ...base, ...status, resultSize: sizeUnlessFailed(status, size) };
}

function sizeUnlessFailed(status: { status: TurnCallStatus }, size: string): string | null {
  return status.status === "completed" ? size : null;
}

function completedWithoutFailure(item: ToolCallItem): boolean {
  if (item.state !== "output-available") return false;
  return item.output === null || toolErrorMessage(parseToolOutput(item.output)) === null;
}

function callStatus(
  state: ToolCallItem["state"],
  failure: string | null,
): { status: TurnCallStatus; error: string | null } {
  switch (state) {
    case "input-available":
      return { status: "running", error: null };
    case "cancelled":
      return { status: "cancelled", error: null };
    case "no-output":
      return { status: "no-output", error: null };
    case "output-error":
      return { status: "failed", error: failure };
    case "output-available":
      return failure === null
        ? { status: "completed", error: null }
        : { status: "failed", error: failure };
  }
}

/**
 * A statement API's own outcome (`{"status": {"state": "FAILED", "error": ...}}`,
 * as Databricks reports it), or `null` when the output reports none.
 */
function statementState(
  parsed: ParsedToolOutput,
): { status: TurnCallStatus; error: string | null } | null {
  if (parsed.kind !== "json" || !isRecord(parsed.value)) return null;
  const status = parsed.value.status;
  if (!isRecord(status) || typeof status.state !== "string") return null;
  switch (status.state.toUpperCase()) {
    case "FAILED": {
      const error = isRecord(status.error) ? status.error.message : undefined;
      return {
        status: "failed",
        error: typeof error === "string" && error !== "" ? error : "The statement failed.",
      };
    }
    case "CANCELED":
    case "CANCELLED":
      return { status: "cancelled", error: null };
    case "PENDING":
    case "RUNNING":
      return { status: "running", error: null };
    default:
      return null;
  }
}

// Argument keys that hold SQL by name: `sql`, `sql_query`, `sqlText`.
const SQL_KEY_SUFFIXES = new Set(["query", "text", "statement", "string", "command"]);
// Keys that hold SQL only when the tool runs queries and the text reads as SQL.
const QUERY_KEY_WORDS = new Set(["query", "statement"]);
const QUERY_TOOL_WORDS = new Set(["query", "statement"]);
// Tools that take SQL without running it.
const NON_EXECUTING_TOOL_WORDS = new Set([
  "lint",
  "format",
  "validate",
  "parse",
  "translate",
  "convert",
  "transpile",
  "generate",
  "generator",
]);
// Statement openings, strict enough that "Show me the top customers" is not SQL.
const SQL_START_RE =
  /^(?:with\s+\w+(?:\s*\([^)]*\))?\s+as\s*\(|select\s[\s\S]*\bfrom\s|select\s+[\d'"*(@-]|show\s+(?:tables|views|databases|schemas|catalogs|columns|functions|grants|partitions|create|tblproperties)\b|(?:describe|desc)\s+(?:table|extended|formatted|detail|history|schema|database|catalog|function|[\w`"]+\.[\w`"]+)|explain\s+(?:select|with|insert|update|delete|analyze|extended|formatted|cost)\b|insert\s+(?:into|overwrite)\s|update\s+\S+\s+set\s|delete\s+from\s|merge\s+into\s|create\s+(?:or\s+replace\s+)?(?:temp\s+|temporary\s+)?(?:table|view|schema|database|index|function)\b|alter\s+(?:table|view|schema)\s|drop\s+(?:table|view|schema|database|index)\b|pragma\s|values\s*\()/i;
// Punctuation, qualified names or clauses prose rarely has ("Select the top 5
// customers from EMEA" has none), required where the tool may take a question.
const SQL_STRUCTURE_RE =
  /[*;,()=<>]|\b\w+\.\w+\b|\b(?:group\s+by|order\s+by|limit\s+\d|join|having|union)\b/i;
const SQL_KEYWORD_START_RE =
  /^(?:select|with|insert|update|delete|merge|create|alter|drop|truncate|pragma|values|call|use|set|explain|describe|desc|show|grant|revoke|copy|optimize|vacuum|analyze|refresh|cache|uncache|msck)\b/i;
const READ_ONLY_SQL_RE = /^(?:select|with|show|describe|desc|explain|values)\b/i;
const LEADING_SQL_COMMENTS_RE = /^(?:\s+|--[^\n]*(?:\n|$)|\/\*[\s\S]*?\*\/)*/;

function sqlArgument(tool: string, args: Record<string, unknown>): string | null {
  const toolWords = identifierWords(tool);
  if (toolWords.some((word) => NON_EXECUTING_TOOL_WORDS.has(word))) return null;
  // `text_to_sql` and `sql_to_python` convert SQL; they do not run it.
  const sqlIndex = toolWords.indexOf("sql");
  const converts = [toolWords[sqlIndex - 1], toolWords[sqlIndex + 1]].some(
    (word) => word === "to" || word === "2",
  );
  const sqlTool = sqlIndex >= 0 && !converts;
  const queryTool = toolWords.some((word) => QUERY_TOOL_WORDS.has(word));
  for (const [key, value] of Object.entries(args)) {
    if (typeof value !== "string" || value.trim() === "") continue;
    const keyWords = identifierWords(key);
    const [first, second, ...rest] = keyWords;
    if (
      first === "sql" &&
      rest.length === 0 &&
      (second === undefined || SQL_KEY_SUFFIXES.has(second))
    ) {
      return value;
    }
    const last = keyWords.at(-1);
    if (last === undefined || !QUERY_KEY_WORDS.has(last)) continue;
    // A SQL tool's statement needs no FROM (`SELECT current_catalog()`), only a
    // keyword and SQL punctuation; a generic query tool's text must read fully as SQL.
    const statement = withoutLeadingComments(value);
    const readsAsSql =
      looksLikeSql(value) ||
      (SQL_KEYWORD_START_RE.test(statement) && SQL_STRUCTURE_RE.test(statement));
    if (sqlTool && readsAsSql) return value;
    if (queryTool && looksLikeSql(value) && SQL_STRUCTURE_RE.test(value)) return value;
  }
  return null;
}

function withoutLeadingComments(text: string): string {
  return text.replace(LEADING_SQL_COMMENTS_RE, "");
}

/** Whether `text` opens with a SQL statement, after any leading comments. */
export function looksLikeSql(text: string): boolean {
  return SQL_START_RE.test(withoutLeadingComments(text));
}

// Tool-name words that mark a `code` argument as code the tool runs.
const CODE_RUNTIME_WORDS = new Set([
  "python",
  "ipython",
  "jupyter",
  "notebook",
  "interpreter",
  "repl",
]);
const CODE_RUN_WORDS = new Set(["exec", "execute", "run", "eval"]);
// Tool-name words for handling code without running it, unless a run word says otherwise.
const CODE_HANDLING_WORDS = new Set([
  ...NON_EXECUTING_TOOL_WORDS,
  "analyze",
  "check",
  "create",
  "edit",
  "explain",
  "review",
  "save",
  "update",
  "write",
]);
const PYTHON_TOOL_WORDS = new Set(["python", "ipython", "jupyter", "notebook"]);

function codeArgument(
  tool: string,
  args: Record<string, unknown>,
): { text: string; language: TurnQueryLanguage } | null {
  const code = args.code;
  if (typeof code !== "string" || code.trim() === "") return null;
  const words = identifierWords(tool);
  const namesRun = words.some((word) => CODE_RUN_WORDS.has(word));
  // `lint_python` inspects code; `generate_and_run_code` still runs it.
  if (!namesRun && words.some((word) => CODE_HANDLING_WORDS.has(word))) return null;
  const runsCode =
    words.some((word) => CODE_RUNTIME_WORDS.has(word)) || (words.includes("code") && namesRun);
  if (!runsCode) return null;
  const declared = typeof args.language === "string" ? args.language.toLowerCase() : null;
  const python =
    declared === "python" ||
    (declared === null && words.some((word) => PYTHON_TOOL_WORDS.has(word)));
  return { text: code, language: python ? "python" : "code" };
}

// Argument keys naming where a query reads from, outermost first.
const CATALOG_KEYS = ["catalog", "catalog_name"];
const SCHEMA_KEYS = ["schema", "schema_name", "database", "database_name", "dataset", "db"];
const TABLE_KEYS = ["table", "table_name", "full_table_name", "table_full_name"];
const WAREHOUSE_KEYS = ["warehouse_id", "warehouse"];

function firstString(args: Record<string, unknown>, keys: readonly string[]): string | null {
  for (const key of keys) {
    const value = ownValue(args, key);
    if (typeof value === "string" && value.trim() !== "") return value.trim();
  }
  return null;
}

/** The table the arguments name, qualified by any catalog and schema they also name. */
function argumentTable(args: Record<string, unknown>): string | null {
  const table = firstString(args, TABLE_KEYS);
  if (table === null) return null;
  if (table.includes(".")) return table;
  return [firstString(args, CATALOG_KEYS), firstString(args, SCHEMA_KEYS), table]
    .filter((part) => part !== null)
    .join(".");
}

/** Where the arguments say a query runs: a table, a schema, or a warehouse. */
function argumentDataSource(args: Record<string, unknown>): string | null {
  const warehouse = firstString(args, WAREHOUSE_KEYS);
  const scope =
    argumentTable(args) ??
    ([firstString(args, CATALOG_KEYS), firstString(args, SCHEMA_KEYS)]
      .filter((part) => part !== null)
      .join(".") ||
      null);
  if (scope !== null && warehouse !== null) return `${scope} (warehouse ${warehouse})`;
  if (warehouse !== null) return `warehouse ${warehouse}`;
  return scope;
}

const WEB_SEARCH_TOOLS = new Set(["WebSearch", "web_search"]);
const URL_KEYS = ["url", "uri"];
// Tool-name words for calls that change or send something; their table and
// URL arguments are targets, not sources.
const WRITE_TOOL_WORDS = new Set([
  "add",
  "alter",
  "approve",
  "archive",
  "assign",
  "attach",
  "close",
  "create",
  "delete",
  "drop",
  "edit",
  "detach",
  "insert",
  "merge",
  "patch",
  "post",
  "publish",
  "push",
  "put",
  "register",
  "remove",
  "rename",
  "replace",
  "save",
  "send",
  "set",
  "share",
  "submit",
  "subscribe",
  "truncate",
  "update",
  "upload",
  "upsert",
  "write",
]);

// Tool-name words for calls that read what their arguments name.
const READ_TOOL_WORDS = new Set([
  "browse",
  "crawl",
  "crawling",
  "describe",
  "download",
  "extract",
  "fetch",
  "get",
  "goto",
  "inspect",
  "list",
  "lookup",
  "navigate",
  "preview",
  "query",
  "read",
  "retrieve",
  "sample",
  "scrape",
  "search",
  "select",
  "show",
  "view",
  "visit",
]);
// Arguments that pick what a multi-purpose tool does (`action: "remove"`).
const ACTION_KEYS = ["action", "method", "operation", "op"];
const READ_ACTIONS = new Set([
  "browse",
  "crawl",
  "describe",
  "extract",
  "fetch",
  "get",
  "head",
  "list",
  "navigate",
  "options",
  "query",
  "read",
  "scrape",
  "search",
  "show",
  "view",
]);

/**
 * Whether a call's name says it reads what its arguments name, and no write
 * word or non-read action (`sys_session_link_pr` with `action: "remove"`) says otherwise.
 */
function readsItsTargets(name: string, args: Record<string, unknown>): boolean {
  const words = identifierWords(splitToolName(name).tool);
  if (words.some((word) => WRITE_TOOL_WORDS.has(word))) return false;
  const actions = ACTION_KEYS.map((key) => ownValue(args, key)).filter(
    (action) => action !== undefined && action !== null,
  );
  const readActions = actions.every(
    (action) => typeof action === "string" && READ_ACTIONS.has(action.trim().toLowerCase()),
  );
  if (!readActions) return false;
  // `http_request` with `method: "GET"` reads, though its name doesn't say so.
  return words.some((word) => READ_TOOL_WORDS.has(word)) || actions.length > 0;
}

/** The sources a call's name and arguments say it read. */
function sourceRefs(
  name: string,
  args: Record<string, unknown>,
  query: QueryCall | null,
): SourceRef[] {
  const bare = stripOmnigentPrefix(name);
  // A shell `cat` is a command the queries list already shows, not a file read.
  if (query?.language === "shell") return [];
  const out: SourceRef[] = [];
  if (query === null && (toolCallCategory({ name, args }) === "read" || bare === "view_image")) {
    const path = formatToolTitle(name, args).filePath;
    if (path !== undefined) out.push({ kind: "file", value: path });
  }
  const reads = query === null && readsItsTargets(name, args);
  const readsTable =
    query === null
      ? reads
      : query.language === "sql" && READ_ONLY_SQL_RE.test(withoutLeadingComments(query.text));
  const table = readsTable ? argumentTable(args) : null;
  if (table !== null) out.push({ kind: "table", value: table });
  if (reads) {
    for (const key of URL_KEYS) {
      const url = webUrl(ownValue(args, key));
      if (url !== null) out.push({ kind: "url", value: url });
    }
  }
  if (WEB_SEARCH_TOOLS.has(bare) && typeof args.query === "string" && args.query.trim() !== "") {
    out.push({ kind: "web_search", value: args.query.trim() });
  }
  return out;
}

/** An http(s) URL as written, or `null` for anything else. */
export function webUrl(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const text = value.trim();
  try {
    const url = new URL(text);
    return url.protocol === "http:" || url.protocol === "https:" ? text : null;
  } catch {
    return null;
  }
}

function nativeQueryCall(item: NativeToolItem): string | null {
  if (item.toolType !== "code_interpreter_call") return null;
  const code = item.data.code;
  return typeof code === "string" && code.trim() !== "" ? code : null;
}

function nativeQuery(item: NativeToolItem, key: string, code: string): TurnQuery {
  const status = item.data.status;
  return {
    key,
    item,
    label: item.label,
    toolName: item.toolType,
    // The provider's code interpreter runs Python.
    language: "python",
    text: code,
    dataSource: null,
    status: status === "completed" ? "completed" : status === "failed" ? "failed" : null,
    error: null,
    resultSize: null,
  };
}

function nativeSearchQuery(item: NativeToolItem): string | null {
  if (item.toolType !== "web_search_call") return null;
  // A search that reports any outcome but completion read nothing; older records carry none.
  const status = item.data.status;
  if (typeof status === "string" && status !== "completed") return null;
  const action = item.data.action;
  const query = isRecord(action) ? action.query : undefined;
  return typeof query === "string" && query.trim() !== "" ? query.trim() : null;
}

// Fields a query result reports its rows in, at the top level or under `manifest`/`result`.
// A total counts the whole result; any other count may be one page of it.
const TOTAL_ROW_COUNT_KEYS = ["total_row_count", "totalRowCount", "total_rows", "totalRows"];
const PAGE_ROW_COUNT_KEYS = ["row_count", "rowCount", "num_rows", "rows_returned"];
const ROW_ARRAY_KEYS = ["rows", "data_array", "records", "results", "data"];
// Markers that the rows returned are one page of a longer result.
const MORE_ROWS_KEYS = ["next_page_token", "nextPageToken", "pageToken", "next_chunk_index"];
const ENVELOPE_KEYS = ["manifest", "result"];

interface RowCount {
  count: number;
  /** The rows are a page, or were cut short; the full result has more. */
  partial: boolean;
}

function isRowArray(value: unknown): value is unknown[] {
  return Array.isArray(value) && value.every((row) => isRecord(row) || Array.isArray(row));
}

function hasMoreRows(record: Record<string, unknown>): boolean {
  if (record.has_more === true || record.hasMore === true) return true;
  return MORE_ROWS_KEYS.some((key) => {
    const value = ownValue(record, key);
    return (typeof value === "string" && value !== "") || typeof value === "number";
  });
}

/** A non-negative integer count, also as a digit string (BigQuery's `totalRows`). */
function countField(record: Record<string, unknown>, keys: readonly string[]): number | null {
  for (const key of keys) {
    const count = ownValue(record, key);
    if (typeof count === "number" && Number.isInteger(count) && count >= 0) return count;
    if (typeof count === "string" && /^\d+$/.test(count)) return Number(count);
  }
  return null;
}

/**
 * Rows a query result reports. The record and its `manifest`/`result` envelopes
 * are read together, so a marker on any of them applies to a count on another.
 * A total stays exact across pages, but not when the result was truncated.
 */
function rowCount(value: unknown): RowCount | null {
  if (isRowArray(value)) return { count: value.length, partial: false };
  if (!isRecord(value)) return null;
  const records = [value];
  const envelopeArrays: unknown[][] = [];
  for (const key of ENVELOPE_KEYS) {
    const envelope = ownValue(value, key);
    if (isRecord(envelope)) records.push(envelope);
    else if (isRowArray(envelope)) envelopeArrays.push(envelope);
  }
  const truncated = records.some((record) => record.truncated === true);
  const partial = truncated || records.some(hasMoreRows);
  for (const record of records) {
    const total = countField(record, TOTAL_ROW_COUNT_KEYS);
    if (total !== null) return { count: total, partial: truncated };
  }
  for (const record of records) {
    const count = countField(record, PAGE_ROW_COUNT_KEYS);
    if (count !== null) return { count, partial };
  }
  for (const record of records) {
    for (const key of ROW_ARRAY_KEYS) {
      const rows = ownValue(record, key);
      if (isRowArray(rows)) return { count: rows.length, partial };
    }
  }
  const [rows] = envelopeArrays;
  return rows === undefined ? null : { count: rows.length, partial };
}

function sqlResultSize(parsed: ParsedToolOutput): string {
  const rows = parsed.kind === "json" ? rowCount(parsed.value) : null;
  if (rows === null) return lineCount(parsed.text);
  const unit = rows.count === 1 && !rows.partial ? "row" : "rows";
  return `${rows.count.toLocaleString()}${rows.partial ? "+" : ""} ${unit}`;
}

function lineCount(text: string): string {
  const trimmed = text.replace(/\n+$/, "");
  if (trimmed.trim() === "") return "No output";
  const lines = trimmed.split("\n").length;
  return `${lines.toLocaleString()} ${lines === 1 ? "line" : "lines"}`;
}
