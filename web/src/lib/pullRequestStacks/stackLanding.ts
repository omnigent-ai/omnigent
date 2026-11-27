// How a stack whose PRs all target the default branch lands: from the bottom up, one PR at a
// time, with its format's own tool. Advice only, for a PR the page proved is such a stack; it
// never holds a merge back.

import { stackFormats } from "./formats";
import type { StackMember, StackMembersRead, StackMemberStatuses } from "./stackMembers";
import {
  type StackLandingTool,
  type StackParse,
  stackPullRequestRef,
  stackRefKey,
} from "./stackModel";
import { previousInStack } from "./stackNavigation";
import { descriptionMetadataEditable } from "./stackTrust";

/** A stack as the page read it: its parse, and its members' read with each one's status. */
export interface LandingStack {
  parse: StackParse;
  members: StackMembersRead;
  statuses: StackMemberStatuses;
}

/** The PR on screen as the page last read it. */
export interface LandingPullRequest {
  url: string;
  /** "OPEN", "MERGED" or "CLOSED", as the PR's own latest read says. */
  state?: string;
  head_ref?: string | null;
  base_ref?: string | null;
}

/** What the comparison with the merged parent found, for the current PR and reader only. */
export type StackLandingDiagnostic =
  | { kind: "separable" }
  | { kind: "not_carried" }
  | { kind: "in_base" }
  | { kind: "reading" }
  | { kind: "stale" }
  | { kind: "failed" };

export interface StackLandingParent {
  number: number;
  label: string;
}

/** The one line the merge area shows for a stacked PR, if any. */
export type StackLandingNotice =
  /** The parent is open: it has to land first. */
  | { kind: "merge-parent-first"; parent: StackLandingParent }
  /** The parent's read found nothing of it, so whether it landed isn't known. */
  | { kind: "parent-unknown"; parent: StackLandingParent }
  /** The parent landed as a squash, and this PR still carries its commits. */
  | { kind: "sync"; parent: StackLandingParent }
  /** The PR or its stack moved since it was checked; only Reload checks again. */
  | { kind: "stale"; parent: StackLandingParent }
  | { kind: "failed"; parent: StackLandingParent };

// Repository names ignore case on GitHub; branch names don't.
const sameRepository = (left: string | null | undefined, right: string | null | undefined) =>
  !!left && !!right && left.toLowerCase() === right.toLowerCase();

/** This PR as the members read found it, only when that read is of the PR the page routed to. */
function selfMember(stack: LandingStack, pr: LandingPullRequest): StackMember | null {
  const routed = stackPullRequestRef(pr.url);
  const status = stack.statuses.nodes[stack.parse.selfIndex];
  if (!routed || status?.kind !== "member") return null;
  const read = stackPullRequestRef(status.member.url);
  return read && stackRefKey(read) === stackRefKey(routed) ? status.member : null;
}

/**
 * Whether the PR on screen targets its repository's default branch, as both the members read
 * and the PR's own latest read agree. An older shell names no default branch, so it proves nothing.
 */
export function defaultBaseProven(
  stack: LandingStack | null,
  pr: LandingPullRequest | null,
): boolean {
  if (!stack || !pr || stack.members.kind !== "loaded") return false;
  const self = selfMember(stack, pr);
  const routed = stackPullRequestRef(pr.url);
  if (!self || !routed) return false;
  const defaultBranch: unknown = self.base_default_branch;
  if (typeof defaultBranch !== "string" || defaultBranch === "") return false;
  // A retarget the members read hasn't caught up with leaves the two reads disagreeing.
  return (
    self.base_ref === defaultBranch &&
    pr.base_ref === self.base_ref &&
    sameRepository(self.base_repository, routed.repository)
  );
}

/** The tool that lands a verified stack in a format with one, the only kind landing advice is given for. */
export function stackLandingTool(parse: StackParse): StackLandingTool | null {
  if (!descriptionMetadataEditable(parse)) return null;
  return stackFormats().find((format) => format.id === parse.format)?.landing ?? null;
}

/**
 * A stack its tool lands, whose PR on screen is still open, as its own latest read says, and
 * proven to target the default branch. A merged or closed PR has nothing left to land.
 */
export function stackLandingEligible(
  stack: LandingStack | null,
  pr: LandingPullRequest | null,
): stack is LandingStack {
  return (
    !!stack &&
    stackLandingTool(stack.parse) !== null &&
    pr?.state === "OPEN" &&
    defaultBaseProven(stack, pr)
  );
}

/** The PR directly below this one, and its member read when there is one. */
function directParent(stack: LandingStack) {
  const step = previousInStack(stack.parse);
  if (step.kind !== "go" || !step.destination.node.ref) return null;
  const number = step.destination.node.ref.number;
  const status = stack.statuses.nodes[step.destination.index];
  return {
    parent: { number, label: `#${number}` },
    member: status?.kind === "member" ? status.member : null,
  };
}

/** Whether the merge area wants the comparison with the parent: only once it has merged. */
export function wantsLandingDiagnostic(
  stack: LandingStack | null,
  pr: LandingPullRequest | null,
): boolean {
  if (!stackLandingEligible(stack, pr)) return false;
  return directParent(stack)?.member?.state === "MERGED";
}

/** What the merge area tells the person about landing this PR, or null for nothing. */
export function stackLandingNotice(
  stack: LandingStack | null,
  pr: LandingPullRequest | null,
  diagnostic: StackLandingDiagnostic | null,
): StackLandingNotice | null {
  if (!stackLandingEligible(stack, pr)) return null;
  const below = directParent(stack);
  if (!below) return null;
  const { parent, member } = below;
  if (!member) return { kind: "parent-unknown", parent };
  if (member.state === "OPEN") return { kind: "merge-parent-first", parent };
  if (member.state !== "MERGED") return null;
  switch (diagnostic?.kind) {
    case "separable":
      return { kind: "sync", parent };
    case "stale":
      return { kind: "stale", parent };
    case "failed":
      return { kind: "failed", parent };
    default:
      return null;
  }
}

/** What the person reads for a notice, naming the stack's tool. */
export function stackLandingMessage(notice: StackLandingNotice, tool: StackLandingTool): string {
  const { label } = notice.parent;
  switch (notice.kind) {
    case "merge-parent-first":
      return `Merge ${label} first. A stack lands in order, starting with its first PR, and ${tool.landName} does that for you.`;
    case "parent-unknown":
      return `Couldn't tell whether ${label} is merged. A stack lands in order, starting with its first PR.`;
    case "sync":
      return `${label} is merged, but this PR still includes its commits. Run ${tool.syncCommand} before you merge.`;
    case "stale":
      return "This stack changed since it was checked.";
    case "failed":
      return `Couldn't check whether this PR still includes ${label}'s commits.`;
  }
}

// Git refuses control characters in a branch name, and a leading dash would read as an option.
const isControl = (character: string) => {
  const code = character.charCodeAt(0);
  return code < 0x20 || code === 0x7f;
};

/**
 * The branch to land through: the PR's head as GitHub last named it, for a stack that lands
 * with its tool. Null when there's none, or it can't be a branch name.
 */
export function stackLandBranch(
  stack: LandingStack | null,
  pr: LandingPullRequest | null,
): string | null {
  if (!stackLandingEligible(stack, pr)) return null;
  const branch = pr?.head_ref;
  if (typeof branch !== "string" || branch === "" || branch.startsWith("-")) return null;
  return [...branch].some(isControl) ? null : branch;
}
