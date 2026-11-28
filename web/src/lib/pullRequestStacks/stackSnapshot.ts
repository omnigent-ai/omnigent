// One record per stack, shared by every member: what the last member read said the stack is,
// so a member opened next shows the same stack at once while its own reads are still out.
// A record is navigation evidence only; each member's own reads stay what merging, comparing
// and editing trust.

import { type NativeStackAnswer, type NativeStackRead, readNativeStack } from "./nativeStack";
import type { NativeStackReadAnswer } from "./nativeStackHost";
import type { StackMember, StackMembersAnswer } from "./stackMembers";
import {
  type StackParse,
  type StackPullRequestRef,
  stackPullRequestRef,
  stackRefKey,
} from "./stackModel";

/** How long a record stands in for a member's own reads before it's too old to show. */
export const STACK_SNAPSHOT_MAX_AGE_MS = 5 * 60_000;

type NativeStackShape = NonNullable<NativeStackAnswer["stack"]>;

/** GitHub's word that a PR is stacked, read whole or in part. */
export type NativeStackedRead = Extract<NativeStackRead, { kind: "stacked" | "stacked-partial" }>;

/** GitHub's own stack, as one member's read gave it whole. */
export interface NativeStackEvidence {
  kind: "native";
  viewer: string;
  stack: NativeStackShape;
}

/** A description's stack: its members, whose statuses one shared read gives. */
export interface DescribedStackEvidence {
  kind: "described";
}

export interface StackSnapshot {
  /** The same for every member's read of the same stack; another stack has another. */
  identity: string;
  evidence: NativeStackEvidence | DescribedStackEvidence;
  /** Each member's ref key, in the stack's order. */
  members: string[];
  /** The member whose read this came from. */
  sourceUrl: string;
  /** When that read came back, never when it was last shown. */
  readAt: number;
}

const memberKey = (url: string): string | null => {
  const ref = stackPullRequestRef(url);
  return ref ? stackRefKey(ref) : null;
};

/** A short, stable hash of `text`, for an identity that doesn't carry it whole. */
function digest(text: string): string {
  let hash = 5381;
  for (let index = 0; index < text.length; index += 1) {
    hash = ((hash << 5) + hash + text.charCodeAt(index)) | 0;
  }
  return `${text.length}.${(hash >>> 0).toString(36)}`;
}

/** GitHub's stack by its own id, whichever member read it. */
export function nativeStackIdentity(stack: NativeStackShape): string {
  return `native:${stack.id}`;
}

/**
 * A description's stack by its members and their shape, whichever member's description it is:
 * two descriptions that disagree about the stack are two stacks.
 */
export function describedStackIdentity(parse: StackParse): string {
  const shape = parse.nodes.map((node) => [
    node.ref ? stackRefKey(node.ref) : `branch:${node.branch}`,
    node.depth,
    node.parentIndex,
    node.external,
  ]);
  const dependency = parse.dependency?.ref ? stackRefKey(parse.dependency.ref) : null;
  return `described:${parse.format}:${digest(JSON.stringify([shape, dependency]))}`;
}

/** The record of GitHub's stack from one member's read, when it listed the stack's entries. */
export function nativeStackSnapshot(
  read: NativeStackReadAnswer,
  sourceUrl: string,
  readAt: number,
): StackSnapshot | null {
  const { stack } = read.answer;
  if (!stack) return null;
  const members = stack.entries.nodes.flatMap(({ pullRequest }) => {
    const key = pullRequest ? memberKey(pullRequest.url) : null;
    return key ? [key] : [];
  });
  return {
    identity: nativeStackIdentity(stack),
    evidence: { kind: "native", viewer: read.viewer, stack },
    members,
    sourceUrl,
    readAt,
  };
}

/** The record of a description's stack, from the member whose description it is. */
export function describedStackSnapshot(
  parse: StackParse,
  sourceUrl: string,
  readAt: number,
): StackSnapshot {
  const members = parse.nodes.flatMap((node) =>
    node.ref && !node.external ? [stackRefKey(node.ref)] : [],
  );
  return {
    identity: describedStackIdentity(parse),
    evidence: { kind: "described" },
    members,
    sourceUrl,
    readAt,
  };
}

/** What a member's own details say of it, to tell whether a record still describes it. */
export interface MemberRevision {
  head_sha?: string | null;
  base_ref?: string | null;
  base_sha?: string | null;
}

/** A record's own word on a member, or undefined where it has none. */
function recordedMember(
  snapshot: StackSnapshot,
  url: string,
  members: StackMembersAnswer | null,
): StackMember | undefined {
  const key = memberKey(url);
  const sameMember = (candidate: { url: string }) => memberKey(candidate.url) === key;
  if (snapshot.evidence.kind === "native") {
    const node = snapshot.evidence.stack.entries.nodes.find(
      ({ pullRequest }) => pullRequest && sameMember(pullRequest),
    );
    return node?.pullRequest?.details;
  }
  return members ? memberInAnswer(members, url) : undefined;
}

/**
 * Whether `snapshot` may stand in for `url`'s own stack reads now: young enough, still naming
 * it as a member, and, where its details are known, read at the same head and base.
 */
export function snapshotDescribes(
  snapshot: StackSnapshot,
  url: string,
  {
    now,
    revision,
    members = null,
  }: { now: number; revision: MemberRevision | null; members?: StackMembersAnswer | null },
): boolean {
  if (now - snapshot.readAt > STACK_SNAPSHOT_MAX_AGE_MS) return false;
  const key = memberKey(url);
  if (key === null || !snapshot.members.includes(key)) return false;
  // A PR shown from its list row before its details have no head to compare yet.
  if (revision === null || !revision.head_sha) return true;
  const recorded = recordedMember(snapshot, url, members);
  // Without the record's own word on its head and base, it can't tell a push from none.
  return !!recorded && sameMemberRevision(recorded, revision);
}

/** Whether a member read records the PR at the head and base its own details give. */
export function sameMemberRevision(recorded: MemberRevision, revision: MemberRevision): boolean {
  const same = (left: string | null | undefined, right: string | null | undefined) =>
    (left ?? null) === (right ?? null);
  return (
    same(recorded.head_sha, revision.head_sha) &&
    same(recorded.base_ref, revision.base_ref) &&
    same(recorded.base_sha, revision.base_sha)
  );
}

/** What a members read says of `url`, if it read it. */
export function memberInAnswer(answer: StackMembersAnswer, url: string): StackMember | undefined {
  const key = memberKey(url);
  return key === null ? undefined : answer.members.find((member) => memberKey(member.url) === key);
}

/**
 * GitHub's stack, as `self` would read it, from another member's read: its place by the
 * stack's own positions, so sizes, gaps and partial reads stay GitHub's. Null where the record
 * doesn't list `self` with a position.
 */
export function nativeStackForMember(
  evidence: NativeStackEvidence,
  self: StackPullRequestRef,
): { read: NativeStackedRead; answer: NativeStackReadAnswer } | null {
  const key = stackRefKey(self);
  const entries = evidence.stack.entries.nodes.filter(
    ({ pullRequest }) => pullRequest && memberKey(pullRequest.url) === key,
  );
  if (entries.length !== 1) return null;
  const answer: NativeStackAnswer = {
    stackEntry: { position: entries[0]!.position },
    stack: evidence.stack,
  };
  const read = readNativeStack(answer, self);
  if (read.kind !== "stacked" && read.kind !== "stacked-partial") return null;
  return { read, answer: { viewer: evidence.viewer, answer } };
}
