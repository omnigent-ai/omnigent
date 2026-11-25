// Where each PR a stacked PR's description lists stands, read on this Mac as the
// account that read the stacked PR, and only in its repository. An entry the read
// didn't cover keeps its place in the tree with its status unknown, never guessed.

import type { CodeHostPullRequest, CodeHostReadScheduling } from "@/lib/codeHosts";
import { DesktopPullRequestError, readOnThisMac } from "@/lib/desktopPullRequests";
import type { StackNode, StackParse } from "@/lib/pullRequestStacks/stackModel";

export const PULL_REQUEST_STACK_MEMBERS_OPERATION = "pull_request_stack_members";

/** The most members one read looks up; the shell refuses more. */
export const MAX_STACK_MEMBER_LOOKUPS = 50;

/** A member as the shell reads it. */
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
  /** The base repository's default branch; an older shell doesn't name it. */
  base_default_branch?: string | null;
}

export interface StackMembersAnswer {
  /** The account GitHub read these as. */
  viewer: string;
  members: StackMember[];
  /** PRs GitHub has none of, or won't show this account. */
  missing: number[];
  /** PRs whose read failed, so nothing is known of them. */
  unread: number[];
  /**
   * When this Mac sent the read, epoch ms; absent where the members stand in for another
   * read, as a list's do.
   */
  readAt?: number;
}

/** How the stack's members were read, as far as it went. */
export type StackMembersRead =
  /** This Mac can't read them: an older or partial shell, or a PR read elsewhere. */
  | { kind: "unavailable" }
  | { kind: "loading" }
  | { kind: "failed" }
  | { kind: "loaded"; answer: StackMembersAnswer };

export type StackMemberStatus =
  | { kind: "member"; member: StackMember }
  /** GitHub has no such PR, or this account can't see it. */
  | { kind: "missing" }
  | { kind: "loading" }
  /** Not read: no shell path, a failed read, or past the lookup cap. */
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

/**
 * Read `numbers` in `target`'s repository, as `target` reads. A shell that offers the
 * read but refuses it as unsupported answers null, so the stack shows without statuses.
 */
export async function fetchStackMembers(
  target: CodeHostPullRequest,
  numbers: readonly number[],
  scheduling: CodeHostReadScheduling = {},
): Promise<StackMembersAnswer | null> {
  const readAt = Date.now();
  try {
    const { data } = await readOnThisMac<Partial<StackMembersAnswer>>(
      target,
      PULL_REQUEST_STACK_MEMBERS_OPERATION,
      { repository: target.repository, numbers: numbers.slice(0, MAX_STACK_MEMBER_LOOKUPS) },
      scheduling,
    );
    return {
      viewer: typeof data?.viewer === "string" ? data.viewer : "",
      members: Array.isArray(data?.members) ? data.members : [],
      missing: Array.isArray(data?.missing) ? data.missing : [],
      unread: Array.isArray(data?.unread) ? data.unread : [],
      readAt,
    };
  } catch (error) {
    if (error instanceof DesktopPullRequestError && error.code === "unsupported") return null;
    throw error;
  }
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
