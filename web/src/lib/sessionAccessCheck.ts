// A session's own snapshot decides the viewer's access to it, so every surface that greys an
// action "while checking" depends on that read settling. This module watches the shared
// `["session", id]` query, whichever caller started its read, marks a read still unanswered
// past a deadline as overdue so surfaces can offer a retry.

import {
  type Query,
  type QueryCacheNotifyEvent,
  type QueryClient,
  QueryClientContext,
} from "@tanstack/react-query";
import { useContext, useSyncExternalStore } from "react";

import { ApiError } from "@/lib/sessionsApi";

/** A read still unanswered this long is overdue: surfaces stop saying "Checking…". */
export const ACCESS_CHECK_DEADLINE_MS = 15_000;

interface LookupEntry {
  query: Query;
  overdue: boolean;
  timers: ReturnType<typeof setTimeout>[];
}

/** One client's reads in flight, by session id. */
const lookupsByClient = new WeakMap<QueryClient, Map<string, LookupEntry>>();
const listeners = new Set<() => void>();

function notify(): void {
  for (const listener of [...listeners]) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The session a query key reads, for the shared snapshot key only. */
function sessionIdOf(queryKey: readonly unknown[]): string | null {
  const [scope, id] = queryKey;
  return queryKey.length === 2 && scope === "session" && typeof id === "string" ? id : null;
}

function start(lookups: Map<string, LookupEntry>, sessionId: string, query: Query): void {
  stop(lookups, sessionId);
  const entry: LookupEntry = { query, overdue: false, timers: [] };
  lookups.set(sessionId, entry);
  const isCurrent = () => lookups.get(sessionId) === entry;
  entry.timers.push(
    setTimeout(() => {
      if (!isCurrent()) return;
      entry.overdue = true;
      notify();
    }, ACCESS_CHECK_DEADLINE_MS),
  );
}

/** End `sessionId`'s current read. */
function stop(lookups: Map<string, LookupEntry>, sessionId: string): void {
  const entry = lookups.get(sessionId);
  if (!entry) return;
  for (const timer of entry.timers) clearTimeout(timer);
  lookups.delete(sessionId);
  if (entry.overdue) notify();
}

/**
 * Follow one session query through its own actions, so observer notifications never settle a
 * read early. Every read is timed, with or without a snapshot cached from before; only a read
 * with nothing cached reads as "Checking…", which `hasSessionAccessCheckFailed` decides.
 */
function observe(lookups: Map<string, LookupEntry>, event: QueryCacheNotifyEvent): void {
  const sessionId = sessionIdOf(event.query.queryKey);
  if (sessionId === null) return;
  const entry = lookups.get(sessionId);
  const current = entry?.query === event.query ? entry : undefined;
  if (event.type === "removed") {
    if (current) stop(lookups, sessionId);
    return;
  }
  if (event.type !== "updated") return;
  const { action } = event;
  switch (action.type) {
    case "fetch":
      // A new read restarts the clock; pausing and resuming the same read doesn't.
      start(lookups, sessionId, event.query);
      return;
    case "success":
    case "error":
      if (current) stop(lookups, sessionId);
      return;
    case "setState":
      // A cancelled read reverts to idle.
      if (current && event.query.state.fetchStatus === "idle") stop(lookups, sessionId);
      return;
    default:
      return;
  }
}

/**
 * Watch `queryClient`'s session reads, once per client; the app does this as it creates its
 * client, and the hooks below on first use. A read already in flight is timed from then.
 */
export function watchSessionLookups(queryClient: QueryClient): ReadonlyMap<string, LookupEntry> {
  const existing = lookupsByClient.get(queryClient);
  if (existing) return existing;
  const lookups = new Map<string, LookupEntry>();
  lookupsByClient.set(queryClient, lookups);
  const cache = queryClient.getQueryCache();
  for (const query of cache.getAll()) {
    const sessionId = sessionIdOf(query.queryKey);
    if (sessionId !== null && query.state.fetchStatus !== "idle") start(lookups, sessionId, query);
  }
  cache.subscribe((event) => observe(lookups, event));
  return lookups;
}

/**
 * The server says the session is gone or no longer visible to this viewer. That outranks a
 * snapshot cached from before, unlike a transient failure.
 */
export function isSessionAccessLost(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 403 || error.status === 404);
}

/** Nothing says what the viewer may do: no snapshot, and its read failed or ran out of time. */
export function hasSessionAccessCheckFailed({
  snapshot,
  error,
  overdue,
}: {
  snapshot: unknown;
  error: unknown;
  overdue: boolean;
}): boolean {
  if (snapshot != null) return false;
  return overdue || (error != null && !isSessionAccessLost(error));
}

/** Whether `sessionId`'s current read on `queryClient` has run past the deadline unanswered. */
export function isSessionLookupOverdue(
  queryClient: QueryClient,
  sessionId: string | null,
): boolean {
  if (sessionId === null) return false;
  return watchSessionLookups(queryClient).get(sessionId)?.overdue === true;
}

/** {@link isSessionLookupOverdue} for the current client, live; false outside a query client. */
export function useSessionLookupOverdue(sessionId: string | null): boolean {
  const queryClient = useContext(QueryClientContext);
  return useSyncExternalStore(
    subscribe,
    () => (queryClient ? isSessionLookupOverdue(queryClient, sessionId) : false),
    () => false,
  );
}

/** Stop timing every read on `queryClient`; for tests. */
export function resetSessionLookupsForTest(queryClient: QueryClient): void {
  const lookups = lookupsByClient.get(queryClient);
  if (!lookups) return;
  for (const sessionId of [...lookups.keys()]) stop(lookups, sessionId);
}
