// A pull request link in chat, as the shared pull request actions see it: the PR
// it names, and whether it can be linked to the chat's session. Plain facts in,
// so the link's menu and ⌘K can't disagree.

import {
  type CodeHostPullRequest,
  parseCodeHostPullRequestUrl,
  isDeviceReadProvider,
} from "@/lib/codeHosts";
import { SESSION_PULL_REQUEST_LIMIT_REASON } from "@/lib/sessionPullRequestMembership";
import {
  AVAILABLE,
  type CommandAvailability,
  isCommandAvailable,
  unavailableBecause,
} from "@/store/paletteCommands";

import type { PullRequestTarget } from "../pullRequestCommands/model";

export const ALREADY_LINKED_REASON = "Already linked to this session.";
export const LINK_PENDING_REASON = "Wait for the pull request being linked.";

/** The PR a chat link names, on github.com, GitHub Enterprise or a device-read host; null for any other link. */
export function chatPullRequestTarget(
  href: string | null | undefined,
  /** The PR as the chat already recognized it, such as on a GitHub Enterprise host it knows. */
  known?: CodeHostPullRequest | null,
): PullRequestTarget | null {
  const pullRequest = known ?? parseCodeHostPullRequestUrl(href);
  if (!pullRequest) return null;
  return {
    url: pullRequest.url,
    number: pullRequest.number,
    // A chat link knows no title; ⌘K names it by number.
    title: null,
    provider: isDeviceReadProvider(pullRequest.provider) ? pullRequest.provider : "github",
  };
}

export interface LinkToSessionFacts {
  /** Who may link PRs to the session: hidden without one, unavailable without edit access. */
  access: CommandAvailability;
  linked: boolean;
  /** The session links as many PRs as it can, so this one has no room. */
  full?: boolean;
  /** A link to the session is already in progress; links run one at a time. */
  pending: boolean;
  /** Why the session's host can't verify a link right now, or null while it can. */
  hostUnavailableReason: string | null;
}

/** Whether a PR can be linked to the session now, and why not, for every entry point. */
export function linkToSessionAvailability(facts: LinkToSessionFacts): CommandAvailability {
  if (!isCommandAvailable(facts.access)) return facts.access;
  if (facts.linked) return unavailableBecause(ALREADY_LINKED_REASON);
  if (facts.full) return unavailableBecause(SESSION_PULL_REQUEST_LIMIT_REASON);
  if (facts.pending) return unavailableBecause(LINK_PENDING_REASON);
  if (facts.hostUnavailableReason) {
    return unavailableBecause(facts.hostUnavailableReason);
  }
  return AVAILABLE;
}
