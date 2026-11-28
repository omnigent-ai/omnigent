// Building a new gadget from a description: its session gets a short note to build and publish
// it, then the description. The detailed guidance is in the `gadget` skill.

import type { Gadget } from "../api/gadgetApi";

const LEADING_REQUEST =
  /^\s*(?:(?:please|hi|hey)[\s,]+)*(?:(?:can|could|would)\s+you\s+)?(?:build|create|make|design|i\s+want|i\s+need|i'd\s+like|i\s+would\s+like|give\s+me)(?:\s+me)?(?:\s+(?:a|an|the))?\s+/i;
// Where a name stops: a sentence end, an ellipsis, a colon or semicolon, or a comma.
const CLAUSE_END = /(?:\.{3}|…|[.!?:;,])(?=\s|$)/g;
// A dot that ends an abbreviation ("e.g.", "etc.") or an initial ("U.S."), not a sentence.
const ABBREVIATION =
  /(?:^|[\s(])(?:e\.g|i\.e|etc|vs|approx|incl|cf|esp|dr|mr|mrs|ms|st|no)\.$|(?:^|[\s.])[a-z]\.$/i;
const TRAILING_STOP_WORDS =
  /\s+(?:a|an|the|and|or|for|my|our|your|with|of|to|from|in|on|by|at|that|which|as)$/i;
const TRAILING_PUNCTUATION = /[\s.,;:!?…\-–—'"]+$/;
const URL_START = /^(?:[a-z][a-z0-9+.-]*:\/\/|www\.)/i;
// The server allows longer names; a derived one only has to read well in the list and tabs.
const MAX_DERIVED_NAME = 60;
// A comma ends the name only after this much text, so "Hi, a board" is not cut to "Hi".
const MIN_CLAUSE_BEFORE_COMMA = 12;

function firstClause(text: string): string {
  for (const match of text.matchAll(CLAUSE_END)) {
    const end = match.index;
    const mark = match[0];
    if (mark === "." && ABBREVIATION.test(text.slice(0, end + 1))) {
      continue;
    }
    if (mark === "," && end < MIN_CLAUSE_BEFORE_COMMA) {
      continue;
    }
    return text.slice(0, end);
  }
  return text;
}

/** A short name from the start of a description; the session can rename it later. */
export function gadgetNameFrom(description: string): string {
  const firstLine = description.trim().split("\n")[0] ?? "";
  let name = firstClause(firstLine.replace(LEADING_REQUEST, ""))
    .replace(/\s+/g, " ")
    .replace(TRAILING_PUNCTUATION, "")
    .trim();
  if (name.length > MAX_DERIVED_NAME) {
    // Stop at the last whole word that fits, without a dangling "for" or "and".
    const cut = name.slice(0, MAX_DERIVED_NAME + 1);
    const lastSpace = cut.lastIndexOf(" ");
    name = lastSpace > 0 ? cut.slice(0, lastSpace) : cut.slice(0, MAX_DERIVED_NAME);
    let previous: string;
    do {
      previous = name;
      name = name.replace(TRAILING_STOP_WORDS, "").replace(TRAILING_PUNCTUATION, "");
    } while (name !== previous);
  }
  if (!/[\p{L}\p{N}]/u.test(name)) {
    return "New gadget";
  }
  return URL_START.test(name) ? name : name.charAt(0).toUpperCase() + name.slice(1);
}

/**
 * How every prompt that hands work to a gadget session starts. A session whose host predates
 * gadgets has neither the skill nor the tools; it should say so rather than improvise.
 */
export const GADGET_SKILL_INSTRUCTION =
  "Load the gadget skill and follow it. Without the gadget skill or sys_gadget_* tools, say this session's host doesn't support gadgets yet, name what's missing, and stop.";

/** How the session prompts name a gadget, so its sys_gadget_* tools reach it. */
export function gadgetPromptLabel(app: Pick<Gadget, "id" | "name">): string {
  return `${JSON.stringify(app.name)} (gadget_id ${app.id})`;
}

/**
 * The message that starts a new gadget's session: a short note naming the gadget, then the
 * viewer's description. The `gadget` skill carries how to build one. Without a description (a
 * blank gadget), the viewer types it below the note.
 */
export function composeGadgetBuildPrompt(
  app: Pick<Gadget, "id" | "name">,
  description = "",
): string {
  const note = [
    GADGET_SKILL_INSTRUCTION,
    `Please build my new gadget ${gadgetPromptLabel(app)} from my description below, publish it and give it a short name.`,
    "This is also my gadget session, where I'll send its actions and change requests.",
  ].join(" ");
  return `${note}\n\n${description.trim()}`;
}
