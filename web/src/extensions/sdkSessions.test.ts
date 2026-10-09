import { describe, expect, it, vi } from "vitest";
import {
  drainSessionPages,
  type ExtensionSessionPage,
  validateSessionPageLimit,
  type ExtensionSessionSummary,
  SESSIONS_LIST_ALL_MAX_RESTARTS,
  STALE_CURSOR_ERROR_CODE,
} from "../../../sdks/web-extension/src/sessions";

function session(id: string): ExtensionSessionSummary {
  return {
    id,
    title: id,
    status: "idle",
    titleProvisional: false,
    unread: false,
    workspace: null,
    gitBranch: null,
    projectId: null,
    createdAt: 1,
    updatedAt: 1,
  };
}

function failure(code: string, message: string): Error {
  return Object.assign(new Error(message), { code });
}

describe("drainSessionPages", () => {
  it("drains pages in server order and stops on hasMore false", async () => {
    const pages: ExtensionSessionPage[] = [
      { sessions: [session("one")], nextCursor: "cursor-1", hasMore: true },
      { sessions: [session("two")], nextCursor: "ignored", hasMore: false },
    ];
    const fetchPage = vi.fn(async () => pages.shift()!);

    const result = await drainSessionPages(fetchPage, failure);

    expect(result.map((item) => item.id)).toEqual(["one", "two"]);
    expect(fetchPage).toHaveBeenNthCalledWith(1, null);
    expect(fetchPage).toHaveBeenNthCalledWith(2, "cursor-1");
  });

  it("rejects a repeated or missing cursor", async () => {
    const repeated = vi.fn(async () => ({
      sessions: [],
      nextCursor: "same",
      hasMore: true,
    }));
    await expect(drainSessionPages(repeated, failure)).rejects.toMatchObject({
      code: "InvalidResponse",
    });

    await expect(
      drainSessionPages(async () => ({ sessions: [], nextCursor: null, hasMore: true }), failure),
    ).rejects.toMatchObject({ code: "InvalidResponse" });
  });

  it("bounds page and total-session counts", async () => {
    let page = 0;
    await expect(
      drainSessionPages(
        async () => ({
          sessions: [],
          nextCursor: `cursor-${page++}`,
          hasMore: true,
        }),
        failure,
      ),
    ).rejects.toMatchObject({ code: "LimitExceeded" });

    await expect(
      drainSessionPages(
        async () => ({
          sessions: Array.from({ length: 5_001 }, (_, index) => session(String(index))),
          nextCursor: null,
          hasMore: false,
        }),
        failure,
      ),
    ).rejects.toMatchObject({ code: "LimitExceeded" });
  });

  it("validates SDK page limits before making a request", () => {
    expect(validateSessionPageLimit(undefined, failure)).toBe(25);
    expect(validateSessionPageLimit(1, failure)).toBe(1);
    expect(validateSessionPageLimit(1_000, failure)).toBe(1_000);
    expect(() => validateSessionPageLimit(1_001, failure)).toThrow("session page limit");
  });

  it("restarts the walk from page 1 when a cursor goes stale", async () => {
    // A session deleted mid-walk kills the cursor; the retried walk must
    // return the full post-deletion list, not a truncated or doubled one.
    let attempt = 0;
    const fetchPage = vi.fn(async (after: string | null) => {
      if (attempt === 0 && after === "cursor-1") {
        attempt = 1;
        throw failure(STALE_CURSOR_ERROR_CODE, "cursor gone");
      }
      return after === null
        ? { sessions: [session("one")], nextCursor: "cursor-1", hasMore: true }
        : { sessions: [session("two")], nextCursor: null, hasMore: false };
    });

    const result = await drainSessionPages(fetchPage, failure);

    expect(result.map((item) => item.id)).toEqual(["one", "two"]);
    expect(fetchPage).toHaveBeenNthCalledWith(3, null);
  });

  it("gives up after a bounded number of stale-cursor restarts", async () => {
    const stale = vi.fn(async (after: string | null) => {
      if (after !== null) throw failure(STALE_CURSOR_ERROR_CODE, "cursor gone");
      return { sessions: [session("one")], nextCursor: "cursor-1", hasMore: true };
    });

    await expect(drainSessionPages(stale, failure)).rejects.toMatchObject({
      code: STALE_CURSOR_ERROR_CODE,
    });
    // One initial walk plus the bounded restarts, two calls each.
    expect(stale).toHaveBeenCalledTimes((SESSIONS_LIST_ALL_MAX_RESTARTS + 1) * 2);
  });

  it("propagates page failures unchanged", async () => {
    const original = Object.assign(new Error("offline"), { code: "Unavailable" });
    await expect(drainSessionPages(async () => Promise.reject(original), failure)).rejects.toBe(
      original,
    );
  });
});
