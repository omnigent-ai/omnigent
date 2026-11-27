// When "Link a pull request" can run, shared by ⌘K and the Pull Requests panel
// so they can't disagree. Linking needs edit access, as the server requires.

import { useMemo } from "react";

import type { CommandAvailability } from "@/store/paletteCommands";

import { sandboxAsleepLinkHint } from "./pullRequestListModel";
import {
  type CommandSession,
  sessionCommandAvailability,
  useCommandSession,
} from "./commandContext";

export const LINK_PULL_REQUEST_NEEDS_EDIT = "You need edit access to link pull requests.";

/** Why a link the session's host must verify can't be made while the host is away. */
export function linkNeedsHostReason(sandboxAsleep: boolean, sandboxResuming: boolean): string {
  return sandboxAsleep
    ? sandboxAsleepLinkHint(sandboxResuming)
    : "To link a PR, please reconnect the session’s host.";
}

/** Availability for linking a pull request to `session`; hidden without one. */
export function linkPullRequestAvailability(session: CommandSession | null): CommandAvailability {
  return sessionCommandAvailability(session, "edit", LINK_PULL_REQUEST_NEEDS_EDIT);
}

/**
 * The same rule for a surface showing `sessionId`, such as a grid pane that
 * isn't the routed session. Its own snapshot decides; until it loads, access
 * is unconfirmed.
 */
export function useLinkPullRequestAvailability(sessionId: string): CommandAvailability {
  const session = useCommandSession(sessionId);
  return useMemo(() => linkPullRequestAvailability(session), [session]);
}
