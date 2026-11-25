// Check Again after a change GitHub may or may not have made: a read of the PR on this Mac
// that this check started, and that answered for this PR, is the only thing that settles it.

import { hashKey, type QueryClient } from "@tanstack/react-query";

import {
  fetchPullRequestInfo,
  type PullRequestAutoMerge,
} from "@/newPullRequestPage/hooks/usePullRequests";
import type { FreshPullRequestFacts } from "./changeAttempts";
import { macReadTargetAs, readScope } from "@/lib/desktopReadPath";
import { getCurrentUserId } from "@/lib/identity";
import { namedPullRequestWritten, type MacNamedReader } from "@/lib/macNamedReads";
import { autoMergeKnown } from "@/lib/pullRequestAutoMerge";
import {
  pullRequestRecordRead,
  readRecord,
  type PullRequestRecordRead,
} from "@/lib/pullRequestRecord";
import {
  currentWorkspaceAuthGeneration,
  macRouteCurrent,
  macWriteRoute,
} from "@/lib/desktopPullRequestWrites";

import type { HostChangeRoute } from "./pullRequestPageRoute";

/**
 * - `confirmed`: a fresh read of this PR on this Mac answered with a known state.
 * - `failed`: no such read, or it didn't answer; the hold stays.
 * - `stale`: the workspace user, the read's scope or this Mac's account changed meanwhile, so
 *   the read can't speak for the change; nothing is said.
 */
export type FreshReadResult = "confirmed" | "failed" | "stale";

/** A fresh read's result, and where it confirmed the PR stands. */
export interface FreshRead {
  result: FreshReadResult;
  /** Set only where `confirmed`: the state and draft flag this very read answered. */
  facts: FreshPullRequestFacts | null;
}

export async function confirmFreshPullRequestRead(
  ...args: Parameters<typeof readFreshPullRequest>
): Promise<FreshReadResult> {
  return (await readFreshPullRequest(...args)).result;
}

export async function readFreshPullRequest(
  queryClient: QueryClient,
  conversationId: string,
  prUrl: string,
  /**
   * The login the change was made as, and the page's own login it was read as (null for the
   * host's account): a read as any other can't settle it. Its credential may have been
   * renewed since; the read is bound to the one current while it runs.
   */
  madeAs: { login: string; reader: MacNamedReader | null; host?: HostChangeRoute },
): Promise<FreshRead> {
  if (madeAs.host) return readFreshPullRequestOnHost(queryClient, conversationId, prUrl);
  const { reader } = madeAs;
  const generation = currentWorkspaceAuthGeneration();
  const scope = readScope(prUrl, reader);
  // Under the login's sign-in when the check starts; it must hold until the read answers.
  const started = macWriteRoute(prUrl, reader);
  const actor = started?.actor;
  if (actor && actor.login.toLowerCase() !== madeAs.login.toLowerCase()) return STALE;
  // The PR's own read on this Mac as the account that acts, in its current scope: the session's
  // list, a mutation's context, or a read as the Pull Requests page's own login never confirm.
  if (!actor) return FAILED;
  const target = macReadTargetAs(prUrl, reader);
  const record = pullRequestRecordRead("github-info", conversationId, prUrl, scope, target);
  const { queryKey } = record;
  // A shared record of another account than the one that acts can't settle the change.
  if (record.owner.kind === "mac" && record.owner.reader !== null) {
    if (record.owner.reader.toLowerCase() !== actor.login.toLowerCase()) return STALE;
  }
  // A Pull Requests page reading as its own login shows no read as the actor; this check
  // starts one itself. An observed query is refetched, cancelling a read already in flight.
  const existing = queryClient.getQueryCache().find({ queryKey, exact: true });
  if (!existing && !target) return FAILED;
  const query =
    existing ??
    queryClient.getQueryCache().build(queryClient, {
      queryKey,
      queryFn: ({ signal }) =>
        readRecord(queryClient, record, target, (sentTarget) =>
          fetchPullRequestInfo(conversationId, prUrl, {
            signal,
            macTarget: sentTarget,
            hostServeable: false,
            sessionless: true,
          }),
        ),
      retry: false,
    });
  const updatesBefore = query.state.dataUpdateCount;
  let fetched = true;
  try {
    if (existing) {
      // Cancels a read already in flight, so the answer comes from a read this check started.
      await queryClient.refetchQueries(
        { queryKey, exact: true },
        { cancelRefetch: true, throwOnError: true },
      );
    } else {
      await query.fetch();
    }
  } catch {
    fetched = false;
  }
  // A read made as another account, or in another scope, says nothing about this change.
  if (
    generation !== currentWorkspaceAuthGeneration() ||
    !started ||
    !macRouteCurrent(prUrl, started) ||
    JSON.stringify(readScope(prUrl, reader)) !== JSON.stringify(scope) ||
    !readsAsActor(
      record,
      pullRequestRecordRead(
        "github-info",
        conversationId,
        prUrl,
        scope,
        macReadTargetAs(prUrl, reader),
      ),
      actor.login,
    )
  ) {
    return STALE;
  }
  const info = query.state.data as ReadPullRequestInfo | undefined;
  const readState = info?.pr?.state?.toUpperCase();
  const confirmed =
    fetched &&
    query.state.status === "success" &&
    query.state.dataUpdateCount > updatesBefore &&
    info?.selected_pr_url === prUrl &&
    // GitHub's own URL for the PR can differ from the one it was opened by in case.
    (!info.pr?.url || info.pr.url.toLowerCase() === prUrl.toLowerCase()) &&
    (readState === "OPEN" || readState === "CLOSED" || readState === "MERGED");
  // The change is settled; what the Pull Requests page shows as its own login reads it again.
  if (!confirmed) return FAILED;
  namedPullRequestWritten(prUrl, getCurrentUserId());
  return { result: "confirmed", facts: factsOf(info) };
}

interface ReadPullRequestInfo {
  selected_pr_url?: string | null;
  pr?: {
    url?: string | null;
    state?: string | null;
    is_draft?: boolean | null;
    reviewers?: unknown;
    auto_merge?: PullRequestAutoMerge | null;
  } | null;
}

function factsOf(info: ReadPullRequestInfo | undefined): FreshPullRequestFacts {
  return {
    state: info?.pr?.state?.toUpperCase() as FreshPullRequestFacts["state"],
    isDraft: info?.pr?.is_draft === true,
    reviewers: reviewersOf(info?.pr?.reviewers),
    autoMerge: autoMergeKnown(info?.pr),
  };
}

/**
 * Check Again for a change the session's host made: a read through the host that this check
 * sent, answering for this PR with a known state; the page's own reads follow it.
 */
async function readFreshPullRequestOnHost(
  queryClient: QueryClient,
  conversationId: string,
  prUrl: string,
): Promise<FreshRead> {
  const generation = currentWorkspaceAuthGeneration();
  let info: ReadPullRequestInfo;
  try {
    info = (await fetchPullRequestInfo(conversationId, prUrl)) as ReadPullRequestInfo;
  } catch {
    return FAILED;
  }
  // Another workspace user since: this read can't speak for the change.
  if (generation !== currentWorkspaceAuthGeneration()) return STALE;
  const state = info.pr?.state?.toUpperCase();
  const confirmed =
    info.selected_pr_url === prUrl &&
    (!info.pr?.url || info.pr.url.toLowerCase() === prUrl.toLowerCase()) &&
    (state === "OPEN" || state === "CLOSED" || state === "MERGED");
  if (!confirmed) return FAILED;
  void queryClient.invalidateQueries({ queryKey: ["github-info", conversationId] });
  return { result: "confirmed", facts: factsOf(info) };
}

/**
 * Whether the record the PR's views show now, `shown`, is the one the check read (`read`) or,
 * for a read sent before the scope had a reader, the record its own answer proved: the actor's,
 * in the epoch the read was sent in. The read's answer itself stays the one it got.
 */
function readsAsActor(
  read: PullRequestRecordRead,
  shown: PullRequestRecordRead,
  actor: string,
): boolean {
  if (hashKey(shown.queryKey) === hashKey(read.queryKey)) return true;
  const { owner } = read;
  const now = shown.owner;
  return (
    owner.kind === "mac" &&
    owner.reader === null &&
    now.kind === "mac" &&
    // Same as the read's own viewer: within one epoch a reader only moves from none to its first.
    now.reader?.toLowerCase() === actor.toLowerCase() &&
    now.epoch === owner.epoch
  );
}

/**
 * The answer's reviewers; null where it sent none, or any it can't read, since a list missing
 * one can't say who is still asked.
 */
function reviewersOf(value: unknown): FreshPullRequestFacts["reviewers"] {
  if (!Array.isArray(value)) return null;
  const reviewers: { login: string; kind: string; state: string }[] = [];
  for (const reviewer of value) {
    const { login, kind, state } = (reviewer ?? {}) as Record<string, unknown>;
    if (typeof login !== "string" || typeof state !== "string") return null;
    if (kind !== "user" && kind !== "team") return null;
    reviewers.push({ login, kind, state });
  }
  return reviewers;
}

const STALE: FreshRead = { result: "stale", facts: null };
const FAILED: FreshRead = { result: "failed", facts: null };
