// Building a new thingy from a description: its session gets a short note to build and publish
// it, then the description. The detailed guidance is in the `thingy` skill.

import type { Thingy } from "../api/thingyApi";

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
export function thingyNameFrom(description: string): string {
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
    return "New thingy";
  }
  return URL_START.test(name) ? name : name.charAt(0).toUpperCase() + name.slice(1);
}

/** How the session prompts name a thingy, so its sys_thingy_* tools reach it. */
export function thingyPromptLabel(app: Pick<Thingy, "id" | "name">): string {
  return `${JSON.stringify(app.name)} (thingy_id ${app.id})`;
}

/**
 * The message that starts a new thingy's session: a short note naming the thingy, then the
 * viewer's description. The `thingy` skill carries how to build one. Without a description (a
 * blank thingy), the viewer types it below the note.
 */
export function composeThingyBuildPrompt(
  app: Pick<Thingy, "id" | "name">,
  description = "",
): string {
  const note = [
    "Load the thingy skill and follow it.",
    `Please build my new thingy ${thingyPromptLabel(app)} from my description below, and publish it.`,
    "This is also my thingy session, where I'll send its actions and change requests.",
  ].join(" ");
  return `${note}\n\n${description.trim()}`;
}
