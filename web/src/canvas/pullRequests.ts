// Cards show the session's stored primary PR from the session list.

import { useMemo } from "react";
import type { GitProviderDisplay } from "@/lib/gitProviders";
import type { Conversation } from "@/hooks/useConversations";
import { storesSessionPullRequests } from "@/hooks/useSessionPullRequests";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { useLivePullRequests } from "./legacyPullRequests";

export interface CanvasPullRequest {
  number: number;
  title: string;
  /** "OPEN" | "MERGED" | "CLOSED" as last fetched, or null before the host fetched it. */
  state: string | null;
  url: string;
  /** Git provider id, which picks the number prefix: `!` for Azure DevOps, else `#`. */
  provider?: string | null;
  provider_display?: GitProviderDisplay | null;
  /** Epoch seconds the host last fetched `state`; older states show their age. */
  state_checked_at?: number | null;
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
    state_checked_at: pr.state_checked_at ?? null,
  };
}

/** Each session's card PR, keyed by session id. Pass a memoized list. */
export function usePullRequests(sessions: readonly Conversation[]): CanvasPullRequests {
  const info = useServerInfo();
  const stored = storesSessionPullRequests(info);
  const live = useLivePullRequests(sessions, info !== "loading" && !stored);
  const fromList = useMemo(
    () => Object.fromEntries(sessions.map((session) => [session.id, canvasPullRequest(session)])),
    [sessions],
  );
  return stored ? fromList : live;
}
