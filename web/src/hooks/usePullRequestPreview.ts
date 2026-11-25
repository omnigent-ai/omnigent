import { useQuery } from "@tanstack/react-query";

import { readOnThisMac } from "@/lib/desktopPullRequests";
import { parseCodeHostPullRequestUrl } from "@/lib/codeHosts";
import { readPullRequestLookupOnHost } from "@/lib/hostPullRequestReads";
import { usePullRequestMethodServed } from "@/pullRequestPage/pullRequestPageRoute";
import {
  macOffers,
  macReadTarget,
  readPathSettled,
  readScope,
  useDesktopReadPathVersion,
} from "@/lib/desktopReadPath";
import type { PullRequestSuggestion } from "@/lib/pullRequestSuggestions";
import { useMacReadScopes } from "@/hooks/useMacReadScopes";

export type PullRequestPreviewState =
  | { status: "none" }
  | { status: "loading" }
  | { status: "ready"; preview: PullRequestSuggestion }
  | { status: "error"; message: string };

/**
 * The typed PR link as this Mac looks it up, for a PR this Mac reads; GitHub
 * Enterprise hosts count once this Mac's gh is signed in to them. Elsewhere, the session's
 * host looks it up where it serves that to this caller. It never blocks linking.
 */
export function usePullRequestPreview(
  input: string,
  enabled: boolean,
  sessionId = "",
): PullRequestPreviewState {
  // The read path's store decides; its version re-renders this once it changes.
  useDesktopReadPathVersion();
  useMacReadScopes();
  const typed = input.trim();
  const settled = enabled && /^https?:\/\//i.test(typed) && readPathSettled(typed);
  const target = settled ? macReadTarget(typed) : null;
  const macLooksUp = !!target && macOffers(target.provider, "pull_request_lookup");
  const hostServes = usePullRequestMethodServed(sessionId, "lookupPullRequest");
  const hostTarget =
    !macLooksUp && enabled && hostServes ? parseCodeHostPullRequestUrl(typed) : null;
  const onHost = hostTarget?.provider === "github";
  const looksUp = macLooksUp || onHost;
  const query = useQuery({
    queryKey: onHost
      ? ["pull-request-preview", "host", sessionId, hostTarget.url]
      : ["pull-request-preview", target?.url, ...(target ? readScope(target.url) : [])],
    queryFn: async () =>
      onHost
        ? parsePullRequestLookup(await readPullRequestLookupOnHost(sessionId, hostTarget.url))
        : parsePullRequestLookup(
            (
              await readOnThisMac<unknown>(target!, "pull_request_lookup", {
                repository: target!.repository,
                number: target!.number,
              })
            ).data,
          ),
    enabled: looksUp,
    staleTime: 30_000,
    retry: false,
  });
  if (!looksUp) return { status: "none" };
  if (query.data) return { status: "ready", preview: query.data };
  // An answer without a PR's details, e.g. from an older shell, just shows no preview.
  if (query.data === null) return { status: "none" };
  if (query.error) {
    return {
      status: "error",
      message: query.error instanceof Error ? query.error.message : "Something went wrong.",
    };
  }
  return { status: "loading" };
}

/** The lookup's PR, when it carries what a preview shows; null for an answer without one. */
export function parsePullRequestLookup(data: unknown): PullRequestSuggestion | null {
  const pr = data as Partial<PullRequestSuggestion> | null;
  if (typeof pr?.url !== "string" || !Number.isInteger(pr.number)) return null;
  if (typeof pr.title !== "string") return null;
  return {
    url: pr.url,
    number: pr.number!,
    title: pr.title,
    state: typeof pr.state === "string" ? pr.state : null,
    is_draft: pr.is_draft === true,
    repository: typeof pr.repository === "string" ? pr.repository : null,
    updated_at: typeof pr.updated_at === "string" ? pr.updated_at : null,
  };
}
