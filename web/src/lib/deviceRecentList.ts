// Device-local, owner-scoped recent lists with bounded items and scopes.

import { useSyncExternalStore } from "react";
import { z } from "zod";

import { getCurrentUserId, subscribeIdentity } from "@/lib/identity";
import { durableServerScope } from "@/lib/durableServerScope";

export interface DeviceRecentListOptions<Item> {
  /** The storage key prefix every list of this kind starts with. */
  storagePrefix: string;
  /** The stored JSON field that holds a scope's items. */
  field: string;
  /** How many items a scope keeps, most recent first. */
  maxItems: number;
  /** How many scopes keep a list. */
  maxScopes: number;
  item: z.ZodType<Item>;
  /** Whether two items are the same one, so recording it again moves it to the front. */
  same: (left: Item, right: Item) => boolean;
  /**
   * Whose lists survive a change of workspace user or server: `current-owner` (the default)
   * drops everyone else's, as recents do; `every-owner` keeps each owner's, for preferences a
   * user expects back after signing in again. `maxScopes` then applies to each owner apart.
   */
  retention?: "current-owner" | "every-owner";
}

export interface DeviceRecentList<Item> {
  /** A scope's items, most recent first; `scope` is compared without case. */
  read: (scope: readonly string[]) => Item[];
  /** Replace a scope's items with their first `maxItems`; an empty list forgets the scope. */
  write: (scope: readonly string[], items: readonly Item[]) => void;
  /** Who lists are kept for now, or null without a stable server or a user. */
  owner: () => string | null;
  /**
   * Put `added` first, its last item first as the most recent, for `owner` (from `owner()` when
   * the change was made). Nothing is kept once another user or server is signed in instead.
   */
  record: (scope: readonly string[], added: readonly Item[], owner: string | null) => void;
  /**
   * Drop the oldest past the scope limit and unreadable lists, and, keeping only the current
   * owner's, other users' and servers' lists once that owner is known.
   */
  prune: () => void;
  /** A scope's items, re-read when they change or another user signs in. */
  useItems: (scope: readonly string[] | null) => Item[];
  /**
   * Call `listener` after any list changes: here, in another window on this device, or by
   * another user signing in. Returns the unsubscribe.
   */
  subscribe: (listener: () => void) => () => void;
  /** Whether this device keeps any list of this kind, for any user or server. */
  hasSaved: () => boolean;
  /** Forget subscriptions, so a test starts clean. */
  resetForTests: () => void;
}

function isOwner(text: string): boolean {
  try {
    const owner: unknown = JSON.parse(text);
    return (
      Array.isArray(owner) && owner.length === 2 && owner.every((part) => typeof part === "string")
    );
  } catch {
    return false;
  }
}

function deviceStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

export function createDeviceRecentList<Item>({
  storagePrefix,
  field,
  maxItems,
  maxScopes,
  item,
  same,
  retention = "current-owner",
}: DeviceRecentListOptions<Item>): DeviceRecentList<Item> {
  const savedSchema = z.object({ usedAt: z.number() }).passthrough();
  // A list saved under a higher limit reads as its first items, so lowering it loses nothing else.
  const itemsSchema = z.array(item).transform((items) => items.slice(0, maxItems));
  // Every key of this kind: the prefix, then the owner as a JSON array, so a longer prefix's
  // lists, such as `${storagePrefix}:branches`, are never this kind's.
  const keyStart = `${storagePrefix}:[`;

  /** The owner part of a key, as `userPrefix` writes it, or null for a key it never writes. */
  const ownerOf = (key: string): string | null => {
    if (!key.startsWith(keyStart)) return null;
    // The owner is a JSON array of two strings and the scope after it starts with `:[`; a string
    // may hold `]:[` too, so try each until one reads as the owner.
    for (
      let end = key.indexOf("]:[", keyStart.length);
      end !== -1;
      end = key.indexOf("]:[", end + 1)
    ) {
      if (isOwner(key.slice(keyStart.length - 1, end + 1))) return key.slice(0, end + 2);
    }
    return null;
  };

  const userPrefix = (): string | null => {
    const server = durableServerScope();
    const userId = getCurrentUserId();
    return server && userId ? `${storagePrefix}:${JSON.stringify([server, userId])}:` : null;
  };
  const storageKey = (scope: readonly string[]): string | null => {
    const prefix = userPrefix();
    return prefix ? `${prefix}${JSON.stringify(scope.map((part) => part.toLowerCase()))}` : null;
  };

  let version = 0;
  const listeners = new Set<() => void>();
  const changed = () => {
    version += 1;
    for (const listener of listeners) listener();
  };

  const parse = (storage: Storage, key: string): { usedAt: number; items: Item[] } | null => {
    try {
      const saved = savedSchema.safeParse(JSON.parse(storage.getItem(key) ?? "null"));
      if (!saved.success) return null;
      const items = itemsSchema.safeParse(saved.data[field]);
      return items.success ? { usedAt: saved.data.usedAt, items: items.data } : null;
    } catch {
      return null;
    }
  };

  const savedKeys = (storage: Storage): string[] => {
    const keys: string[] = [];
    try {
      for (let index = 0; index < storage.length; index++) {
        const key = storage.key(index);
        if (key?.startsWith(keyStart)) keys.push(key);
      }
    } catch {
      // Storage that can't be listed keeps no lists this can read either.
    }
    return keys;
  };

  const remove = (storage: Storage, key: string) => {
    try {
      storage.removeItem(key);
    } catch {
      // Denied: it stays unreadable to anyone else's key anyway.
    }
  };

  const prune = (written: string | null = null) => {
    const storage = deviceStorage();
    if (!storage) return;
    const prefix = userPrefix();
    const byOwner = new Map<string, { key: string; usedAt: number }[]>();
    for (const key of savedKeys(storage)) {
      const usedAt = parse(storage, key)?.usedAt ?? null;
      const owner = ownerOf(key);
      // Only a known owner drops another's: an unresolved or failed identity keeps every list.
      const anotherOwners = retention === "current-owner" && !!prefix && owner !== prefix;
      if (usedAt === null || owner === null || anotherOwners) {
        remove(storage, key);
        continue;
      }
      const lists = byOwner.get(owner) ?? [];
      lists.push({ key, usedAt });
      byOwner.set(owner, lists);
    }
    // Each owner keeps its newest; on a tie the list just written counts as newest.
    for (const lists of byOwner.values()) {
      lists.sort(
        (left, right) =>
          right.usedAt - left.usedAt ||
          Number(right.key === written) - Number(left.key === written),
      );
      for (const { key } of lists.slice(maxScopes)) remove(storage, key);
    }
  };

  const read = (scope: readonly string[]): Item[] => {
    const storage = deviceStorage();
    const key = storageKey(scope);
    return (storage && key ? parse(storage, key)?.items : null) ?? [];
  };

  const write = (scope: readonly string[], items: readonly Item[]) => {
    const storage = deviceStorage();
    const key = storageKey(scope);
    if (!storage || !key) return;
    try {
      const kept = items.slice(0, maxItems);
      if (kept.length === 0) storage.removeItem(key);
      else storage.setItem(key, JSON.stringify({ usedAt: Date.now(), [field]: kept }));
    } catch {
      // Quota exceeded or denied: the list stored before stays as it was.
    }
    prune(key);
    changed();
  };

  const record = (scope: readonly string[], added: readonly Item[], owner: string | null) => {
    if (added.length === 0 || owner === null || owner !== userPrefix()) return;
    const kept = read(scope).filter((known) => !added.some((fresh) => same(fresh, known)));
    // A batch naming an item twice keeps it once, where it was recorded last.
    const newest = [...added]
      .reverse()
      .filter((fresh, index, all) => all.findIndex((other) => same(other, fresh)) === index);
    write(scope, [...newest, ...kept].slice(0, maxItems));
  };

  // Ends what `follow` listens to; null while nothing is followed.
  let stopFollowing: (() => void) | null = null;
  const follow = () => {
    if (stopFollowing) return;
    const stopIdentity = subscribeIdentity(() => {
      prune();
      changed();
    });
    // Another window on this device changed a list of this kind, or cleared storage.
    const onStorage = (event: StorageEvent) => {
      if (event.key === null || event.key.startsWith(keyStart)) changed();
    };
    if (typeof window !== "undefined") window.addEventListener("storage", onStorage);
    stopFollowing = () => {
      stopIdentity();
      if (typeof window !== "undefined") window.removeEventListener("storage", onStorage);
    };
  };
  // One array per version and scope, so each picker re-renders only when its own items changed.
  let cachedVersion = -1;
  const cached = new Map<string, Item[]>();
  const none: Item[] = [];
  const snapshot = (scope: readonly string[] | null, current: number): Item[] => {
    const key = scope ? storageKey(scope) : null;
    if (!scope || !key) return none;
    if (cachedVersion !== current) {
      cachedVersion = current;
      cached.clear();
    }
    let items = cached.get(key);
    if (!items) {
      items = read(scope);
      cached.set(key, items);
    }
    return items;
  };

  const subscribe = (listener: () => void) => {
    listeners.add(listener);
    follow();
    return () => {
      listeners.delete(listener);
    };
  };

  const useItems = (scope: readonly string[] | null): Item[] => {
    const current = useSyncExternalStore(
      subscribe,
      () => version,
      () => 0,
    );
    return snapshot(scope, current);
  };

  return {
    read,
    write,
    owner: userPrefix,
    record,
    prune,
    useItems,
    subscribe,
    hasSaved: () => {
      const storage = deviceStorage();
      return !!storage && savedKeys(storage).length > 0;
    },
    resetForTests: () => {
      stopFollowing?.();
      stopFollowing = null;
      listeners.clear();
      cached.clear();
      cachedVersion = -1;
      version = 0;
    },
  };
}
