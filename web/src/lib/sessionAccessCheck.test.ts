import { onlineManager, QueryClient, QueryObserver } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { sessionQueryOptions } from "@/hooks/useSession";

import {
  ACCESS_CHECK_DEADLINE_MS,
  hasSessionAccessCheckFailed,
  isSessionAccessLost,
  isSessionLookupOverdue,
  resetSessionLookupsForTest,
  watchSessionLookups,
} from "./sessionAccessCheck";
import * as sessionsApi from "./sessionsApi";
import { ApiError } from "./sessionsApi";
import type { Session } from "./types";

const SESSION = "conv_access";
const KEY = ["session", SESSION] as const;
const SNAPSHOT = { id: SESSION, permissionLevel: 4, parentSessionId: null } as Session;

/** A read the test answers when it chooses. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

let client: QueryClient;

/** Start a read of the shared key the way a prefetch or a store does, not through useSession. */
function startRead(queryFn: () => Promise<Session>) {
  return client.fetchQuery({ queryKey: KEY, queryFn, retry: false }).catch(() => undefined);
}

const overdue = () => isSessionLookupOverdue(client, SESSION);

beforeEach(() => {
  vi.useFakeTimers();
  client = new QueryClient();
  watchSessionLookups(client);
});

afterEach(() => {
  resetSessionLookupsForTest(client);
  client.clear();
  onlineManager.setOnline(true);
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("watchSessionLookups", () => {
  it("leaves a read that answers in time alone", async () => {
    await startRead(() => Promise.resolve(SNAPSHOT));
    await vi.advanceTimersByTimeAsync(ACCESS_CHECK_DEADLINE_MS);
    expect(overdue()).toBe(false);
  });

  it("marks a read any caller starts overdue at the deadline, not before", async () => {
    void startRead(() => new Promise(() => {}));
    await vi.advanceTimersByTimeAsync(ACCESS_CHECK_DEADLINE_MS - 1);
    expect(overdue()).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(overdue()).toBe(true);
  });

  it("times a read already in flight when watching starts", async () => {
    const unwatched = new QueryClient();
    void unwatched
      .fetchQuery({ queryKey: KEY, queryFn: () => new Promise<Session>(() => {}) })
      .catch(() => undefined);
    watchSessionLookups(unwatched);
    await vi.advanceTimersByTimeAsync(ACCESS_CHECK_DEADLINE_MS);
    expect(isSessionLookupOverdue(unwatched, SESSION)).toBe(true);
    resetSessionLookupsForTest(unwatched);
    unwatched.clear();
  });

  it("keeps counting while a read is paused offline", async () => {
    onlineManager.setOnline(false);
    void client
      .fetchQuery({
        queryKey: KEY,
        queryFn: () => Promise.resolve(SNAPSHOT),
        networkMode: "online",
      })
      .catch(() => undefined);
    await vi.advanceTimersByTimeAsync(0);
    expect(client.getQueryState(KEY)?.fetchStatus).toBe("paused");
    await vi.advanceTimersByTimeAsync(ACCESS_CHECK_DEADLINE_MS);
    expect(overdue()).toBe(true);
  });

  it("clears the overdue mark when a late answer arrives", async () => {
    const read = deferred<Session>();
    void startRead(() => read.promise);
    await vi.advanceTimersByTimeAsync(20_000);
    expect(overdue()).toBe(true);
    read.resolve(SNAPSHOT);
    await vi.advanceTimersByTimeAsync(0);
    expect(overdue()).toBe(false);
  });

  it("treats a snapshot written straight to the cache as the answer", async () => {
    void startRead(() => new Promise(() => {}));
    await vi.advanceTimersByTimeAsync(ACCESS_CHECK_DEADLINE_MS);
    client.setQueryData(KEY, SNAPSHOT);
    expect(overdue()).toBe(false);
  });

  it("restarts the clock for a new read", async () => {
    void startRead(() => new Promise(() => {}));
    await vi.advanceTimersByTimeAsync(ACCESS_CHECK_DEADLINE_MS - 1_000);
    await client.cancelQueries({ queryKey: KEY, exact: true });
    void startRead(() => new Promise(() => {}));
    await vi.advanceTimersByTimeAsync(1_000);
    expect(overdue()).toBe(false);
    await vi.advanceTimersByTimeAsync(ACCESS_CHECK_DEADLINE_MS);
    expect(overdue()).toBe(true);
  });

  it("ignores other queries, including other session-scoped keys", async () => {
    void client
      .fetchQuery({ queryKey: ["session", SESSION, "items"], queryFn: () => new Promise(() => {}) })
      .catch(() => undefined);
    await vi.advanceTimersByTimeAsync(ACCESS_CHECK_DEADLINE_MS);
    expect(overdue()).toBe(false);
  });
});

describe("watchSessionLookups with a watching component", () => {
  /** Watch the shared query as useSession does, so the cache also notifies its observer. */
  function observe() {
    const observer = new QueryObserver(client, sessionQueryOptions(SESSION));
    const unsubscribe = observer.subscribe(() => {});
    return { observer, unsubscribe };
  }

  it("settles an observed read that fails, rather than marking it overdue later", async () => {
    vi.spyOn(sessionsApi, "getSessionSlim").mockRejectedValue(new ApiError("Forbidden", 403, null));
    const { unsubscribe } = observe();
    await vi.advanceTimersByTimeAsync(ACCESS_CHECK_DEADLINE_MS);
    expect(overdue()).toBe(false);
    unsubscribe();
  });

  it("stops timing an observed read that is cancelled", async () => {
    vi.spyOn(sessionsApi, "getSessionSlim").mockReturnValue(new Promise(() => {}));
    const { unsubscribe } = observe();
    await vi.advanceTimersByTimeAsync(0);
    await client.cancelQueries({ queryKey: KEY, exact: true });
    await vi.advanceTimersByTimeAsync(ACCESS_CHECK_DEADLINE_MS);
    expect(overdue()).toBe(false);
    unsubscribe();
  });
});

describe("hasSessionAccessCheckFailed", () => {
  it("is false whenever a snapshot is known", () => {
    expect(
      hasSessionAccessCheckFailed({ snapshot: {}, error: new TypeError("x"), overdue: true }),
    ).toBe(false);
  });

  it("is true for an overdue or transiently failed read without a snapshot", () => {
    expect(hasSessionAccessCheckFailed({ snapshot: null, error: null, overdue: true })).toBe(true);
    expect(
      hasSessionAccessCheckFailed({
        snapshot: null,
        error: new ApiError("x", 500, null),
        overdue: false,
      }),
    ).toBe(true);
  });

  it("leaves lost access and a read still in time alone", () => {
    expect(hasSessionAccessCheckFailed({ snapshot: null, error: null, overdue: false })).toBe(
      false,
    );
    expect(
      hasSessionAccessCheckFailed({
        snapshot: null,
        error: new ApiError("x", 403, null),
        overdue: false,
      }),
    ).toBe(false);
    expect(isSessionAccessLost(new ApiError("x", 404, null))).toBe(true);
    expect(isSessionAccessLost(new ApiError("x", 401, null))).toBe(false);
  });
});
