// People a reviewer picker can offer: the PR's reviewers, the people this user requested reviews
// from on this device, and the repository's assignable users and teams. Fields are folded once
// per list change, so each keystroke only matches and keeps the best few.

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

/** The line shown when the repository's own candidates couldn't be read. */
export function reviewCandidatesFailureNote(repository: string | null): string {
  return repository
    ? `Couldn't load who can review in ${repository}. Type a login to request anyone.`
    : "Couldn't load who can review here. Type a login to request anyone.";
}
