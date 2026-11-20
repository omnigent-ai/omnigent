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
  it("preserves ended rules and normalizes equivalent day sets when parsing", () => {
    const parsed = parseRRuleToScheduleModel(
      "FREQ=WEEKLY;BYDAY=FR,MO,MO;BYHOUR=9;BYMINUTE=0;UNTIL=20200101T235959Z",
    );
    expect(parsed).not.toBeNull();
    expect(buildRRule(parsed!)).toBe(
      "FREQ=WEEKLY;BYDAY=MO,FR;BYHOUR=9;BYMINUTE=0;UNTIL=20200101T235959Z",
    );
  });
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

  it.each([
    { customFreq: "minutely", interval: 10 },
    { customFreq: "minutely", interval: 11 },
    { customFreq: "minutely", interval: 366 * 60 },
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
        expect(parsed).toMatchObject({
          preset: "custom",
          customFreq: overrides.customFreq,
          interval: overrides.interval,
        });
        expect(buildRRule(parsed!)).toBe(rule);
      }
    },
  );

  it.each([
    "FREQ=MINUTELY;INTERVAL=10;BYMINUTE=5",
    "FREQ=MINUTELY;INTERVAL=10;BYHOUR=9",
    "FREQ=MINUTELY;INTERVAL=10;BYSECOND=30",
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

describe("hourly schedules limited to hours and days", () => {
  const businessHours = model({
    preset: "hourly",
    minute: 0,
    hourWindow: { start: 9, end: 17 },
    hourlyDays: ["MO", "TU", "WE", "TH", "FR"],
  });

  it("lists the window's hours and days in the rule", () => {
    expect(buildRRule(businessHours)).toBe(
      "FREQ=HOURLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9,10,11,12,13,14,15,16,17;BYMINUTE=0",
    );
    expect(buildRRule(model({ preset: "hourly", minute: 30, hourlyDays: ["SU", "SA"] }))).toBe(
      "FREQ=HOURLY;BYDAY=SA,SU;BYMINUTE=30",
    );
  });

  it("steps a custom interval from the first hour of the window", () => {
    const rule = buildRRule(
      model({
        preset: "custom",
        customFreq: "hourly",
        interval: 3,
        minute: 15,
        hourWindow: { start: 8, end: 18 },
      }),
    );
    expect(rule).toBe("FREQ=HOURLY;BYHOUR=8,11,14,17;BYMINUTE=15");
    expect(parseRRuleToScheduleModel(rule)).toMatchObject({
      preset: "custom",
      customFreq: "hourly",
      interval: 3,
      hourWindow: { start: 8, end: 17 },
    });
  });

  it("keeps the unrestricted hourly rules unchanged", () => {
    expect(buildRRule(model({ preset: "hourly", minute: 5 }))).toBe("FREQ=HOURLY;BYMINUTE=5");
    expect(buildRRule(model({ preset: "custom", customFreq: "hourly", interval: 2 }))).toBe(
      "FREQ=HOURLY;INTERVAL=2;BYMINUTE=0",
    );
  });

  it.each([
    "FREQ=HOURLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9,10,11,12,13,14,15,16,17;BYMINUTE=0",
    // The shape agents create through the scheduled-task tool.
    "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9,10,11,12,13,14,15,16,17;BYMINUTE=0",
  ])("opens %s in the hourly controls", (rule) => {
    const parsed = parseRRuleToScheduleModel(rule);
    expect(parsed).toMatchObject({
      preset: "hourly",
      minute: 0,
      hourWindow: { start: 9, end: 17 },
      hourlyDays: ["MO", "TU", "WE", "TH", "FR"],
    });
    expect(buildRRule(parsed!)).toBe(buildRRule(businessHours));
  });

  it("round-trips days and hours with each ending", () => {
    for (const end of [
      undefined,
      { type: "count", count: 5 },
      { type: "until", until: "20271231T155959Z" },
    ] as ScheduleModel["end"][]) {
      const rule = buildRRule({ ...businessHours, end });
      expect(buildRRule(parseRRuleToScheduleModel(rule)!)).toBe(rule);
    }
    const daysOnly = "FREQ=HOURLY;INTERVAL=2;BYDAY=SA,SU;BYMINUTE=0";
    expect(parseRRuleToScheduleModel(daysOnly)).toMatchObject({
      preset: "custom",
      interval: 2,
      hourlyDays: ["SA", "SU"],
    });
    expect(buildRRule(parseRRuleToScheduleModel(daysOnly)!)).toBe(daysOnly);
  });

  it("treats every hour of the day as no hour limit", () => {
    const allHours = Array.from({ length: 24 }, (_, hour) => hour).join(",");
    const parsed = parseRRuleToScheduleModel(`FREQ=HOURLY;BYHOUR=${allHours};BYMINUTE=0`);
    expect(buildRRule(parsed!)).toBe("FREQ=HOURLY;BYMINUTE=0");
  });

  it.each([
    "FREQ=HOURLY;BYHOUR=9,10,14;BYMINUTE=0",
    "FREQ=HOURLY;INTERVAL=2;BYHOUR=9,11;BYMINUTE=0",
    "FREQ=HOURLY;BYHOUR=9,10;BYMINUTE=0,30",
    "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO;BYHOUR=9,10;BYMINUTE=0",
    "FREQ=WEEKLY;BYHOUR=9,10;BYMINUTE=0",
    "FREQ=HOURLY;BYHOUR=9,24;BYMINUTE=0",
    "FREQ=HOURLY;BYDAY=1MO;BYMINUTE=0",
  ])("leaves rules without a visual equivalent unsupported: %s", (rule) => {
    expect(parseRRuleToScheduleModel(rule)).toBeNull();
  });

  it("validates the window and days", () => {
    expect(validateSchedule(businessHours)).toBeNull();
    expect(validateSchedule({ ...businessHours, hourWindow: { start: 17, end: 9 } })).toBe(
      "Choose an end hour at or after the start hour.",
    );
    expect(validateSchedule({ ...businessHours, hourlyDays: [] })).toBe(
      "Pick at least one day of the week.",
    );
  });
});

describe("visual schedule validation", () => {
  it.each([
    { customFreq: "weekly", interval: 209, weekdays: ["MO"] },
    { customFreq: "yearly", month: 2, monthDays: [30, 31] },
    { customFreq: "yearly", month: 4, monthDays: [31] },
    { customFreq: "monthly", monthDays: Array.from({ length: 25 }, (_, index) => index + 1) },
    { customFreq: "yearly", interval: 9 },
    { customFreq: "monthly", interval: 97 },
    { customFreq: "yearly", interval: 5, month: 2, monthDays: [29] },
  ] as Partial<ScheduleModel>[])("rejects backend-incompatible recurrence %j", (overrides) => {
    expect(validateSchedule(model({ preset: "custom", ...overrides }))).not.toBeNull();
  });

  it.each([
    { customFreq: "weekly", interval: 209, weekdays: ["FR"] },
    { customFreq: "yearly", month: 2, monthDays: [29, 30] },
    { customFreq: "yearly", interval: 4, month: 2, monthDays: [29] },
    { customFreq: "yearly", interval: 8, hour: 0, month: 1, monthDays: [1] },
    { customFreq: "monthly", interval: 96, hour: 0, monthDays: [1] },
    { customFreq: "monthly", interval: 95, monthDays: [31] },
    { customFreq: "yearly", interval: 366, monthDays: [1, 15] },
    { customFreq: "monthly", interval: 366, monthDays: [1, 15] },
    { customFreq: "hourly", interval: 366 },
  ] as Partial<ScheduleModel>[])("retains supported recurrence %j", (overrides) => {
    expect(validateSchedule(model({ preset: "custom", ...overrides }))).toBeNull();
  });

  it("distinguishes invalid day numbers from an empty selection", () => {
    expect(
      validateSchedule(model({ preset: "custom", customFreq: "monthly", monthDays: [32] })),
    ).toBe("Days of the month must be whole numbers from 1 to 31.");
    expect(
      validateSchedule(model({ preset: "custom", customFreq: "monthly", monthDays: [] })),
    ).toBe("Pick at least one day of the month.");
  });

  it("requires a future cutoff for submission without preventing parsing ended tasks", () => {
    const schedule = model({ end: { type: "until", until: "20300610T235959Z" } });
    expect(validateSchedule(schedule, { now: new Date("2030-06-11T00:00:00Z") })).toBe(
      "Choose an end date in the future.",
    );
    expect(validateSchedule(schedule, { now: new Date("2030-06-10T23:59:59Z") })).not.toBeNull();
    expect(validateSchedule(schedule, { now: new Date("2030-06-10T23:59:58Z") })).toBeNull();
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

describe("minute cadence policy", () => {
  it.each([9, 10, 11, 30, 59, 60, 366 * 60, 366 * 60 + 1])(
    "validates %i minutes by execution target and session mode",
    (interval) => {
      const schedule = model({ preset: "custom", customFreq: "minutely", interval });
      for (const executionTarget of ["connected_host", "managed_sandbox"] as const) {
        for (const sessionMode of ["new", "continue"] as const) {
          const minimum =
            executionTarget === "connected_host" || sessionMode === "continue" ? 10 : 60;
          expect(
            validateSchedule(schedule, { executionTarget, sessionMode, subhourSupported: true }) ===
              null,
          ).toBe(interval >= minimum && interval <= 366 * 60);
          expect(validateSchedule(schedule, { executionTarget, sessionMode }) === null).toBe(
            interval >= 60 && interval <= 366 * 60,
          );
        }
      }
      expect(
        validateSchedule(schedule, { sessionMode: "continue", subhourSupported: true }) === null,
      ).toBe(interval >= 10 && interval <= 366 * 60);
      expect(validateSchedule(schedule) === null).toBe(interval >= 60 && interval <= 366 * 60);
      const newSession = validateSchedule(schedule, { sessionMode: "new", subhourSupported: true });
      expect(newSession === null).toBe(interval >= 60 && interval <= 366 * 60);
      if (interval < 60) {
        expect(newSession).toBe(
          "New sandbox sessions require schedules of at least 60 minutes. Continue in the same session or use a connected host for shorter intervals.",
        );
        for (const sessionMode of ["new", "continue"] as const) {
          expect(validateSchedule(schedule, { sessionMode })).toBe(
            "This server requires schedules of at least 60 minutes.",
          );
        }
      }
      expect(validateSchedule(schedule, { sessionMode: "continue" }) === null).toBe(
        interval >= 60 && interval <= 366 * 60,
      );
    },
  );
});
