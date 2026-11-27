// Reads a PR description with whichever stack format recognizes it first.

import type { StackFormat, StackParse, StackSelf } from "@/lib/pullRequestStacks/stackModel";

/** The first format's parse, in the order given, or null when none finds a stack. */
export function parseStack(
  formats: readonly StackFormat[],
  body: string | null | undefined,
  self: StackSelf,
): StackParse | null {
  if (!body) return null;
  for (const format of formats) {
    const parsed = format.parse(body, self);
    if (parsed !== null) return parsed;
  }
  return null;
}
