// Tests for the RRULE builder's Hourly minute-of-hour handling.
//
// The broad buildRRule/validateSchedule coverage lives in scheduleText.test.ts
// (it exercises every preset + Custom frequency). This file adds the case that
// closes a specific bug: the Hourly preset must honor the chosen minute-of-hour
// rather than hard-coding :00, so "Hourly at :30" actually fires at :30.

import { describe, expect, it } from "vitest";
import {
  buildRRule,
  DEFAULT_SCHEDULE_MODEL,
  formatTimeOfDay24,
  parseRRuleToScheduleModel,
  validateSchedule,
  type ScheduleModel,
} from "./scheduleBuilder";

/** A schedule model with overrides on top of the default. */
function model(overrides: Partial<ScheduleModel>): ScheduleModel {
  return { ...DEFAULT_SCHEDULE_MODEL, ...overrides };
}

describe("buildRRule — Hourly minute-of-hour", () => {
  it("fires on the hour when minute is 0", () => {
    expect(buildRRule(model({ preset: "hourly", minute: 0 }))).toBe("FREQ=HOURLY;BYMINUTE=0");
  });

  it("honors a non-zero snapped minute (30 → :30, not the hard-coded :00)", () => {
    expect(buildRRule(model({ preset: "hourly", minute: 30 }))).toBe("FREQ=HOURLY;BYMINUTE=30");
  });
});

describe("parseRRuleToScheduleModel", () => {
  it("round-trips daily rules into the visible schedule controls", () => {
    expect(parseRRuleToScheduleModel("FREQ=DAILY;BYHOUR=14;BYMINUTE=30")).toMatchObject({
      preset: "daily",
      hour: 14,
      minute: 30,
    });
  });

  it("recognizes weekdays as the dedicated preset", () => {
    expect(
      parseRRuleToScheduleModel("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=8;BYMINUTE=0"),
    ).toMatchObject({
      preset: "weekdays",
      hour: 8,
      minute: 0,
    });
  });

  it("round-trips weekly day selections", () => {
    expect(parseRRuleToScheduleModel("FREQ=WEEKLY;BYDAY=WE,MO;BYHOUR=9;BYMINUTE=15")).toMatchObject(
      {
        preset: "weekly",
        hour: 9,
        minute: 15,
        weekdays: ["MO", "WE"],
      },
    );
  });

  it("round-trips hourly minute selections", () => {
    expect(parseRRuleToScheduleModel("FREQ=HOURLY;BYMINUTE=45")).toMatchObject({
      preset: "hourly",
      minute: 45,
    });
  });

  it("rejects interval rules the visible form cannot represent", () => {
    expect(parseRRuleToScheduleModel("FREQ=DAILY;INTERVAL=2;BYHOUR=9;BYMINUTE=0")).toBeNull();
  });

  it("round-trips non-quarter-hour daily minutes", () => {
    expect(parseRRuleToScheduleModel("FREQ=DAILY;BYHOUR=17;BYMINUTE=7")).toMatchObject({
      preset: "daily",
      hour: 17,
      minute: 7,
    });
  });

  it("round-trips non-quarter-hour hourly minute selections", () => {
    expect(parseRRuleToScheduleModel("FREQ=HOURLY;BYMINUTE=7")).toMatchObject({
      preset: "hourly",
      minute: 7,
    });
  });
});

describe("parseTimeOfDayInput", () => {
  it("accepts common 12-hour and 24-hour time input", async () => {
    const { parseTimeOfDayInput } = await import("./scheduleBuilder");
    expect(parseTimeOfDayInput("5:00 PM")).toEqual({ hour: 17, minute: 0 });
    expect(parseTimeOfDayInput("5:07 PM")).toEqual({ hour: 17, minute: 7 });
    expect(parseTimeOfDayInput("17:07")).toEqual({ hour: 17, minute: 7 });
  });

  it("rejects invalid times", async () => {
    const { parseTimeOfDayInput } = await import("./scheduleBuilder");
    expect(parseTimeOfDayInput("25:00")).toBeNull();
    expect(parseTimeOfDayInput("5:99 PM")).toBeNull();
    expect(parseTimeOfDayInput("not a time")).toBeNull();
  });
});

describe("parseMinuteOfHourInput", () => {
  it("accepts any minute in an hour", async () => {
    const { parseMinuteOfHourInput } = await import("./scheduleBuilder");
    expect(parseMinuteOfHourInput(":00")).toBe(0);
    expect(parseMinuteOfHourInput("7")).toBe(7);
    expect(parseMinuteOfHourInput(":59")).toBe(59);
  });

  it("rejects invalid minute input", async () => {
    const { parseMinuteOfHourInput } = await import("./scheduleBuilder");
    expect(parseMinuteOfHourInput(":60")).toBeNull();
    expect(parseMinuteOfHourInput("abc")).toBeNull();
  });
});

describe("formatTimeOfDay24", () => {
  it("formats a strict zero-padded 24-hour HH:MM", () => {
    expect(formatTimeOfDay24(9, 0)).toBe("09:00");
    expect(formatTimeOfDay24(0, 0)).toBe("00:00");
    expect(formatTimeOfDay24(17, 5)).toBe("17:05");
    expect(formatTimeOfDay24(23, 59)).toBe("23:59");
  });
});

describe("validateSchedule — active range", () => {
  it("accepts a null active range (unrestricted)", () => {
    expect(validateSchedule(model({ preset: "hourly", activeRange: null }))).toBeNull();
  });

  it("accepts a valid range, including an overnight one", () => {
    expect(
      validateSchedule(model({ preset: "hourly", activeRange: { start: "09:00", end: "17:00" } })),
    ).toBeNull();
    expect(
      validateSchedule(model({ preset: "hourly", activeRange: { start: "22:00", end: "06:00" } })),
    ).toBeNull();
  });

  it("rejects an unparseable start or end", () => {
    expect(
      validateSchedule(model({ preset: "hourly", activeRange: { start: "9:00", end: "17:00" } })),
    ).toMatch(/valid start and end/i);
    expect(
      validateSchedule(model({ preset: "hourly", activeRange: { start: "09:00", end: "not-a-time" } })),
    ).toMatch(/valid start and end/i);
    expect(
      validateSchedule(model({ preset: "hourly", activeRange: { start: "24:00", end: "17:00" } })),
    ).toMatch(/valid start and end/i);
  });

  it("rejects a range whose start equals its end", () => {
    expect(
      validateSchedule(model({ preset: "hourly", activeRange: { start: "09:00", end: "09:00" } })),
    ).toMatch(/must differ/i);
  });
});
