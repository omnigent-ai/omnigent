// Answers this Mac read from GitHub, kept on this device across restarts and handed back only
// while this Mac still reads GitHub as the account that read them. One lifecycle for every such
// cache: an entry restores only after the Mac's account is confirmed to be the one that saved it,
// or, for a login a page reads as by name, after a fresh read this run proved that login. A save
// lands only while its scope's accepted reader is that account, and a scope that moves to another
// account, or another workspace user signing in, drops what was kept for the old one. A page
// leaving a named login keeps what was kept for it, since the login itself didn't change.

import {
  experimental_createQueryPersister,
  type AsyncStorage,
  type PersistedQuery,
} from "@tanstack/query-persist-client-core";
import { hashKey, type Query, type QueryKey, type QueryPersister } from "@tanstack/react-query";
import { z } from "zod";

import { acceptRestoredMacAnswer, macReadTicket } from "@/lib/desktopReadPath";
import { durableServerScope } from "@/lib/durableServerScope";
import { getCurrentUserId, getIdentityStatus, subscribeIdentity } from "@/lib/identity";
import {
  acceptedMacReader,
  onMacReadScopeAdvanced,
  type MacReadScopeKey,
} from "@/lib/macReadScope";
import { keyHasScope } from "@/lib/macReadScopeQueries";
import {
  namedReaderEpoch,
  namedRestoreProof,
  namedRestoreProofHolds,
  subscribeNamedReads,
  type MacNamedReader,
} from "@/lib/macNamedReads";
import { desktopMacAccounts } from "@/lib/nativeBridge";

const GITHUB_HOST = "github.com";
/** How long a restore waits for the shell to name the account before reading GitHub. */
const ACCOUNT_CHECK_MS = 2_000;

export { durableServerScope };

/**
 * How a restore went: kept and handed back, nothing saved, refused, the shell didn't say, or
 * no fresh read proved the named login in time.
 */
export type MacDurableRestoreOutcome = "restored" | "none" | "refused" | "timeout" | "unproven";

/** Where an entry may be kept now: the scope whose accepted reader may save it, and whether it may restore. */
export interface MacDurableKeep {
  scope: MacReadScopeKey;
  /** Whether the entry may restore now; others are kept for display at most, or for later. */
  restorable: boolean;
  /**
   * The login the entry was read as by name, where it was: it restores only once a fresh read
   * this run proved that login for the same scope, never on the Mac's account's check.
   */
  reader?: MacNamedReader;
}

/** An entry as saved: TanStack's persisted query, its data checked by the cache's own schema. */
export interface MacDurableEntry<Data> {
  buster: string;
  queryHash: string;
  queryKey: unknown[];
  state: {
    data: Data;
    dataUpdatedAt: number;
    errorUpdatedAt: 0;
    status: "success";
  };
}

export interface MacDurableReadCacheOptions<Snapshot, Data> {
  /** Every entry's storage key starts with `${prefix}-`. */
  prefix: string;
  /** Moves when the saved shape changes, so older entries are dropped. */
  buster: string;
  maxAgeMs: number;
  /** A larger entry isn't kept; the query reads again instead. */
  maxEntryChars: number;
  /** What a saved entry's data must hold; parsing also strips fields nothing shows. */
  dataSchema: z.ZodType<Data>;
  /** Where `queryKey`'s entry may be kept now, or null when it may not (another user, server, account). */
  keepOf: (queryKey: QueryKey) => MacDurableKeep | null;
  /**
   * Whether the entry at a storage key may restore at all, checked before it's read. Without it,
   * whatever `keepOf` says of the saved entry decides.
   */
  restoresAt?: (storageKey: string) => boolean;
  /** The login GitHub said read the saved data. */
  viewerOf: (data: Data) => string;
  /** What to save from a query's answer under `queryKey`, or null when it holds nothing that may be kept. */
  saveOf: (snapshot: Snapshot, queryKey: QueryKey) => Data | null;
  /**
   * Whether a saved entry's data still belongs under its key, checked on every read of it, such
   * as an answer naming the revision its key does. Without it, the schema alone decides.
   */
  entryHolds?: (queryKey: QueryKey, data: Data) => boolean;
  /**
   * The key a query's entry is kept under, where it isn't the query's own: views that key one
   * answer apart in memory then share one entry. Null keeps nothing for that query.
   */
  durableKeyOf?: (queryKey: QueryKey) => QueryKey | null;
  /** The most entries this cache keeps; past it, the least recently used go first. */
  maxEntries?: number;
  /**
   * The answer a restored entry hands back, marked restored where the cache shows that.
   * `readAt` is when the entry was read, for an answer that dates what of it stays on screen.
   */
  restoreOf: (data: Data, readAt: number) => Snapshot;
  /**
   * When an answer about to be kept was read, where parts of it are older than the query's last
   * update: an entry is as old as the oldest read it holds, so keeping it again renews nothing.
   */
  readAtOf?: (snapshot: Snapshot) => number | undefined;
  /**
   * The most every entry of this cache may hold together. Past it, or when the device is full,
   * the least recently used entries of this cache go first; never another cache's.
   */
  totalBudgetChars?: number;
  /**
   * Read GitHub again right after a restore when the restored answer is stale, so it paints at
   * once and refreshes behind it. Off where the caller reads a restored answer again itself.
   */
  refetchOnRestore?: boolean;
  /**
   * Something else that can change what may be kept, such as named reads turning off: told
   * `prune`, it calls it then. Returns the unsubscribe.
   */
  prunesOn?: (prune: () => void) => () => void;
  /** Told after a saved entry goes, so a view of saved entries can update. */
  onRemoved?: () => void;
  /** Told how each restore of a restorable entry went. */
  onRestore?: (outcome: MacDurableRestoreOutcome) => void;
  now?: () => number;
}

export interface MacDurableReadCache<Snapshot, Data, Key extends QueryKey> {
  /** The query's persister, or undefined where this device can't keep anything. */
  persister: () => QueryPersister<Snapshot, Key> | undefined;
  /**
   * Follow account, scope and user changes from now on, before any query asks for the
   * persister, so a sign-out drops what was kept even where nothing was read this run.
   */
  watch: () => void;
  /** The entry kept now for `queryKey`, unverified, or null. Never enters a query's cache. */
  savedEntry: (queryKey: QueryKey) => MacDurableEntry<Data> | null;
  /** Drop every entry that may not be kept now, and every damaged one. */
  prune: () => void;
  storageKeyOf: (queryKey: QueryKey) => string;
  /** Forget the persister and stop its watchers, so a test starts clean. */
  resetForTests: () => void;
}

const TIMED_OUT = Symbol("timed out");

/**
 * `query` as the persister sees it, kept under `durableKey`: its state, restore and refetch stay
 * the query's own, while what's saved and read back is named by the shared key. The refetch
 * after a restore isn't awaited by anyone, so its failure lands in the query's state alone.
 */
function keptAs<Q extends Query<unknown, Error, unknown, QueryKey>>(
  query: Q,
  durableKey: QueryKey,
): Q {
  return {
    queryKey: durableKey,
    queryHash: hashKey(durableKey),
    get state() {
      return query.state;
    },
    setState: (state: Q["state"]) => query.setState(state),
    isStale: () => query.isStale(),
    fetch: () => query.fetch().catch(() => undefined),
  } as unknown as Q;
}

/** The proof a restore as `reader` needs, once a fresh read gives it; null if none in time. */
async function namedProof(reader: MacNamedReader, viewer: string) {
  const now = namedRestoreProof(reader, viewer);
  if (now) return now;
  return new Promise<ReturnType<typeof namedRestoreProof>>((resolve) => {
    const finish = (proof: ReturnType<typeof namedRestoreProof>) => {
      clearTimeout(timer);
      stop();
      resolve(proof);
    };
    const timer = setTimeout(() => finish(null), ACCOUNT_CHECK_MS);
    const stop = subscribeNamedReads(() => {
      const proof = namedRestoreProof(reader, viewer);
      if (proof) finish(proof);
    });
  });
}

/** Whether the server said who is signed in, or that nobody is. */
function signedInUserKnown(): boolean {
  return getCurrentUserId() !== null || getIdentityStatus().kind === "signedOut";
}

function deviceStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

async function currentMacAccount() {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<typeof TIMED_OUT>((resolve) => {
    timer = setTimeout(() => resolve(TIMED_OUT), ACCOUNT_CHECK_MS);
  });
  try {
    return await Promise.race([desktopMacAccounts(GITHUB_HOST), timeout]);
  } finally {
    clearTimeout(timer);
  }
}

/** A durable cache of Mac reads under the lifecycle above; see `MacDurableReadCacheOptions`. */
export function createMacDurableReadCache<Snapshot, Data, Key extends QueryKey>(
  options: MacDurableReadCacheOptions<Snapshot, Data>,
): MacDurableReadCache<Snapshot, Data, Key> {
  const { prefix, buster, maxAgeMs, maxEntryChars, keepOf, viewerOf } = options;
  const now = options.now ?? Date.now;
  const usedKey = `${prefix}:used`;
  const entrySchema = z.object({
    buster: z.literal(buster),
    queryHash: z.string(),
    queryKey: z.array(z.unknown()),
    state: z.object({
      data: options.dataSchema,
      dataUpdatedAt: z.number(),
      errorUpdatedAt: z.literal(0),
      status: z.literal("success"),
    }),
  });

  const storageKeyOf = (queryKey: QueryKey) => `${prefix}-${hashKey(queryKey)}`;

  function savedKeys(storage: Storage): string[] {
    const keys: string[] = [];
    try {
      for (let index = 0; index < storage.length; index++) {
        const key = storage.key(index);
        if (key?.startsWith(`${prefix}-`)) keys.push(key);
      }
    } catch {
      // Unreadable: nothing is listed, so nothing is restored.
    }
    return keys;
  }

  /** A saved entry that's whole, current in shape and age, and stored under its own key. */
  function parseEntry(raw: string, key: string): MacDurableEntry<Data> | null {
    if (raw.length > maxEntryChars) return null;
    let parsed: ReturnType<typeof entrySchema.safeParse>;
    try {
      parsed = entrySchema.safeParse(JSON.parse(raw));
    } catch {
      return null;
    }
    if (!parsed.success) return null;
    const entry = parsed.data as MacDurableEntry<Data>;
    if (entry.queryHash !== hashKey(entry.queryKey) || storageKeyOf(entry.queryKey) !== key) {
      return null;
    }
    if (options.entryHolds && !options.entryHolds(entry.queryKey, entry.state.data)) return null;
    const age = now() - entry.state.dataUpdatedAt;
    return age >= 0 && age <= maxAgeMs ? entry : null;
  }

  function savedEntryAt(storage: Storage, key: string): MacDurableEntry<Data> | null {
    try {
      const raw = storage.getItem(key);
      return raw ? parseEntry(raw, key) : null;
    } catch {
      return null;
    }
  }

  // When each entry was last restored or saved, for least-recently-used eviction.
  function usedTimes(storage: Storage): Record<string, number> {
    try {
      const parsed = z
        .record(z.string(), z.number())
        .safeParse(JSON.parse(storage.getItem(usedKey) ?? "{}"));
      return parsed.success ? parsed.data : {};
    } catch {
      return {};
    }
  }

  function markUsed(storage: Storage, key: string | null, dropped: readonly string[] = []): void {
    if (options.totalBudgetChars === undefined && options.maxEntries === undefined) return;
    const times = new Map(Object.entries(usedTimes(storage)));
    for (const gone of dropped) times.delete(gone);
    if (key) times.set(key, now());
    try {
      storage.setItem(usedKey, JSON.stringify(Object.fromEntries(times)));
    } catch {
      // Full or denied: eviction falls back to each entry's read time.
    }
  }

  function remove(storage: Storage, key: string): void {
    try {
      storage.removeItem(key);
    } catch {
      // Denied: the entry can't be restored without its scope's reader anyway.
    }
    markUsed(storage, null, [key]);
    options.onRemoved?.();
  }

  function prune(): void {
    const storage = deviceStorage();
    // Until the server says who is signed in, nothing can be judged; that answer prunes again.
    if (!storage || !signedInUserKnown()) return;
    for (const key of savedKeys(storage)) {
      const entry = savedEntryAt(storage, key);
      if (!entry || !keepOf(entry.queryKey)) remove(storage, key);
    }
  }

  let lastChecked: { key: string; raw: string; entry: MacDurableEntry<Data> | null } | null = null;

  function savedEntry(queryKey: QueryKey): MacDurableEntry<Data> | null {
    if (!keepOf(queryKey)) return null;
    const key = storageKeyOf(queryKey);
    let raw: string | null | undefined;
    try {
      raw = deviceStorage()?.getItem(key);
    } catch {
      return null;
    }
    if (!raw) return null;
    if (lastChecked?.key !== key || lastChecked.raw !== raw) {
      lastChecked = { key, raw, entry: parseEntry(raw, key) };
    }
    return lastChecked.entry;
  }

  /** Whether `data` may be saved for `queryKey` now: GitHub's answer as its scope's current reader. */
  function savable(queryKey: QueryKey, data: Data): boolean {
    const keep = keepOf(queryKey);
    if (!keep) return false;
    const reader = acceptedMacReader(keep.scope);
    return !!reader && reader.toLowerCase() === viewerOf(data).toLowerCase();
  }

  /** The entry at `key`, only once this Mac still reads GitHub as the account that saved it. */
  async function verifiedEntry(storage: Storage, key: string): Promise<string | null> {
    prune();
    const restoresHere = () => options.restoresAt?.(key) ?? true;
    if (!restoresHere()) return null;
    const entry = savedEntryAt(storage, key);
    const keep = entry ? keepOf(entry.queryKey) : null;
    if (!entry || !keep) {
      options.onRestore?.("none");
      return null;
    }
    if (!keep.restorable) return null;
    if (keep.reader) return verifiedNamedEntry(storage, key, entry, keep.reader);
    // Taken before asking, so an account change while the shell answers refuses it.
    const ticket = macReadTicket("github", GITHUB_HOST);
    const choice = await currentMacAccount();
    if (!restoresHere() || !keepOf(entry.queryKey)?.restorable) return null;
    // No answer in time: the entry stays, and GitHub is read instead.
    if (choice === TIMED_OUT) {
      options.onRestore?.("timeout");
      return null;
    }
    const effective = choice?.effective;
    const viewer = viewerOf(entry.state.data);
    // Refused, or another account now: the entry goes, and GitHub is read instead.
    if (
      !effective ||
      !acceptRestoredMacAnswer(ticket, { host: GITHUB_HOST, ...effective }, viewer)
    ) {
      remove(storage, key);
      options.onRestore?.("refused");
      return null;
    }
    markUsed(storage, key);
    options.onRestore?.("restored");
    return JSON.stringify(entry);
  }

  /**
   * A named login's entry, once a fresh read this run proved that login for its scope. Without
   * that proof in time it stays, and GitHub is read; a refusal later drops it with its scope.
   */
  async function verifiedNamedEntry(
    storage: Storage,
    key: string,
    entry: MacDurableEntry<Data>,
    reader: MacNamedReader,
  ): Promise<string | null> {
    // Taken before waiting, so a leave, refusal or user switch meanwhile refuses the restore.
    const before = { epoch: namedReaderEpoch(reader), userId: getCurrentUserId() ?? "" };
    const proof = await namedProof(reader, viewerOf(entry.state.data));
    const stillHere = () =>
      (options.restoresAt?.(key) ?? true) && keepOf(entry.queryKey)?.restorable === true;
    if (
      !proof ||
      proof.epoch !== before.epoch ||
      proof.userId !== before.userId ||
      !stillHere() ||
      !namedRestoreProofHolds(reader, proof)
    ) {
      options.onRestore?.("unproven");
      return null;
    }
    markUsed(storage, key);
    options.onRestore?.("restored");
    return JSON.stringify(entry);
  }

  function serialize({ queryKey, queryHash, buster: savedBuster, state }: PersistedQuery): string {
    const snapshot = state.data as Snapshot | undefined;
    if (state.status !== "success" || snapshot === undefined) return "";
    const data = options.saveOf(snapshot, queryKey);
    if (data === null || !savable(queryKey, data)) return "";
    if (options.entryHolds && !options.entryHolds(queryKey, data)) return "";
    const parsed = entrySchema.safeParse({
      buster: savedBuster,
      queryHash,
      queryKey,
      state: {
        data,
        dataUpdatedAt: Math.min(state.dataUpdatedAt, options.readAtOf?.(snapshot) ?? Infinity),
        errorUpdatedAt: 0,
        status: "success",
      },
    });
    return parsed.success ? JSON.stringify(parsed.data) : "";
  }

  function deserialize(raw: string): PersistedQuery {
    const entry = JSON.parse(raw) as MacDurableEntry<Data>;
    return {
      ...entry,
      state: {
        ...entry.state,
        data: options.restoreOf(entry.state.data, entry.state.dataUpdatedAt),
      },
    } as unknown as PersistedQuery;
  }

  /** This cache's other entries, least recently used first, with their sizes. */
  function evictionOrder(
    storage: Storage,
    keeping: string,
  ): { key: string; chars: number; usedAt: number }[] {
    const times = usedTimes(storage);
    return savedKeys(storage)
      .filter((key) => key !== keeping)
      .map((key) => {
        let raw: string | null = null;
        try {
          raw = storage.getItem(key);
        } catch {
          // Unreadable: counted as empty, and first to go.
        }
        const entry = raw ? parseEntry(raw, key) : null;
        const usedAt = times[key] ?? entry?.state.dataUpdatedAt ?? 0;
        return { key, chars: raw?.length ?? 0, usedAt };
      })
      .sort((left, right) => left.usedAt - right.usedAt);
  }

  /**
   * Save `value` at `key`, making room within the budget and the device's quota by evicting LRU
   * entries. The budget counts every key and value this cache stores, its usage index included,
   * in UTF-16 code units as storage does; a value that can't fit is kept nowhere.
   */
  function write(storage: Storage, key: string, value: string): void {
    const budget = options.totalBudgetChars;
    const limited = budget !== undefined || options.maxEntries !== undefined;
    const saved = () => {
      try {
        storage.setItem(key, value);
        return true;
      } catch {
        return false;
      }
    };
    if (!limited) {
      if (!saved()) remove(storage, key);
      return;
    }
    const survivors = evictionOrder(storage, key);
    const times = usedTimes(storage);
    const usedAt = now();
    // The index as it will be written: only what survives, and this entry as just used.
    const indexOf = () =>
      JSON.stringify(
        Object.fromEntries([
          ...survivors.map((other) => [other.key, times[other.key] ?? other.usedAt]),
          [key, usedAt],
        ]),
      );
    const evictOldest = () => remove(storage, survivors.shift()!.key);
    if (options.maxEntries !== undefined) {
      while (survivors.length >= options.maxEntries) evictOldest();
    }
    if (budget !== undefined) {
      const total = () =>
        usedKey.length +
        indexOf().length +
        key.length +
        value.length +
        survivors.reduce((sum, other) => sum + other.key.length + other.chars, 0);
      while (total() > budget && survivors.length > 0) evictOldest();
      if (total() > budget) {
        remove(storage, key);
        return;
      }
    }
    // Full: free the least recently used entry of this cache and try again; with none left,
    // keep nothing rather than a partial answer.
    while (!saved()) {
      if (survivors.length === 0) {
        remove(storage, key);
        return;
      }
      evictOldest();
    }
    try {
      storage.setItem(usedKey, indexOf());
    } catch {
      // Full or denied: eviction falls back to each entry's read time.
    }
  }

  function durableStorage(storage: Storage): AsyncStorage {
    return {
      getItem: (key) => verifiedEntry(storage, key),
      setItem: (key, value) => {
        // Checked again here: the scope may have moved since the entry was made.
        const entry = value ? parseEntry(value, key) : null;
        if (!entry || !savable(entry.queryKey, entry.state.data)) return;
        prune();
        write(storage, key, value);
      },
      removeItem: (key) => remove(storage, key),
    };
  }

  let stopWatching: (() => void) | null = null;

  /** Drop what's kept for a scope whose account changed, and another user's on a switch. */
  function watchAccounts(storage: Storage): () => void {
    const stopFollowingScopes = onMacReadScopeAdvanced((scope: MacReadScopeKey, move) => {
      // The page left the login; it didn't sign out or change, so its entries stay.
      if (move === "left") return;
      for (const key of savedKeys(storage)) {
        const entry = savedEntryAt(storage, key);
        if (!entry || keyHasScope(entry.queryKey, scope)) remove(storage, key);
      }
    });
    const stopFollowingUser = subscribeIdentity(prune);
    const stopFollowingOthers = options.prunesOn?.(prune);
    return () => {
      stopFollowingScopes();
      stopFollowingUser();
      stopFollowingOthers?.();
    };
  }

  function watch(): void {
    if (stopWatching) return;
    const storage = deviceStorage();
    if (!storage) return;
    stopWatching = watchAccounts(storage);
    prune();
  }

  type PersisterFn = QueryPersister<Snapshot, Key>;
  let persister: PersisterFn | null | undefined;

  return {
    persister() {
      if (persister === undefined) {
        const storage = deviceStorage();
        if (!storage) {
          persister = null;
        } else {
          watch();
          const persisterFn = experimental_createQueryPersister({
            storage: durableStorage(storage),
            prefix,
            buster,
            maxAge: maxAgeMs,
            serialize,
            deserialize,
            refetchOnRestore: options.refetchOnRestore ?? false,
          }).persisterFn as PersisterFn;
          const { durableKeyOf } = options;
          persister = (queryFn, context, query) => {
            const durableKey = durableKeyOf ? durableKeyOf(query.queryKey) : query.queryKey;
            if (!durableKey) return Promise.resolve(queryFn(context));
            return persisterFn(queryFn, context, keptAs(query, durableKey));
          };
        }
      }
      return persister ?? undefined;
    },
    watch,
    savedEntry,
    prune,
    storageKeyOf,
    resetForTests() {
      stopWatching?.();
      stopWatching = null;
      persister = undefined;
      lastChecked = null;
    },
  };
}
