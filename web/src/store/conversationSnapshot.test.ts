import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as SessionsApi from "@/lib/sessionsApi";
import { fetchSessionItemsPage, getSessionSlim, INITIAL_WINDOW_ITEMS } from "@/lib/sessionsApi";
import type { Session } from "@/lib/types";
import { fetchConversationSnapshot, type SnapshotReadPolicy } from "./conversationSnapshot";

vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApi>()),
  getSessionSlim: vi.fn(),
  fetchSessionItemsPage: vi.fn(),
}));

const getSession = vi.mocked(getSessionSlim);
const getPage = vi.mocked(fetchSessionItemsPage);
const session = (title: string) => ({ id: "conv-1", title }) as unknown as Session;
const page = { items: [], hasMore: false };

function timeoutError(): DOMException {
  return new DOMException("session read timed out", "TimeoutError");
}

/** Retries any failure up to `attempts` times; times out the attempts listed in `timeOut`. */
function policy({ attempts = 3, timeOut = [] as number[] } = {}): SnapshotReadPolicy {
  return {
    async withRetries(read) {
      for (let attempt = 0; ; attempt++) {
        try {
          // eslint-disable-next-line no-await-in-loop
          return await read(attempt);
        } catch (error) {
          if (attempt + 1 >= attempts) throw error;
        }
      }
    },
    withDeadline(read, attempt) {
      const controller = new AbortController();
      const result = read(controller.signal);
      if (!timeOut.includes(attempt)) return result;
      result.catch(() => {});
      controller.abort(timeoutError());
      return Promise.reject(timeoutError());
    },
  };
}

describe("fetchConversationSnapshot", () => {
  let client: QueryClient;
  beforeEach(() => {
    client = new QueryClient();
    getPage.mockResolvedValue(page);
  });
  afterEach(() => {
    client.clear();
    vi.resetAllMocks();
  });

  it("reads a fresh session and the newest page, and caches the session", async () => {
    client.setQueryData(["session", "conv-1"], session("cached"));
    getSession.mockResolvedValue(session("fresh"));

    const snapshot = await fetchConversationSnapshot(client, "conv-1", policy());

    expect(snapshot).toEqual({ session: session("fresh"), page });
    expect(getSession).toHaveBeenCalledWith(
      "conv-1",
      expect.objectContaining({ refreshState: true }),
    );
    expect(getPage).toHaveBeenCalledWith(
      "conv-1",
      expect.objectContaining({ limit: INITIAL_WINDOW_ITEMS }),
    );
    expect(client.getQueryData(["session", "conv-1"])).toEqual(session("fresh"));
  });

  it("retries a failed read without repeating the other one", async () => {
    getSession.mockRejectedValueOnce(new Error("503")).mockResolvedValue(session("fresh"));

    await fetchConversationSnapshot(client, "conv-1", policy());

    expect(getSession).toHaveBeenCalledTimes(2);
    expect(getPage).toHaveBeenCalledTimes(1);
  });

  it("drops a session read it started when that read times out, so the retry reads afresh", async () => {
    const signals: AbortSignal[] = [];
    getSession.mockImplementation((_id, options) => {
      if (options?.signal) signals.push(options.signal);
      return signals.length === 1 ? new Promise(() => {}) : Promise.resolve(session("fresh"));
    });

    const snapshot = await fetchConversationSnapshot(client, "conv-1", policy({ timeOut: [0] }));

    expect(snapshot.session).toEqual(session("fresh"));
    expect(signals).toHaveLength(2);
    expect(signals[0]?.aborted).toBe(true);
  });

  it("reads directly after timing out on another caller's read, and leaves that read running", async () => {
    let otherCallerSignal: AbortSignal | undefined;
    // Another caller already has the session read in flight; it never answers.
    const otherCallersRead = client.fetchQuery({
      queryKey: ["session", "conv-1"],
      queryFn: ({ signal }) => {
        otherCallerSignal = signal;
        return new Promise<Session>(() => {});
      },
    });
    // Teardown cancels it; that rejection belongs to the other caller.
    otherCallersRead.catch(() => {});
    getSession.mockResolvedValue(session("direct"));

    const snapshot = await fetchConversationSnapshot(client, "conv-1", policy({ timeOut: [0] }));

    expect(snapshot.session).toEqual(session("direct"));
    expect(getSession).toHaveBeenCalledTimes(1);
    expect(client.getQueryData(["session", "conv-1"])).toEqual(session("direct"));
    expect(otherCallerSignal?.aborted).toBe(false);
  });
});
