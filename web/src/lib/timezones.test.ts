import { describe, expect, it } from "vitest";
import { dateInTimezone, endOfDayInTimezone, commonTimezones } from "./timezones";

describe("schedule timezones", () => {
  it("includes UTC and saved timezone aliases", () => {
    expect(commonTimezones("US/Pacific")).toEqual(expect.arrayContaining(["UTC", "US/Pacific"]));
  });

  it.each([
    ["2027-06-10", "Asia/Singapore", "2027-06-10T15:59:59.000Z"],
    ["2027-06-10", "Asia/Kathmandu", "2027-06-10T18:14:59.000Z"],
    ["2024-03-10", "America/New_York", "2024-03-11T03:59:59.000Z"],
    ["2024-11-03", "America/New_York", "2024-11-04T04:59:59.000Z"],
    ["2027-06-10", "UTC", "2027-06-10T23:59:59.000Z"],
    ["2023-04-01", "America/Santiago", "2023-04-02T03:59:59.000Z"],
    ["2023-03-25", "America/Asuncion", "2023-03-26T03:59:59.000Z"],
    ["2022-10-29", "Asia/Beirut", "2022-10-29T21:59:59.000Z"],
    ["2009-06-19", "Asia/Dhaka", "2009-06-19T16:59:59.000Z"],
    ["0002-06-10", "UTC", "0002-06-10T23:59:59.000Z"],
    ["0020-06-10", "UTC", "0020-06-10T23:59:59.000Z"],
    ["9999-12-31", "Pacific/Kiritimati", "9999-12-31T09:59:59.000Z"],
    ["9999-12-31", "America/New_York", "+010000-01-01T04:59:59.000Z"],
  ])("ends %s in %s at %s", (day, timezone, expected) => {
    const date = endOfDayInTimezone(day, timezone)!;
    expect(date.toISOString()).toBe(expected);
    expect(dateInTimezone(date, timezone)).toBe(day);
    expect(dateInTimezone(new Date(date.getTime() + 1000), timezone)).not.toBe(day);
  });

  it("rejects invalid and skipped calendar dates", () => {
    expect(endOfDayInTimezone("2027-02-30", "UTC")).toBeNull();
    expect(endOfDayInTimezone("", "UTC")).toBeNull();
    expect(endOfDayInTimezone("2011-12-30", "Pacific/Apia")).toBeNull();
  });

  it("handles unsupported browser timezones and invalid instants without throwing", () => {
    expect(dateInTimezone(new Date(), "Unknown/Timezone")).toBe("");
    expect(endOfDayInTimezone("2027-06-10", "Unknown/Timezone")).toBeNull();
    expect(dateInTimezone(new Date(NaN), "UTC")).toBe("");
  });
});
