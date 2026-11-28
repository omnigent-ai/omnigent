// What a list already knows of a PR's stack, so the PR's page shows the stack the list nests it
// in from its first frame. Display and navigation evidence only, like another member's
// snapshot: the PR's own reads replace it, and merging, comparing and editing wait for them.

import type { NativeStackAnswer, NativeStackMember } from "./nativeStack";
import type { StackMember, StackMembersAnswer } from "./stackMembers";
import { stackPullRequestRef } from "./stackModel";

export interface ListedStack {
  /** GitHub's own stack as the list's read gave it; absent where the list can't say. */
  native?: NativeStackAnswer;
  /** A member as the list shows it, or null where the list doesn't list it. */
  member: (url: string) => StackMember | null;
  /** The account the list reads as. */
  viewer: string;
}

/**
 * The members read the list stands in for: each of `urls` the list shows, once. One it doesn't
 * show is unread, so it reads as unknown until the PR's own read says.
 */
export function listedStackMembers(
  listed: ListedStack,
  urls: readonly string[],
): StackMembersAnswer {
  const members: StackMember[] = [];
  const unread: number[] = [];
  const seen = new Set<number>();
  for (const url of urls) {
    const number = stackPullRequestRef(url)?.number;
    if (number === undefined || seen.has(number)) continue;
    seen.add(number);
    const member = listed.member(url);
    if (member) members.push(member);
    else unread.push(number);
  }
  return { viewer: listed.viewer, members, missing: [], unread };
}

type ListedEntry = NativeStackMember | null;

/**
 * Whether `listed`, the list's whole read of GitHub's stack, is the same stack as `own`, a read
 * GitHub cut off before the stack's end, and agrees with every entry `own` lists: the list then
 * shows more of the same stack than the PR's own read does.
 */
export function listedStackExtends(listed: NativeStackAnswer, own: NativeStackAnswer): boolean {
  const whole = listed.stack;
  const capped = own.stack;
  if (!whole || !capped || whole.entries.hasNextPage || !capped.entries.hasNextPage) return false;
  if (whole.id !== capped.id || whole.size !== capped.size) return false;
  if (whole.baseRefName !== capped.baseRefName) return false;
  if (listed.stackEntry?.position !== own.stackEntry?.position) return false;
  const listedAt = new Map(whole.entries.nodes.map((node) => [node.position, node.pullRequest]));
  return capped.entries.nodes.every(
    (node) =>
      listedAt.has(node.position) && listedAt.get(node.position)?.url === node.pullRequest?.url,
  );
}

/**
 * GitHub's stack for one PR as the list's rows read it together: each row of the same stack adds
 * the entries its own read listed, its own included, so a stack longer than one read lists reads
 * whole where the list shows every member. A position two reads disagree on is left out.
 */
export function listedNativeStack(
  own: NativeStackAnswer,
  rows: Iterable<NativeStackAnswer | undefined>,
): NativeStackAnswer {
  const stack = own.stack;
  if (!stack) return own;
  const entries = new Map<number, ListedEntry>();
  const disputed = new Set<number>();
  const add = (position: number, entry: ListedEntry) => {
    if (disputed.has(position)) return;
    if (!entries.has(position)) {
      entries.set(position, entry);
      return;
    }
    const known = entries.get(position)!;
    if (known?.url === entry?.url) return;
    entries.delete(position);
    disputed.add(position);
  };
  for (const answer of [own, ...rows]) {
    const theirs = answer?.stack;
    if (!theirs || theirs.id !== stack.id) continue;
    if (theirs.size !== stack.size || theirs.baseRefName !== stack.baseRefName) continue;
    for (const node of theirs.entries.nodes) add(node.position, node.pullRequest);
  }
  const nodes = [...entries]
    .sort(([left], [right]) => left - right)
    .map(([position, pullRequest]) => ({ position, pullRequest }));
  const whole =
    nodes.length === stack.size && nodes.every((node, index) => node.position === index + 1);
  return {
    ...own,
    stack: {
      ...stack,
      entries: { hasNextPage: stack.entries.hasNextPage && !whole, nodes },
    },
  };
}
