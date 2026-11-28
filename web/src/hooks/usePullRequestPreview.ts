import { useQuery } from "@tanstack/react-query";

import { parseCodeHostPullRequestUrl } from "@/lib/codeHosts";
import { readPullRequestLookupOnHost } from "@/lib/hostPullRequestReads";
import { usePullRequestMethodServed } from "@/pullRequestPage/pullRequestPageRoute";
import type { PullRequestSuggestion } from "@/lib/pullRequestSuggestions";

export type PullRequestPreviewState =
  | { status: "none" }
  | { status: "loading" }
  | { status: "ready"; preview: PullRequestSuggestion }
  | { status: "error"; message: string };

/**
 * The typed GitHub PR link as the session's host looks it up, where it serves that to this
 * caller. Without the host's method nothing is shown, and it never blocks linking.
 */
export function usePullRequestPreview(
  input: string,
  enabled: boolean,
  sessionId = "",
): PullRequestPreviewState {
  const typed = input.trim();
  const hostServes = usePullRequestMethodServed(sessionId, "lookupPullRequest");
  const hostTarget =
    enabled && hostServes && /^https?:\/\//i.test(typed)
      ? parseCodeHostPullRequestUrl(typed)
      : null;
  const looksUp = hostTarget?.provider === "github";
  const query = useQuery({
    queryKey: ["pull-request-preview", "host", sessionId, hostTarget?.url],
    queryFn: async () =>
      parsePullRequestLookup(await readPullRequestLookupOnHost(sessionId, hostTarget!.url)),
    enabled: looksUp,
    staleTime: 30_000,
    retry: false,
  });
  if (!looksUp) return { status: "none" };
  if (query.data) return { status: "ready", preview: query.data };
  // An answer without a PR's details just shows no preview.
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
