import { describe, expect, it } from "vitest";
import { dateInTimezone, endOfDayInTimezone, commonTimezones } from "./timezones";

describe("schedule timezones", () => {
  it("includes UTC and saved timezone aliases", () => {
    expect(commonTimezones("US/Pacific")).toEqual(expect.arrayContaining(["UTC", "US/Pacific"]));
  });

  it.each([
    ["2027-06-10", "Asia/Singapore", "2027-06-10T15:59:59.000Z"],
    ["2027-06-10", "Asia/Kathmandu", "2027-06-10T18:14:59.000Z"],
    ["2027-03-14", "America/New_York", "2027-03-15T03:59:59.000Z"],
    ["2027-11-07", "America/New_York", "2027-11-08T04:59:59.000Z"],
    ["2027-06-10", "UTC", "2027-06-10T23:59:59.000Z"],
  ])("ends %s in %s at %s", (day, timezone, expected) => {
    const date = endOfDayInTimezone(day, timezone)!;
    expect(date.toISOString()).toBe(expected);
    expect(dateInTimezone(date, timezone)).toBe(day);
  });

  it("rejects invalid and skipped calendar dates", () => {
    expect(endOfDayInTimezone("2027-02-30", "UTC")).toBeNull();
    expect(endOfDayInTimezone("", "UTC")).toBeNull();
    expect(endOfDayInTimezone("2011-12-30", "Pacific/Apia")).toBeNull();
  });
});
