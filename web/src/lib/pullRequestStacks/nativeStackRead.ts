// GitHub's own record of a PR's stack, read on this Mac. A shell without the read, a host
// whose schema lacks stacks, or an answer of any other shape all mean "can't say", and the
// page keeps to the PR's description.

import type { CodeHostPullRequest, CodeHostReadScheduling } from "@/lib/codeHosts";
import { DesktopPullRequestError, readOnThisMac } from "@/lib/desktopPullRequests";
import { macOffers } from "@/lib/desktopReadPath";

import { type NativeStackAnswer, readNativeStack } from "./nativeStack";
import { memberFromWire, nativeStackFromWire } from "./nativeStackWire";
import type { StackSelf } from "./stackModel";
import type { StackMembersAnswer } from "./stackMembers";

export const PULL_REQUEST_NATIVE_STACK_OPERATION = "pull_request_native_stack";

/** The page's cache of native stacks, which Refresh and the stack diff's Reload read again. */
export const NATIVE_STACK_QUERY = "pull-request-native-stack";

/** GitHub's stack for a PR, and the account it was read as. */
export interface NativeStackReadAnswer {
  viewer: string;
  answer: NativeStackAnswer;
  /** When this Mac sent the read, epoch ms; absent for an answer from elsewhere. */
  readAt?: number;
}

/** Whether this Mac's shell can read GitHub's stacks for `target`'s provider. */
export function nativeStackOffered(target: CodeHostPullRequest): boolean {
  return macOffers(target.provider, PULL_REQUEST_NATIVE_STACK_OPERATION);
}

/** The shell's answer as the stack model reads it, or undefined when it can't say. */
export function nativeStackAnswerFromWire(wire: unknown): NativeStackReadAnswer | undefined {
  if (!wire || typeof wire !== "object" || Array.isArray(wire)) return undefined;
  const { supported, viewer, native_stack: nativeStack } = wire as Record<string, unknown>;
  if (supported !== true || typeof viewer !== "string" || !viewer) return undefined;
  const answer = nativeStackFromWire(nativeStack, memberFromWire);
  return answer && { viewer, answer };
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
 * GitHub's stack for `target` as this Mac reads it, or undefined when it can't say. A shell
 * that doesn't offer the read is never asked.
 */
export async function fetchNativeStack(
  target: CodeHostPullRequest,
  scheduling: CodeHostReadScheduling = {},
): Promise<NativeStackReadAnswer | undefined> {
  if (!nativeStackOffered(target)) return undefined;
  const readAt = Date.now();
  try {
    const { data } = await readOnThisMac<unknown>(
      target,
      PULL_REQUEST_NATIVE_STACK_OPERATION,
      {
        repository: target.repository,
        number: target.number,
      },
      scheduling,
    );
    const read = nativeStackAnswerFromWire(data);
    return read && { ...read, readAt };
  } catch (error) {
    if (error instanceof DesktopPullRequestError && error.code === "unsupported") return undefined;
    throw error;
  }
}
