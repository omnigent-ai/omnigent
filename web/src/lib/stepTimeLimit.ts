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
const LENGTH = /^(\d+(?:\.\d+)?)\s*(minutes?|mins?|m|hours?|hrs?|h|days?|d)\b/i;

/** The step's time limit as its description words it, e.g. "2 hours"; null without one. */
export function stepTimeLimit(goal: string | null): string | null {
  for (const line of goal?.split("\n") ?? []) {
    const match = LIMIT_LINE.exec(line);
    if (match) return match[1] || null;
  }
  return null;
}

/** How long a limit allows in minutes, e.g. 120 for "2 hours"; null when it starts with no length. */
export function timeLimitMinutes(limit: string): number | null {
  const match = LENGTH.exec(limit.trim());
  if (!match) return null;
  const unit = match[2]!.toLowerCase();
  return Number(match[1]) * (unit.startsWith("d") ? 1440 : unit.startsWith("h") ? 60 : 1);
}

/** The length a limit starts with, e.g. "2 hours" for "2 hours, then tell me"; null without one. */
export function timeLimitLength(limit: string): string | null {
  return LENGTH.exec(limit.trim())?.[0] ?? null;
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
 * A proposed plan's steps, each keeping its saved step's time limit unless the proposal words
 * its own, so a limit set on the saved workflow outlasts a proposal written before it.
 */
export function withSavedTimeLimits(
  proposed: readonly WorkflowStep[],
  saved: readonly WorkflowStep[],
): WorkflowStep[] {
  const savedLimits = new Map(saved.map((step) => [step.id, stepTimeLimit(step.goal)]));
  return proposed.map((step) => {
    const limit = savedLimits.get(step.id);
    return limit && stepTimeLimit(step.goal) === null
      ? { ...step, goal: withStepTimeLimit(step.goal, limit) }
      : step;
  });
}
