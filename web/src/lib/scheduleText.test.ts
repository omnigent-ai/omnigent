// Tests for the client-side schedule text + next-run derivation, and the RRULE
// builder + validation that feed them. Covers every shape the create form can
// produce: the simple presets (Hourly/Daily/Weekdays/Weekly) and all five
// Custom frequencies (with INTERVAL, multi weekday/month-day selects, and
// yearly BYMONTH), plus the 1-hour-floor validation.

import { describe, expect, it } from "vitest";
import { describeSchedule, formatClockTime, nextRunAtMs } from "./scheduleText";
import {
  buildRRule,
  DEFAULT_SCHEDULE_MODEL,
  validateSchedule,
  type ScheduleModel,
} from "./scheduleBuilder";

/** Convenience: a model with overrides on top of the default. */
function model(overrides: Partial<ScheduleModel>): ScheduleModel {
  return { ...DEFAULT_SCHEDULE_MODEL, ...overrides };
}

describe("formatClockTime", () => {
  it("formats 12-hour times with AM/PM", () => {
    expect(formatClockTime(8, 0)).toBe("8:00 AM");
    expect(formatClockTime(0, 0)).toBe("12:00 AM");
    expect(formatClockTime(12, 30)).toBe("12:30 PM");
    expect(formatClockTime(13, 5)).toBe("1:05 PM");
    expect(formatClockTime(23, 59)).toBe("11:59 PM");
  });
});

describe("buildRRule — simple presets", () => {
  it("hourly (no inputs)", () => {
    expect(buildRRule(model({ preset: "hourly" }))).toBe("FREQ=HOURLY;BYMINUTE=0");
  });

  it("daily (time only)", () => {
    expect(buildRRule(model({ preset: "daily", hour: 9, minute: 0 }))).toBe(
      "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
    );
  });

  it("weekdays (Mon–Fri + time)", () => {
    expect(buildRRule(model({ preset: "weekdays", hour: 8, minute: 0 }))).toBe(
      "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=8;BYMINUTE=0",
    );
  });

  it("weekly (multi weekday, sorted to calendar order)", () => {
    expect(
      buildRRule(model({ preset: "weekly", weekdays: ["FR", "MO"], hour: 8, minute: 0 })),
    ).toBe("FREQ=WEEKLY;BYDAY=MO,FR;BYHOUR=8;BYMINUTE=0");
  });

  it("weekly falls back to MO when the (invalid) empty set is built", () => {
    // validateSchedule blocks submit, but buildRRule must still emit a legal rule.
    expect(buildRRule(model({ preset: "weekly", weekdays: [], hour: 8, minute: 0 }))).toBe(
      "FREQ=WEEKLY;BYDAY=MO;BYHOUR=8;BYMINUTE=0",
    );
  });
});

describe("buildRRule — custom frequencies with INTERVAL", () => {
  it("custom hourly: every X hours at minute Y", () => {
    expect(
      buildRRule(model({ preset: "custom", customFreq: "hourly", interval: 2, minute: 15 })),
    ).toBe("FREQ=HOURLY;INTERVAL=2;BYMINUTE=15");
  });

  it("custom daily: every X days at h:m", () => {
    expect(
      buildRRule(
        model({ preset: "custom", customFreq: "daily", interval: 3, hour: 9, minute: 30 }),
      ),
    ).toBe("FREQ=DAILY;INTERVAL=3;BYHOUR=9;BYMINUTE=30");
  });

  it("custom weekly: every X weeks on <days> at h:m", () => {
    expect(
      buildRRule(
        model({
          preset: "custom",
          customFreq: "weekly",
          interval: 2,
          weekdays: ["MO", "WE", "FR"],
          hour: 8,
          minute: 0,
        }),
      ),
    ).toBe("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE,FR;BYHOUR=8;BYMINUTE=0");
  });

  it("custom monthly: every X months on <multi days> at h:m", () => {
    expect(
      buildRRule(
        model({
          preset: "custom",
          customFreq: "monthly",
          interval: 3,
          monthDays: [15, 1],
          hour: 6,
          minute: 0,
        }),
      ),
    ).toBe("FREQ=MONTHLY;INTERVAL=3;BYMONTHDAY=1,15;BYHOUR=6;BYMINUTE=0");
  });

  it("custom yearly: every X years in month Y on <multi days> at h:m", () => {
    expect(
      buildRRule(
        model({
          preset: "custom",
          customFreq: "yearly",
          interval: 2,
          month: 3,
          monthDays: [1],
          hour: 9,
          minute: 0,
        }),
      ),
    ).toBe("FREQ=YEARLY;INTERVAL=2;BYMONTH=3;BYMONTHDAY=1;BYHOUR=9;BYMINUTE=0");
  });

  it("clamps a bad interval up to 1 in the built rule", () => {
    expect(buildRRule(model({ preset: "custom", customFreq: "daily", interval: 0 }))).toContain(
      "INTERVAL=1",
    );
  });

  it("allows day-of-month up to 31 (short months handled by rrule)", () => {
    expect(
      buildRRule(model({ preset: "custom", customFreq: "monthly", interval: 1, monthDays: [31] })),
    ).toContain("BYMONTHDAY=31");
  });
});

describe("validateSchedule — 1-hour floor + required selections", () => {
  it("accepts every simple preset", () => {
    expect(validateSchedule(model({ preset: "hourly" }))).toBeNull();
    expect(validateSchedule(model({ preset: "daily" }))).toBeNull();
    expect(validateSchedule(model({ preset: "weekdays" }))).toBeNull();
    expect(validateSchedule(model({ preset: "weekly", weekdays: ["MO"] }))).toBeNull();
  });

  it("rejects an empty weekly weekday set", () => {
    expect(validateSchedule(model({ preset: "weekly", weekdays: [] }))).toMatch(
      /at least one day/i,
    );
  });

  it("rejects a sub-1 interval (the 1h floor) on custom hourly", () => {
    expect(
      validateSchedule(model({ preset: "custom", customFreq: "hourly", interval: 0 })),
    ).toMatch(/at least 1/i);
  });

  it("rejects a non-integer / <1 interval for any custom frequency", () => {
    expect(validateSchedule(model({ preset: "custom", customFreq: "daily", interval: 0 }))).toMatch(
      /at least 1/i,
    );
    expect(
      validateSchedule(model({ preset: "custom", customFreq: "daily", interval: 1.5 })),
    ).toMatch(/whole number/i);
  });

  it("accepts a valid custom hourly interval (>=1 → within the floor)", () => {
    expect(
      validateSchedule(model({ preset: "custom", customFreq: "hourly", interval: 1 })),
    ).toBeNull();
    expect(
      validateSchedule(model({ preset: "custom", customFreq: "hourly", interval: 6 })),
    ).toBeNull();
  });

  it("requires at least one custom weekly weekday and monthly/yearly day", () => {
    expect(
      validateSchedule(
        model({ preset: "custom", customFreq: "weekly", interval: 1, weekdays: [] }),
      ),
    ).toMatch(/at least one day/i);
    expect(
      validateSchedule(
        model({ preset: "custom", customFreq: "monthly", interval: 1, monthDays: [] }),
      ),
    ).toMatch(/at least one day of the month/i);
    expect(
      validateSchedule(
        model({ preset: "custom", customFreq: "yearly", interval: 1, monthDays: [] }),
      ),
    ).toMatch(/at least one day of the month/i);
  });
});

describe("describeSchedule", () => {
  it("collapses the Mon–Fri set to Weekdays", () => {
    expect(describeSchedule("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=8;BYMINUTE=0")).toBe(
      "Weekdays at 8:00 AM",
    );
  });

  it("renders a daily rule", () => {
    expect(describeSchedule("FREQ=DAILY;BYHOUR=9;BYMINUTE=0")).toBe("Every day at 9:00 AM");
  });

  it("renders a specific weekly day and a multi-day set", () => {
    expect(describeSchedule("FREQ=WEEKLY;BYDAY=MO;BYHOUR=14;BYMINUTE=30")).toBe(
      "Weekly on Monday at 2:30 PM",
    );
    expect(describeSchedule("FREQ=WEEKLY;BYDAY=MO,FR;BYHOUR=8;BYMINUTE=0")).toBe(
      "Weekly on Monday and Friday at 8:00 AM",
    );
  });

  it("renders weekends", () => {
    expect(describeSchedule("FREQ=WEEKLY;BYDAY=SA,SU;BYHOUR=10;BYMINUTE=0")).toBe(
      "Weekends at 10:00 AM",
    );
  });

  it("renders plain hourly and interval-hourly", () => {
    expect(describeSchedule("FREQ=HOURLY;BYMINUTE=0")).toBe("Hourly");
    // interval-1 hourly with a non-zero BYMINUTE shows the minute (not bare "Hourly").
    expect(describeSchedule("FREQ=HOURLY;BYMINUTE=30")).toBe("Hourly at :30");
    expect(describeSchedule("FREQ=HOURLY;INTERVAL=2;BYMINUTE=15")).toBe("Every 2 hours at :15");
  });

  it("renders interval daily / weekly", () => {
    expect(describeSchedule("FREQ=DAILY;INTERVAL=3;BYHOUR=9;BYMINUTE=30")).toBe(
      "Every 3 days at 9:30 AM",
    );
    expect(describeSchedule("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;BYHOUR=8;BYMINUTE=0")).toBe(
      "Every 2 weeks on Monday and Wednesday at 8:00 AM",
    );
  });

  it("renders monthly with single + multi day, plain + interval", () => {
    expect(describeSchedule("FREQ=MONTHLY;BYMONTHDAY=1;BYHOUR=6;BYMINUTE=0")).toBe(
      "Monthly on the 1st at 6:00 AM",
    );
    expect(describeSchedule("FREQ=MONTHLY;INTERVAL=3;BYMONTHDAY=1,15;BYHOUR=6;BYMINUTE=0")).toBe(
      "Every 3 months on the 1st and 15th at 6:00 AM",
    );
  });

  it("renders yearly with month + day, plain + interval", () => {
    expect(describeSchedule("FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=1;BYHOUR=9;BYMINUTE=0")).toBe(
      "Yearly in March on the 1st at 9:00 AM",
    );
    expect(
      describeSchedule("FREQ=YEARLY;INTERVAL=2;BYMONTH=12;BYMONTHDAY=25;BYHOUR=9;BYMINUTE=0"),
    ).toBe("Every 2 years in December on the 25th at 9:00 AM");
  });

  it("falls back to the raw rule for an unparseable string", () => {
    expect(describeSchedule("not a rule")).toBe("not a rule");
  });

  it("round-trips every builder output to non-empty readable text", () => {
    const cases: ScheduleModel[] = [
      model({ preset: "hourly" }),
      model({ preset: "daily" }),
      model({ preset: "weekdays" }),
      model({ preset: "weekly", weekdays: ["TU", "TH"] }),
      model({ preset: "custom", customFreq: "hourly", interval: 4, minute: 0 }),
      model({ preset: "custom", customFreq: "daily", interval: 2 }),
      model({ preset: "custom", customFreq: "weekly", interval: 2, weekdays: ["MO"] }),
      model({ preset: "custom", customFreq: "monthly", interval: 1, monthDays: [1, 15] }),
      model({ preset: "custom", customFreq: "yearly", interval: 1, month: 6, monthDays: [1] }),
    ];
    for (const m of cases) {
      const text = describeSchedule(buildRRule(m));
      expect(text.length).toBeGreaterThan(0);
      // Never leaks the raw FREQ= rule as the "readable" text.
      expect(text.startsWith("FREQ=")).toBe(false);
    }
  });
});

describe("nextRunAtMs", () => {
  it("computes the next daily occurrence after now", () => {
    // 2026-01-01 06:00 UTC; a 9:00 UTC daily rule fires later the same day.
    const now = new Date("2026-01-01T06:00:00Z");
    const next = nextRunAtMs("FREQ=DAILY;BYHOUR=9;BYMINUTE=0", "UTC", now);
    expect(next).not.toBeNull();
    const d = new Date(next!);
    expect(d.getUTCHours()).toBe(9);
    expect(d.getUTCDate()).toBe(1);
  });

  it("rolls to the next day when today's time has passed", () => {
    const now = new Date("2026-01-01T12:00:00Z");
    const next = nextRunAtMs("FREQ=DAILY;BYHOUR=9;BYMINUTE=0", "UTC", now);
    expect(new Date(next!).getUTCDate()).toBe(2);
  });

  it("handles interval-daily (every 3 days)", () => {
    const now = new Date("2026-01-01T00:00:00Z");
    const next = nextRunAtMs("FREQ=DAILY;INTERVAL=3;BYHOUR=9;BYMINUTE=0", "UTC", now);
    expect(next).not.toBeNull();
    expect(new Date(next!).getUTCHours()).toBe(9);
  });

  it("handles yearly with BYMONTH + BYMONTHDAY", () => {
    const now = new Date("2026-01-01T00:00:00Z");
    const next = nextRunAtMs("FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=1;BYHOUR=9;BYMINUTE=0", "UTC", now);
    expect(next).not.toBeNull();
    const d = new Date(next!);
    expect(d.getUTCMonth()).toBe(2); // March (0-indexed)
    expect(d.getUTCDate()).toBe(1);
  });

  it("handles multi-day monthly (picks the nearest upcoming day)", () => {
    const now = new Date("2026-01-10T00:00:00Z");
    const next = nextRunAtMs("FREQ=MONTHLY;BYMONTHDAY=1,15;BYHOUR=9;BYMINUTE=0", "UTC", now);
    expect(next).not.toBeNull();
    // Next after the 10th is the 15th of the same month.
    expect(new Date(next!).getUTCDate()).toBe(15);
  });

  it("returns null for an unparseable rule", () => {
    expect(nextRunAtMs("garbage", "UTC")).toBeNull();
  });
});
