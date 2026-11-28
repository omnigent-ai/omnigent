// The people and teams past reviewer searches found through the session's host, kept on this
// device per repository, so the reviewer picker and @ completion open with them listed before
// a new search answers. Only logins, names and team slugs are kept, within small caps; nothing
// leaves this device. Entries belong to the workspace user and server they were found for, and
// go when that user signs out or another user signs in (`startReviewerSearchCache`).

import { z } from "zod";

import { durableServerScope } from "@/lib/durableServerScope";
import { getCurrentUserId, getIdentityStatus, subscribeIdentity } from "@/lib/identity";

const STORAGE_KEY = "omnigent:reviewer-search";
/** Moves when the saved shape changes, so older entries are dropped. */
const SCHEMA = 1;
export const MAX_SAVED_PEOPLE = 200;
export const MAX_SAVED_TEAMS = 50;
export const MAX_SAVED_AGE_MS = 30 * 24 * 60 * 60_000;
/** Every repository's entry together, at most; the least recently found go first. */
export const MAX_SAVED_CHARS = 256 * 1024;

const nameSchema = z.string().max(256).nullable();
const personSchema = z.object({ login: z.string().min(1).max(100), name: nameSchema });
const teamSchema = z.object({ slug: z.string().min(1).max(100), name: nameSchema });
const entrySchema = z.object({
  owner: z.string().min(1),
  server: z.string().min(1),
  at: z.number(),
  people: z.array(personSchema).max(MAX_SAVED_PEOPLE),
  teams: z.array(teamSchema).max(MAX_SAVED_TEAMS),
});
const savedSchema = z.object({
  schema: z.literal(SCHEMA),
  repositories: z.record(z.string(), entrySchema),
});

export type SavedReviewer = z.infer<typeof personSchema>;
export type SavedTeam = z.infer<typeof teamSchema>;
type Saved = z.infer<typeof savedSchema>;

export interface ReviewerSearchFound {
  people: readonly SavedReviewer[];
  teams: readonly SavedTeam[];
}

const NOTHING: ReviewerSearchFound = { people: [], teams: [] };

/** `host/owner/name` as a key: GitHub's names ignore case. */
const repositoryKey = (host: string, repository: string) => `${host}/${repository}`.toLowerCase();

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

function load(): Saved {
  const empty: Saved = { schema: SCHEMA, repositories: {} };
  try {
    const raw = storage()?.getItem(STORAGE_KEY);
    if (!raw) return empty;
    const parsed = savedSchema.safeParse(JSON.parse(raw));
    return parsed.success ? parsed.data : empty;
  } catch {
    return empty;
  }
}

function save(saved: Saved): void {
  const target = storage();
  if (!target) return;
  try {
    if (Object.keys(saved.repositories).length === 0) target.removeItem(STORAGE_KEY);
    else target.setItem(STORAGE_KEY, JSON.stringify(saved));
  } catch {
    // Full or unavailable storage: the picker still searches, without a head start.
  }
}

/** Who and where the entries are for now; null while signed out or off any known server. */
function currentScope(): { owner: string; server: string } | null {
  const owner = getCurrentUserId();
  const server = durableServerScope();
  return owner && server ? { owner, server } : null;
}

/** What past searches found in `repository` on `host`, for the user and server now. */
export function savedReviewerSearch(host: string, repository: string, now = Date.now()) {
  const scope = currentScope();
  if (!scope) return NOTHING;
  const entry = load().repositories[repositoryKey(host, repository)];
  if (!entry || entry.owner !== scope.owner || entry.server !== scope.server) return NOTHING;
  if (now - entry.at > MAX_SAVED_AGE_MS) return NOTHING;
  return { people: entry.people, teams: entry.teams };
}

function merged<T>(found: readonly T[], kept: readonly T[], key: (item: T) => string, cap: number) {
  const seen = new Set<string>();
  const out: T[] = [];
  // The newest search's come first, so the oldest are the ones a full list drops.
  for (const item of [...found, ...kept]) {
    const id = key(item).toLowerCase();
    if (seen.has(id)) continue;
    seen.add(id);
    out.push(item);
    if (out.length >= cap) break;
  }
  return out;
}

/** Keep what a search found in `repository`, ahead of what earlier ones did, within the caps. */
export function rememberReviewerSearch(
  host: string,
  repository: string,
  found: ReviewerSearchFound,
  now = Date.now(),
): void {
  const scope = currentScope();
  if (!scope) return;
  const saved = load();
  const key = repositoryKey(host, repository);
  const kept = saved.repositories[key];
  const same = kept && kept.owner === scope.owner && kept.server === scope.server;
  const people = merged(
    found.people,
    same ? kept.people : [],
    (person) => person.login,
    MAX_SAVED_PEOPLE,
  );
  const teams = merged(found.teams, same ? kept.teams : [], (team) => team.slug, MAX_SAVED_TEAMS);
  const repositories: Saved["repositories"] = {};
  // Another user's, or an expired one, isn't kept alongside.
  for (const [other, entry] of Object.entries(saved.repositories)) {
    if (other !== key && now - entry.at <= MAX_SAVED_AGE_MS && entry.owner === scope.owner) {
      repositories[other] = entry;
    }
  }
  const justFound = {
    owner: scope.owner,
    server: scope.server,
    at: now,
    people: people.map(({ login, name }) => ({ login, name })),
    teams: teams.map(({ slug, name }) => ({ slug, name })),
  };
  // Over the total: drop the least recently found repositories, the one just found last.
  const newestFirst = [
    [key, justFound] as const,
    ...Object.entries(repositories).sort(([, left], [, right]) => right.at - left.at),
  ];
  const sized = (entries: typeof newestFirst): Saved => ({
    schema: SCHEMA,
    repositories: Object.fromEntries(entries),
  });
  let next = sized(newestFirst);
  while (JSON.stringify(next).length > MAX_SAVED_CHARS && newestFirst.length > 0) {
    newestFirst.pop();
    next = sized(newestFirst);
  }
  save(next);
}

/** Forget every saved search, as signing out does. */
export function clearReviewerSearches(): void {
  save({ schema: SCHEMA, repositories: {} });
}

/**
 * Keep only the signed-in user's entries: everything goes once the server says nobody is
 * signed in, and another user's entries go as soon as a user is known, across reloads too.
 */
export function reconcileReviewerSearches(): void {
  const status = getIdentityStatus();
  if (status.kind === "signedOut") {
    clearReviewerSearches();
    return;
  }
  if (status.kind !== "signedIn") return;
  const user = status.userId;
  const saved = load();
  const kept = Object.fromEntries(
    Object.entries(saved.repositories).filter(([, entry]) => user !== null && entry.owner === user),
  );
  if (Object.keys(kept).length !== Object.keys(saved.repositories).length) {
    save({ schema: SCHEMA, repositories: kept });
  }
}

let started = false;

/** Reconcile now and on every change of identity, from app start; once per page. */
export function startReviewerSearchCache(): void {
  if (started) return;
  started = true;
  reconcileReviewerSearches();
  subscribeIdentity(reconcileReviewerSearches);
}

export function resetReviewerSearchCacheForTests(): void {
  started = false;
  storage()?.removeItem(STORAGE_KEY);
}
