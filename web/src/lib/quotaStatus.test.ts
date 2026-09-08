import { describe, expect, it } from "vitest";

import {
  formatDuration,
  formatResetsIn,
  formatShare,
  formatUsedPercent,
  formatWindowScope,
  parseQuotaStatus,
  usedFraction,
  type QuotaWorkstream,
} from "./quotaStatus";

function workstream(overrides: Partial<QuotaWorkstream> = {}): QuotaWorkstream {
  return {
    id: "agent-infra",
    weight: 1,
    explicitSharePpm: null,
    active: true,
    borrowAfterSeconds: 300,
    lastSeenAt: null,
    activeReservations: 0,
    activeEstimatedPpm: 0,
    oldestActiveAgeSeconds: null,
    ...overrides,
  };
}

describe("parseQuotaStatus", () => {
  it("maps the server payload onto camel-cased fields", () => {
    const status = parseQuotaStatus({
      generated_at: 100,
      observed_at: 101,
      active_reservations: 3,
      windows: [
        {
          provider: "anthropic",
          lane: "claude-max",
          limit_id: "claude",
          window_name: "five_hour",
          model_scope: "*",
          used_ppm: 160000,
          window_seconds: 18000,
          resets_at: 500,
          hard_allowed: null,
          source: "claude-native-usage",
          observed_at: 99,
          burst_factor: 1.58,
        },
      ],
      workstreams: [
        {
          id: "agent-infra",
          weight: 2,
          explicit_share_ppm: null,
          active: 1,
          borrow_after_seconds: 300,
          last_seen_at: 90,
          active_reservations: 3,
          active_estimated_ppm: 25584,
          oldest_active_age_seconds: 1739,
        },
      ],
      burst: { initial_burst_factor: 2, max_burst_factor: null, adaptive_enabled: true },
    });

    expect(status.windows[0]).toMatchObject({
      limitId: "claude",
      windowName: "five_hour",
      usedPpm: 160000,
      burstFactor: 1.58,
    });
    expect(status.workstreams[0]).toMatchObject({
      weight: 2,
      active: true,
      activeReservations: 3,
      oldestActiveAgeSeconds: 1739,
    });
    expect(status.burst).toEqual({
      initialBurstFactor: 2,
      maxBurstFactor: null,
      adaptiveEnabled: true,
    });
  });

  it("drops malformed rows instead of rendering placeholder entries", () => {
    const status = parseQuotaStatus({
      windows: ["nope", null, { provider: "anthropic" }],
      workstreams: [{ id: "" }, "nope", { id: "kept" }],
      burst: null,
    });

    expect(status.windows).toHaveLength(1);
    expect(status.windows[0].lane).toBe("unknown");
    expect(status.workstreams.map((row) => row.id)).toEqual(["kept"]);
    expect(status.burst.adaptiveEnabled).toBeNull();
  });

  it("rejects a payload that is not an object", () => {
    expect(() => parseQuotaStatus(null)).toThrow(/Invalid quota status/);
    expect(() => parseQuotaStatus("nope")).toThrow(/Invalid quota status/);
  });
});

describe("formatting", () => {
  it("keeps one decimal only where a rounded percent would read as zero", () => {
    expect(formatUsedPercent(3000)).toBe("0.3%");
    expect(formatUsedPercent(0)).toBe("0%");
    expect(formatUsedPercent(160000)).toBe("16%");
    expect(formatUsedPercent(1_000_000)).toBe("100%");
  });

  it("clamps the bar fraction to the 0-1 range", () => {
    expect(usedFraction(-5)).toBe(0);
    expect(usedFraction(500_000)).toBe(0.5);
    // The controller can report a window as over-consumed; the bar stays full.
    expect(usedFraction(1_500_000)).toBe(1);
  });

  it("formats durations at second, minute, hour and day scales", () => {
    expect(formatDuration(45)).toBe("45s");
    expect(formatDuration(750)).toBe("12m");
    expect(formatDuration(12000)).toBe("3h 20m");
    expect(formatDuration(10800)).toBe("3h");
    expect(formatDuration(187200)).toBe("2d 4h");
    expect(formatDuration(-5)).toBe("0s");
  });

  it("reports a passed reset as due rather than a negative countdown", () => {
    expect(formatResetsIn(1000, 100)).toBe("in 15m");
    expect(formatResetsIn(100, 1000)).toBe("due");
    expect(formatResetsIn(null, 100)).toBeNull();
  });

  it("hides the model scope when it covers every model", () => {
    const base = parseQuotaStatus({
      windows: [{ limit_id: "claude", model_scope: "*" }],
    }).windows[0];
    expect(formatWindowScope(base)).toBe("claude");
    expect(formatWindowScope({ ...base, modelScope: "fable" })).toBe("claude · fable");
  });
});

describe("formatShare", () => {
  it("prefers an explicit share over a derived one", () => {
    const rows = [workstream({ id: "a", explicitSharePpm: 300000 }), workstream({ id: "b" })];
    expect(formatShare(rows[0], rows)).toBe("30%");
  });

  it("splits what explicit shares leave over, by weight", () => {
    const rows = [
      workstream({ id: "explicit", explicitSharePpm: 500000 }),
      workstream({ id: "heavy", weight: 3 }),
      workstream({ id: "light", weight: 1 }),
    ];
    // 50% remains, split 3:1.
    expect(formatShare(rows[1], rows)).toBe("~38%");
    expect(formatShare(rows[2], rows)).toBe("~13%");
  });

  it("shows a raw weight for an idle bucket, which claims no live share", () => {
    const rows = [workstream({ id: "idle", active: false, weight: 2 }), workstream({ id: "busy" })];
    expect(formatShare(rows[0], rows)).toBe("weight 2");
  });
});
