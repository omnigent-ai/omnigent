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

// A line of its own, as a paragraph or a list item, e.g. "- **Time limit:** 2 hours".
const LIMIT_LINE = /^([ \t]*(?:[-*+][ \t]+)?)\*\*Time limit:\*\*[ \t]*(.*?)[ \t]*$/i;
const DETAILS_LINE = /^([ \t]*)([-*+][ \t]+)?\*\*Details:\*\*/i;
// A fence's run of backticks or tildes, and what follows it.
const FENCE = /^[ \t]*(`{3,}|~{3,})(.*)$/;
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

interface MarkdownNode {
  type: string;
  position?: { start: { line: number }; end: { line: number } };
  children?: MarkdownNode[];
}

/** A description's code blocks, fenced or indented, as Markdown reads them. */
function codeBlocks(lines: readonly string[]): CodeBlock[] {
  const blocks: CodeBlock[] = [];
  const visit = (node: MarkdownNode) => {
    if (node.type === "code" && node.position) {
      blocks.push({ start: node.position.start.line - 1, end: node.position.end.line - 1 });
      return;
    }
    for (const child of node.children ?? []) visit(child);
  };
  visit(unified().use(remarkParse).parse(lines.join("\n")) as MarkdownNode);
  return blocks;
}

/** The indexes of the lines *pattern* matches outside code blocks, where an example names no limit. */
function matchingLines(
  lines: readonly string[],
  pattern: RegExp,
  blocks: readonly CodeBlock[] = codeBlocks(lines),
): number[] {
  const inCode = (index: number) => blocks.some(({ start, end }) => start <= index && index <= end);
  return lines.flatMap((line, index) => (!inCode(index) && pattern.test(line) ? [index] : []));
}

/** The fenced code block a description leaves open at its end, which would take in a new last line. */
function openBlockAtEnd(lines: readonly string[], blocks: readonly CodeBlock[]): CodeBlock | null {
  const last = blocks.at(-1);
  if (!last || last.end !== lines.length - 1) return null;
  const opening = FENCE.exec(lines[last.start]!)?.[1];
  if (!opening) return null;
  const closing = last.end > last.start ? FENCE.exec(lines[last.end]!) : null;
  const closed =
    !!closing &&
    closing[1]![0] === opening[0] &&
    closing[1]!.length >= opening.length &&
    !closing[2]!.trim();
  return closed ? null : last;
}

/** The step's time limit as its description words it, e.g. "2 hours"; null without one. */
export function stepTimeLimit(goal: string | null): string | null {
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
    const unit = part[2]!.toLowerCase();
    minutes += Number(part[1]) * (unit.startsWith("d") ? 1440 : unit.startsWith("h") ? 60 : 1);
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
  const blocks = codeBlocks(lines);
  const found = matchingLines(lines, LIMIT_LINE, blocks);
  const kept = limitLine ? found[0] : undefined;
  // From the last, so the earlier lines keep their places.
  for (const at of [...found].reverse()) {
    if (at === kept) {
      // A list item stays one.
      lines[at] = `${LIMIT_LINE.exec(lines[at]!)![1]}${limitLine}`;
      continue;
    }
    lines.splice(at, 1);
    // Keep one blank line between the paragraphs on either side.
    if (lines[at - 1]?.trim() === "" && lines[at]?.trim() === "") lines.splice(at, 1);
  }
  if (found.length === 0 && limitLine) {
    const details = matchingLines(lines, DETAILS_LINE, blocks)[0] ?? -1;
    const open = openBlockAtEnd(lines, blocks);
    const detailsMatch = details < 0 ? null : DETAILS_LINE.exec(lines[details]!);
    const listMarker = detailsMatch?.[2] ? `${detailsMatch[1]}${detailsMatch[2]}` : "";
    if (listMarker) {
      // Details is a list item, so the limit joins the list just above it.
      lines.splice(details, 0, `${listMarker}${limitLine}`);
    } else if (details >= 0) {
      const before = details > 0 && lines[details - 1]!.trim() !== "" ? [""] : [];
      lines.splice(details, 0, ...before, limitLine, "");
    } else if (open) {
      // A code block left open at the end would take in the limit, so it goes before the block.
      const before = open.start > 0 && lines[open.start - 1]!.trim() !== "" ? [""] : [];
      lines.splice(open.start, 0, ...before, limitLine, "");
    } else {
      if (lines.length > 0 && lines.at(-1)!.trim() !== "") lines.push("");
      lines.push(limitLine);
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
