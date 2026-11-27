// How the PR page shows a stack's entries and its Previous and Next commands, shared by the
// header's stack menu, the Overview's stack card and ⌘K so their labels can't drift.

import type { StackMemberStatus, StackMemberStatuses } from "@/lib/pullRequestStacks/stackMembers";
import type { StackNode, StackPullRequestRef } from "@/lib/pullRequestStacks/stackModel";
import type { StackStep } from "@/lib/pullRequestStacks/stackNavigation";
import { type CommandAvailability, AVAILABLE, unavailableBecause } from "@/store/paletteCommands";

/** An entry's tint: its PR state's, a review's, or muted for anything not read. */
export type StackEntryTone =
  "open" | "draft" | "merged" | "closed" | "approved" | "changes" | "muted";

export interface StackEntryView {
  /** The PR's title once read, else its branch. */
  title: string;
  /**
   * `#101` in this PR's repository, `owner/repo#12` in another, with the host before it on
   * another host; null for a branch not pushed yet.
   */
  number: string | null;
  status: string;
  /** The icon's tint, from the PR's state. */
  tone: StackEntryTone;
  /** The status's tint: a review's verdict stands out, a pending one doesn't. */
  statusTone: StackEntryTone;
  /** The PR's state for its icon, when it was read. */
  state: { state: string; isDraft: boolean } | null;
}

const REVIEWS: Record<string, { status: string; statusTone: StackEntryTone }> = {
  APPROVED: { status: "Approved", statusTone: "approved" },
  CHANGES_REQUESTED: { status: "Changes requested", statusTone: "changes" },
  REVIEW_REQUIRED: { status: "Review required", statusTone: "muted" },
};

/** A PR's number as this PR's page names it, with as much of where it lives as differs. */
export function stackReferenceLabel(
  ref: StackPullRequestRef,
  self: StackPullRequestRef | null,
): string {
  if (self && ref.host === self.host) {
    return ref.repository === self.repository
      ? `#${ref.number}`
      : `${ref.repository}#${ref.number}`;
  }
  return `${ref.host}/${ref.repository}#${ref.number}`;
}

/**
 * Where an entry opens: a PR in this repository on the page, any other in the browser, as
 * the description's own link would; null for a branch not pushed yet.
 */
export interface StackLink {
  url: string;
  external: boolean;
}

export function stackEntryLink(node: StackNode): StackLink | null {
  return node.ref ? { url: node.ref.url, external: node.external } : null;
}

/**
 * How much of the stack's state was read, or null once all of it was. It counts the PRs in
 * this repository the description lists, its dependency included; others are never read.
 */
export function stackCoverage(statuses: StackMemberStatuses): string | null {
  if (statuses.complete || statuses.total === 0) return null;
  const all = [...statuses.nodes, ...(statuses.dependency ? [statuses.dependency] : [])];
  if (all.some((status) => status.kind === "loading")) return "Loading statuses…";
  if (statuses.loaded === 0) return "Statuses unavailable";
  return `Status loaded for ${statuses.loaded} of ${statuses.total}`;
}

/** One entry as the menu and the card show it, `self` being the PR whose page shows it. */
export function stackEntryView(
  node: StackNode,
  status: StackMemberStatus,
  self: StackPullRequestRef | null,
): StackEntryView {
  const number = node.ref ? stackReferenceLabel(node.ref, self) : null;
  const plain = (text: string): StackEntryView => ({
    title: node.branch,
    number,
    status: text,
    tone: "muted",
    statusTone: "muted",
    state: null,
  });
  switch (status.kind) {
    case "member": {
      const { member } = status;
      const state = { state: member.state, isDraft: member.is_draft };
      const title = member.title || node.branch;
      const entry = (label: string, tone: StackEntryTone, statusTone = tone): StackEntryView => ({
        title,
        number,
        status: label,
        tone,
        statusTone,
        state,
      });
      if (member.state === "MERGED") return entry("Merged", "merged");
      if (member.state === "CLOSED") return entry("Closed", "closed");
      if (member.is_draft) return entry("Draft", "draft");
      const review = member.review_decision ? REVIEWS[member.review_decision] : undefined;
      return review
        ? entry(review.status, "open", review.statusTone)
        : entry("Open", "open", "muted");
    }
    case "missing":
      return plain("Not found");
    case "loading":
      return plain("Loading…");
    case "unknown":
      return plain("Status unavailable");
    case "unpushed":
      return plain("Not pushed yet");
    case "external":
      return plain("In another repository");
  }
}

/** A move through the stack as every entry point shows it: Previous, Next, First or Last. */
export interface StackStepCommand {
  label: string;
  availability: CommandAvailability;
  /** The PR it opens, or `choose` to let the person pick among several; null when unavailable. */
  target: string | "choose" | null;
}

export type StackMove = "previous" | "next" | "first" | "last";

const STACK_MOVE_NAMES: Record<StackMove, string> = {
  previous: "Previous in stack",
  next: "Next in stack",
  first: "Go to first in stack",
  last: "Go to last in stack",
};

export function stackStepCommand(direction: StackMove, step: StackStep): StackStepCommand {
  const name = STACK_MOVE_NAMES[direction];
  switch (step.kind) {
    case "go": {
      const { destination } = step;
      const number = destination.node.ref ? ` (#${destination.node.ref.number})` : "";
      return { label: `${name}${number}`, availability: AVAILABLE, target: destination.url };
    }
    case "choose":
      return { label: `${name}…`, availability: AVAILABLE, target: "choose" };
    case "unavailable":
      return { label: name, availability: unavailableBecause(step.reason), target: null };
  }
}
