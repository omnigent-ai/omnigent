// Generic presentation helpers for tool-call payloads, shared by the trigger
// row, the result line, and the expanded field view so all three read a
// payload the same way.

import { stripAnsi } from "@/lib/ansi";
import { isPathologicalText } from "@/lib/markdownLimits";

const ACRONYMS: Record<string, string> = {
  ai: "AI",
  api: "API",
  cli: "CLI",
  csv: "CSV",
  db: "DB",
  html: "HTML",
  http: "HTTP",
  id: "ID",
  ids: "IDs",
  json: "JSON",
  llm: "LLM",
  mcp: "MCP",
  os: "OS",
  pdf: "PDF",
  pr: "PR",
  prs: "PRs",
  sdk: "SDK",
  sql: "SQL",
  ssh: "SSH",
  ui: "UI",
  uri: "URI",
  url: "URL",
  urls: "URLs",
};

/** Lowercased words of a snake_case, kebab-case, dotted, or camelCase identifier. */
export function identifierWords(identifier: string): string[] {
  return identifier
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/([A-Z]+)([A-Z][a-z])/g, "$1 $2")
    .split(/[\s_.-]+/)
    .filter((word) => word.length > 0)
    .map((word) => word.toLowerCase());
}

/** `record[key]` for an own property only, so payload keys like `constructor` miss. */
export function ownValue<T>(record: Readonly<Record<string, T>>, key: string): T | undefined {
  return Object.hasOwn(record, key) ? record[key] : undefined;
}

/** Words as a sentence-case label: `["next", "run", "at"]` -> "Next run at". */
export function sentenceCase(words: string[]): string {
  return words
    .map((word, index) => {
      const acronym = ownValue(ACRONYMS, word);
      if (acronym !== undefined) return acronym;
      return index === 0 ? word.charAt(0).toUpperCase() + word.slice(1) : word;
    })
    .join(" ");
}

/** A payload key as a label: `todoList` -> "Todo list", `next_run_at` -> "Next run at". */
export function humanizeKey(key: string): string {
  const label = sentenceCase(identifierWords(key));
  return label.length > 0 ? label : key;
}

/** A tool output read as JSON when it is a JSON object or array, else as text. */
export type ParsedToolOutput =
  { kind: "json"; value: unknown; text: string } | { kind: "text"; text: string };

/**
 * Parse a tool output for display. Wrapped text is unwrapped first, so a tool
 * that returns JSON reads as that JSON rather than as escaped text: MCP
 * content blocks (`[{"type": "text", "text": "..."}]`) and Omnigent's
 * `{"result": "..."}` wrapper for a harness's non-JSON tool output.
 */
export function parseToolOutput(output: string): ParsedToolOutput {
  const json = parseJsonContainer(output);
  if (json === undefined) return { kind: "text", text: output };
  const wrapped = contentBlockText(json) ?? resultWrapperText(json);
  if (wrapped === null) return { kind: "json", value: json, text: output };
  const inner = parseJsonContainer(wrapped);
  return inner === undefined
    ? { kind: "text", text: wrapped }
    : { kind: "json", value: inner, text: wrapped };
}

/** The text in a `{"result": "..."}` wrapper, or `null` when `value` is not one. */
function resultWrapperText(value: unknown): string | null {
  if (!isRecord(value) || typeof value.result !== "string") return null;
  return Object.keys(value).length === 1 ? value.result : null;
}

function parseJsonContainer(text: string): unknown {
  const trimmed = text.trim();
  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return undefined;
  try {
    return JSON.parse(trimmed) as unknown;
  } catch {
    return undefined;
  }
}

/** Joined text of an MCP content-block array, or `null` when `value` is not one. */
function contentBlockText(value: unknown): string | null {
  if (!Array.isArray(value) || value.length === 0) return null;
  const texts: string[] = [];
  for (const block of value) {
    if (!isRecord(block) || typeof block.type !== "string") return null;
    if (block.type === "text") {
      if (typeof block.text !== "string") return null;
      texts.push(block.text);
    }
  }
  return texts.length > 0 ? texts.join("\n\n") : null;
}

// Prefixes tools and MCP servers use for a failed call, e.g. "Error: 422 ...",
// "MCP error -32602: ...", "Error executing tool x: ...".
const ERROR_TEXT_RE = /^(?:(?:error|fatal)\s*:|mcp error\b|error (?:executing|calling|running)\b)/i;
const TOOL_USE_ERROR_RE = /^<tool_use_error>([\s\S]*?)(?:<\/tool_use_error>)?\s*$/;
const UNNAMED_ERROR = "The tool reported an error.";

/** The failure message a tool reported in its output, or `null` when it succeeded. */
export function toolErrorMessage(parsed: ParsedToolOutput): string | null {
  if (parsed.kind === "json") {
    if (!isRecord(parsed.value)) return null;
    const { error, message } = parsed.value;
    if (typeof error === "string" && error.length > 0) return error;
    if (isRecord(error)) return nonEmpty(error.message) ?? UNNAMED_ERROR;
    if (error === true) return nonEmpty(message) ?? UNNAMED_ERROR;
    return null;
  }
  const text = stripAnsi(parsed.text).trim();
  const wrapped = TOOL_USE_ERROR_RE.exec(text);
  if (wrapped !== null) return nonEmpty(wrapped[1]!.trim()) ?? UNNAMED_ERROR;
  return ERROR_TEXT_RE.test(text) ? text : null;
}

function nonEmpty(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

/** First non-blank line of a text, without ANSI escapes. */
export function firstLine(text: string): string {
  for (const line of stripAnsi(text).split("\n")) {
    const trimmed = line.trim();
    if (trimmed.length > 0) return trimmed;
  }
  return "";
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// Field names that carry a record's display title, its supporting detail, and
// its lifecycle status, across the shapes MCP servers and harness tools return.
const TITLE_KEYS = [
  "title",
  "name",
  "label",
  "subject",
  "summary",
  "content",
  "step",
  "text",
  "message",
] as const;
const DETAIL_KEYS = ["description", "detail", "details", "body", "reason"] as const;
const STATUS_KEYS = ["status", "state"] as const;

const TITLE_MAX_CHARS = 200;

export interface PickedField {
  key: string;
  value: string;
}

/** The first of `keys` holding a non-blank string accepted by `accept`. */
function pickString(
  record: Record<string, unknown>,
  keys: readonly string[],
  accept: (value: string) => boolean = () => true,
): PickedField | null {
  for (const key of keys) {
    const value = record[key];
    if (typeof value !== "string") continue;
    const trimmed = value.trim();
    if (trimmed.length > 0 && accept(trimmed)) return { key, value: trimmed };
  }
  return null;
}

/** The single-line field that titles a record (`title`, `name`, ...), or `null`. */
export function pickTitleField(record: Record<string, unknown>): PickedField | null {
  // A multi-line `content` or `text` is a document body, not a title.
  return pickString(
    record,
    TITLE_KEYS,
    (value) => !value.includes("\n") && value.length <= TITLE_MAX_CHARS,
  );
}

/** The field with a record's supporting detail (`description`, ...), or `null`. */
export function pickDetailField(record: Record<string, unknown>): PickedField | null {
  return pickString(record, DETAIL_KEYS);
}

/** The field with a record's lifecycle status (`status`, `state`), or `null`. */
export function pickStatusField(record: Record<string, unknown>): PickedField | null {
  return pickString(record, STATUS_KEYS, (value) => value.length <= 40);
}

export type StatusTone = "success" | "active" | "failure" | "neutral";

const STATUS_TONES: Record<string, StatusTone> = {
  completed: "success",
  complete: "success",
  done: "success",
  success: "success",
  succeeded: "success",
  passed: "success",
  merged: "success",
  resolved: "success",
  "in-progress": "active",
  running: "active",
  active: "active",
  started: "active",
  failed: "failure",
  failure: "failure",
  error: "failure",
  errored: "failure",
  cancelled: "failure",
  canceled: "failure",
  blocked: "failure",
  rejected: "failure",
};

/** How a status value should be colored: "in_progress" -> "active". */
export function statusTone(status: string): StatusTone {
  const key = status
    .trim()
    .toLowerCase()
    .replace(/[\s_]+/g, "-");
  return ownValue(STATUS_TONES, key) ?? "neutral";
}

/** A status value as words: "in_progress" -> "in progress". */
export function statusLabel(status: string): string {
  return status.trim().replace(/[_-]+/g, " ");
}

const ISO_TIMESTAMP_RE =
  /^(\d{4})-(\d{2})-(\d{2})T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?$/;

/** Epoch milliseconds of an ISO-8601 date-time string, or `null` for anything else. */
export function isoTimestampMs(text: string): number | null {
  const match = ISO_TIMESTAMP_RE.exec(text);
  if (match === null) return null;
  // Date.parse rolls an impossible date (February 30) into the next month.
  const [year, month, day] = match.slice(1, 4).map(Number) as [number, number, number];
  const calendar = new Date(Date.UTC(year, month - 1, day));
  if (calendar.getUTCMonth() !== month - 1 || calendar.getUTCDate() !== day) return null;
  const ms = Date.parse(text);
  return Number.isFinite(ms) ? ms : null;
}

// The only field names whose text renders as markdown (an issue description, a PR body).
const PROSE_KEY_WORDS = new Set(["body", "comment", "description", "note", "summary"]);
// Field names whose text is source, a command, output or data, which must keep its exact layout.
const CODE_KEY_WORDS = new Set([
  "cmd",
  "code",
  "command",
  "content",
  "contents",
  "diff",
  "html",
  "json",
  "log",
  "logs",
  "output",
  "patch",
  "pattern",
  "query",
  "regex",
  "script",
  "source",
  "sql",
  "stack",
  "stderr",
  "stdout",
  "str",
  "string",
  "traceback",
  "xml",
  "yaml",
]);
// Tags in GitHub bodies, such as `<details>`, `<summary>` and `<br>`, which render as markdown.
const GITHUB_BODY_TAG = /<\/?(?:details|summary|br|sub|sup|kbd)(?![\w-])(?:\s[^<>]*)?\/?>/gi;
// A markup tag such as `<instructions>`, even split over lines or opening a quoted value, which
// may hold `<` or `>`; autolinks do not match.
const MARKUP_TAG = /<\/?[A-Za-z][\w-]*(?:\s[^<>]*)?\/?>|<[A-Za-z][\w-]*\s[^<>]*=\s*["']/;
// A line opening an HTML block: one starting with `<`, but not an autolink such as `<https://x>`.
const HTML_BLOCK_START = /^<(?![A-Za-z][A-Za-z\d+.-]{1,31}:[^\s<>]*>)/;

// Indentation, `>` quote markers and list bullets that can nest a block in a line.
const CONTAINER_PREFIX = /^(?:[ \t>]|(?:[-*+]|\d{1,9}[.)])[ \t])*/;
// A fence indented at most three spaces; a deeper one is code or nested in a list.
const PLAIN_FENCE = /^( {0,3})(`{3,}|~{3,})(.*)$/;
// A link reference definition such as `[//]: # (note)`, which renders as nothing; its label
// may escape brackets and run over lines, up to markdown's 999 characters.
const REFERENCE_DEFINITION = new RegExp(
  String.raw`${CONTAINER_PREFIX.source}\[(?:[^\\\]]|\\[\s\S]){0,999}\]:`,
  "m",
);

// Drops closed fences, and code spans outside HTML blocks; from an unclear fence, keeps the rest.
function withoutCode(text: string): string {
  const lines = text.split("\n");
  const kept: string[] = [];
  let fence: { start: number; indent: string; marker: string } | null = null;
  // What ends the HTML block in progress, in which fences and code spans are raw HTML.
  let htmlBlockEnd: "blank line" | "-->" | null = null;
  // Whether the paragraph so far leaves no code span or HTML tag open.
  let spansClear = true;
  for (const [index, line] of lines.entries()) {
    const content = line.replace(CONTAINER_PREFIX, "");
    const fenceLike = /^(?:`{3}|~{3})/.test(content);
    const [, indent = "", marker = "", info = ""] = PLAIN_FENCE.exec(line) ?? [];
    // A nested fence, one in an HTML block or with a backtick in its info may not be a fence.
    // In a list item's fence, a shallower line ends the item and a deeper marker may close it.
    const unclear =
      fence === null
        ? fenceLike &&
          (htmlBlockEnd !== null || marker === "" || (marker[0] === "`" && info.includes("`")))
        : fence.indent !== "" &&
          ((line.trim() !== "" && !line.startsWith(fence.indent)) || (fenceLike && marker === ""));
    if (unclear) return [...kept, ...lines.slice(index)].join("\n");
    if (fence !== null) {
      if (
        marker[0] === fence.marker[0] &&
        marker.length >= fence.marker.length &&
        info.trim() === ""
      ) {
        kept.length = fence.start;
        fence = null;
        continue;
      }
      kept.push(line);
    } else if (fenceLike) {
      fence = { start: kept.length, indent, marker };
      kept.push(line);
    } else if (htmlBlockEnd !== null) {
      if (htmlBlockEnd === "-->" ? line.includes("-->") : line.trim() === "") htmlBlockEnd = null;
      kept.push(line);
    } else if (HTML_BLOCK_START.test(content)) {
      const comment = content.startsWith("<!--");
      htmlBlockEnd = !comment ? "blank line" : content.includes("-->", 4) ? null : "-->";
      kept.push(line);
    } else if (spansClear) {
      const { text: withoutSpans, clear } = withoutCodeSpans(line);
      kept.push(withoutSpans);
      spansClear = clear;
    } else {
      kept.push(line);
      spansClear = line.trim() === "";
    }
  }
  return kept.join("\n");
}

// `line` without its code spans, read left to right as markdown does. From an HTML tag, a link
// target that may hold a backtick or tag, or a backtick run with no partner on the line, the
// rest stays and `clear` is false.
function withoutCodeSpans(line: string): { text: string; clear: boolean } {
  const special = /\\|<[A-Za-z/!?]|\]\(|`+/g;
  let text = "";
  let position = 0;
  for (let match = special.exec(line); match !== null; match = special.exec(line)) {
    if (match[0] === "\\") {
      special.lastIndex = match.index + 2;
      continue;
    }
    if (match[0] === "](") {
      // Markdown takes a link's destination and title whole, backticks included, even over lines.
      const target = /(\S*)(\s+["'(])?/y;
      target.lastIndex = special.lastIndex;
      const [, destination = "", title] = target.exec(line) ?? [];
      const closed = destination.includes(")") && title === undefined;
      if (closed && !/[`<]/.test(destination)) continue;
      return { text: text + line.slice(position), clear: false };
    }
    const runs = /`+/g;
    runs.lastIndex = special.lastIndex;
    let closer = match[0].startsWith("<") ? null : runs.exec(line);
    while (closer !== null && closer[0].length !== match[0].length) closer = runs.exec(line);
    if (closer === null) return { text: text + line.slice(position), clear: false };
    text += line.slice(position, match.index);
    position = runs.lastIndex;
    special.lastIndex = position;
  }
  return { text: text + line.slice(position), clear: true };
}

// Whether an HTML block, up to a blank line, holds a tag other than GitHub's, even an unfinished
// one. Markdown passes such a block raw, where `<pre` swallows the text after it.
function hidesInHtmlBlock(text: string): boolean {
  let inBlock = false;
  for (const line of text.split("\n")) {
    const opens = HTML_BLOCK_START.test(line.replace(CONTAINER_PREFIX, ""));
    inBlock = line.trim() !== "" && (inBlock || opens);
    if (inBlock && /<[A-Za-z/]/.test(line.replace(GITHUB_BODY_TAG, ""))) return true;
  }
  return false;
}

// `text` without its closed `<!-- -->` comments, which markdown hides; linear in its length.
function withoutHtmlComments(text: string): string {
  let kept = "";
  let position = 0;
  for (;;) {
    const open = text.indexOf("<!--", position);
    const close = open < 0 ? -1 : text.indexOf("-->", open + 4);
    if (close < 0) return kept + text.slice(position);
    kept += text.slice(position, open);
    position = close + 3;
  }
}

// JSON, XML or an HTML document, but not prose opening with `[link](url)` or `[1] See`.
function looksLikeData(text: string): boolean {
  const start = text.trimStart();
  if (/^(?:\{|<\?xml\b|<!DOCTYPE\b)/i.test(start)) return true;
  if (!start.startsWith("[")) return false;
  // `[{`, `["`, `[[` and `[]` open an array even when the text is cut off.
  if (/^\[\s*[[{"\]]/.test(start)) return true;
  try {
    JSON.parse(start);
    return true;
  } catch {
    return false;
  }
}

// Whether a long string is prose to render as markdown. Only a prose field name
// marks prose, and code-like names, data, markup and indented code stay literal.
export function isProseText(text: string, key?: string): boolean {
  const keyWords = key === undefined ? [] : identifierWords(key);
  if (!keyWords.some((word) => hasKeyWord(PROSE_KEY_WORDS, word))) return false;
  if (keyWords.some((word) => hasKeyWord(CODE_KEY_WORDS, word))) return false;
  if (isPathologicalText(text) || looksLikeData(text)) return false;
  // Fenced and inline code render literally as markdown too, so only the rest is checked.
  const outsideCode = withoutHtmlComments(withoutCode(text));
  const markup = outsideCode.replace(GITHUB_BODY_TAG, "");
  // A markup tag, an unclosed `<!--`, `<?`, `<![CDATA[` or `<!X`, or a raw HTML block hides text.
  if (MARKUP_TAG.test(markup) || /<[?!]/.test(markup) || hidesInHtmlBlock(outsideCode)) {
    return false;
  }
  if (REFERENCE_DEFINITION.test(outsideCode)) return false;
  const lines = outsideCode.split("\n").filter((line) => line.trim().length > 0);
  const codeLike = lines.filter(
    (line) => !/^\s+(?:[-*+]|\d+\.) /.test(line) && /^(?:\t| {4})|[;{}]\s*$/.test(line),
  ).length;
  return codeLike * 4 <= lines.length;
}

// A key word or its plural (`commands`, `patches`, `queries`) in `words`.
function hasKeyWord(words: ReadonlySet<string>, word: string): boolean {
  const singulars = [
    word,
    word.replace(/s$/, ""),
    word.replace(/es$/, ""),
    word.replace(/ies$/, "y"),
  ];
  return singulars.some((singular) => words.has(singular));
}
