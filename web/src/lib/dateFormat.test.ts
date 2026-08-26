import { describe, expect, it } from "vitest";
import { formatMonthDay, formatMonthDayYear, formatTime, isSameLocalDay } from "./dateFormat";

// The cached formatters must stay output-identical to the per-call
// `toLocale*String(undefined, opts)` they replaced — that equivalence is the
// whole basis for caching them.
describe("cached formatters match toLocale*String", () => {
  const dates = [
    new Date("2024-01-05T00:07:00"),
    new Date("2024-07-04T13:45:00"),
    new Date("2023-12-31T23:59:00"),
  ];

  it.each(dates)("formatTime(%s)", (date) => {
    expect(formatTime(date)).toBe(
      date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }),
    );
  });

  it.each(dates)("formatMonthDay(%s)", (date) => {
    expect(formatMonthDay(date)).toBe(
      date.toLocaleDateString(undefined, { month: "short", day: "numeric" }),
    );
  });

  it.each(dates)("formatMonthDayYear(%s)", (date) => {
    expect(formatMonthDayYear(date)).toBe(
      date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }),
    );
  });
});

describe("isSameLocalDay", () => {
  it("matches two instants on the same local day", () => {
    expect(isSameLocalDay(new Date("2024-03-04T00:00:00"), new Date("2024-03-04T23:59:59"))).toBe(
      true,
    );
  });

  it("separates adjacent local days", () => {
    expect(isSameLocalDay(new Date("2024-03-04T23:59:59"), new Date("2024-03-05T00:00:00"))).toBe(
      false,
    );
  });

  it("separates the same day-of-month in different months and years", () => {
    expect(isSameLocalDay(new Date("2024-03-04T12:00:00"), new Date("2024-04-04T12:00:00"))).toBe(
      false,
    );
    expect(isSameLocalDay(new Date("2023-03-04T12:00:00"), new Date("2024-03-04T12:00:00"))).toBe(
      false,
    );
  });

  it("agrees with the toDateString comparison it replaced", () => {
    const a = new Date("2024-03-04T08:00:00");
    const b = new Date("2024-03-04T20:00:00");
    const c = new Date("2024-03-05T08:00:00");
    expect(isSameLocalDay(a, b)).toBe(a.toDateString() === b.toDateString());
    expect(isSameLocalDay(a, c)).toBe(a.toDateString() === c.toDateString());
  });
});
