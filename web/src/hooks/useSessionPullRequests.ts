// The session's linked pull requests as the server stores them. The runner or
// host still fetches every PR from its forge; the server keeps the list and the
// last summaries, so these answer while both are offline.
//
//   useSessionPullRequests — GET    /v1/sessions/{id}/pull-requests
//   useUnlinkSessionPr     — DELETE /v1/sessions/{id}/pull-requests?url=
//
// The session list item carries the first PR (`Conversation.pull_request`) for
// the sidebar, canvas and composer, which read it without a request.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { GitProviderDisplay } from "@/lib/gitProviders";
import { authenticatedFetch } from "@/lib/identity";
import { isTempConvId } from "@/lib/tempConversationId";
import {
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
}

interface SessionPullRequestList {
  object: "list";
  data: PullRequestAssociation[];
}

export function sessionPullRequestsKey(conversationId: string | undefined) {
  return ["session-pull-requests", conversationId] as const;
}

async function errorMessage(response: Response): Promise<Error> {
  try {
    const body = (await response.json()) as { error?: { message?: string }; detail?: unknown };
    if (body?.error?.message) return new Error(body.error.message);
    if (typeof body?.detail === "string" && body.detail) return new Error(body.detail);
  } catch {
    // Non-JSON body (gateway error) — keep the status line.
  }
  return new Error(`${response.status} ${response.statusText}`);
}

/** Fetch the session's linked PRs from the server, in display order. */
export async function fetchSessionPullRequests(
  conversationId: string,
): Promise<PullRequestAssociation[]> {
  const response = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(conversationId)}/pull-requests`,
  );
  if (!response.ok) throw await errorMessage(response);
  return ((await response.json()) as SessionPullRequestList).data;
}

/** The session's linked PRs from the server, in display order. Never waits on the host. */
export function useSessionPullRequests(rawConversationId: string | undefined) {
  // A `temp:*` id (navigate-first new-chat window) has no server session.
  const conversationId = isTempConvId(rawConversationId) ? undefined : rawConversationId;
  return useQuery({
    queryKey: sessionPullRequestsKey(conversationId),
    queryFn: () => fetchSessionPullRequests(conversationId!),
    enabled: !!conversationId,
    staleTime: 30_000,
  });
}

/**
 * Unlink a PR on the server, which works while the host is offline; the host
 * applies it with its next request. Returns the remaining PRs.
 */
export function useUnlinkSessionPr(conversationId: string) {
  const queryClient = useQueryClient();
  const infoKey = ["github-info", conversationId];
  return useMutation({
    mutationFn: async (url: string): Promise<PullRequestAssociation[]> => {
      const response = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(conversationId)}/pull-requests?${new URLSearchParams({ url })}`,
        { method: "DELETE" },
      );
      // @deprecated server-pr-tracking compat, remove in 0.24.0 (~2026-11-24): servers
      // before 0.20.0 lack this route, so unlink through the runner or host instead.
      if (response.status === 404 || response.status === 405) {
        return (await updateSessionPr(conversationId, { url, action: "remove" })).prs ?? [];
      }
      if (!response.ok) throw await errorMessage(response);
      return ((await response.json()) as SessionPullRequestList).data;
    },
    onSuccess: async (remaining, url) => {
      queryClient.setQueryData(sessionPullRequestsKey(conversationId), remaining);
      // A poll that started before the unlink would bring the PR back.
      await queryClient.cancelQueries({ queryKey: infoKey });
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
      queryClient.invalidateQueries({
        queryKey: infoKey,
        predicate: (query) => query.queryKey[2] !== url,
      });
    },
  });
}
