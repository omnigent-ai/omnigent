// A workflow step's time limit lives in its description, as a line of its own such as
// "**Time limit:** 2 hours" between Outputs and Details, where the workflow skill reads it.

import remarkParse from "remark-parse";
import { unified } from "unified";

import { formatDuration } from "@/lib/relativeTime";
import type { WorkflowStep } from "@/lib/workflowGraphLayout";

/** The limits a step's time limit menu offers, in the words it writes. */
export const TIME_LIMIT_CHOICES = [
  "30 minutes",
  "1 hour",
  "2 hours",
  "4 hours",
  "8 hours",
  "1 day",
] as const;

// A line of its own, as a paragraph, a list item or a quote, with the colon in the bold or just
// after it, e.g. "- **Time limit:** 2 hours" or "1. **Time limit**: 2 hours".
const LIMIT_LINE =
  /^([ \t]*(?:>[ \t]?)*[ \t]*(?:(?:[-*+]|\d{1,9}[.)])[ \t]+)?)\*\*Time limit(?::\*\*|\*\*[ \t]*:)[ \t]*(.*?)[ \t]*$/i;
const DETAILS_LINE = /^([ \t]*)((?:[-*+]|\d{1,9}[.)])[ \t]+)?\*\*Details(?::\*\*|\*\*[ \t]*:)/i;
// Most descriptions name no limit, and those need no parsing.
const LIMIT_MARK = /\*\*Time limit(?::\*\*|\*\*[ \t]*:)/i;
// Minutes in a length's unit, by the unit's first letter.
const UNIT_MINUTES: Readonly<Record<string, number>> = { d: 1440, h: 60, m: 1 };
// One part of a length, e.g. "1 hour" in "1 hour 30 minutes" or "1h" in "1h30m", with what
// separates it from the next.
const LENGTH_PART =
  /^(\d+(?:\.\d+)?)\s*(minutes?|mins?|m|hours?|hrs?|h|days?|d)(?![a-z])[\s,]*(?:and\s+)?/i;

/** A description's lines, which may break with CRLF. */
function descriptionLines(goal: string | null): string[] {
  return goal ? goal.split(/\r?\n/) : [];
}

interface CodeBlock {
  /** Its first and last lines, from 0. */
  start: number;
  end: number;
}

/** Where a description holds code, as Markdown reads it. */
interface CodeLines {
  /** Fenced or indented code blocks, fences included. */
  blocks: readonly CodeBlock[];
  /** The lines that start inside an inline code span, which can run across lines. */
  spans: readonly CodeBlock[];
}

const NO_CODE: CodeLines = { blocks: [], spans: [] };

interface MarkdownNode {
  type: string;
  position?: { start: { line: number }; end: { line: number } };
  children?: MarkdownNode[];
}

// A page reads the same descriptions on every render, and parsing is the costly part.
const parsedCode = new Map<string, CodeLines>();
const PARSED_CODE_KEPT = 200;

function codeLines(lines: readonly string[]): CodeLines {
  const text = lines.join("\n");
  const parsed = parsedCode.get(text);
  if (parsed) return parsed;
  const blocks: CodeBlock[] = [];
  const spans: CodeBlock[] = [];
  const visit = (node: MarkdownNode) => {
    const { position } = node;
    if (node.type === "code" && position) {
      blocks.push({ start: position.start.line - 1, end: position.end.line - 1 });
      return;
    }
    if (node.type === "inlineCode" && position && position.end.line > position.start.line) {
      // From the line after the one it opens on.
      spans.push({ start: position.start.line, end: position.end.line - 1 });
      return;
    }
    for (const child of node.children ?? []) visit(child);
  };
  visit(unified().use(remarkParse).parse(text) as MarkdownNode);
  const code = { blocks, spans };
  if (parsedCode.size >= PARSED_CODE_KEPT) parsedCode.delete(parsedCode.keys().next().value!);
  parsedCode.set(text, code);
  return code;
}

/** The indexes of the lines *pattern* matches outside code, where an example names no limit. */
function matchingLines(
  lines: readonly string[],
  pattern: RegExp,
  code: CodeLines = codeLines(lines),
): number[] {
  const ranges = [...code.blocks, ...code.spans];
  const inCode = (index: number) => ranges.some(({ start, end }) => start <= index && index <= end);
  return lines.flatMap((line, index) => (!inCode(index) && pattern.test(line) ? [index] : []));
}

/** The step's time limit as its description words it, e.g. "2 hours"; null without one. */
export function stepTimeLimit(goal: string | null): string | null {
  if (!goal || !LIMIT_MARK.test(goal)) return null;
  const lines = descriptionLines(goal);
  for (const index of matchingLines(lines, LIMIT_LINE)) {
    // An empty line names no limit; a later one may.
    const text = LIMIT_LINE.exec(lines[index]!)?.[2];
    if (text) return text;
  }
  return null;
}

/** The length a limit starts with, e.g. "1 hour 30 minutes", and how many minutes it is. */
function leadingLength(limit: string): { text: string; minutes: number } | null {
  const text = limit.trim();
  let rest = text;
  let minutes = 0;
  for (let part = LENGTH_PART.exec(rest); part; part = LENGTH_PART.exec(rest)) {
    minutes += Number(part[1]) * UNIT_MINUTES[part[2]![0]!.toLowerCase()]!;
    rest = rest.slice(part[0].length);
  }
  if (rest === text || minutes === 0) return null;
  const length = text.slice(0, text.length - rest.length).replace(/[\s,]*(?:and\s*)?$/i, "");
  return { text: length, minutes };
}

/** How long a limit allows in minutes, e.g. 90 for "1 hour 30 minutes"; null without a length. */
export function timeLimitMinutes(limit: string): number | null {
  return leadingLength(limit)?.minutes ?? null;
}

/** The length a limit starts with, e.g. "2 hours" for "2 hours, then tell me"; null without one. */
export function timeLimitLength(limit: string): string | null {
  return leadingLength(limit)?.text ?? null;
}

/** A limit in a few characters, e.g. "2h" for "2 hours"; null when it starts with no length. */
export function shortTimeLimit(limit: string): string | null {
  const minutes = timeLimitMinutes(limit);
  return minutes === null ? null : formatDuration(minutes * 60);
}

/**
 * The step's description with its time limit set to *limit*, or without one for null. The
 * limit replaces the description's first Time limit line, and any others go; without one it
 * goes before Details, or last.
 *
 * @returns The new description; null when nothing else is left.
 */
export function withStepTimeLimit(goal: string | null, limit: string | null): string | null {
  const lines = descriptionLines(goal);
  const limitLine = limit ? `**Time limit:** ${limit}` : null;
  const code = limitLine || (goal && LIMIT_MARK.test(goal)) ? codeLines(lines) : NO_CODE;
  const found = matchingLines(lines, LIMIT_LINE, code);
  const kept = limitLine ? found[0] : undefined;
  // From the last, so the earlier lines keep their places.
  for (const at of [...found].reverse()) {
    if (at === kept) {
      // Its indent, quote and list marker stay, so the line stays in its list or quote.
      lines[at] = `${LIMIT_LINE.exec(lines[at]!)![1]}${limitLine}`;
      continue;
    }
    lines.splice(at, 1);
    // Keep one blank line between the paragraphs on either side.
    if (lines[at - 1]?.trim() === "" && lines[at]?.trim() === "") lines.splice(at, 1);
  }
  if (found.length === 0 && limitLine) {
    const details = matchingLines(lines, DETAILS_LINE, code)[0] ?? -1;
    const detailsMatch = details < 0 ? null : DETAILS_LINE.exec(lines[details]!);
    const listMarker = detailsMatch?.[2] ? `${detailsMatch[1]}${detailsMatch[2]}` : "";
    if (listMarker) {
      // Details is a list item, so the limit joins the list just above it.
      lines.splice(details, 0, `${listMarker}${limitLine}`);
    } else if (details >= 0) {
      const before = details > 0 && lines[details - 1]!.trim() !== "" ? [""] : [];
      lines.splice(details, 0, ...before, limitLine, "");
    } else {
      // Last, unless a code block left open at the end would take the line in, as Markdown
      // reads it: then just before that block.
      const tail = lines.length > 0 && lines.at(-1)!.trim() !== "" ? ["", limitLine] : [limitLine];
      const last = lines.length + tail.length - 1;
      const open = codeLines([...lines, ...tail]).blocks.find(
        ({ start, end }) => start < lines.length && end === last,
      );
      if (open) {
        const before = open.start > 0 && lines[open.start - 1]!.trim() !== "" ? [""] : [];
        lines.splice(open.start, 0, ...before, limitLine, "");
      } else {
        lines.push(...tail);
      }
    }
  }
  // Only the edited lines change: the line breaks stay, and only blank lines at the ends go.
  const text = lines
    .join(goal?.includes("\r\n") ? "\r\n" : "\n")
    .replace(/^(?:[ \t]*\r?\n)+/, "")
    .replace(/(?:\r?\n[ \t]*)+$/, "");
  return text.trim() ? text : null;
}

/**
 * The step's description with its time limit's length set to *length*, keeping any words
 * after the old length, e.g. "8 hours. Tell me after." from "4 hours. Tell me after.".
 */
export function withTimeLimitLength(goal: string | null, length: string): string | null {
  const limit = stepTimeLimit(goal);
  const old = limit ? timeLimitLength(limit) : null;
  return withStepTimeLimit(goal, limit && old ? `${length}${limit.slice(old.length)}` : length);
}

/** The step's description with the limit its menu chose, or without one for null. */
export function withChosenTimeLimit(goal: string | null, limit: string | null): string | null {
  return limit === null ? withStepTimeLimit(goal, null) : withTimeLimitLength(goal, limit);
}

/**
 * A proposed plan's steps with the saved workflow's time limits. A saved step's limit is set
 * from its menu, so a proposal leaves it alone; a step the proposal adds keeps its own.
 */
export function withSavedTimeLimits(
  proposed: readonly WorkflowStep[],
  saved: readonly WorkflowStep[],
): WorkflowStep[] {
  const savedLimits = new Map(saved.map((step) => [step.id, stepTimeLimit(step.goal)]));
  return proposed.map((step) => {
    if (!savedLimits.has(step.id)) return step;
    const limit = savedLimits.get(step.id) ?? null;
    return stepTimeLimit(step.goal) === limit
      ? step
      : { ...step, goal: withStepTimeLimit(step.goal, limit) };
  });
}

/** A step's description without its time limit, and the limit, to compare steps by. */
export function splitTimeLimit(goal: string | null): [string | null, string | null] {
  return [withStepTimeLimit(goal, null), stepTimeLimit(goal)];
}
