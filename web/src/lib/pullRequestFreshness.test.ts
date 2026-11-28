import { describe, expect, it } from "vitest";

import {
  freshnessAge,
  freshnessLabel,
  pullRequestFreshness,
  type PullRequestFreshnessSource,
} from "./pullRequestFreshness";

const NOW = new Date(2026, 10, 19, 15, 0).getTime();
const MINUTE = 60_000;

function source(overrides: Partial<PullRequestFreshnessSource>): PullRequestFreshnessSource {
  return {
    updatedAt: NOW - 5 * MINUTE,
    restored: true,
    refreshing: false,
    failed: false,
    ...overrides,
  };
}

describe("pullRequestFreshness", () => {
  it("shows nothing once every read is this run's", () => {
    expect(pullRequestFreshness([])).toBeNull();
    expect(pullRequestFreshness([source({ restored: false })])).toBeNull();
    expect(pullRequestFreshness([source({ updatedAt: 0 })])).toBeNull();
  });

  it("names the oldest kept read, refreshing while any kept read is", () => {
    expect(
      pullRequestFreshness([
        source({ updatedAt: NOW - 12 * MINUTE }),
        source({ updatedAt: NOW - 30 * MINUTE, refreshing: true }),
        // This run's read is fresh, whatever its age, so it doesn't count.
        source({ updatedAt: NOW - 90 * MINUTE, restored: false, failed: true }),
      ]),
    ).toEqual({ updatedAt: NOW - 30 * MINUTE, refreshing: true, failed: false });
  });

  it("says a kept read's refresh failed", () => {
    expect(pullRequestFreshness([source({ failed: true })])?.failed).toBe(true);
  });
});

describe("freshnessAge", () => {
  it.each([
    [30_000, null],
    [MINUTE, "1 min ago"],
    [59 * MINUTE, "59 min ago"],
    [60 * MINUTE, "1 hr ago"],
    [14 * 60 * MINUTE, "14 hr ago"],
    [24 * 60 * MINUTE, "yesterday"],
  ])("reads an age of %i ms as %s", (age, expected) => {
    expect(freshnessAge(NOW - age, NOW)).toBe(expected);
  });

  it("gives older data its date, with the year once it's another year's", () => {
    expect(freshnessAge(new Date(2026, 10, 3, 9, 0).getTime(), NOW)).toMatch(/^on .*3/);
    expect(freshnessAge(new Date(2025, 10, 3, 9, 0).getTime(), NOW)).toMatch(/2025/);
  });

  it("shows no age for a time ahead of this Mac's clock, or none at all", () => {
    expect(freshnessAge(NOW + 10 * MINUTE, NOW)).toBeNull();
    expect(freshnessAge(Number.NaN, NOW)).toBeNull();
    expect(freshnessAge(0, NOW)).toBeNull();
  });
});

describe("freshnessLabel", () => {
  it("reads the age, the refresh and the exact time", () => {
    const label = freshnessLabel(
      { updatedAt: NOW - 12 * MINUTE, refreshing: true, failed: false },
      NOW,
    );
    expect(label?.text).toBe("Updated 12 min ago");
    expect(label?.status).toBe("refreshing");
    expect(label?.exact).toMatch(/^Updated /);
    expect(
      freshnessLabel({ updatedAt: NOW - 12 * MINUTE, refreshing: false, failed: true }, NOW)
        ?.status,
    ).toBe("couldn't refresh");
    expect(freshnessLabel({ updatedAt: NOW, refreshing: true, failed: false }, NOW)).toBeNull();
  });
});
