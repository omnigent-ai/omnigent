import { describe, expect, it } from "vitest";
import {
  absoluteTime,
  formatMessageTimestamp,
  longRelativeTime,
  relativeTime,
} from "./relativeTime";

const NOW = new Date("2026-05-28T12:00:00Z").getTime();
const MIN = 60_000;
const HR = 60 * MIN;
const DAY = 24 * HR;
const WEEK = 7 * DAY;
const MONTH = 30 * DAY;
const YEAR = 365 * DAY;

describe("relativeTime", () => {
  it("returns 'now' for diffs under one minute", () => {
    expect(relativeTime(NOW - 30_000, NOW)).toBe("now");
    expect(relativeTime(NOW - 59_000, NOW)).toBe("now");
  });

  it("uses 'Nm' for minutes (floor)", () => {
    expect(relativeTime(NOW - MIN, NOW)).toBe("1m");
    expect(relativeTime(NOW - 5 * MIN, NOW)).toBe("5m");
    expect(relativeTime(NOW - 59 * MIN, NOW)).toBe("59m");
  });

  it("uses 'Nh' for hours", () => {
    expect(relativeTime(NOW - HR, NOW)).toBe("1h");
    expect(relativeTime(NOW - 23 * HR, NOW)).toBe("23h");
  });

  it("uses 'Nd' for days", () => {
    expect(relativeTime(NOW - DAY, NOW)).toBe("1d");
    expect(relativeTime(NOW - 6 * DAY, NOW)).toBe("6d");
  });

  it("uses 'Nw' for weeks", () => {
    expect(relativeTime(NOW - WEEK, NOW)).toBe("1w");
    expect(relativeTime(NOW - 3 * WEEK, NOW)).toBe("3w");
  });

  it("uses 'Nmo' (not 'Nm') for months to disambiguate from minutes", () => {
    // The whole point of the 'mo' suffix: 1mo and 1m must not collide.
    expect(relativeTime(NOW - MONTH, NOW)).toBe("1mo");
    expect(relativeTime(NOW - 6 * MONTH, NOW)).toBe("6mo");
    expect(relativeTime(NOW - MONTH, NOW)).not.toBe("1m");
  });

  it("uses 'Ny' for years", () => {
    expect(relativeTime(NOW - YEAR, NOW)).toBe("1y");
    expect(relativeTime(NOW - 3 * YEAR, NOW)).toBe("3y");
  });

  it("clamps future timestamps to 'now' (no negative diffs)", () => {
    // Clock skew can briefly put the server ahead of the client;
    // surface no time rather than a negative.
    expect(relativeTime(NOW + 5_000, NOW)).toBe("now");
  });

  it("renders empty for a non-finite timestamp instead of 'NaNy'", () => {
    // A missing/malformed updated_at reaching the formatter must not fall
    // through to the years branch as "NaNy".
    expect(relativeTime(NaN, NOW)).toBe("");
    expect(relativeTime(Number(undefined), NOW)).toBe("");
    expect(absoluteTime(NaN)).toBe("");
  });

  it("rolls cleanly at unit boundaries", () => {
    // Just under a unit boundary keeps the smaller unit; at the
    // boundary, the next unit starts. Pins the floor semantics.
    expect(relativeTime(NOW - (HR - 1), NOW)).toBe("59m");
    expect(relativeTime(NOW - HR, NOW)).toBe("1h");
    expect(relativeTime(NOW - (DAY - 1), NOW)).toBe("23h");
    expect(relativeTime(NOW - DAY, NOW)).toBe("1d");
    expect(relativeTime(NOW - (WEEK - 1), NOW)).toBe("6d");
    expect(relativeTime(NOW - WEEK, NOW)).toBe("1w");
  });
});

describe("formatMessageTimestamp", () => {
  // Local times, so the stamps don't depend on the machine's time zone.
  const now = new Date(2026, 10, 12, 9, 30);
  // ICU may put a narrow no-break space before AM/PM.
  const stamp = (date: Date) =>
    formatMessageTimestamp(date.getTime() / 1000, now).replace(/\s/g, " ");

  it("is the time today, adds the day before today, and the year before this year", () => {
    expect(stamp(new Date(2026, 10, 12, 8, 5))).toBe("8:05 AM");
    expect(stamp(new Date(2026, 10, 11, 20, 56))).toBe("Nov 11, 8:56 PM");
    expect(stamp(new Date(2025, 10, 11, 20, 56))).toBe("Nov 11, 2025, 8:56 PM");
  });
});

describe("longRelativeTime", () => {
  it.each([
    ["seconds", NOW - 30_000, "30 seconds ago"],
    ["a minute at 60 seconds", NOW - MIN, "1 minute ago"],
    ["minutes", NOW - 5 * MIN, "5 minutes ago"],
    ["an hour at 60 minutes", NOW - HR, "1 hour ago"],
    ["hours", NOW - 3 * HR, "3 hours ago"],
    ["yesterday", NOW - DAY, "yesterday"],
    ["days", NOW - 4 * DAY, "4 days ago"],
    ["months", NOW - 90 * DAY, "3 months ago"],
    ["years, not dozens of months", NOW - 3 * 365 * DAY, "3 years ago"],
  ])("reads %s in a sentence", (_label, time, label) => {
    expect(longRelativeTime(time, NOW)).toBe(label);
  });

  // `Date.parse("")`, a missing timestamp, is NaN.
  it.each([NaN, Infinity])("has no label for the time %s", (time) => {
    expect(longRelativeTime(time, NOW)).toBe("");
    expect(longRelativeTime(NOW, time)).toBe("");
  });
});
