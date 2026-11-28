// Where a PR read waits among the shell's GitHub reads. A list row, a read ahead and the PR's
// open view can want the same query, so its priority comes from who wants it when the read
// starts: its observers, and the reads ahead that claim it without one. The highest wins.

import { hashKey, isCancelledError, type Query, type QueryClient } from "@tanstack/react-query";

import type { CodeHostReadPriority } from "@/lib/nativeBridge";

const READ_PRIORITY = "readPriority";

const RANK: Record<CodeHostReadPriority, number> = { background: 0, prefetch: 1, interactive: 2 };

/**
 * One read in flight: the priority it went out with, its call to the shell settling, and
 * whether a promotion read it again, after which only its settling still counts.
 */
interface ReadInFlight {
  priority: CodeHostReadPriority;
  done: Promise<void>;
  superseded: boolean;
}

// The reads in flight by their query's hash: a promoted read may still run beside its newer one.
const readsInFlight = new Map<string, ReadInFlight[]>();
// The reads ahead that want a query, by its hash, each with the reads it started.
const claims = new Map<string, Set<SpeculativeClaim>>();
// The clients whose cache promotes a read as someone who wants it more starts watching it.
const watched = new WeakSet<QueryClient>();
// The promotions under way by their query's hash: the lower read's cancel, until it reads again.
const promotions = new Map<string, Promise<unknown>>();

/** An observer's `meta`, naming the priority it wants its reads at. */
export function readPriorityMeta(priority: CodeHostReadPriority): Record<string, string> {
  return { [READ_PRIORITY]: priority };
}

/** A list row's own read is a background one; anything else, one the user asked for. */
export function infoReadMeta(listRow: boolean): Record<string, string> {
  return readPriorityMeta(listRow ? "background" : "interactive");
}

function metaPriority(meta: Record<string, unknown> | undefined): CodeHostReadPriority {
  const named = meta?.[READ_PRIORITY];
  return named === "background" || named === "prefetch" ? named : "interactive";
}

function higher(left: CodeHostReadPriority, right: CodeHostReadPriority): CodeHostReadPriority {
  return RANK[left] >= RANK[right] ? left : right;
}

/** Whether `observer` may read `query` now; a disabled one wants nothing from it yet. */
function observerEnabled(observer: Query["observers"][number], query: Query): boolean {
  const enabled = observer.options.enabled;
  return typeof enabled === "function" ? enabled(query) !== false : enabled !== false;
}

/**
 * The priority `queryKey`'s read takes now: the highest its enabled observers and claims want.
 * With nobody watching or claiming it, e.g. a plain fetch, it's a read the user asked for.
 */
export function observedReadPriority(
  client: QueryClient,
  queryKey: readonly unknown[],
): CodeHostReadPriority {
  const query = client.getQueryCache().find({ queryKey, exact: true });
  const wanting = (query?.observers ?? []).filter((observer) => observerEnabled(observer, query!));
  const claimed = (claims.get(hashKey(queryKey))?.size ?? 0) > 0;
  if (wanting.length === 0) return claimed ? "prefetch" : "interactive";
  let priority: CodeHostReadPriority = claimed ? "prefetch" : "background";
  for (const observer of wanting) {
    priority = higher(priority, metaPriority(observer.options.meta));
  }
  return priority;
}

/** A read ahead's hold on one query: its reads go out as prefetch, while nobody wants it more. */
export interface SpeculativeClaim {
  /** Let go of the query; a read already under way keeps running. */
  release: () => void;
  /** Settles once every call to the shell this claim's reads made has answered. */
  settled: () => Promise<void>;
  /** Whether a read this claim started is still waiting on the shell. */
  pending: () => boolean;
}

/** Claim `queryKey` for a read ahead, until the returned claim lets go of it. */
export function claimSpeculativeRead(
  client: QueryClient,
  queryKey: readonly unknown[],
): SpeculativeClaim {
  watchPromotions(client);
  const hash = hashKey(queryKey);
  const started: Promise<void>[] = [];
  let open = 0;
  const claim: SpeculativeClaim & { track: (done: Promise<void>) => void } = {
    release: () => {
      const held = claims.get(hash);
      held?.delete(claim);
      if (held?.size === 0) claims.delete(hash);
    },
    settled: () => Promise.all(started).then(() => {}),
    pending: () => open > 0,
    track: (done) => {
      open += 1;
      started.push(done.finally(() => (open -= 1)));
    },
  };
  const held = claims.get(hash) ?? new Set();
  held.add(claim);
  claims.set(hash, held);
  // A read already out lower, such as a list row's own, goes again as this claim's.
  if (readsInFlight.has(hash)) queueMicrotask(() => promoteRead(client, queryKey));
  return claim;
}

/** Whether anyone but `claim` wants `queryKey`: an enabled observer, or another read ahead. */
function wantedBesides(client: QueryClient, queryKey: readonly unknown[], claim: SpeculativeClaim) {
  const query = client.getQueryCache().find({ queryKey, exact: true });
  if (query?.observers.some((observer) => observerEnabled(observer, query))) return true;
  return [...(claims.get(hashKey(queryKey)) ?? [])].some((other) => other !== claim);
}

/**
 * Run `fetch` of `queryKey` as a read ahead: it goes out as prefetch while nobody wants it more,
 * and settles only once the shell has answered every call it made, even after its query was
 * cancelled, so whoever paces it never counts a call still out as done. `until` aborting lets
 * go of it: its query is cancelled, so the shell drops a call still waiting, unless someone
 * else wants it too, whose read goes on.
 */
export async function readAhead<T>(
  client: QueryClient,
  queryKey: readonly unknown[],
  fetch: () => Promise<T>,
  until?: AbortSignal,
): Promise<T> {
  const claim = claimSpeculativeRead(client, queryKey);
  const letGo = () => {
    const wanted = wantedBesides(client, queryKey, claim);
    claim.release();
    if (!wanted) void client.cancelQueries({ queryKey, exact: true });
  };
  until?.addEventListener("abort", letGo, { once: true });
  const hash = hashKey(queryKey);
  const read = async (): Promise<T> => {
    try {
      return await fetch();
    } catch (error) {
      // A promotion cancelled the lower read this joined: join the one it reads again.
      const promotion = promotions.get(hash);
      if (!isCancelledError(error) || !promotion || until?.aborted) throw error;
      await promotion;
      return read();
    }
  };
  try {
    return await read();
  } finally {
    until?.removeEventListener("abort", letGo);
    await claim.settled();
    claim.release();
  }
}

/**
 * Run `queryKey`'s read at the priority it takes now, remembered while it's in flight. Its
 * claims hear of it, so a read ahead counts the shell's call until it really answers, even
 * after its query was cancelled.
 */
export async function readAtObservedPriority<T>(
  client: QueryClient,
  queryKey: readonly unknown[],
  read: (priority: CodeHostReadPriority) => Promise<T>,
): Promise<T> {
  watchPromotions(client);
  const hash = hashKey(queryKey);
  const priority = observedReadPriority(client, queryKey);
  let finish: () => void = () => {};
  const entry: ReadInFlight = {
    priority,
    done: new Promise((resolve) => {
      finish = resolve;
    }),
    superseded: false,
  };
  readsInFlight.set(hash, [...(readsInFlight.get(hash) ?? []), entry]);
  for (const claim of claims.get(hash) ?? []) {
    (claim as SpeculativeClaim & { track: (done: Promise<void>) => void }).track(entry.done);
  }
  try {
    return await read(priority);
  } finally {
    finish();
    const left = (readsInFlight.get(hash) ?? []).filter((each) => each !== entry);
    if (left.length > 0) readsInFlight.set(hash, left);
    else readsInFlight.delete(hash);
  }
}

/** `queryKey`'s newest read in flight that no promotion has read again, or null. */
function newestInFlight(hash: string): ReadInFlight | null {
  return readsInFlight.get(hash)?.findLast((entry) => !entry.superseded) ?? null;
}

/**
 * Read `queryKey` again at the priority its watchers want now, when its newest read in flight
 * went out lower; whether it did. That read is cancelled, so the shell drops it if it hasn't
 * run yet; one already running finishes, and its answer is set aside.
 */
export function promoteRead(client: QueryClient, queryKey: readonly unknown[]): boolean {
  const hash = hashKey(queryKey);
  const inFlight = newestInFlight(hash);
  if (inFlight === null) return false;
  const wanted = observedReadPriority(client, queryKey);
  if (RANK[wanted] <= RANK[inFlight.priority]) return false;
  // Set aside at once, so neither a second watcher nor one after its replacement answered
  // promotes it again; it still counts until its own call settles.
  inFlight.superseded = true;
  // Settles once the read again has started, for a read ahead that joined the lower one.
  const promotion = client
    .cancelQueries({ queryKey, exact: true })
    .then(() => {
      void client.refetchQueries({ queryKey, exact: true, type: "active" });
    })
    .finally(() => {
      if (promotions.get(hash) === promotion) promotions.delete(hash);
    });
  promotions.set(hash, promotion);
  return true;
}

/** Promote a query's read as soon as a watcher that wants it more starts watching it. */
function watchPromotions(client: QueryClient): void {
  if (watched.has(client)) return;
  watched.add(client);
  client.getQueryCache().subscribe((event) => {
    if (event.type !== "observerAdded" && event.type !== "observerOptionsUpdated") return;
    const { queryKey } = event.query;
    if (!readsInFlight.has(event.query.queryHash)) return;
    // After the observer's own subscription settles, as it may start a fetch of its own.
    queueMicrotask(() => promoteRead(client, queryKey));
  });
}

/** Forget every read in flight and claim, for tests. */
export function resetReadPrioritiesForTests(): void {
  readsInFlight.clear();
  claims.clear();
  promotions.clear();
}
