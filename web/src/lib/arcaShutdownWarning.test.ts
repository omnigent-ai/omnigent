import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  dateKey,
  dismissToday,
  isArcaHost,
  isDismissedToday,
  isToastedToday,
  isOptedOut,
  isWarningWindow,
  markToastedToday,
  offersWorkweek,
  optOut,
} from "./arcaShutdownWarning";

beforeEach(() => localStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe("Arca shutdown rules", () => {
  it("warns at 17:00 on weekdays through late evening, but not on weekends", () => {
    expect(isWarningWindow(new Date(2026, 9, 5, 16, 59))).toBe(false);
    expect(isWarningWindow(new Date(2026, 9, 5, 17, 0))).toBe(true);
    expect(isWarningWindow(new Date(2026, 9, 9, 23, 30))).toBe(true);
    expect(isWarningWindow(new Date(2026, 9, 10, 17, 0))).toBe(false);
    expect(isWarningWindow(new Date(2026, 9, 11, 17, 0))).toBe(false);
  });

  it("offers workweek only Monday through Wednesday", () => {
    for (const day of [5, 6, 7]) expect(offersWorkweek(new Date(2026, 9, day))).toBe(true);
    for (const day of [8, 9, 10, 11]) expect(offersWorkweek(new Date(2026, 9, day))).toBe(false);
  });

  it("recognizes seeded names and stored ids but excludes renamed and arclet names", () => {
    expect(isArcaHost({ host_id: "a", name: "jackson's arca" }, null)).toBe(true);
    expect(isArcaHost({ host_id: "a", name: "renamed" }, "a")).toBe(true);
    expect(isArcaHost({ host_id: "a", name: "renamed" }, null)).toBe(false);
    expect(isArcaHost({ host_id: "a", name: "jackson's arclet" }, null)).toBe(false);
  });

  it("uses the local date and resets daily keys at midnight", () => {
    const monday = new Date(2026, 9, 5, 23, 59);
    const tuesday = new Date(2026, 9, 6, 0, 0);
    expect(dateKey(monday)).toBe("2026-10-05");
    expect(dateKey(tuesday)).toBe("2026-10-06");
    dismissToday(monday);
    markToastedToday(monday);
    expect(isDismissedToday(monday)).toBe(true);
    expect(isToastedToday(monday)).toBe(true);
    expect(isDismissedToday(tuesday)).toBe(false);
    expect(isToastedToday(tuesday)).toBe(false);
    optOut();
    expect(isOptedOut()).toBe(true);
  });

  it("keeps choices in this tab when localStorage is unavailable", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("storage unavailable");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("storage unavailable");
    });
    const now = new Date(2026, 9, 5, 17, 0);
    dismissToday(now);
    markToastedToday(now);
    optOut();
    expect(isDismissedToday(now)).toBe(true);
    expect(isToastedToday(now)).toBe(true);
    expect(isOptedOut()).toBe(true);
  });
});
