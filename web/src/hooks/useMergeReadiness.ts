// The Pull Requests page's reads of merge readiness and owned files, from the registered merge
// readiness source. Without one, no PR has a readiness view and none marks owned files.

import type { QueryClient } from "@tanstack/react-query";

import { pullRequestFastTrackAccount } from "@/lib/githubFastTrack";
import type { MergeReadinessView } from "@/lib/mergeReadiness";
import { githubPullRequestLocation } from "@/lib/pullRequestReference";
import {
  type MergeReadinessReadOptions,
  type MergeReadinessRequirement,
  type MergeReadinessTarget,
  type OwnedFilesTarget,
  pullRequestIntegrations,
} from "@/lib/pullRequestIntegrations";
import {
  normalizePullRequestInfo,
  type PullRequestInfo,
} from "@/newPullRequestPage/hooks/usePullRequests";

export type { MergeReadinessRequirement, MergeReadinessTarget, OwnedFilesTarget };

const NO_VIEWS: ReadonlyMap<string, MergeReadinessView> = new Map();

const source = () => pullRequestIntegrations().mergeReadiness;

/**
 * A session's PR as a readiness target, read as the account in that PR's own response.
 * Accounts are chosen per repository, so a response about another PR gives no account.
 */
export function sessionMergeReadinessTarget(
  info: PullRequestInfo | undefined,
  url: string,
  headSha: string | null | undefined,
): MergeReadinessTarget {
  const normalized = info?.pr?.url === url ? normalizePullRequestInfo(info) : undefined;
  return {
    url,
    account: pullRequestFastTrackAccount({
      provider: normalized?.provider,
      prHost: githubPullRequestLocation(url)?.host,
      selectedAccount: normalized?.auth?.selected_account,
    }),
    headSha: headSha ?? null,
  };
}

function useNoMergeReadiness(): ReadonlyMap<string, MergeReadinessView> {
  return NO_VIEWS;
}

function useNoOwnedFiles(): readonly string[] | null {
  return null;
}

/** Each target's merge readiness by URL; PRs the source doesn't cover are absent. */
export function useMergeReadiness(
  targets: readonly MergeReadinessTarget[],
  options: MergeReadinessReadOptions,
): ReadonlyMap<string, MergeReadinessView> {
  const useRead = source()?.useMergeReadiness ?? useNoMergeReadiness;
  return useRead(targets, options);
}

/** The GitHub read the source joins with its own, or null without a source. */
export function mergeReadinessGitHubOperation(): string | null {
  return source()?.githubOperation ?? null;
}

/** Read every PR's merge readiness again now, for an explicit user refresh. */
export async function refreshMergeReadiness(queryClient: QueryClient): Promise<void> {
  await source()?.refresh(queryClient);
}

/** Read one pull request's merge readiness again now, for its Refresh. */
export async function refreshPullRequestMergeReadiness(
  queryClient: QueryClient,
  prUrl: string,
): Promise<void> {
  await source()?.refreshPullRequest(queryClient, prUrl);
}

/** Which of `prUrl`'s merge readiness reads `queryKey` is, or null for any other query. */
export function mergeReadinessReadOf(
  queryKey: readonly unknown[],
  prUrl: string,
): "readiness" | "checks" | null {
  return source()?.readOf(queryKey, prUrl) ?? null;
}

/** Whether `queryKey` is one of `prUrl`'s merge readiness reads. */
export function isPullRequestMergeReadinessKey(
  queryKey: readonly unknown[],
  prUrl: string,
): boolean {
  return mergeReadinessReadOf(queryKey, prUrl) !== null;
}

const requirementsByReader = new Map<symbol, ReadonlyMap<string, MergeReadinessRequirement>>();
const requirementListeners = new Set<() => void>();

/** Say what `reader` has rendered of each readiness read, by query hash; null once it's gone. */
export function publishMergeReadinessRequirements(
  reader: symbol,
  requirements: ReadonlyMap<string, MergeReadinessRequirement> | null,
) {
  if (requirements) requirementsByReader.set(reader, requirements);
  else requirementsByReader.delete(reader);
  for (const listener of requirementListeners) listener();
}

/** What every mounted reader has rendered of the readiness read hashed `readinessQueryHash`. */
export function mergeReadinessRequirements(
  readinessQueryHash: string,
): MergeReadinessRequirement[] {
  return [...requirementsByReader.values()].flatMap((requirements) => {
    const requirement = requirements.get(readinessQueryHash);
    return requirement ? [requirement] : [];
  });
}

/** Called whenever a reader renders another answer or its checks requirement. Returns the unsubscribe. */
export function onMergeReadinessRequirementsChanged(listener: () => void): () => void {
  requirementListeners.add(listener);
  return () => {
    requirementListeners.delete(listener);
  };
}

/** The paths the reader owns in `target`'s pull request, or null while unknown or off. */
export function useOwnedFiles(
  target: OwnedFilesTarget,
  options: MergeReadinessReadOptions,
): readonly string[] | null {
  const useRead = source()?.useOwnedFiles ?? useNoOwnedFiles;
  return useRead(target, options);
}

/**
 * The paths among `changedPaths` the reader owns, or null when that marks nothing useful:
 * they own none of them, or every one.
 */
export function ownedChangedPaths(
  changedPaths: readonly string[],
  ownedPaths: readonly string[] | null | undefined,
): ReadonlySet<string> | null {
  if (!ownedPaths || ownedPaths.length === 0) return null;
  const owned = new Set(ownedPaths);
  const changedOwned = new Set(changedPaths.filter((path) => owned.has(path)));
  if (changedOwned.size === 0 || changedOwned.size === new Set(changedPaths).size) return null;
  return changedOwned;
}
