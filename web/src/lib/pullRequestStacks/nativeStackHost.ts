// GitHub's own record of a PR's stack, read through the session's host, and the members it
// names. Where the host can't say, the page keeps to the PR's description.

import { readPullRequestRpc } from "@/lib/pullRequestRpc";

import { type NativeStackAnswer, readNativeStack } from "./nativeStack";
import { memberFromWire, nativeStackFromWire } from "./nativeStackWire";
import type { StackSelf } from "./stackModel";
import type { StackMembersAnswer } from "./stackMembers";

/** The page's cache of native stacks, which Refresh and the stack diff's Reload read again. */
export const NATIVE_STACK_QUERY = "pull-request-native-stack";

/** GitHub's stack for a PR, and the account it was read as. */
export interface NativeStackReadAnswer {
  viewer: string;
  answer: NativeStackAnswer;
  /** When the read was sent, epoch ms; absent for an answer from elsewhere. */
  readAt?: number;
}

/**
 * The stack's members as the page's statuses and comparison read them, as the account the
 * stack was read as. Only the entries the stack model kept count: a member it rejected, such
 * as one in another repository or listed twice, never stands in for one it kept.
 */
export function nativeStackMembers(
  { viewer, answer, readAt }: NativeStackReadAnswer,
  self: StackSelf,
): StackMembersAnswer {
  const read = readNativeStack(answer, self);
  const entries = read.kind === "stacked" || read.kind === "stacked-partial" ? read.entries : [];
  const members = entries.flatMap(({ member }) => (member.details ? [member.details] : []));
  return { viewer, members, missing: [], unread: [], ...(readAt === undefined ? {} : { readAt }) };
}

/**
 * The host method that reads GitHub's own stack for a PR. A host that doesn't serve it, such as
 * an older one, leaves the page to the description's stack.
 */
export const NATIVE_STACK_HOST_METHOD = "GetPullRequestNativeStack";

/** The host's answer as the stack model reads it, or undefined when it can't say. */
export function nativeStackAnswerFromWire(wire: unknown): NativeStackReadAnswer | undefined {
  if (!wire || typeof wire !== "object" || Array.isArray(wire)) return undefined;
  const { supported, viewer, native_stack: nativeStack } = wire as Record<string, unknown>;
  if (supported !== true || typeof viewer !== "string" || !viewer) return undefined;
  const answer = nativeStackFromWire(nativeStack, memberFromWire);
  return answer && { viewer, answer };
}

/** How the page asks a session's host for GitHub's stack of a PR, in the host's wire shape. */
export type NativeStackHostRead = (
  sessionId: string,
  prUrl: string,
  signal?: AbortSignal,
) => Promise<unknown>;

// The session's host answers in the shape the Mac shell gives, which the stack model parses.
const workspaceRead: NativeStackHostRead = (sessionId, prUrl, signal) =>
  readPullRequestRpc<unknown>(
    sessionId,
    "getPullRequestNativeStack",
    (client) => client.getPullRequestNativeStack({ prUrl }),
    signal,
  );
let hostRead: NativeStackHostRead = workspaceRead;

/** Stand in for the host's answer in a test; null restores the workspace read. */
export function setNativeStackHostReadForTests(read: NativeStackHostRead | null): void {
  hostRead = read ?? workspaceRead;
}

/** GitHub's stack for `prUrl` as `sessionId`'s host reads it, or undefined when it can't say. */
export async function fetchNativeStackOnHost(
  sessionId: string,
  prUrl: string,
  signal?: AbortSignal,
): Promise<NativeStackReadAnswer | undefined> {
  const readAt = Date.now();
  const read = nativeStackAnswerFromWire(await hostRead(sessionId, prUrl, signal));
  return read && { ...read, readAt };
}
