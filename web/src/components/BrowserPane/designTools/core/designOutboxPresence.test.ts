import "fake-indexeddb/auto";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DESIGN_OUTBOX_DATABASE,
  hasSavedDesignRequests,
  noteSavedDesignRequests,
} from "./designOutboxPresence";

const HINT = "omnigent.designOutbox.saved";

afterEach(async () => {
  vi.restoreAllMocks();
  localStorage.removeItem(HINT);
  await new Promise((resolve) => {
    indexedDB.deleteDatabase(DESIGN_OUTBOX_DATABASE).onsuccess = resolve;
  });
});

describe("design outbox presence", () => {
  it("follows the hint while there is one", async () => {
    noteSavedDesignRequests(true);
    expect(await hasSavedDesignRequests()).toBe(true);
    noteSavedDesignRequests(false);
    expect(localStorage.getItem(HINT)).toBe("0");
    expect(await hasSavedDesignRequests()).toBe(false);
  });

  it("without a hint it couldn't write, looks for the database itself", async () => {
    localStorage.setItem(HINT, "0");
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("Full", "QuotaExceededError");
    });
    noteSavedDesignRequests(true);
    // A stale "nothing saved" hint can't stand.
    expect(localStorage.getItem(HINT)).toBeNull();
    expect(await hasSavedDesignRequests()).toBe(false);
    await new Promise((resolve) => {
      const opening = indexedDB.open(DESIGN_OUTBOX_DATABASE, 1);
      opening.onsuccess = () => {
        opening.result.close();
        resolve(undefined);
      };
    });
    expect(await hasSavedDesignRequests()).toBe(true);
  });

  it("without a hint, loads the outbox when the databases can't be listed", async () => {
    vi.spyOn(indexedDB, "databases").mockRejectedValue(new DOMException("No", "UnknownError"));
    expect(await hasSavedDesignRequests()).toBe(true);
    vi.restoreAllMocks();
    // A browser without `databases()` at all.
    Object.defineProperty(indexedDB, "databases", { configurable: true, value: undefined });
    try {
      expect(await hasSavedDesignRequests()).toBe(true);
    } finally {
      Reflect.deleteProperty(indexedDB, "databases");
    }
    expect(await hasSavedDesignRequests()).toBe(false);
  });
});
