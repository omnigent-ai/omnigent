// A description draft that edits only the text after a verified stack section. The draft's
// body is always the original text above the section, the original section, then the edited
// text, so saving, discarding and conflicts read it as any other description draft.

import { stackFormats } from "@/lib/pullRequestStacks/formats";
import {
  rebaseStackDescriptionEdit,
  type StackDescriptionSegments,
  stackDescriptionSegments,
  withEditedSuffix,
} from "@/lib/pullRequestStacks/stackDescription";
import { parseStack } from "@/lib/pullRequestStacks/stackFormats";
import type { StackSelf, VerifiedStackParse } from "@/lib/pullRequestStacks/stackModel";
import { descriptionMetadataEditable } from "./stackTrust";

/**
 * Whether `body` still begins with the draft's text above and stack section. A body edited as
 * a whole may not, and its text after the section can't be told apart any more.
 */
export function stackDraftKeepsSection(body: string, stack: StackDescriptionSegments): boolean {
  return body.startsWith(stack.prefix + stack.metadata);
}

/** The stack section a draft keeps, read as the stack it lists. */
export function keptStackParse(
  stack: StackDescriptionSegments,
  self: StackSelf,
): VerifiedStackParse | null {
  return verifiedParse(stack.prefix + stack.metadata, self);
}

/** The text a stack draft lets the person edit: everything after the stack section. */
export function editableStackSuffix(body: string, stack: StackDescriptionSegments): string {
  return body.slice(stack.prefix.length + stack.metadata.length);
}

/** The draft's body once the editable text reads `suffix`. */
export function withStackSuffix(stack: StackDescriptionSegments, suffix: string): string {
  return withEditedSuffix(stack, suffix);
}

/** The segments a save leaves the draft based on, once `saved` is on GitHub. */
export function stackAfterSave(
  stack: StackDescriptionSegments,
  saved: string,
): StackDescriptionSegments {
  return { ...stack, suffix: editableStackSuffix(saved, stack) };
}

function verifiedParse(body: string, self: StackSelf): VerifiedStackParse | null {
  const parse = parseStack(stackFormats(), body, self);
  return parse && descriptionMetadataEditable(parse) ? parse : null;
}

export const STACK_INFO_UPDATED =
  "Stack info was updated on GitHub. Your edit was kept; review and save.";

/** What a draft becomes when GitHub's description changed since it was read. */
export type StackDraftChange =
  /** Only the stack section changed: the edit moves onto GitHub's, still unsaved. */
  | { kind: "rebased"; body: string; expected: string; stack: StackDescriptionSegments }
  /** The text changed too: the person chooses, with the draft still editing only its text. */
  | { kind: "conflict" }
  /**
   * GitHub's stack section isn't in the usual format any more: the person sees and chooses
   * from their whole description, so nothing writes a section they can't see.
   */
  | { kind: "conflict-full-body" };

export function stackDraftAfterChange(
  stack: StackDescriptionSegments,
  body: string,
  now: string,
  self: StackSelf,
): StackDraftChange {
  const fresh = verifiedParse(now, self);
  if (!fresh || !stackDraftKeepsSection(body, stack)) return { kind: "conflict-full-body" };
  const rebase = rebaseStackDescriptionEdit(stack, editableStackSuffix(body, stack), now, fresh);
  if (rebase.kind === "conflict") return { kind: "conflict" };
  return {
    kind: "rebased",
    body: rebase.draft,
    expected: rebase.expected,
    stack: stackDescriptionSegments(now, fresh),
  };
}

/**
 * Keeping the person's text over GitHub's `now`: their text after GitHub's own stack section
 * and the text above it, so a stack tool's newer section is never written back over.
 */
export function stackDraftApplyingMine(
  stack: StackDescriptionSegments,
  body: string,
  now: string,
  self: StackSelf,
): { body: string; stack: StackDescriptionSegments } | null {
  const fresh = verifiedParse(now, self);
  if (!fresh || !stackDraftKeepsSection(body, stack)) return null;
  const segments = stackDescriptionSegments(now, fresh);
  return {
    body: withEditedSuffix(segments, editableStackSuffix(body, stack)),
    stack: segments,
  };
}
