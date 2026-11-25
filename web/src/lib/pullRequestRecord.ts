// One record per pull request for what this Mac reads, shared by every view that shows it: the
// Pull Requests page, the sidebar and each session that links it. A record belongs to one owner,
// a Mac read scope's proven reader in one lifecycle epoch, and only an answer read as that reader
// in that epoch goes into it (`publishPullRequestRead`). Reads through a session's host, and Mac
// reads on a shell that can't prove who read, stay keyed by their session as before.
//
// Every key of a PR read is built here, and every write that patches one names its exact owner.

import {
  hashKey,
  useQueryClient,
  type Query,
  type QueryClient,
  type QueryFilters,
  type QueryKey,
} from "@tanstack/react-query";
import { useSyncExternalStore } from "react";

import { macAnswersCarryProvenance } from "@/lib/desktopReadPath";
import { useQueryCacheSubscription } from "@/lib/queryCacheSubscription";
import {
  ReaderMovedError,
  type MacReadTarget,
  type MacRecordPublication,
} from "@/lib/desktopPullRequests";
import {
  acceptedMacReader,
  macReadEpoch,
  macReadPublishable,
  type MacReadProvider,
  type MacReadScopeKey,
  type MacReadTicket,
} from "@/lib/macReadScope";

/** The read families a PR's record holds. */
export type PullRequestReadFamily =
  | "github-info"
  | "github-changed-files"
  | "github-pr-diff"
  | "github-pr-commits"
  | "github-pr-commit-diff"
  | "github-review-threads"
  | "github-merge-options"
  | "github-reviewers";

export const PULL_REQUEST_READ_FAMILIES: readonly PullRequestReadFamily[] = [
  "github-info",
  "github-changed-files",
  "github-pr-diff",
  "github-pr-commits",
  "github-pr-commit-diff",
  "github-review-threads",
  "github-merge-options",
  "github-reviewers",
];

/**
 * The summary record: what list reads (the inbox, search pages, row stats) say of a PR, field by
 * field, under the same owner as its other records. Kept apart from the read families, so a
 * Refresh of the PR's reads leaves it in place.
 */
export const PULL_REQUEST_SUMMARY_FAMILY = "pr-summary";

/** Every family a PR's shared record holds: its reads and its summary. */
export type PullRequestRecordFamily = PullRequestReadFamily | typeof PULL_REQUEST_SUMMARY_FAMILY;

const PULL_REQUEST_RECORD_FAMILIES: readonly PullRequestRecordFamily[] = [
  ...PULL_REQUEST_READ_FAMILIES,
  PULL_REQUEST_SUMMARY_FAMILY,
];

const RECORD = "pr";
const READER = "reader";

/**
 * Who a PR read belongs to. `mac`: a Mac read scope's reader (null until one is proven, a
 * bootstrap that holds no lasting record) in one epoch. `session`: a read through that session's
 * host, or a Mac read on a shell that proves no reader; `scope` is the read scope as before.
 */
export type PullRequestRecordOwner =
  | { kind: "mac"; scope: MacReadScopeKey; reader: string | null; epoch: number }
  | { kind: "session"; session: string; scope: readonly unknown[] };

const isMacScope = (scope: readonly unknown[]): scope is MacReadScopeKey =>
  (scope[0] === "mac-read" && scope.length === 4) ||
  (scope[0] === "mac-read-as" && scope.length === 5);

/**
 * The owner a view's read of a PR goes to now, from its read scope: a shared record where a Mac
 * read's answers prove who read them, else the session's own. Derived again each render, so a
 * change of reader or epoch moves the view to its new record.
 */
export function pullRequestRecordOwner(
  session: string,
  scope: readonly unknown[],
  macTarget: Pick<MacReadTarget, "provider"> | null,
): PullRequestRecordOwner {
  if (
    macTarget &&
    isMacScope(scope) &&
    !!scope[1] &&
    // Named reads are proven by name; a host-wide read only on a shell that proves its answers.
    (scope[0] === "mac-read-as" || macAnswersCarryProvenance(macTarget.provider as MacReadProvider))
  ) {
    const reader = scope[0] === "mac-read-as" ? scope[4] : acceptedMacReader(scope);
    return { kind: "mac", scope, reader, epoch: macReadEpoch(scope) };
  }
  return { kind: "session", session, scope };
}

/** A read through `session`'s host, or one of its own: kept apart from every other view's. */
export function sessionRecordOwner(
  session: string,
  scope: readonly unknown[] = [],
): PullRequestRecordOwner {
  return { kind: "session", session, scope };
}

/** `family`'s key for `prUrl` as `owner`; `rest` is the family's own parts (revision, head…). */
export function pullRequestRecordKey(
  family: PullRequestRecordFamily,
  prUrl: string,
  owner: PullRequestRecordOwner,
  ...rest: readonly unknown[]
): QueryKey {
  if (owner.kind === "mac") {
    return [family, RECORD, prUrl, ...owner.scope, READER, owner.reader, owner.epoch, ...rest];
  }
  return [family, owner.session, prUrl, ...rest, ...owner.scope];
}

/** The owner-wide prefix of `family`'s keys for `prUrl` as a Mac record owner. */
function macPrefix(family: PullRequestReadFamily, prUrl: string, owner: PullRequestRecordOwner) {
  if (owner.kind !== "mac") return null;
  return [family, RECORD, prUrl, ...owner.scope, READER, owner.reader, owner.epoch];
}

/** Whether `queryKey` is a shared record's: a Mac read keyed by its reader and epoch. */
export function isPullRequestRecordKey(queryKey: QueryKey): boolean {
  return (
    PULL_REQUEST_RECORD_FAMILIES.includes(queryKey[0] as PullRequestRecordFamily) &&
    queryKey[1] === RECORD
  );
}

/**
 * The reader and epoch of a shared record's key under `scope`, or null for a key that isn't a
 * record of that scope.
 */
export function pullRequestRecordReaderIn(
  queryKey: QueryKey,
  scope: MacReadScopeKey,
): { reader: string | null; epoch: number } | null {
  if (!isPullRequestRecordKey(queryKey)) return null;
  const at = 3;
  if (!scope.every((part, index) => queryKey[at + index] === part)) return null;
  const [marker, reader, epoch] = queryKey.slice(at + scope.length, at + scope.length + 3);
  if (marker !== READER || typeof epoch !== "number") return null;
  return { reader: typeof reader === "string" ? reader : null, epoch };
}

/** The Mac read scope a shared record's key is under, or null for any other key. */
export function pullRequestRecordScope(queryKey: QueryKey): MacReadScopeKey | null {
  if (!isPullRequestRecordKey(queryKey)) return null;
  const head = queryKey[3];
  const length = head === "mac-read" ? 4 : head === "mac-read-as" ? 5 : 0;
  if (!length || queryKey[3 + length] !== READER) return null;
  return queryKey.slice(3, 3 + length) as unknown as MacReadScopeKey;
}

/**
 * Whether `queryKey` is an overview a view reading as `readAs` (one of this Mac's logins, or
 * null for each host's own account) shows: a shared record under that reading, or `session`'s
 * own overview of a PR.
 */
export function isOverviewReadAs(
  queryKey: QueryKey,
  session: string,
  readAs: { host: string; login: string } | null,
): boolean {
  if (queryKey[0] !== "github-info") return false;
  if (queryKey[1] === session) return queryKey.length >= 3;
  const scope = pullRequestRecordScope(queryKey);
  if (!scope) return false;
  if (!readAs) return scope[0] === "mac-read";
  return (
    scope[0] === "mac-read-as" &&
    scope[3] === readAs.host.toLowerCase() &&
    scope[4] === readAs.login.toLowerCase()
  );
}

/** The epoch a shared record's key was cut in, or null for any other key. */
export function pullRequestRecordEpoch(queryKey: QueryKey): number | null {
  if (!isPullRequestRecordKey(queryKey)) return null;
  const at = queryKey.indexOf(READER, 3);
  return at >= 0 && typeof queryKey[at + 2] === "number" ? (queryKey[at + 2] as number) : null;
}

/**
 * Where a family's own parts (`rest`: revision, head…) start in a PR read's key: after the
 * session and URL in a session's key, after the reader and epoch in a shared record's.
 */
export function pullRequestReadRestAt(queryKey: QueryKey): number {
  if (!isPullRequestRecordKey(queryKey)) return 3;
  const at = queryKey.indexOf(READER, 3);
  return at >= 0 ? at + 3 : queryKey.length;
}

/**
 * Whether `queryKey` is a read, in a PR read family, of `prUrl` that `session` shows: its own
 * session's key, or a shared record (any reader's, which every view of the PR shows).
 */
export function isSessionPullRequestRead(
  queryKey: QueryKey,
  session: string,
  prUrl: string,
): boolean {
  if (!PULL_REQUEST_READ_FAMILIES.includes(queryKey[0] as PullRequestReadFamily)) return false;
  if (queryKey[2] !== prUrl) return false;
  return queryKey[1] === session || queryKey[1] === RECORD;
}

/** The PR URL any of this module's keys is for, or null for another key. */
export function pullRequestRecordUrl(queryKey: QueryKey): string | null {
  if (!PULL_REQUEST_READ_FAMILIES.includes(queryKey[0] as PullRequestReadFamily)) return null;
  return typeof queryKey[2] === "string" ? queryKey[2] : null;
}

/**
 * The entries of `family` for `prUrl` that belong to exactly `owner`, every revision of them:
 * the only filter a write may patch, cancel before patching, or set data through.
 */
export function pullRequestRecordFilter(
  family: PullRequestReadFamily,
  prUrl: string,
  owner: PullRequestRecordOwner,
): QueryFilters {
  const prefix = macPrefix(family, prUrl, owner);
  if (prefix) return { queryKey: prefix };
  if (owner.kind !== "session") throw new Error("unreachable");
  const { session, scope } = owner;
  return {
    queryKey: [family, session, prUrl],
    // The session's scope ends its key; another scope's entry, a named login's say, isn't this one.
    predicate: (query) =>
      endsWith(query.queryKey, scope) && !isPullRequestRecordKey(query.queryKey),
  };
}

const endsWith = (key: QueryKey, tail: readonly unknown[]) =>
  key.length >= tail.length &&
  tail.every((part, index) => key[key.length - tail.length + index] === part);

/**
 * Every view's entries of `prUrl`, any owner, in `families` (all by default): for reading the PR
 * again, never for patching it.
 */
export function invalidatePullRequestEverywhere(
  client: QueryClient,
  prUrl: string,
  families: readonly PullRequestReadFamily[] = PULL_REQUEST_READ_FAMILIES,
): Promise<void> {
  return client.invalidateQueries({
    predicate: (query) => readsPullRequest(query, prUrl, families),
  });
}

/** What a session's list says of its PRs, as both of its lists cache it. */
interface ListedPullRequests {
  prs?: readonly { url?: string | null }[];
  selected_pr_url?: string | null;
}

/** The PRs the cache knows `session` shows: its overview's list and its stored links. */
function sessionPullRequestUrls(client: QueryClient, session: string): Set<string> {
  const urls = new Set<string>();
  for (const listed of [
    client.getQueryData<ListedPullRequests>(["github-info", session]),
    client.getQueryData<ListedPullRequests>(["session-pull-requests", session]),
  ]) {
    for (const pr of listed?.prs ?? []) if (pr.url) urls.add(pr.url);
    if (listed?.selected_pr_url) urls.add(listed.selected_pr_url);
  }
  return urls;
}

/**
 * Which queries of `families` `session` shows: its own keys, and the shared records of the PRs
 * it links or `shown` names (a PR the view shows that no list of it holds, as the Pull Requests
 * page's open one). For reading again only, never for patching.
 */
export function sessionPullRequestReads(
  client: QueryClient,
  session: string,
  families: readonly PullRequestReadFamily[] = PULL_REQUEST_READ_FAMILIES,
  shown: readonly string[] = [],
): (query: Pick<Query, "queryKey">) => boolean {
  const urls = sessionPullRequestUrls(client, session);
  for (const url of shown) urls.add(url);
  return ({ queryKey }) =>
    families.includes(queryKey[0] as PullRequestReadFamily) &&
    (queryKey[1] === session || (queryKey[1] === RECORD && urls.has(queryKey[2] as string)));
}

/** Read again everything of `families` `session` shows; see {@link sessionPullRequestReads}. */
export function invalidateSessionPullRequestReads(
  client: QueryClient,
  session: string,
  families: readonly PullRequestReadFamily[] = PULL_REQUEST_READ_FAMILIES,
  shown: readonly string[] = [],
): Promise<void> {
  return client.invalidateQueries({
    predicate: sessionPullRequestReads(client, session, families, shown),
  });
}

/**
 * Read `prUrl` again as `session` shows it, in `families`: its own keys and the PR's shared
 * records. For reading again only, never for patching.
 */
export function invalidateSessionPullRequest(
  client: QueryClient,
  session: string,
  prUrl: string,
  families: readonly PullRequestReadFamily[] = PULL_REQUEST_READ_FAMILIES,
): Promise<void> {
  return client.invalidateQueries({
    predicate: ({ queryKey }) =>
      families.includes(queryKey[0] as PullRequestReadFamily) &&
      isSessionPullRequestRead(queryKey, session, prUrl),
  });
}

/** Whether `query` is a read of `prUrl` in one of `families`, under any owner. */
export function readsPullRequest(
  query: Pick<Query, "queryKey">,
  prUrl: string,
  families: readonly PullRequestReadFamily[] = PULL_REQUEST_READ_FAMILIES,
): boolean {
  const [family] = query.queryKey;
  return (
    families.includes(family as PullRequestReadFamily) &&
    pullRequestRecordUrl(query.queryKey) === prUrl
  );
}

/** Where each record's answer came from in its scope's order, so an older one never replaces it. */
const sequences = new WeakMap<Query, number>();

/**
 * The Mac target a record's read goes out with: it names the record's owner, so the read's answer
 * is published only into a record of the reader that answered, in the epoch it was sent in.
 * Null for a session-owned read, which publishes as before.
 */
export function publicationFor(owner: PullRequestRecordOwner): MacRecordPublication | null {
  return owner.kind === "mac"
    ? { owner: { scope: owner.scope, reader: owner.reader, epoch: owner.epoch } }
    : null;
}

/**
 * The accepted answer behind `publication`, while it may still go into `owner`'s record, with
 * the owner its answer proved; else `ReaderMovedError`. A record's read always publishes
 * through the Mac read that checks it, so nothing else may fill it.
 */
function checkedPublication(
  owner: Extract<PullRequestRecordOwner, { kind: "mac" }>,
  publication: MacRecordPublication | null,
) {
  const published = publication?.published;
  if (!published) throw new ReaderMovedError();
  const { viewer, ticket } = published;
  const proven = { ...owner, reader: owner.reader ?? viewer };
  if (!macReadPublishable(ticket, viewer, proven)) throw new ReaderMovedError();
  return { ticket, proven };
}

/**
 * After a record's read, as its query settles: the answer may still go into the record only if
 * its scope hasn't moved since it was accepted (else `ReaderMovedError`, and nothing is kept). It
 * notes the answer's place in the order, and a bootstrap read (before its scope had a reader)
 * also goes into the record of the reader its own answer proved, unless that record holds a newer
 * answer already. The bootstrap entry itself never lasts (see `recordGcTime`).
 */
export function publishPullRequestRead<Data>(
  client: QueryClient,
  queryKey: QueryKey,
  owner: PullRequestRecordOwner,
  publication: MacRecordPublication | null,
  data: Data,
  readAt: number,
): void {
  if (owner.kind !== "mac") return;
  const { ticket, proven } = checkedPublication(owner, publication);
  const own = client.getQueryCache().find({ queryKey, exact: true });
  if (own) sequences.set(own, ticket.sequence);
  if (owner.reader !== null) return;
  const family = queryKey[0] as PullRequestReadFamily;
  const prUrl = queryKey[2] as string;
  const rest = queryKey.slice(macPrefix(family, prUrl, owner)!.length);
  const target = pullRequestRecordKey(family, prUrl, proven, ...rest);
  const held = client.getQueryCache().find({ queryKey: target, exact: true });
  if (held && held.state.data !== undefined && (sequences.get(held) ?? -1) > ticket.sequence)
    return;
  client.setQueryData(target, data, { updatedAt: readAt });
  const promoted = client.getQueryCache().find({ queryKey: target, exact: true });
  if (promoted) sequences.set(promoted, ticket.sequence);
}

/** What a list read knows of a PR, as its summary keeps it. */
export interface PullRequestSummaryFields {
  title: string;
  /** GitHub's state: `OPEN`, `CLOSED` or `MERGED`. */
  state: string;
  isDraft: boolean;
  /** Null for a deleted account. */
  author: string | null;
  /** `owner/name`. */
  repository: string;
  number: number;
  /** When GitHub last saw the PR change, in milliseconds. */
  updatedAt: number;
  /**
   * The base and head a list read saw, as one value, and when that read was sent (client
   * milliseconds), so a PR opened from its row can read its patch before its overview answers.
   */
  listedCommits: { base: string; head: string; readAt: number };
}

/** One field as its summary keeps it: the value, and the place in its scope's order of the read that sent it. */
export interface PullRequestSummaryField<Value> {
  value: Value;
  sequence: number;
}

/** A PR's summary: each field the newest read that knew it said, by when the read was sent. */
export type PullRequestSummary = {
  [Field in keyof PullRequestSummaryFields]?: PullRequestSummaryField<
    PullRequestSummaryFields[Field]
  >;
};

/** One PR of a list answer, with the fields that answer knows of it. */
export interface PullRequestSummaryRow {
  url: string;
  fields: Partial<PullRequestSummaryFields>;
}

/** How long a summary nothing shows stays: as long as the PR's own records. */
export const PULL_REQUEST_SUMMARY_GC_TIME = 30 * 60_000;

/** Give `client`'s summaries their lifetime; nothing observes them, so it's never set by a view. */
export function bindPullRequestSummaryDefaults(client: QueryClient): void {
  client.setQueryDefaults([PULL_REQUEST_SUMMARY_FAMILY], { gcTime: PULL_REQUEST_SUMMARY_GC_TIME });
}

/** The key of `prUrl`'s summary as `owner`; null unless it's a proven reader's shared record. */
export function pullRequestSummaryKey(
  prUrl: string,
  owner: PullRequestRecordOwner | null,
): QueryKey | null {
  if (owner?.kind !== "mac" || owner.reader === null) return null;
  return pullRequestRecordKey(PULL_REQUEST_SUMMARY_FAMILY, prUrl, owner);
}

/**
 * Put what one accepted Mac read answered of `rows` into their summaries, as the reader that
 * answered, in the record its scope shows now. Only for an answer `macReadPublishable` keeps as
 * that record's own; a field changes only for a read sent after the one that last set it. Never
 * for rows on screen or a saved list: each list read calls it with its own answer.
 */
export function publishPullRequestSummaries(
  client: QueryClient,
  answer: { viewer: string | null; ticket: MacReadTicket },
  rows: readonly PullRequestSummaryRow[],
  readAt: number = Date.now(),
): void {
  const { viewer, ticket } = answer;
  if (!viewer || rows.length === 0) return;
  const scope = JSON.parse(ticket.scope) as MacReadScopeKey;
  const owner = pullRequestRecordOwner("", scope, { provider: scope[2] });
  if (owner.kind !== "mac" || owner.reader === null) return;
  if (!macReadPublishable(ticket, viewer, owner)) return;
  for (const { url, fields } of rows) {
    const key = pullRequestSummaryKey(url, owner)!;
    const held = client.getQueryData<PullRequestSummary>(key);
    const merged = mergedSummary(held, fields, ticket.sequence);
    if (merged !== held) client.setQueryData(key, merged, { updatedAt: readAt });
  }
}

/** `held` with each of `fields` a read sent at `sequence` knows, where no later read set it. */
function mergedSummary(
  held: PullRequestSummary | undefined,
  fields: Partial<PullRequestSummaryFields>,
  sequence: number,
): PullRequestSummary | undefined {
  let merged = held;
  for (const field of Object.keys(fields) as (keyof PullRequestSummaryFields)[]) {
    const value = fields[field];
    if (value === undefined) continue;
    const stored = held?.[field];
    if (stored && stored.sequence >= sequence) continue;
    merged = { ...merged, [field]: { value, sequence } };
  }
  return merged;
}

/**
 * The data cached under exactly `queryKey`, read from the cache on each render, without adding an
 * entry or an observer: a removed or reset record reads as nothing at once.
 */
export function useExactRecord<Data>(queryKey: QueryKey | null): Data | undefined {
  const client = useQueryClient();
  const hash = queryKey ? hashKey(queryKey) : null;
  return useSyncExternalStore(useQueryCacheSubscription(client, hash), () =>
    hash ? (client.getQueryCache().get(hash)?.state.data as Data | undefined) : undefined,
  );
}

/** A bootstrap entry goes as soon as nothing shows it; a record keeps the family's own time. */
export function recordGcTime(owner: PullRequestRecordOwner | null, gcTime: number): number {
  return owner?.kind === "mac" && owner.reader === null ? 0 : gcTime;
}

/** A view's read of one PR family: the owner it goes to now and the key it's cached under. */
export interface PullRequestRecordRead {
  owner: PullRequestRecordOwner;
  queryKey: QueryKey;
}

/**
 * `family`'s read of `prUrl` from `session` under `scope`: a shared record where a Mac read
 * proves its reader, else the session's own key, with the family's `rest` parts.
 */
export function pullRequestRecordRead(
  family: PullRequestReadFamily,
  session: string,
  prUrl: string,
  scope: readonly unknown[],
  macTarget: Pick<MacReadTarget, "provider"> | null,
  ...rest: readonly unknown[]
): PullRequestRecordRead {
  const owner = pullRequestRecordOwner(session, scope, macTarget);
  return { owner, queryKey: pullRequestRecordKey(family, prUrl, owner, ...rest) };
}

/** Bootstrap reads still out, by their key: a view can move off one before it answers. */
const bootstrapReads = new Map<string, Promise<unknown>>();

/**
 * A reader's record opened while its bootstrap read (sent before the scope had a reader) is still
 * out: that read's answer is what proved the reader, and it goes into this record, so the record
 * takes it rather than reading again. Resolves to the promoted answer, or undefined to read.
 */
export async function joinBootstrapRead<Data>(
  client: QueryClient,
  record: PullRequestRecordRead | null,
): Promise<Data | undefined> {
  const owner = record?.owner;
  if (owner?.kind !== "mac" || owner.reader === null) return undefined;
  const family = record!.queryKey[0] as PullRequestReadFamily;
  const prUrl = record!.queryKey[2] as string;
  const rest = record!.queryKey.slice(macPrefix(family, prUrl, owner)!.length);
  const bootstrapKey = pullRequestRecordKey(family, prUrl, { ...owner, reader: null }, ...rest);
  const pending = bootstrapReads.get(hashKey(bootstrapKey));
  if (!pending) return undefined;
  // Its failure is its own; this record then reads for itself.
  await pending.catch(() => undefined);
  const own = client.getQueryCache().find({ queryKey: record!.queryKey, exact: true });
  return own && sequences.has(own) ? (own.state.data as Data | undefined) : undefined;
}

/**
 * Run a record's read with `macTarget` naming the record it publishes into, then publish its
 * answer (see {@link publishPullRequestRead}). Without a record (no PR yet) it just reads.
 * `readAt` is when the read was sent.
 */
export async function readRecord<Data, Target extends object>(
  client: QueryClient,
  record: PullRequestRecordRead | null,
  macTarget: Target | null,
  read: (macTarget: (Target & Pick<MacReadTarget, "publication">) | null) => Promise<Data>,
  readAt: number = Date.now(),
): Promise<Data> {
  const joined = await joinBootstrapRead<Data>(client, record);
  if (joined !== undefined) return joined;
  const publication = record ? publicationFor(record.owner) : null;
  const reading = read(macTarget && publication ? { ...macTarget, publication } : macTarget).then(
    (data) => {
      if (record) {
        publishPullRequestRead(client, record.queryKey, record.owner, publication, data, readAt);
      }
      return data;
    },
  );
  const bootstrap = record?.owner.kind === "mac" && record.owner.reader === null;
  if (!bootstrap) return reading;
  const id = hashKey(record.queryKey);
  bootstrapReads.set(id, reading);
  try {
    return await reading;
  } finally {
    if (bootstrapReads.get(id) === reading) bootstrapReads.delete(id);
  }
}

/**
 * Read more of a record, a next page say, that its caller merges into the record itself: only
 * an answer that may still go into the record comes back, else `ReaderMovedError`.
 */
export async function readMoreOfRecord<Data, Target extends object>(
  record: PullRequestRecordRead | null,
  macTarget: Target | null,
  read: (macTarget: (Target & Pick<MacReadTarget, "publication">) | null) => Promise<Data>,
): Promise<Data> {
  const publication = record ? publicationFor(record.owner) : null;
  const data = await read(macTarget && publication ? { ...macTarget, publication } : macTarget);
  if (record?.owner.kind === "mac") checkedPublication(record.owner, publication);
  return data;
}

/** A query's retry that never retries an answer its record refused: the view moved on. */
export function recordRetry<Error>(
  retry: boolean | number | ((failureCount: number, error: Error) => boolean) = 3,
): (failureCount: number, error: Error) => boolean {
  return (failureCount, error) => {
    if (error instanceof ReaderMovedError) return false;
    if (typeof retry === "function") return retry(failureCount, error);
    if (typeof retry === "number") return failureCount < retry;
    return retry;
  };
}
