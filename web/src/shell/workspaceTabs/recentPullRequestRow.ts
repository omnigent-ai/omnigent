// How the new-tab page lists a recent pull request: its title first, as a browser's
// address bar lists a page, then who wrote it and where, with the number kept at the
// trailing edge so a narrow sidebar still shows which PR it is. Where is named as PR
// lists name it, so two PRs that would read alike show their provider or host.

import type { PullRequest, PullRequestAssociation } from "@/hooks/usePullRequests";
import { pullRequestIdentityParts, type NamedPullRequest } from "@/lib/pullRequestIdentity";
import {
  hasNewerPullRequestLifecycle,
  type PullRequestStatusSummary,
} from "@/lib/pullRequestStatus";

import { pullRequestNumber, pullRequestUrlParts } from "./placeTitle";
import type { WorkspacePlace } from "./workspacePlace";

/** What's known about the session's pull requests, without fetching any one of them. */
export interface RecentPullRequestFacts {
  /** The session's linked pull requests, stored or live. */
  linked: readonly PullRequestAssociation[];
  /** Each pull request's details already read, by URL. */
  details: ReadonlyMap<string, PullRequest>;
  /** When each of `details` was read, in epoch milliseconds, where the cache says. */
  detailsUpdatedAt?: ReadonlyMap<string, number>;
}

/**
 * What's known of a PR's lifecycle: its details, unless the session's link saw a newer change,
 * as when it merged after the details were read.
 */
export function knownPullRequestStatus(
  url: string,
  facts: RecentPullRequestFacts,
): PullRequestStatusSummary | undefined {
  const details = facts.details.get(url);
  const linked = facts.linked.find((pr) => pr.url === url);
  if (!details) return linked;
  if (!linked) return details;
  const readAt = facts.detailsUpdatedAt?.get(url) ?? 0;
  return hasNewerPullRequestLifecycle(linked, details, readAt) ? linked : details;
}

export interface RecentPullRequestRow {
  label: string;
  /** `author · [provider · host ·] repository`, leaving out what isn't known. */
  description?: string;
  /** The number, such as `#338`. */
  detail?: string;
  /** Terms that match the row when its text is cut off. */
  keywords: string[];
}

/** The pull request a recent place shows, when it's the PR itself rather than one of its files. */
export function recentPullRequestUrl(place: WorkspacePlace): string | undefined {
  return place.kind === "pull" && place.path === undefined ? place.url : undefined;
}

/** A PR the session no longer links, named from its URL and any details already read. */
function namedFromUrl(url: string, details: PullRequest | undefined): NamedPullRequest | null {
  const number = pullRequestNumber(url, details?.number);
  const parts = pullRequestUrlParts(url);
  if (number === null || !parts) return null;
  return { url, host: parts.host, repository: parts.repository, number };
}

/** Each recent PR's row, by URL, named among the session's linked PRs and the other recent ones. */
export function recentPullRequestRows(
  urls: readonly string[],
  facts: RecentPullRequestFacts,
): Map<string, RecentPullRequestRow> {
  const linkedByUrl = new Map(facts.linked.map((pr) => [pr.url, pr]));
  const unlinked = urls.flatMap((url) =>
    linkedByUrl.has(url) ? [] : (namedFromUrl(url, facts.details.get(url)) ?? []),
  );
  const identities = pullRequestIdentityParts([...facts.linked, ...unlinked]);
  return new Map(
    urls.map((url) => {
      const details = facts.details.get(url);
      const linked = linkedByUrl.get(url);
      const identity = identities.get(url);
      const title = details?.title?.trim() || linked?.title?.trim() || undefined;
      const author = details?.author?.trim() || undefined;
      const where = identity ? [...identity.qualifiers, identity.repository].filter(Boolean) : [];
      const description = [author, ...where].filter(Boolean).join(" · ");
      const number = identity?.number;
      const row: RecentPullRequestRow = {
        label: title ?? (number ? `Pull request ${number}` : "Pull request"),
        ...(description ? { description } : {}),
        ...(title && number ? { detail: number } : {}),
        keywords: [
          "pull request",
          ...(author ? [author] : []),
          ...where,
          ...(number ? [number, number.replace(/^\D+/, "")] : []),
        ],
      };
      return [url, row];
    }),
  );
}
