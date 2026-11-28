// GitHub's own stack as the shell sends it, read into the stack model: the PR page's read with
// each member's details, and the list's lighter one. Any other shape means "can't say".

import type { NativeStackAnswer, NativeStackMember } from "./nativeStack";
import type { StackMember } from "./stackMembers";

const isObject = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const isInteger = (value: unknown): value is number => Number.isInteger(value);
const textOrNull = (value: unknown): string | null => (typeof value === "string" ? value : null);
const STATES = new Set<StackMember["state"]>(["OPEN", "CLOSED", "MERGED"]);
const REVIEWS = new Set<NonNullable<StackMember["review_decision"]>>([
  "APPROVED",
  "CHANGES_REQUESTED",
  "REVIEW_REQUIRED",
]);

/** A member as the stack members read shapes one, or null where it isn't one. */
export function memberFromWire(value: unknown): NativeStackMember | null {
  if (!isObject(value)) return null;
  const { url, number, title, state, is_draft, head_ref, review_decision } = value;
  if (typeof url !== "string" || !isInteger(number) || typeof title !== "string") return null;
  if (!STATES.has(state as StackMember["state"]) || typeof is_draft !== "boolean") return null;
  if (typeof head_ref !== "string" || !head_ref) return null;
  const details: StackMember = {
    url,
    number,
    title,
    state: state as StackMember["state"],
    is_draft,
    updated_at: textOrNull(value.updated_at),
    review_decision: REVIEWS.has(review_decision as NonNullable<StackMember["review_decision"]>)
      ? (review_decision as StackMember["review_decision"])
      : null,
    head_ref,
    head_sha: textOrNull(value.head_sha),
    base_ref: textOrNull(value.base_ref),
    base_sha: textOrNull(value.base_sha),
    head_repository: textOrNull(value.head_repository),
    base_repository: textOrNull(value.base_repository),
    base_default_branch: textOrNull(value.base_default_branch),
  };
  return { url, number, state: details.state, isDraft: is_draft, headRefName: head_ref, details };
}

/** A member as the list's read names one: enough to nest and hand off, or null. */
export function listMemberFromWire(value: unknown): NativeStackMember | null {
  if (!isObject(value)) return null;
  const { url, number, state, is_draft, head_ref } = value;
  if (typeof url !== "string" || !isInteger(number)) return null;
  if (!STATES.has(state as StackMember["state"]) || typeof is_draft !== "boolean") return null;
  if (typeof head_ref !== "string" || !head_ref) return null;
  return { url, number, state: state as string, isDraft: is_draft, headRefName: head_ref };
}

/** The shell's `native_stack` as the stack model reads it, each member read by `member`. */
export function nativeStackFromWire(
  value: unknown,
  member: (value: unknown) => NativeStackMember | null,
): NativeStackAnswer | undefined {
  if (!isObject(value)) return undefined;
  const { stackEntry, stack } = value;
  if (stackEntry !== null && !(isObject(stackEntry) && isInteger(stackEntry.position))) {
    return undefined;
  }
  const entry = stackEntry === null ? null : { position: stackEntry.position as number };
  if (stack === null) return { stackEntry: entry, stack: null };
  if (!isObject(stack) || !isObject(stack.entries) || !Array.isArray(stack.entries.nodes)) {
    return undefined;
  }
  const { id, number, size, baseRefName } = stack;
  const { hasNextPage, nodes } = stack.entries;
  if (typeof id !== "string" || !isInteger(number) || !isInteger(size)) return undefined;
  if (typeof baseRefName !== "string" || typeof hasNextPage !== "boolean") return undefined;
  return {
    stackEntry: entry,
    stack: {
      id,
      number,
      size,
      baseRefName,
      entries: {
        hasNextPage,
        nodes: (nodes as unknown[]).flatMap((node) =>
          isObject(node) && isInteger(node.position)
            ? [{ position: node.position, pullRequest: member(node.pullRequest) }]
            : [],
        ),
      },
    },
  };
}

/** The list's `native_stack` for one PR, or undefined where it can't say. */
export function listNativeStackFromWire(value: unknown): NativeStackAnswer | undefined {
  return nativeStackFromWire(value, listMemberFromWire);
}
