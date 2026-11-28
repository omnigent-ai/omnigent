import { describe, expect, it } from "vitest";

import { retrySummary } from "./AutomationRetryField";

describe("retrySummary", () => {
  it.each([
    [0, 300, "Off"],
    [1, 60, "1 retry · 1 minute apart"],
    [3, 300, "3 retries · 5 minutes apart"],
    [2, 3600, "2 retries · 1 hour apart"],
    [1, 90, "1 retry · 90 seconds apart"],
  ])("describes %i retries %i seconds apart", (maxRetries, delay, expected) => {
    expect(retrySummary(maxRetries, delay)).toBe(expected);
  });
});
