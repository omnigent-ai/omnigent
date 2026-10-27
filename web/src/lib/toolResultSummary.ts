// One-line outcome shown under a tool call's row, so a collapsed transcript
// reads as "did X, got Y" and failures are visible without expanding.

import type { ToolState } from "@/lib/renderItems";
import { type ShellToolOutput, parseShellToolOutput } from "@/lib/shellToolOutput";
import { shellToolCall, toolCallCategory } from "@/lib/toolTitle";
import {
  type ParsedToolOutput,
  firstLine,
  humanizeKey,
  isRecord,
  isoTimestampMs,
  parseToolOutput,
  pickStatusField,
  pickTitleField,
  statusLabel,
  toolErrorMessage,
} from "@/lib/toolValue";
import { absoluteTime } from "@/lib/relativeTime";

/** A tool call as the transcript renders it. */
export interface ToolCall {
  name: string;
  args: Record<string, unknown>;
  /** Tool output, or `null` if not yet available / never produced. */
  output: string | null;
  state: ToolState;
}

export interface ToolResultSummary {
  text: string;
  isError: boolean;
}

const SUMMARY_MAX_CHARS = 300;
const FIELD_SUMMARY_PARTS = 2;
const FILE_SEARCH_TOOLS = new Set(["Grep", "Glob", "grep", "glob"]);
// Harness file reads number each line, after a tab, an arrow (U+2192), or a
// pipe: "    12\tcode", "00012| code".
const NUMBERED_LINE_RE = /^\s*\d+(?:\t|\u2192|\| )/;
// Notes Claude Code appends to a read; they are not lines of the file.
const SYSTEM_REMINDER_RE = /<system-reminder>[\s\S]*?<\/system-reminder>/g;

/**
 * Summarize a finished call's result, or return `null` when there is nothing
 * worth a line: still running, no output yet, or a successful shell command
 * (its panel already shows the output).
 */
export function summarizeToolResult(call: ToolCall): ToolResultSummary | null {
  if (call.output === null || call.state === "input-available") return null;
  const shellCall = shellToolCall(call.name, call.args);
  if (shellCall !== null) {
    const result = parseShellToolOutput(call.output, shellCall.outputFormat);
    // Formats without an exit code (Claude's Bash) still report failures in the text.
    if (result.exitCode === null && !result.timedOut) {
      const error = toolErrorMessage(parseToolOutput(call.output));
      if (error !== null) return failure(firstLine(error));
    }
    return summarizeShellResult(result);
  }
  const parsed = parseToolOutput(call.output);
  const error = toolErrorMessage(parsed);
  if (error !== null) return failure(firstLine(error));

  const category = toolCallCategory({ name: call.name, args: call.args });
  if (category === "read") return summarizeRead(parsed);
  if (category === "edit") return null;
  if (FILE_SEARCH_TOOLS.has(call.name)) return summarizeFileSearch(parsed);
  return summarizeGeneric(parsed);
}

function summarizeShellResult(result: ShellToolOutput): ToolResultSummary | null {
  if (result.timedOut) return failure("Timed out");
  if (result.exitCode === null || result.exitCode === 0) return null;
  // A failing command's last line is usually its error or test summary.
  const lines = result.text.split("\n").map((line) => line.trim());
  const lastLine = lines.filter((line) => line.length > 0).at(-1);
  const status = `Exit code ${result.exitCode}`;
  return failure(lastLine === undefined ? status : `${status}: ${firstLine(lastLine)}`);
}

function summarizeRead(parsed: ParsedToolOutput): ToolResultSummary | null {
  let text = parsed.text;
  if (parsed.kind === "json") {
    if (!isRecord(parsed.value) || typeof parsed.value.content !== "string") {
      return summarizeGeneric(parsed);
    }
    text = parsed.value.content;
  }
  const body = text.replace(SYSTEM_REMINDER_RE, "").replace(/\n$/, "");
  const lines = body.split("\n");
  let count = lines.filter((line) => NUMBERED_LINE_RE.test(line)).length;
  if (count === 0 && body.trim().length > 0) count = lines.length;
  return success(count === 0 ? "Empty file" : plural(count, "line"));
}

function summarizeFileSearch(parsed: ParsedToolOutput): ToolResultSummary {
  const first = firstLine(parsed.text);
  // Claude Code reports its own count ("Found 3 files", "No files found").
  if (/^(?:found \d+|no )/i.test(first)) return success(first);
  const results = parsed.text.split("\n").filter((line) => line.trim().length > 0).length;
  return success(results === 0 ? "No results" : plural(results, "result"));
}

function summarizeGeneric(parsed: ParsedToolOutput): ToolResultSummary {
  if (parsed.kind === "text") {
    const line = firstLine(parsed.text);
    return success(line.length > 0 ? line : "No output");
  }
  const { value } = parsed;
  if (Array.isArray(value)) {
    return success(value.length === 0 ? "No results" : plural(value.length, "result"));
  }
  if (!isRecord(value)) return success(String(value));
  const title = pickTitleField(value);
  if (title !== null) {
    const status = pickStatusField(value);
    return success(status === null ? title.value : `${title.value} · ${statusLabel(status.value)}`);
  }
  const fields = Object.entries(value)
    .filter(([, field]) => typeof field === "string" || typeof field === "number")
    .slice(0, FIELD_SUMMARY_PARTS)
    .map(([key, field]) => `${humanizeKey(key)} ${scalarText(field as string | number)}`);
  return success(
    fields.length > 0 ? fields.join(" · ") : plural(Object.keys(value).length, "field"),
  );
}

function scalarText(value: string | number): string {
  if (typeof value === "number") return String(value);
  const ms = isoTimestampMs(value);
  return ms === null ? firstLine(value) : absoluteTime(ms);
}

function success(text: string): ToolResultSummary {
  return { text: text.slice(0, SUMMARY_MAX_CHARS), isError: false };
}

function failure(text: string): ToolResultSummary {
  return { text: text.slice(0, SUMMARY_MAX_CHARS), isError: true };
}

function plural(count: number, noun: string): string {
  return `${count.toLocaleString()} ${noun}${count === 1 ? "" : "s"}`;
}
