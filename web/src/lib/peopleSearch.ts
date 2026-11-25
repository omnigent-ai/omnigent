// People a reviewer picker can offer: the PR's reviewers, the people this user requested reviews
// from on this device, the people this Mac's account worked with recently, the repository's
// assignable users and teams, and everyone in its organization as this Mac's account sees them.
// Fields are
// folded once per list change, so each keystroke only matches and keeps the best few.

import { DesktopPullRequestError } from "@/lib/desktopPullRequests";
import {
  compareCandidateMatches,
  MatchClass,
  matchCandidate,
  prepareFields,
  tokenizeQuery,
  type CandidateMatch,
  type PreparedField,
} from "@/components/ui/quick-pick/match";

/** A user or team a review can be requested from. */
export interface Person {
  login: string;
  kind: "user" | "team";
  name: string | null;
}

/** Where a person came from, best first: ties in a match go to the earlier source. */
export const PERSON_SOURCES = ["reviewer", "requested", "recent", "assignable", "member"] as const;
export type PersonSource = (typeof PERSON_SOURCES)[number];

export interface PreparedPerson {
  person: Person;
  source: PersonSource;
  fields: PreparedField[];
}

/** How many people a picker shows at once; typing narrows the rest. */
export const PEOPLE_LIMIT = 50;

const personKey = (person: Pick<Person, "login" | "kind">) =>
  `${person.kind}:${person.login.toLowerCase()}`;

/**
 * Everyone from `sources`, each once: the first source to list a person decides their
 * place, and a name from any source fills one the others lack.
 */
export function mergePeople(sources: Partial<Record<PersonSource, readonly Person[]>>): {
  person: Person;
  source: PersonSource;
}[] {
  const merged = new Map<string, { person: Person; source: PersonSource }>();
  for (const source of PERSON_SOURCES) {
    for (const person of sources[source] ?? []) {
      const key = personKey(person);
      const known = merged.get(key);
      if (!known) merged.set(key, { person: { ...person }, source });
      else if (!known.person.name && person.name) known.person.name = person.name;
    }
  }
  return [...merged.values()];
}

/** Fold each person's login and name for matching; do this once per list change. */
export function preparePeople(
  people: readonly { person: Person; source: PersonSource }[],
): PreparedPerson[] {
  return people.map(({ person, source }) => ({
    person,
    source,
    fields: prepareFields([
      { key: "login", text: person.login, weight: 2 },
      // Names match by word or substring; a subsequence across a name is mostly noise.
      { key: "name", text: person.name ?? "", weight: 1, matching: "contiguous" },
    ]),
  }));
}

const sourceRank = (source: PersonSource) => PERSON_SOURCES.indexOf(source);

interface Ranked {
  match: CandidateMatch;
  entry: PreparedPerson;
}

/** Whom a ranking puts first among word-start matches, such as the PR's own people. */
export type LeadingPeople = (entry: PreparedPerson) => boolean;

/**
 * An exact login or name leads; then anyone `leads` names who matches from the start of a
 * word; then someone this user requested here who does; then everyone else, by how well they match.
 */
function tier({ match, entry }: Ranked, leads: LeadingPeople | undefined): number {
  if (match.worstClass === MatchClass.exact) return 0;
  const fromWordStart = match.worstClass <= MatchClass.boundarySubstring;
  if (fromWordStart && leads?.(entry)) return 1;
  if (fromWordStart && entry.source === "requested") return 2;
  return 3;
}

function compareRanked(first: Ranked, second: Ranked, leads?: LeadingPeople): number {
  return (
    tier(first, leads) - tier(second, leads) ||
    compareCandidateMatches(first.match, second.match) ||
    sourceRank(first.entry.source) - sourceRank(second.entry.source) ||
    first.entry.person.login.localeCompare(second.entry.person.login)
  );
}

/**
 * The best `limit` people for `query`, best first. An empty query lists people in source
 * order. Only `limit` results are ever sorted, however many people there are.
 */
export function rankPeople(
  query: string,
  people: readonly PreparedPerson[],
  limit = PEOPLE_LIMIT,
  { leads }: { leads?: LeadingPeople } = {},
): PreparedPerson[] {
  const tokens = tokenizeQuery(query.replace(/^\s*@/, ""));
  if (tokens.length === 0) return people.slice(0, limit);
  const best: Ranked[] = [];
  for (const entry of people) {
    const match = matchCandidate(tokens, entry.fields);
    if (!match) continue;
    const ranked = { match, entry };
    if (best.length === limit && compareRanked(ranked, best[best.length - 1], leads) >= 0) continue;
    let index = best.length;
    while (index > 0 && compareRanked(ranked, best[index - 1], leads) < 0) index -= 1;
    best.splice(index, 0, ranked);
    if (best.length > limit) best.pop();
  }
  return best.map(({ entry }) => entry);
}

/** Why the organization's list can't be shown in full, as the directory reports it. */
export type OrgMembersFailure =
  | "rate_limited"
  | "missing_scope"
  | "sso_required"
  | "no_access"
  | "account_invalid"
  | "query_failed";

/** What the directory says about an organization's list, for the picker's footer. */
export interface OrgMembersStatus {
  organization: string;
  state: "off" | "ready" | "filling" | "refreshing" | "paused" | "unavailable";
  /** A whole list has loaded at least once. */
  complete: boolean;
  truncated: boolean;
  failure: OrgMembersFailure | string | null;
  /** The account the list is read as. */
  login: string | null;
}

/** The directory's cap, as the truncation line states it. */
const MAX_ORG_MEMBERS_SHOWN = "50,000";

/**
 * The one line the picker shows under its list, or null. A first fill explains itself
 * calmly; a list that loaded once keeps working without comment, except when it's
 * truncated; without one, a failure says what to do.
 */
export function orgMembersNote(status: OrgMembersStatus | null): string | null {
  if (!status || status.state === "off") return null;
  const { organization } = status;
  if (status.complete) {
    return status.truncated
      ? `Showing the first ${MAX_ORG_MEMBERS_SHOWN} people in ${organization}. Type a login to request anyone else.`
      : null;
  }
  if (status.state === "paused" || status.failure === "rate_limited") {
    return "GitHub is limiting requests. The people list will finish loading later; you can still type a login.";
  }
  if (status.state === "filling" || !status.failure) {
    return `Loading people in ${organization} for the first time. This happens about once a week.`;
  }
  const account = status.login ? `@${status.login}` : "this account";
  switch (status.failure) {
    case "missing_scope":
      return `Listing people in ${organization} needs GitHub's read:org permission. Run gh auth refresh -s read:org, or type a login.`;
    case "sso_required":
      return `GitHub didn't let ${account} list people in ${organization}. Authorize the GitHub CLI for ${organization}'s single sign-on, or type a login.`;
    case "account_invalid":
      return `The GitHub sign-in for ${account} on this Mac has expired. Run gh auth login, or type a login.`;
    case "no_access":
      return `GitHub didn't let ${account} list people in ${organization}. Type a login to request anyone.`;
    default:
      return `Couldn't load people in ${organization}. Type a login to request anyone.`;
  }
}

/**
 * Whether the user can fix what stopped the list and ask again at once. A rate-limit
 * pause can't be hurried, and a list that loaded once needs no prompt.
 */
export function orgMembersRetryable(status: OrgMembersStatus | null): boolean {
  if (!status || status.complete || status.state === "off") return false;
  if (status.state === "paused" || status.state === "filling") return false;
  return !!status.failure && status.failure !== "rate_limited";
}

/** The line shown when the repository's own candidates couldn't be read. */
export function reviewCandidatesFailureNote(repository: string | null): string {
  return repository
    ? `Couldn't load who can review in ${repository}. Type a login to request anyone.`
    : "Couldn't load who can review here. Type a login to request anyone.";
}

// Account problems the shell explains in its own words; anything else gets the caller's line.
const ACCOUNT_FAILURES = new Set(["signed_out", "account_missing", "account_invalid"]);

/**
 * The footer line for a failed read on this Mac: the shell's own account copy where it has
 * one, else `fallback`. Transport text never reaches the picker.
 */
export function pickerFailureNote(error: unknown, fallback: string): string {
  if (error instanceof DesktopPullRequestError && ACCOUNT_FAILURES.has(error.code)) {
    return `${error.message} You can still type a login.`;
  }
  return fallback;
}

/** The line for a retry of the organization's list that the shell couldn't answer. */
export function orgMembersRetryFailure(error: unknown): string {
  return pickerFailureNote(
    error,
    "Couldn't try again just now. Try again in a moment, or type a login.",
  );
}
