import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  type PullRequestApiError,
  useUpdateSessionPr,
  type usePullRequestInfo,
} from "@/newPullRequestPage/hooks/usePullRequests";
import {
  followPullRequestIntents,
  readSessionPullRequestsAgain,
  storesSessionPullRequests,
} from "@/newPullRequestPage/hooks/useSessionPullRequests";
import { useWorkspaceAsleep, useWorkspaceServeable } from "@/hooks/useWorkspaceChangedFiles";
import { RunnerOfflineError } from "@/lib/workspaceReadErrors";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import {
  canKeepLinksOnThisMac,
  linkPullRequestOnThisMac,
  localPullRequestLinksKey,
} from "@/lib/localPullRequestLinks";
import { SESSION_PULL_REQUEST_LIMIT_REASON } from "@/lib/sessionPullRequestMembership";
import {
  beginPullRequestIntent,
  failPullRequestIntent,
  finishPullRequestIntentRequest,
  pullRequestChangeIsUnclear,
  type PullRequestIntentEnd,
  PullRequestIntentReplaced,
  pullRequestIntentEnd,
} from "@/store/sessionPullRequestIntents";

/**
 * Whether neither the runner nor the host can answer, so PR surfaces fall back
 * to the server's stored PRs. Live info cached before they went away counts as
 * offline too, since its failed refetch would otherwise replace the PR.
 */
export function useSessionHostOffline(
  conversationId: string,
  info: Pick<ReturnType<typeof usePullRequestInfo>, "error">,
): boolean {
  const serveable = useWorkspaceServeable(conversationId);
  const sandboxAsleep = useWorkspaceAsleep(conversationId);
  return serveable === false || sandboxAsleep || info.error instanceof RunnerOfflineError;
}

/** Whether GitSpoke links can be kept on this Mac, so linking one needs no host. */
export function useCanKeepLinksOnThisMac(): boolean {
  const query = useQuery({
    queryKey: ["local-pull-request-links-available"],
    queryFn: canKeepLinksOnThisMac,
    staleTime: 60_000,
  });
  return query.data === true;
}

/** Whether a failed link may still have gone through; see {@link pullRequestChangeIsUnclear}. */
export const linkFailureIsAmbiguous = pullRequestChangeIsUnclear;

/** The HTTP status a server's refusal carries, or null for a request that got no answer. */
function responseStatus(error: unknown): number | null {
  const status = (error as Partial<PullRequestApiError> | null)?.status;
  return typeof status === "number" ? status : null;
}

/** What to tell the user about a link that failed, in the app's own words where it has them. */
function linkFailureForUser(error: unknown): unknown {
  return responseStatus(error) === 409 ? new Error(SESSION_PULL_REQUEST_LIMIT_REASON) : error;
}

/** What a link's end means for whoever asked for it. */
function reportLinkEnd(end: PullRequestIntentEnd, linkedUrl: string): string {
  if (end.status === "settled") return linkedUrl;
  if (end.status === "unconfirmed") throw new Error(end.reason);
  // A newer unlink or link of the PR took over (or, never here, a refusal already reported).
  throw new PullRequestIntentReplaced();
}

/**
 * Links a PR to the session through its host; a GitSpoke PR, which the host can't verify, is
 * linked on this Mac instead. It shows linked at once and its reply writes nothing. It resolves,
 * with the URL the host selected, once a read of the list shows it; a refusal reverts it at
 * once. A newer change to the same PR ends it with {@link PullRequestIntentReplaced}, and a list
 * that never shows it in time ends it with the reason.
 */
export function useAttachSessionPullRequest(conversationId: string) {
  const queryClient = useQueryClient();
  const storesPrs = storesSessionPullRequests(useServerInfo());
  const update = useUpdateSessionPr(conversationId);
  return useMutation({
    mutationKey: ["attach-session-pr", conversationId],
    mutationFn: async (url: string): Promise<string | undefined> => {
      followPullRequestIntents(queryClient);
      const intent = beginPullRequestIntent(conversationId, url, "link");
      const end = pullRequestIntentEnd(conversationId, intent);
      const request = (async (): Promise<{ linkedUrl?: string; error?: unknown }> => {
        try {
          const local = await linkPullRequestOnThisMac(conversationId, url);
          if (local) {
            void queryClient.invalidateQueries({ queryKey: ["github-info", conversationId] });
            void queryClient.invalidateQueries({
              queryKey: localPullRequestLinksKey(conversationId),
            });
            return { linkedUrl: local.url };
          }
          return {
            linkedUrl: (await update.mutateAsync({ url, action: "attach" })).selected_pr_url,
          };
        } catch (error) {
          return { error };
        }
      })();
      // The intent may end first: replaced by a newer change, or out of time.
      const answer = await Promise.race([request, end.then((ended) => ({ ended }))]);
      if ("ended" in answer) return reportLinkEnd(answer.ended, url);
      if (answer.error !== undefined) {
        if (!storesPrs || !linkFailureIsAmbiguous(answer.error)) {
          failPullRequestIntent(conversationId, intent);
          throw answer.error;
        }
        // It may have landed before the answer was lost: the server's list decides.
        const reason = linkFailureForUser(answer.error);
        finishPullRequestIntentRequest(
          conversationId,
          intent,
          reason instanceof Error ? reason.message : undefined,
        );
      } else {
        finishPullRequestIntentRequest(conversationId, intent);
      }
      void readSessionPullRequestsAgain(queryClient, conversationId, storesPrs);
      return reportLinkEnd(await end, answer.linkedUrl ?? url);
    },
  });
}
