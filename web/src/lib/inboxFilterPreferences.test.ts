import { afterEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_INBOX_FILTER, readInboxFilter, writeInboxFilter } from "./inboxFilterPreferences";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("inboxFilterPreferences", () => {
  it('defaults to "all" when nothing is stored', () => {
    expect(DEFAULT_INBOX_FILTER).toBe("all");
    expect(readInboxFilter()).toBe("all");
  });

  it("round-trips every filter value", () => {
    for (const filter of ["all", "unread", "awaiting"] as const) {
      writeInboxFilter(filter);
      expect(readInboxFilter()).toBe(filter);
    }
  });

  it("falls back to the default for an unknown stored value", () => {
    // A tab this build doesn't render would leave the viewer on an empty list.
    localStorage.setItem("omnigent:inbox-filter", "mentions");
    expect(readInboxFilter()).toBe("all");
  });

  it("falls back to the default when storage throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(readInboxFilter()).toBe("all");
  });
});
