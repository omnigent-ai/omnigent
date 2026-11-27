// A referenced PR's title and state, as the session's host looks it up: only once the reference
// is near the viewport, a few at a time, within the page's cap. Until then, and whenever the host
// can't look it up, the reference is a plain link.

import { type QueryClient, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useLayoutEffect, useRef } from "react";

import type { CodeHostPullRequest } from "@/lib/codeHosts";
import { automaticReadsAllowed, useAutomaticReadsAllowed } from "@/lib/githubAutomaticReads";
import type { PullRequestSuggestion } from "@/lib/pullRequestSuggestions";
import { parsePullRequestLookup } from "@/hooks/usePullRequestPreview";
import { readPullRequestLookupOnHost } from "@/lib/hostPullRequestReads";
import { PullRequestApiError } from "@/lib/pullRequestRpc";
import { usePullRequestMethodServed } from "@/pullRequestPage/pullRequestPageRoute";

import { pullRequestReferenceLookups } from "./lookupQueue";
import { type PullRequestReferenceContext, referenceFromUrl } from "./reference";
import type { PullRequestReferenceScopeValue } from "./scope";

/**
 * What's known of a referenced PR, and when the read that said so was sent. A failure is kept
 * too, briefly, so a remount doesn't repeat it, but it says nothing of the PR.
 */
export type ReferenceLookup = (
  { kind: "found"; pr: PullRequestSuggestion } | { kind: "missing" } | { kind: "failed" }
) & {
  /** Epoch ms the read was sent: a lookup's or the view's own. */
  readAt: number;
};

/**
 * Whether `cached` should stay over `incoming`, whoever wrote either: the newer read wins, and
 * a failure never replaces what a read did say.
 */
export function keepsCachedReference(
  cached: ReferenceLookup | undefined,
  incoming: ReferenceLookup,
): boolean {
  if (!cached || cached.kind === "failed") return false;
  return incoming.kind === "failed" || cached.readAt >= incoming.readAt;
}

const FOUND_FRESH_MS = 5 * 60_000;
const MISSING_FRESH_MS = 10 * 60_000;
const FAILED_FRESH_MS = 30_000;

const LOOKUP_FRESH_MS: Record<ReferenceLookup["kind"], number> = {
  found: FOUND_FRESH_MS,
  missing: MISSING_FRESH_MS,
  failed: FAILED_FRESH_MS,
};

/** The query key for `url` through `hostSession`'s host: each session's host answers for itself. */
export function referenceQueryKey(url: string, hostSession: string): readonly unknown[] {
  return ["pull-request-reference", url, "through-host", hostSession];
}

async function lookUpOnHost(conversationId: string, url: string): Promise<ReferenceLookup> {
  const readAt = Date.now();
  try {
    const pr = parsePullRequestLookup(await readPullRequestLookupOnHost(conversationId, url));
    return pr ? { kind: "found", pr, readAt } : { kind: "missing", readAt };
  } catch (error) {
    // The host refuses a PR GitHub doesn't show it as a precondition, which is a 400 here.
    return error instanceof PullRequestApiError && error.status === 400
      ? { kind: "missing", readAt }
      : { kind: "failed", readAt };
  }
}

/**
 * A referenced PR as the view knows it: `found` with its title and state; `loading` while its read is
 * admitted and hasn't answered; `missing` or `failed` once one did; `unread` where nothing
 * here reads it now, such as over the view's cap.
 */
export type ReferencedPullRequest =
  | { status: "found"; pr: PullRequestSuggestion }
  | { status: "loading" | "missing" | "failed" | "unread"; pr: null };

/**
 * What's known of the PR `target` names: not read while it's not near the viewport yet, where
 * the view's session's host can't look it up, or over the view's cap, or held by the read budget or focus. A view with a record of
 * its lookups shows what it has, and never sends a lookup the record answers.
 */
export function useReferencedPullRequest(
  target: CodeHostPullRequest | null,
  scope: PullRequestReferenceScopeValue | null,
  near: boolean,
): ReferencedPullRequest {
  const url = target?.url ?? null;
  const hostSession = scope?.hostSession ?? "";
  const hostServes = usePullRequestMethodServed(hostSession, "lookupPullRequest");
  const looksUp = !!url && !!hostSession && hostServes && target?.provider === "github";
  // The host's reads carry no reader of their own, so only the shared budget and focus hold them.
  const allowed = useAutomaticReadsAllowed(null);
  const queryClient = useQueryClient();
  const key = url ? referenceQueryKey(url, hostSession) : ["pull-request-reference", null];
  const recordKey = JSON.stringify(key);
  const record = scope?.record;
  const admitted = looksUp && near && allowed && !!url && !!scope?.budget.admit(recordKey, url);
  // Asked again when a waiting lookup's turn comes: one its message started streaming over since
  // stays unsent, as a held one does, until it's wanted again.
  const wanted = useRef(near);
  useLayoutEffect(() => {
    wanted.current = near;
  }, [near]);
  const query = useQuery<ReferenceLookup>({
    queryKey: key,
    // A wait ends with its view, and a hold that started meanwhile keeps it from going out;
    // neither is kept as an answer, so the lookup runs once it's wanted and allowed again.
    queryFn: async ({ signal }) => {
      const answer =
        record?.insteadOfLookup(recordKey, Date.now()) ??
        (await pullRequestReferenceLookups.run(
          () => {
            record?.sent(recordKey);
            return lookUpOnHost(hostSession, url!);
          },
          { signal, stillAllowed: () => wanted.current && automaticReadsAllowed(null) },
        ));
      // A read the view made while this one was out, such as its own PR's, may be newer.
      const cached = queryClient.getQueryData<ReferenceLookup>(key);
      const newest = keepsCachedReference(cached, answer) ? cached! : answer;
      record?.answered(recordKey, newest);
      return newest;
    },
    // What the view's record has shows even where nothing may be read now.
    placeholderData: () => record?.remembered(recordKey),
    enabled: admitted,
    staleTime: (settled) => LOOKUP_FRESH_MS[settled.state.data?.kind ?? "failed"],
    retry: false,
  });
  // A PR the view shows from a read made elsewhere joins its record too, with that answer, so a
  // newer one that comes while its row is scrolled away still reaches it.
  useEffect(() => {
    if (!record || !admitted) return;
    record.watch(recordKey);
    const cached = queryClient.getQueryData<ReferenceLookup>(JSON.parse(recordKey) as unknown[]);
    if (cached) record.answered(recordKey, cached);
  }, [record, admitted, recordKey, queryClient]);
  if (!looksUp) return { status: "unread", pr: null };
  const answer = query.data;
  if (answer?.kind === "found") return { status: "found", pr: answer.pr };
  if (answer) return { status: answer.kind, pr: null };
  return { status: admitted ? "loading" : "unread", pr: null };
}

/** A PR a view already read as its reader, and when that read was sent. */
export interface ReferenceSeed {
  pr: PullRequestSuggestion;
  /** Epoch ms; an answer cached later than this is newer and stays. */
  readAt: number;
}

/**
 * Puts the PRs a view already read, as the view's reader, under their reference keys, so its
 * own PR and its stack show their status without another lookup. As for a lookup's answer,
 * the newer read wins: a seed replaces an older answer or a failure, never one read after it.
 */
export function seedReferencedPullRequests(
  queryClient: QueryClient,
  context: PullRequestReferenceContext,
  seeds: readonly ReferenceSeed[],
  /** The view's session, whose host's lookups the seeds answer. */
  hostSession: string | null = null,
): void {
  if (!hostSession) return;
  for (const { pr, readAt } of seeds) {
    const url = referenceFromUrl(pr.url, context)?.target.url;
    if (!url) continue;
    const found: ReferenceLookup = { kind: "found", pr, readAt };
    const key = referenceQueryKey(url, hostSession);
    if (keepsCachedReference(queryClient.getQueryData<ReferenceLookup>(key), found)) continue;
    queryClient.setQueryData(key, found, { updatedAt: readAt });
  }
}
