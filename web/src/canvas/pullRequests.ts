// The pull request each canvas card shows: the session's first linked PR as the
// server stores it (`Conversation.pull_request`). Cards make no runner request
// and keep their PR while the host is offline; the session list stream keeps
// it current.

import { useMemo } from "react";
import type { GitProviderDisplay } from "@/lib/gitProviders";
import type { Conversation } from "@/hooks/useConversations";

export interface CanvasPullRequest {
  number: number;
  title: string;
  /** "OPEN" | "MERGED" | "CLOSED" as last fetched, or null before the host fetched it. */
  state: string | null;
  url: string;
  /** Git provider id, which picks the number prefix: `!` for Azure DevOps, else `#`. */
  provider?: string | null;
  provider_display?: GitProviderDisplay | null;
}

export type CanvasPullRequests = Record<string, CanvasPullRequest | null>;

/** The card's PR for one session; null without one or without an https URL. */
export function canvasPullRequest(session: Conversation): CanvasPullRequest | null {
  const pr = session.pull_request;
  if (!pr || !/^https:\/\//.test(pr.url)) return null;
  return {
    number: pr.number,
    title: pr.title?.trim() || pr.repository,
    state: pr.state ?? null,
    url: pr.url,
    provider: pr.provider,
    provider_display: pr.provider_display ?? null,
  };
}

/** Each session's card PR, keyed by session id. Pass a memoized list. */
export function usePullRequests(sessions: readonly Conversation[]): CanvasPullRequests {
  return useMemo(
    () => Object.fromEntries(sessions.map((session) => [session.id, canvasPullRequest(session)])),
    [sessions],
  );
}
