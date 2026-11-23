import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_INBOX_BADGE_COUNT,
  INBOX_BADGE_COUNT_STORAGE_KEY,
  readInboxBadgeCount,
  subscribeInboxBadgeCount,
  useInboxBadgeCount,
  writeInboxBadgeCount,
} from "./inboxBadgePreferences";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("inboxBadgePreferences", () => {
  it("defaults to counting everything the Inbox lists", () => {
    expect(DEFAULT_INBOX_BADGE_COUNT).toBe("all");
    expect(readInboxBadgeCount()).toBe("all");
  });

  it("stores attention and clears the key when set back to all", () => {
    expect(writeInboxBadgeCount("attention")).toBe(true);
    expect(localStorage.getItem(INBOX_BADGE_COUNT_STORAGE_KEY)).toBe("attention");
    expect(readInboxBadgeCount()).toBe("attention");

    expect(writeInboxBadgeCount("all")).toBe(true);
    expect(localStorage.getItem(INBOX_BADGE_COUNT_STORAGE_KEY)).toBeNull();
    expect(readInboxBadgeCount()).toBe("all");
  });

  it("reads any other stored value as all", () => {
    localStorage.setItem(INBOX_BADGE_COUNT_STORAGE_KEY, "approvals");
    expect(readInboxBadgeCount()).toBe("all");
  });

  it("never throws when storage is inaccessible", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    expect(writeInboxBadgeCount("attention")).toBe(false);
    expect(writeInboxBadgeCount("all")).toBe(false);
    expect(readInboxBadgeCount()).toBe("all");
  });

  it("tells subscribers about saves in this tab and changes from other tabs", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeInboxBadgeCount(listener);

    writeInboxBadgeCount("attention");
    expect(listener).toHaveBeenCalledTimes(1);

    window.dispatchEvent(new StorageEvent("storage", { key: INBOX_BADGE_COUNT_STORAGE_KEY }));
    expect(listener).toHaveBeenCalledTimes(2);

    window.dispatchEvent(new StorageEvent("storage", { key: "some-other-key" }));
    expect(listener).toHaveBeenCalledTimes(2);

    unsubscribe();
    writeInboxBadgeCount("all");
    expect(listener).toHaveBeenCalledTimes(2);
  });

  it("does not notify when storage refuses the save", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeInboxBadgeCount(listener);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    expect(writeInboxBadgeCount("attention")).toBe(false);
    expect(listener).not.toHaveBeenCalled();
    unsubscribe();
  });

  it("keeps the hook in step with saves", () => {
    const { result } = renderHook(() => useInboxBadgeCount());
    expect(result.current).toBe("all");

    act(() => {
      writeInboxBadgeCount("attention");
    });
    expect(result.current).toBe("attention");

    act(() => {
      writeInboxBadgeCount("all");
    });
    expect(result.current).toBe("all");
  });
});
