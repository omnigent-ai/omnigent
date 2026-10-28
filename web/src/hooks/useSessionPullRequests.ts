// A session's linked PRs as the server stores them, which answer while the host is offline.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import type { ServerInfo } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import type { GitProviderDisplay } from "@/lib/gitProviders";
import { authenticatedFetch } from "@/lib/identity";
import { relativeTime } from "@/lib/relativeTime";
import { isTempConvId } from "@/lib/tempConversationId";
import {
  errorFromResponse,
  updateSessionPr,
  type PullRequestAssociation,
  type PullRequestInfo,
} from "@/hooks/usePullRequests";

/** A session's first linked PR, as `SessionListItem.pull_request` carries it. */
export interface SessionPullRequestSummary {
  url: string;
  provider: string;
  provider_display?: GitProviderDisplay | null;
  repository: string;
  number: number;
  title?: string | null;
  /** "OPEN" | "MERGED" | "CLOSED" as last fetched; null before the host fetched it. */
  state?: string | null;
  is_draft?: boolean;
  /** Epoch seconds of the fetch that read `state`; null before one did. */
  state_checked_at?: number | null;
}

/** The session's linked PRs on the server, in display order. */
export interface StoredPullRequests {
  prs: PullRequestAssociation[];
  /**
   * Whether the server has imported the session's PRs from its host. Until it
   * has, an empty list means "not known yet" rather than "none".
   */
  synced: boolean;
}

interface SessionPullRequestList {
  object: "list";
  data: PullRequestAssociation[];
  last_id: string | null;
  has_more: boolean;
  synced?: boolean;
}

/** How long a stored PR state counts as current; the server refreshes older open PRs. */
export const PULL_REQUEST_FRESHNESS_MS = 5 * 60_000;

/**
 * How old a stored PR state is, e.g. `"2h ago"`, once it's past the freshness
 * window; null while it's current or before the host fetched it.
 *
 * @param checkedAt - The state's `state_checked_at`, in epoch seconds.
 */
export function stateAsOf(
  checkedAt: number | null | undefined,
  nowMs: number = Date.now(),
): string | null {
  if (!checkedAt) return null;
  const checkedMs = checkedAt * 1000;
  return nowMs - checkedMs >= PULL_REQUEST_FRESHNESS_MS
    ? `${relativeTime(checkedMs, nowMs)} ago`
    : null;
}

/** Whether the server stores session PRs; older servers have no such routes. */
export function storesSessionPullRequests(info: ServerInfo | "loading"): boolean {
  return info !== "loading" && info.session_pull_requests === true;
}

export function sessionPullRequestsKey(conversationId: string | undefined) {
  return ["session-pull-requests", conversationId] as const;
}

function pullRequestsPath(conversationId: string, query: URLSearchParams): string {
  return `/v1/sessions/${encodeURIComponent(conversationId)}/pull-requests?${query}`;
}

/** Fetch all of the session's linked PRs from the server, in display order. */
export async function fetchSessionPullRequests(
  conversationId: string,
): Promise<StoredPullRequests> {
  const prs: PullRequestAssociation[] = [];
  let synced: boolean | undefined;
  let after: string | null = null;
  // Each page needs the previous page's cursor; a session links at most 50 PRs.
  /* oxlint-disable no-await-in-loop */
  do {
    const query = new URLSearchParams({ limit: "100", ...(after ? { after } : {}) });
    const response = await authenticatedFetch(pullRequestsPath(conversationId, query));
    if (!response.ok) throw await errorFromResponse(response);
    const page = (await response.json()) as SessionPullRequestList;
    prs.push(...page.data);
    synced = page.synced === true;
    const next = page.has_more ? page.last_id : null;
    if (page.has_more && (!next || next === after)) {
      throw new Error("The server returned an invalid page of pull requests");
    }
    after = next;
  } while (after);
  /* oxlint-enable no-await-in-loop */
  return { prs, synced: synced === true };
}

/**
 * The session's linked PRs from the server, in display order. Never waits on the host.
 *
 * @param live - The caller's live PR info, if any. Its list is the server's as of
 *   that read, so a live read that finds a new PR refetches this one.
 */
export function useSessionPullRequests(
  rawConversationId: string | undefined,
  live?: Pick<PullRequestInfo, "tracking_available" | "prs"> | null,
) {
  // A `temp:*` id (navigate-first new-chat window) has no server session.
  const conversationId = isTempConvId(rawConversationId) ? undefined : rawConversationId;
  const stored = storesSessionPullRequests(useServerInfo());
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: sessionPullRequestsKey(conversationId),
    queryFn: () => fetchSessionPullRequests(conversationId!),
    enabled: !!conversationId && stored,
    staleTime: 30_000,
  });
  const liveUrls = live?.tracking_available ? live.prs?.map((pr) => pr.url).join("\n") : undefined;
  const storedUrls = query.data?.prs.map((pr) => pr.url).join("\n");
  useEffect(() => {
    if (query.isSuccess && liveUrls !== undefined && liveUrls !== storedUrls) {
      void queryClient.invalidateQueries({ queryKey: sessionPullRequestsKey(conversationId) });
    }
  }, [conversationId, liveUrls, storedUrls, query.isSuccess, queryClient]);
  return query;
}

/**
 * The PRs to show from the server's list: once it has imported the session's
 * PRs, or when there's no live payload to show instead.
 */
export function storedListToShow(
  stored: StoredPullRequests | undefined,
  hasLive: boolean,
): PullRequestAssociation[] | undefined {
  return stored && (stored.synced || !hasLive) ? stored.prs : undefined;
}

/**
 * Unlink a PR on the server, which works while the host is offline. Returns
 * the remaining PRs.
 */
export function useUnlinkSessionPr(conversationId: string) {
  const queryClient = useQueryClient();
  const serverInfo = useServerInfo();
  const infoKey = ["github-info", conversationId];
  const storedKey = sessionPullRequestsKey(conversationId);
  return useMutation({
    // The panel outlives a session switch; a per-session key leaves a running
    // unlink its own session's callbacks and clears its state from the next one.
    mutationKey: ["unlink-session-pr", conversationId],
    mutationFn: async (
      url: string,
    ): Promise<{ prs: PullRequestAssociation[]; page?: SessionPullRequestList }> => {
      if (serverInfo === "loading") throw new Error("Still connecting to the server; try again.");
      // @deprecated server-pr-tracking compat, remove in 0.24.0 (~2026-11-24): servers
      // before 0.20.0 lack this route, so unlink through the runner or host instead.
      if (!storesSessionPullRequests(serverInfo)) {
        const info = await updateSessionPr(conversationId, { url, action: "remove" });
        return { prs: info.prs ?? [] };
      }
      const response = await authenticatedFetch(
        pullRequestsPath(conversationId, new URLSearchParams({ url })),
        { method: "DELETE" },
      );
      if (!response.ok) throw await errorFromResponse(response);
      const page = (await response.json()) as SessionPullRequestList;
      return { prs: page.data, page };
    },
    onSuccess: async ({ page }, url) => {
      // A read that started before the unlink would bring the PR back.
      await Promise.all([
        queryClient.cancelQueries({ queryKey: storedKey }),
        queryClient.cancelQueries({ queryKey: infoKey }),
      ]);
      // Only a server that stores PRs answers with its list; an older one has none to cache.
      if (page?.has_more) {
        void queryClient.invalidateQueries({ queryKey: storedKey });
      } else if (page) {
        queryClient.setQueryData<StoredPullRequests>(storedKey, {
          prs: page.data,
          synced: page.synced === true,
        });
      }
      queryClient.setQueriesData<PullRequestInfo>({ queryKey: infoKey }, (info) =>
        info?.prs
          ? {
              ...info,
              prs: info.prs.filter((pr) => pr.url !== url),
              ...(info.selected_pr_url === url ? { selected_pr_url: undefined, pr: null } : {}),
            }
          : info,
      );
      // The removed PR's own query stays put until the panel switches selection.
      void queryClient.invalidateQueries({
        queryKey: infoKey,
        predicate: (query) => query.queryKey[2] !== url,
      });
      // Sidebar and Canvas read the session's first PR from the session lists.
      void queryClient.invalidateQueries({ queryKey: ["conversations"] });
      void queryClient.invalidateQueries({ queryKey: ["project-sessions"] });
    },
  });
}
