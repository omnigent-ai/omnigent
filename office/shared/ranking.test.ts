import { describe, expect, it } from "vitest";
import type { RoomSummary } from "./types.ts";
import {
  BASEMENT_AFTER_SECONDS,
  compareRooms,
  computePlacement,
  explainRank,
  inactivityAnchor,
  isBasementEligible,
  placementsEqual,
  prunePlacement,
} from "./ranking.ts";

const DAY = 24 * 60 * 60;
const NOW = 1_800_000_000;

function room(projectId: string, overrides: Partial<RoomSummary> = {}): RoomSummary {
  return {
    projectId,
    name: projectId,
    theme: null,
    agentProblemCount: 0,
    createdAt: NOW - 100 * DAY,
    priority: "normal",
    keepUpstairs: false,
    latestInteractionAt: NOW - DAY,
    firstSyncAt: NOW - 100 * DAY,
    historyComplete: true,
    pendingRequestCount: 0,
    workerCount: 1,
    activeWorkerCount: 0,
    ...overrides,
  };
}

describe("compareRooms", () => {
  it("orders by priority, then recency, then creation, then id", () => {
    const rooms = [
      room("normal-recent", { latestInteractionAt: NOW - 1 }),
      room("high-old", { priority: "high", latestInteractionAt: NOW - 50 * DAY }),
      room("critical", { priority: "critical", latestInteractionAt: null }),
      room("normal-unknown-new", { latestInteractionAt: null, createdAt: NOW - DAY }),
      room("normal-unknown-old-b", { latestInteractionAt: null, createdAt: NOW - 2 * DAY }),
      room("normal-unknown-old-a", { latestInteractionAt: null, createdAt: NOW - 2 * DAY }),
    ];
    expect([...rooms].sort(compareRooms).map((r) => r.projectId)).toEqual([
      "critical",
      "high-old",
      "normal-recent",
      "normal-unknown-new",
      "normal-unknown-old-a",
      "normal-unknown-old-b",
    ]);
  });
});

describe("basement eligibility", () => {
  it("moves a Normal project down exactly 30x24 hours after its last interaction", () => {
    const last = NOW - BASEMENT_AFTER_SECONDS;
    expect(isBasementEligible(room("p", { latestInteractionAt: last + 1 }), NOW)).toBe(false);
    expect(isBasementEligible(room("p", { latestInteractionAt: last }), NOW)).toBe(true);
  });

  it.each([
    ["High priority", { priority: "high" as const }],
    ["Critical priority", { priority: "critical" as const }],
    ["Keep upstairs", { keepUpstairs: true }],
    ["a pending request", { pendingRequestCount: 1 }],
  ])("never moves down with %s", (_label, overrides) => {
    expect(isBasementEligible(room("p", { latestInteractionAt: NOW - 90 * DAY, ...overrides }), NOW)).toBe(false);
  });

  it("measures a project without interactions from its creation", () => {
    const fresh = room("p", { latestInteractionAt: null, createdAt: NOW - 10 * DAY });
    expect(inactivityAnchor(fresh)).toBe(NOW - 10 * DAY);
    expect(isBasementEligible(fresh, NOW)).toBe(false);
  });

  it("never treats missing history as zero activity: the first sync grants a full grace period", () => {
    const incomplete = room("p", {
      latestInteractionAt: null,
      createdAt: NOW - 400 * DAY,
      historyComplete: false,
      firstSyncAt: NOW - 5 * DAY,
    });
    expect(isBasementEligible(incomplete, NOW)).toBe(false);
    // Older history discovered later never shortens the grace period.
    expect(isBasementEligible({ ...incomplete, latestInteractionAt: NOW - 200 * DAY }, NOW)).toBe(false);
  });
});

describe("computePlacement", () => {
  it("packs upstairs rooms with the highest rank on the highest floor and basement rooms from B1", () => {
    const rooms = [
      ...["a", "b", "c", "d"].map((id, index) => room(id, { latestInteractionAt: NOW - index })),
      room("old-1", { latestInteractionAt: NOW - 40 * DAY }),
      room("old-2", { latestInteractionAt: NOW - 41 * DAY }),
    ];
    expect(computePlacement(rooms, NOW)).toEqual({
      upstairs: [["d"], ["a", "b", "c"]],
      basement: [["old-1", "old-2"]],
    });
  });

  it("detects a waiting placement and prunes removed projects", () => {
    const shown = { upstairs: [["a", "gone"]], basement: [] };
    const pruned = prunePlacement(shown, new Set(["a"]));
    expect(pruned).toEqual({ upstairs: [["a"]], basement: [] });
    expect(placementsEqual(pruned, { upstairs: [["a"]], basement: [] })).toBe(true);
    expect(placementsEqual(pruned, { upstairs: [], basement: [["a"]] })).toBe(false);
  });
});

describe("explainRank", () => {
  it("explains an upstairs Normal project's remaining grace", () => {
    const subject = room("p", { latestInteractionAt: NOW - 12 * DAY });
    const explanation = explainRank(subject, [subject], NOW, false);
    expect(explanation.headline).toBe("Normal · Upstairs");
    expect(explanation.details.join(" ")).toContain("12 days ago");
    expect(explanation.details.join(" ")).toContain("Basement eligibility begins in 18 days");
  });

  it("explains a basement project and how to return", () => {
    const subject = room("p", { latestInteractionAt: NOW - 41 * DAY });
    const explanation = explainRank(subject, [subject], NOW, true);
    expect(explanation.headline).toBe("Normal · Basement");
    expect(explanation.details.join(" ")).toContain("No human interaction for 41 days");
    expect(explanation.details.join(" ")).toContain("newer placement is waiting");
  });

  it("states position within a High priority", () => {
    const first = room("first", { priority: "high", latestInteractionAt: NOW - 1 });
    const second = room("second", { priority: "high", latestInteractionAt: NOW - 2 });
    const explanation = explainRank(second, [first, second, room("other")], NOW, false);
    expect(explanation.headline).toBe("High · #2 of 2 High projects");
    expect(explanation.details).toContain("High projects always remain upstairs.");
  });
});
