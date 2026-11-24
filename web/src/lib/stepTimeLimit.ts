// A workflow step's time limit lives in its description, as a line of its own such as
// "**Time limit:** 2 hours" between Outputs and Details, where the workflow skill reads it.

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

const LIMIT_LINE = /^[ \t]*\*\*Time limit:\*\*[ \t]*(.*?)[ \t]*$/i;
const DETAILS_LINE = /^[ \t]*\*\*Details:\*\*/i;
// One part of a length, e.g. "1 hour" in "1 hour 30 minutes", with what separates it from the next.
const LENGTH_PART =
  /^(\d+(?:\.\d+)?)\s*(minutes?|mins?|m|hours?|hrs?|h|days?|d)\b[\s,]*(?:and\s+)?/i;

/** The step's time limit as its description words it, e.g. "2 hours"; null without one. */
export function stepTimeLimit(goal: string | null): string | null {
  for (const line of goal?.split("\n") ?? []) {
    const match = LIMIT_LINE.exec(line);
    if (match) return match[1] || null;
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
  if (rest === text) return null;
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
 * limit replaces the description's own Time limit line, or goes before Details, or last.
 *
 * @returns The new description; null when nothing else is left.
 */
export function withStepTimeLimit(goal: string | null, limit: string | null): string | null {
  const lines = goal ? goal.split("\n") : [];
  const limitLine = limit ? `**Time limit:** ${limit}` : null;
  const at = lines.findIndex((line) => LIMIT_LINE.test(line));
  if (at >= 0 && limitLine) {
    lines[at] = limitLine;
  } else if (at >= 0) {
    lines.splice(at, 1);
    // Keep one blank line between the paragraphs on either side.
    if (lines[at - 1]?.trim() === "" && lines[at]?.trim() === "") lines.splice(at, 1);
  } else if (limitLine) {
    const details = lines.findIndex((line) => DETAILS_LINE.test(line));
    if (details < 0) {
      if (lines.length > 0 && lines.at(-1)!.trim() !== "") lines.push("");
      lines.push(limitLine);
    } else {
      const before = details > 0 && lines[details - 1]!.trim() !== "" ? [""] : [];
      lines.splice(details, 0, ...before, limitLine, "");
    }
  }
  const text = lines.join("\n").trim();
  return text || null;
}

/**
 * A proposed plan's steps with the saved workflow's time limits, for a proposal written before
 * the workflow's last save, so a limit set or cleared since then stands.
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
