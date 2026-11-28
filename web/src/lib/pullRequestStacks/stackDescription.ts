// Editing a description that carries verified stack metadata without touching it: the
// text before the metadata and the metadata itself stay as the original bytes, and only
// the text after it is editable, so nothing is spliced at an offset taken from edited text.

import type { VerifiedStackParse } from "@/lib/pullRequestStacks/stackModel";

export interface StackDescriptionSegments {
  /** Text before the metadata; producers leave it empty. */
  prefix: string;
  /** The metadata, from its header line through its footer's line break. */
  metadata: string;
  /** The user's description after the metadata. */
  suffix: string;
}

export function stackDescriptionSegments(
  body: string,
  parsed: VerifiedStackParse,
): StackDescriptionSegments {
  return {
    prefix: body.slice(0, parsed.span.start),
    metadata: body.slice(parsed.span.start, parsed.span.end),
    suffix: body.slice(parsed.span.end),
  };
}

/** The full description to save after editing only the text after the metadata. */
export function withEditedSuffix(original: StackDescriptionSegments, editedSuffix: string): string {
  return original.prefix + original.metadata + editedSuffix;
}

export type StackDescriptionRebase =
  /** Only the metadata changed on GitHub: the edit moved onto the fresh description, still unsaved. */
  | { kind: "rebased"; draft: string; expected: string }
  /** The user's text changed too, or the fresh metadata can't be trusted: both sides need a person. */
  | { kind: "conflict" };

/**
 * Moves an unsaved suffix edit onto a description that changed on GitHub since editing began,
 * such as a stack tool rewriting its metadata. It never saves; `expected` is what the next save
 * must find on GitHub.
 */
export function rebaseStackDescriptionEdit(
  original: StackDescriptionSegments,
  editedSuffix: string,
  freshBody: string,
  freshParse: VerifiedStackParse | null,
): StackDescriptionRebase {
  if (freshParse === null) return { kind: "conflict" };
  const fresh = stackDescriptionSegments(freshBody, freshParse);
  if (fresh.prefix !== original.prefix || fresh.suffix !== original.suffix) {
    return { kind: "conflict" };
  }
  return { kind: "rebased", draft: withEditedSuffix(fresh, editedSuffix), expected: freshBody };
}
