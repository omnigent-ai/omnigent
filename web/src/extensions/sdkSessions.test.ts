import { describe, expect, it, vi } from "vitest";
import {
  drainSessionPages,
  type ExtensionSessionPage,
  SessionRevisionTracker,
  validateSessionPageLimit,
  type ExtensionSessionSummary,
} from "../../../sdks/web-extension/src/sessions";

function session(id: string): ExtensionSessionSummary {
  return {
    id,
    title: id,
    status: "idle",
    workspace: null,
    createdAt: 1,
    updatedAt: 1,
  };
}

function failure(code: string, message: string): Error {
  return Object.assign(new Error(message), { code });
}

describe("SessionRevisionTracker", () => {
  it("ignores the initial revision and accepts only newer revisions", () => {
    const tracker = new SessionRevisionTracker();
    expect(tracker.accept({ revision: 4 })).toBe(false);
    expect(tracker.initialize({ revision: 4 }, failure)).toBe(false);
    expect(tracker.accept({ revision: 4 })).toBe(false);
    expect(tracker.accept({ revision: 5 })).toBe(true);
    expect(tracker.accept({ revision: 3 })).toBe(false);
    expect(tracker.accept({ sessionId: "hidden" })).toBe(false);
  });

  it("reports an event that races with subscription setup", () => {
    const tracker = new SessionRevisionTracker();
    expect(tracker.accept({ revision: 8 })).toBe(false);
    expect(tracker.initialize({ revision: 7 }, failure)).toBe(true);
  });

  it("rejects a malformed initial response", () => {
    const tracker = new SessionRevisionTracker();
    expect(() => tracker.initialize({ revision: -1 }, failure)).toThrow(
      "Session subscription revision is invalid",
    );
  });
});

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
    expect(() => validateSessionPageLimit(26, failure)).toThrow("session page limit");
  });

  it("propagates page failures unchanged", async () => {
    const original = Object.assign(new Error("offline"), { code: "Unavailable" });
    await expect(drainSessionPages(async () => Promise.reject(original), failure)).rejects.toBe(
      original,
    );
  });
});
