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
  parseRRuleToScheduleModel,
  type ScheduleModel,
  validateSchedule,
  parseUntil,
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

  it("opens interval rules in the custom editor", () => {
    expect(parseRRuleToScheduleModel("FREQ=DAILY;INTERVAL=2;BYHOUR=9;BYMINUTE=0")).toMatchObject({
      preset: "custom",
      customFreq: "daily",
      interval: 2,
    });
  });

  it.each([
    { customFreq: "hourly", interval: 3, minute: 7 },
    { customFreq: "daily", interval: 2 },
    { customFreq: "weekly", interval: 3, weekdays: ["MO", "FR"] },
    { customFreq: "monthly", interval: 1, monthDays: [1, 15, 31] },
    { customFreq: "yearly", interval: 2, month: 6, monthDays: [1, 15] },
  ] as Partial<ScheduleModel>[])(
    "round-trips custom recurrence %j with each ending",
    (overrides) => {
      for (const end of [
        undefined,
        { type: "count", count: 5 },
        { type: "until", until: "20271231T155959Z" },
      ] as ScheduleModel["end"][]) {
        const rule = buildRRule(model({ preset: "custom", ...overrides, end }));
        const parsed = parseRRuleToScheduleModel(rule);
        expect(parsed).not.toBeNull();
        expect(buildRRule(parsed!)).toBe(rule);
      }
    },
  );

  it.each([
    "FREQ=DAILY;BYHOUR=9;BYMINUTE=0;BYSECOND=30",
    "FREQ=WEEKLY;BYDAY=1MO;BYHOUR=9;BYMINUTE=0",
    "FREQ=DAILY;BYHOUR=9;BYMINUTE=0;INTERVAL=367",
    "FREQ=DAILY;BYHOUR=9;BYMINUTE=0;COUNT=1",
    "FREQ=DAILY;BYHOUR=9;BYMINUTE=0;BYHOUR=10",
    "FREQ=DAILY;BYHOUR=9;BYMINUTE=0;COUNT=3;UNTIL=20271231T155959Z",
    "FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=32;BYHOUR=9;BYMINUTE=0",
  ])("does not silently discard unsupported fields: %s", (rule) => {
    expect(parseRRuleToScheduleModel(rule)).toBeNull();
  });

  it("validates bounds and rejects impossible cutoff dates", () => {
    expect(validateSchedule(model({ preset: "custom", interval: 367 }))).not.toBeNull();
    expect(
      validateSchedule(model({ preset: "custom", customFreq: "monthly", monthDays: [32] })),
    ).not.toBeNull();
    expect(validateSchedule(model({ end: { type: "count", count: 1 } }))).not.toBeNull();
    expect(parseUntil("20270230T235959Z")).toBeNull();
    expect(parseUntil("20270228T235959Z")?.toISOString()).toBe("2027-02-28T23:59:59.000Z");
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
