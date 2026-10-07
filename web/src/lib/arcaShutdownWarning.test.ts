import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { isArcaHost } from "./arcaHost";
import {
  dateKey,
  dismissToday,
  isArcaWarningStorageKey,
  isDismissedToday,
  isWarnedToday,
  isOptedOut,
  isWarningWindow,
  markWarnedToday,
  offersWorkweek,
  optOut,
  resetArcaWarningStorageForTests,
} from "./arcaShutdownWarning";

const MON_459PM = new Date(2026, 9, 5, 16, 59);
const MON_5PM = new Date(2026, 9, 5, 17, 0);
const WED_5PM = new Date(2026, 9, 7, 17, 0);
const THU_5PM = new Date(2026, 9, 8, 17, 0);

beforeEach(() => {
  localStorage.clear();
  resetArcaWarningStorageForTests();
});
afterEach(() => vi.restoreAllMocks());

describe("Arca shutdown rules", () => {
  it("listens only to Arca warning and host-id storage changes", () => {
    expect(isArcaWarningStorageKey("omnigent:arca-shutdown:warned")).toBe(true);
    expect(isArcaWarningStorageKey("omnigent:arca-host-id")).toBe(true);
    expect(isArcaWarningStorageKey("unrelated-setting")).toBe(false);
  });

  it("warns at 17:00 on weekdays through late evening, but not on weekends", () => {
    expect(isWarningWindow(MON_459PM)).toBe(false);
    expect(isWarningWindow(MON_5PM)).toBe(true);
    expect(isWarningWindow(new Date(2026, 9, 9, 23, 30))).toBe(true);
    expect(isWarningWindow(new Date(2026, 9, 10, 17, 0))).toBe(false);
    expect(isWarningWindow(new Date(2026, 9, 11, 17, 0))).toBe(false);
  });

  it("offers workweek only Monday through Wednesday", () => {
    expect(offersWorkweek(MON_5PM)).toBe(true);
    expect(offersWorkweek(WED_5PM)).toBe(true);
    expect(offersWorkweek(THU_5PM)).toBe(false);
    expect(offersWorkweek(new Date(2026, 9, 9, 17, 0))).toBe(false);
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
    markWarnedToday(monday);
    expect(isDismissedToday(monday)).toBe(true);
    expect(isWarnedToday(monday)).toBe(true);
    expect(isDismissedToday(tuesday)).toBe(false);
    expect(isWarnedToday(tuesday)).toBe(false);
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
    dismissToday(MON_5PM);
    markWarnedToday(MON_5PM);
    optOut();
    expect(isDismissedToday(MON_5PM)).toBe(true);
    expect(isWarnedToday(MON_5PM)).toBe(true);
    expect(isOptedOut()).toBe(true);
  });

  it("prefers an in-memory write over an older stored day", () => {
    localStorage.setItem("omnigent:arca-shutdown:warned", "2026-10-05");
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("storage unavailable");
    });
    const tuesday = new Date(2026, 9, 6, 17, 0);
    markWarnedToday(tuesday);
    expect(isWarnedToday(tuesday)).toBe(true);
  });
});
