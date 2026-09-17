import { expect, it } from "vitest";
import { refreshInterval } from "./appConfig";

it("accepts positive millisecond intervals and falls back on invalid configuration", () => {
  expect(refreshInterval("120000", 60000)).toBe(120000);
  for (const value of [undefined, "", "-1", "1.5", "NaN", "Infinity", "oops"])
    expect(refreshInterval(value, 60000)).toBe(60000);
});

it("allows polling to be explicitly disabled", () => {
  expect(refreshInterval("0", 60000)).toBe(false);
  expect(refreshInterval("false", 60000)).toBe(false);
});
