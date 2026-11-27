import { afterEach, describe, expect, it, vi } from "vitest";
import { z } from "zod";

import { createWindowDrafts } from "./reviewDraftStorage";

const schema = z.object({ body: z.string() });
const drafts = (namespace = "test") =>
  createWindowDrafts({ namespace, schema, storage: () => window.sessionStorage });

afterEach(() => {
  vi.restoreAllMocks();
  window.sessionStorage.clear();
});

describe("review drafts kept for this window", () => {
  it("keeps one entry per draft, read back after a reload", () => {
    const before = drafts();
    expect(before.save("pr/1", { body: "One" })).toBe("saved");
    expect(before.save("pr/2", { body: "Two" })).toBe("saved");
    expect(window.sessionStorage.length).toBe(2);
    expect(drafts().loadAll()).toEqual(
      expect.arrayContaining([
        { key: "pr/1", value: { body: "One" } },
        { key: "pr/2", value: { body: "Two" } },
      ]),
    );
    expect(before.save("pr/1", null)).toBe("saved");
    expect(drafts().loadAll()).toEqual([{ key: "pr/2", value: { body: "Two" } }]);
  });

  it("skips another store's drafts and anything it can't read", () => {
    drafts("other").save("pr", { body: "Elsewhere" });
    window.sessionStorage.setItem("omnigent:review-drafts:v1:test:broken", "{not json");
    window.sessionStorage.setItem(
      "omnigent:review-drafts:v1:test:wrong",
      JSON.stringify({ v: 1, key: "wrong", value: { body: 3 } }),
    );
    expect(drafts().loadAll()).toEqual([]);
    drafts().save("pr", { body: "Readable" });
    expect(drafts().loadAll()).toEqual([{ key: "pr", value: { body: "Readable" } }]);
  });

  it("never reads drafts other windows share in localStorage", () => {
    window.localStorage.setItem(
      "omnigent:review-drafts:v1:test:pr",
      JSON.stringify({ v: 1, key: "pr", value: { body: "Shared" } }),
    );
    expect(drafts().loadAll()).toEqual([]);
    window.localStorage.clear();
  });

  it("fails closed when storage becomes unreachable after this window kept a draft", () => {
    let reachable = true;
    const kept = createWindowDrafts({
      namespace: "test",
      schema,
      storage: () => (reachable ? window.sessionStorage : null),
    });
    expect(kept.save("pr", { body: "Kept" })).toBe("saved");
    reachable = false;
    expect(kept.save("pr", { body: "Held" })).toBe("failed");
    expect(kept.save("pr", null)).toBe("failed");
    // A window that found a kept draft on load counts too.
    reachable = true;
    const reloaded = createWindowDrafts({
      namespace: "test",
      schema,
      storage: () => (reachable ? window.sessionStorage : null),
    });
    expect(reloaded.loadAll()).toHaveLength(1);
    reachable = false;
    expect(reloaded.save("pr", null)).toBe("failed");
  });

  it("says when storage refuses a write, or there's no storage", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("The quota has been exceeded.", "QuotaExceededError");
    });
    expect(drafts().save("pr", { body: "Too big" })).toBe("failed");
    expect(
      createWindowDrafts({ namespace: "test", schema, storage: () => null }).save("pr", null),
    ).toBe("unavailable");
  });
});
