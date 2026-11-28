// Where each PR a stacked PR's description lists stands, as the session's host finds them by
// walking the PR's branches, and only in its repository. An entry the walk didn't reach keeps
// its place in the tree with its status unknown, never guessed.

import { readPullRequestRpc } from "@/lib/pullRequestRpc";
import type { StackNode, StackParse } from "@/lib/pullRequestStacks/stackModel";

/** The most members one read looks up. */
export const MAX_STACK_MEMBER_LOOKUPS = 50;

/** A member as a stack read gives it. */
export interface StackMember {
  url: string;
  number: number;
  title: string;
  state: "OPEN" | "CLOSED" | "MERGED";
  is_draft: boolean;
  updated_at: string | null;
  review_decision: "APPROVED" | "CHANGES_REQUESTED" | "REVIEW_REQUIRED" | null;
  head_ref: string | null;
  head_sha: string | null;
  base_ref: string | null;
  base_sha: string | null;
  /** Null when the head's fork was deleted. */
  head_repository: string | null;
  base_repository: string | null;
  /** The base repository's default branch, where the read names it. */
  base_default_branch?: string | null;
}

export interface StackMembersAnswer {
  /** The account GitHub read these as; empty where the read doesn't say. */
  viewer: string;
  members: StackMember[];
  /** PRs GitHub has none of, or won't show this account. */
  missing: number[];
  /** PRs whose read failed, so nothing is known of them. */
  unread: number[];
  /**
   * When the read was sent, epoch ms; absent where the members stand in for another read, as a
   * list's do.
   */
  readAt?: number;
}

/** How the stack's members were read, as far as it went. */
export type StackMembersRead =
  /** Nothing reads them: the session's host doesn't serve the stack, or there's no PR. */
  | { kind: "unavailable" }
  | { kind: "loading" }
  | { kind: "failed" }
  | { kind: "loaded"; answer: StackMembersAnswer };

export type StackMemberStatus =
  | { kind: "member"; member: StackMember }
  /** GitHub has no such PR, or this account can't see it. */
  | { kind: "missing" }
  | { kind: "loading" }
  /** Not read: no host path, a failed read, outside the walk, or past the lookup cap. */
  | { kind: "unknown" }
  /** A branch that hasn't been pushed as a PR. */
  | { kind: "unpushed" }
  /** A PR in another repository or on another host, only ever linked to. */
  | { kind: "external" };

export interface StackMemberStatuses {
  /** By position in `nodes`. */
  nodes: StackMemberStatus[];
  dependency: StackMemberStatus | null;
  /** In-repository PRs whose status was read, of all such PRs listed. */
  loaded: number;
  total: number;
  /** Every listed PR's status was read; a missing, unread or past-the-cap PR leaves it false. */
  complete: boolean;
}

/**
 * The PR numbers to read for a stack: this PR first, then its dependency, then the other
 * members in order, up to the cap. Entries in other repositories or on other hosts are never read.
 */
export function stackMemberLookups(parse: StackParse): number[] {
  const self = parse.nodes[parse.selfIndex];
  const ordered = [self, parse.dependency, ...parse.nodes].filter(
    (node): node is StackNode => !!node?.ref && !node.external,
  );
  const numbers = [...new Set(ordered.map((node) => node.ref?.number ?? 0))];
  return numbers.slice(0, MAX_STACK_MEMBER_LOOKUPS);
}

const STATES = new Set<string>(["OPEN", "CLOSED", "MERGED"]);
const textOrNull = (value: unknown): string | null =>
  typeof value === "string" && value ? value : null;

/**
 * The members a `GetPullRequestStack` answer names, among `numbers`, in `repository`. The walk
 * lists only PRs in the repository, and a member another member's base is on is an ancestor the
 * walk read from this repository, so only those name their head's repository.
 */
export function stackMembersFromHostStack(
  answer: unknown,
  repository: string,
  numbers: readonly number[],
  readAt: number,
): StackMembersAnswer {
  const listed = (answer as { members?: unknown } | null)?.members;
  const entries = Array.isArray(listed)
    ? listed.filter(
        (entry): entry is Record<string, unknown> => !!entry && typeof entry === "object",
      )
    : [];
  const bases = new Set(entries.flatMap((entry) => textOrNull(entry.base_ref) ?? []));
  const wanted = new Set(numbers);
  const members: StackMember[] = [];
  for (const entry of entries) {
    const { pr_url: url, number, title, state, is_draft: isDraft } = entry;
    if (typeof url !== "string" || !Number.isInteger(number) || !wanted.has(number as number)) {
      continue;
    }
    if (typeof state !== "string" || !STATES.has(state) || typeof isDraft !== "boolean") continue;
    const headRef = textOrNull(entry.head_ref);
    if (members.some((member) => member.number === number)) continue;
    members.push({
      url,
      number: number as number,
      title: typeof title === "string" ? title : "",
      state: state as StackMember["state"],
      is_draft: isDraft,
      updated_at: null,
      review_decision: null,
      head_ref: headRef,
      head_sha: textOrNull(entry.head_sha),
      base_ref: textOrNull(entry.base_ref),
      base_sha: null,
      head_repository: headRef && bases.has(headRef) ? repository : null,
      base_repository: repository,
    });
  }
  const found = new Set(members.map((member) => member.number));
  const unread = numbers.filter((number) => !found.has(number));
  return { viewer: "", members, missing: [], unread, readAt };
}

/**
 * Read `numbers` around `prUrl`, in `repository`, through the session's host: the stack its
 * branches form, so a listed PR outside that walk stays unknown.
 */
export async function fetchStackMembersOnHost(
  conversationId: string,
  prUrl: string,
  repository: string,
  numbers: readonly number[],
): Promise<StackMembersAnswer> {
  const readAt = Date.now();
  const answer = await readPullRequestRpc<unknown>(
    conversationId,
    "getPullRequestStack",
    (client) => client.getPullRequestStack({ prUrl }),
  );
  return stackMembersFromHostStack(
    answer,
    repository,
    numbers.slice(0, MAX_STACK_MEMBER_LOOKUPS),
    readAt,
  );
}

/** Each entry's status from what the read found, never claiming more than it covered. */
export function stackMemberStatuses(
  parse: StackParse,
  read: StackMembersRead,
): StackMemberStatuses {
  const requested = new Set(stackMemberLookups(parse));
  const answer = read.kind === "loaded" ? read.answer : null;
  const found = new Map(answer?.members.map((member) => [member.number, member]));
  const missing = new Set(answer?.missing);
  let loaded = 0;
  let total = 0;
  const statusOf = (node: StackNode): StackMemberStatus => {
    if (!node.ref) return { kind: "unpushed" };
    if (node.external) return { kind: "external" };
    const number = node.ref.number;
    const member = found.get(number);
    if (member) return { kind: "member", member };
    if (missing.has(number)) return { kind: "missing" };
    return read.kind === "loading" && requested.has(number)
      ? { kind: "loading" }
      : { kind: "unknown" };
  };
  // Each in-repository PR counts once, wherever it's listed.
  const counted = new Set<number>();
  const count = (node: StackNode, status: StackMemberStatus) => {
    if (!node.ref || node.external || counted.has(node.ref.number)) return;
    counted.add(node.ref.number);
    total += 1;
    if (status.kind === "member") loaded += 1;
  };
  const nodes = parse.nodes.map((node) => {
    const status = statusOf(node);
    count(node, status);
    return status;
  });
  const dependency = parse.dependency ? statusOf(parse.dependency) : null;
  if (parse.dependency && dependency) count(parse.dependency, dependency);
  return { nodes, dependency, loaded, total, complete: loaded === total };
}
