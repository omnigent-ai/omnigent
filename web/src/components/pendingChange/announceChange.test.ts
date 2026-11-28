import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ANNOUNCE_REFILL_DELAY_MS, announceChange } from "./announceChange";

function announced(): string | null {
  return document.querySelector('[data-testid="change-announcer"]')?.textContent ?? null;
}

describe("announceChange", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
  });

  it("leaves the region empty past the current task, so a repeated message is heard again", async () => {
    announceChange("Added the label.");
    vi.advanceTimersByTime(ANNOUNCE_REFILL_DELAY_MS);
    expect(announced()).toBe("Added the label.");

    announceChange("Added the label.");
    await Promise.resolve();
    expect(announced()).toBe("");
    vi.advanceTimersByTime(ANNOUNCE_REFILL_DELAY_MS);
    expect(announced()).toBe("Added the label.");
  });

  it("says only the newest of two quick messages", () => {
    announceChange("Adding the label…");
    announceChange("Added the label.");
    vi.advanceTimersByTime(ANNOUNCE_REFILL_DELAY_MS - 1);
    expect(announced()).toBe("");
    vi.advanceTimersByTime(1);
    expect(announced()).toBe("Added the label.");
    vi.advanceTimersByTime(ANNOUNCE_REFILL_DELAY_MS);
    expect(announced()).toBe("Added the label.");
  });
});
