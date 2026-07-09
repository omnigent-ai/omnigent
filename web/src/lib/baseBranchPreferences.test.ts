import { afterEach, describe, expect, it, vi } from "vitest";
import {
  readDefaultBaseBranch,
  subscribeDefaultBaseBranch,
  writeDefaultBaseBranch,
} from "./baseBranchPreferences";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("baseBranchPreferences", () => {
  it("returns null when nothing is stored", () => {
    // No default set — read must say so (null) so the composer leaves the
    // base-branch field blank rather than inventing a branch.
    expect(readDefaultBaseBranch()).toBeNull();
  });

  it("round-trips a written branch name", () => {
    writeDefaultBaseBranch("main");
    // The exact branch written must come back — this is what pre-fills the
    // base-branch field on the next new-branch entry.
    expect(readDefaultBaseBranch()).toBe("main");
  });

  it("trims surrounding whitespace before storing", () => {
    writeDefaultBaseBranch("  develop  ");
    expect(readDefaultBaseBranch()).toBe("develop");
  });

  it("clears the preference when written blank", () => {
    writeDefaultBaseBranch("main");
    writeDefaultBaseBranch("   ");
    // A blank value turns auto-fill off — the slot is emptied, not stored as "".
    expect(readDefaultBaseBranch()).toBeNull();
  });

  it("overwrites the previous value", () => {
    writeDefaultBaseBranch("main");
    writeDefaultBaseBranch("develop");
    // Only the latest value matters; the preference is a single slot.
    expect(readDefaultBaseBranch()).toBe("develop");
  });

  it("never throws when storage is inaccessible", () => {
    // Private-mode / quota failures surface as throws from the Storage API.
    // Both helpers must swallow them — a broken preference must not break
    // settings.
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    expect(() => writeDefaultBaseBranch("main")).not.toThrow();
    expect(readDefaultBaseBranch()).toBeNull();
  });

  it("notifies subscribers on a same-tab write and stops after unsubscribe", () => {
    const onChange = vi.fn();
    const unsubscribe = subscribeDefaultBaseBranch(onChange);

    // A same-tab write fires no `storage` event, so the subscription is the
    // only way a mounted composer learns of the change.
    writeDefaultBaseBranch("main");
    expect(onChange).toHaveBeenCalledTimes(1);

    writeDefaultBaseBranch("develop");
    expect(onChange).toHaveBeenCalledTimes(2);

    unsubscribe();
    writeDefaultBaseBranch("release/1.0");
    // No further calls once unsubscribed.
    expect(onChange).toHaveBeenCalledTimes(2);
  });

  it("notifies subscribers on a cross-tab storage event for this key only", () => {
    const onChange = vi.fn();
    subscribeDefaultBaseBranch(onChange);

    // Another tab writing this key surfaces as a `storage` event here.
    window.dispatchEvent(new StorageEvent("storage", { key: "omnigent:default-base-branch" }));
    expect(onChange).toHaveBeenCalledTimes(1);

    // An unrelated key must not wake the subscriber.
    window.dispatchEvent(new StorageEvent("storage", { key: "omnigent:something-else" }));
    expect(onChange).toHaveBeenCalledTimes(1);
  });
});
