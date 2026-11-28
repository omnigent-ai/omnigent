// The people and teams this workspace user requested reviews from recently in an organization,
// kept on this device so reviewer pickers rank them first. Logins only; names come from the
// pickers' other sources.

import { z } from "zod";

import { parseCodeHostPullRequestUrl } from "@/lib/codeHosts";
import { createDeviceRecentList } from "@/lib/deviceRecentList";
import type { Person } from "@/lib/peopleSearch";

/** How many people an organization remembers, most recent first. */
export const MAX_REQUESTED_REVIEWERS = 20;

type RequestedReviewer = Pick<Person, "login" | "kind">;

const list = createDeviceRecentList<RequestedReviewer>({
  storagePrefix: "omnigent:requested-reviewers",
  field: "reviewers",
  maxItems: MAX_REQUESTED_REVIEWERS,
  maxScopes: 50,
  item: z.object({ login: z.string().min(1), kind: z.enum(["user", "team"]) }),
  same: (left, right) =>
    left.kind === right.kind && left.login.toLowerCase() === right.login.toLowerCase(),
});

/** A pull request's organization on its host, or null for a link that names none. */
function scopeOf(prUrl: string): string[] | null {
  const target = parseCodeHostPullRequestUrl(prUrl);
  const organization = target?.repository.split("/")[0];
  return target && organization ? [target.host, organization] : null;
}

/** Who requested reviewers are kept for now; take it when a change is made. */
export const requestedReviewersOwner = list.owner;

/** Remember reviews just requested on `prUrl` for `owner`, the latest first. */
export function recordRequestedReviewers(
  prUrl: string,
  requested: readonly RequestedReviewer[],
  owner: string | null,
): void {
  const scope = scopeOf(prUrl);
  if (!scope) return;
  list.record(
    scope,
    requested.map(({ login, kind }) => ({ login, kind })),
    owner,
  );
}

const NO_PEOPLE: Person[] = [];

/** Who this user requested recently in `prUrl`'s organization, most recent first. */
export function useRequestedReviewers(prUrl: string | null): Person[] {
  const reviewers = list.useItems(prUrl ? scopeOf(prUrl) : null);
  return peopleOf(reviewers);
}

// One array per stored list, so a picker folds its people again only when it changed.
const peopleFor = new WeakMap<readonly RequestedReviewer[], Person[]>();
function peopleOf(reviewers: readonly RequestedReviewer[]): Person[] {
  if (reviewers.length === 0) return NO_PEOPLE;
  let people = peopleFor.get(reviewers);
  if (!people) {
    people = reviewers.map(({ login, kind }) => ({ login, kind, name: null }));
    peopleFor.set(reviewers, people);
  }
  return people;
}

/** Forget subscriptions, so a test starts clean. */
export const resetRequestedReviewersForTests = list.resetForTests;
