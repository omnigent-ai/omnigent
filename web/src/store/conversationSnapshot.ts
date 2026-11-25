/**
 * The snapshot a conversation paints from when it binds: the session metadata
 * plus the newest page of items.
 */
import type { QueryClient } from "@tanstack/react-query";
import {
  fetchSessionItemsPage,
  getSessionSlim,
  INITIAL_WINDOW_ITEMS,
  type SessionItemsPage,
} from "@/lib/sessionsApi";
import type { Session } from "@/lib/types";

/** Session metadata and the newest page of items: everything a bind paints from. */
export interface ConversationSnapshot {
  session: Session;
  page: SessionItemsPage;
}

/** How a snapshot's reads repeat and how long one attempt may take. */
export interface SnapshotReadPolicy {
  /** Repeats a read whose failure a retry can fix; `attempt` counts from 0. */
  withRetries: <T>(read: (attempt: number) => Promise<T>) => Promise<T>;
  /** Bounds one attempt, aborting its signal and rejecting with a `TimeoutError`. */
  withDeadline: <T>(read: (signal: AbortSignal) => Promise<T>, attempt: number) => Promise<T>;
}

function isTimeoutError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "TimeoutError";
}

/**
 * Fetch the snapshot a cold bind paints from. Always refetches the session
 * (`staleTime: 0`): a cached one can predate items committed while the user was
 * elsewhere. `retry: false` because the policy owns retries.
 */
export async function fetchConversationSnapshot(
  client: QueryClient,
  id: string,
  { withRetries, withDeadline }: SnapshotReadPolicy,
): Promise<ConversationSnapshot> {
  const queryKey = ["session", id];
  // Set once a session read times out, and kept set through later failures: the shared
  // read it may have joined is still the one that never answers.
  let sessionReadTimedOut = false;
  const readSession = (attempt: number): Promise<Session> =>
    withDeadline((signal) => {
      const joinsSharedRead = client.isFetching({ queryKey, exact: true }) > 0;
      // A retry after a timeout reads directly rather than rejoin a shared read that
      // may never answer, and its result seeds the shared cache.
      if (sessionReadTimedOut && joinsSharedRead) {
        return getSessionSlim(id, { refreshState: true, signal }).then((fresh) => {
          client.setQueryData(queryKey, fresh);
          return fresh;
        });
      }
      // An attempt that starts the request drops it on timeout, so the retry reads
      // afresh. One that joins another caller's read leaves that read to its caller.
      if (!joinsSharedRead) {
        signal.addEventListener(
          "abort",
          () => void client.cancelQueries({ queryKey, exact: true }),
          { once: true },
        );
      }
      return client.fetchQuery({
        queryKey,
        queryFn: ({ signal: querySignal }) =>
          getSessionSlim(id, { refreshState: true, signal: querySignal }),
        staleTime: 0,
        retry: false,
      });
    }, attempt).catch((error: unknown) => {
      if (isTimeoutError(error)) sessionReadTimedOut = true;
      throw error;
    });
  // Each read retries on its own, so a failed one doesn't repeat the other.
  const [session, page] = await Promise.all([
    withRetries(readSession),
    withRetries((attempt) =>
      withDeadline(
        (signal) => fetchSessionItemsPage(id, { limit: INITIAL_WINDOW_ITEMS, signal }),
        attempt,
      ),
    ),
  ]);
  return { session, page };
}
