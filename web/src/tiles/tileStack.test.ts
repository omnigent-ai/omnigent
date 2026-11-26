import { afterEach, describe, expect, it } from "vitest";
import {
  isQuiet,
  parkTile,
  partitionStack,
  pruneStack,
  QUIET_STACK_AFTER_MS,
  unstackTile,
  writeTileStack,
} from "./tileStack";

const NOW_MS = 1_800_000_000_000;
const ago = (ms: number) => (NOW_MS - ms) / 1000;

function row(id: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    status: "idle" as const,
    updated_at: ago(60_000),
    pending_elicitations_count: 0,
    ...overrides,
  };
}

afterEach(() => localStorage.clear());

describe("isQuiet", () => {
  it("needs idle, not waiting, and a day without activity", () => {
    const old = ago(QUIET_STACK_AFTER_MS + 1);
    expect(isQuiet(row("a", { updated_at: old }), NOW_MS)).toBe(true);
    expect(isQuiet(row("a"), NOW_MS)).toBe(false);
    expect(isQuiet(row("a", { updated_at: old, status: "running" }), NOW_MS)).toBe(false);
    expect(isQuiet(row("a", { updated_at: old, pending_elicitations_count: 1 }), NOW_MS)).toBe(
      false,
    );
  });
});

describe("partitionStack", () => {
  const old = ago(QUIET_STACK_AFTER_MS + 1);
  const rows = [
    row("busy"),
    row("parked"),
    row("quiet", { updated_at: old }),
    row("kept", { updated_at: old }),
    row("waiting-parked", { pending_elicitations_count: 1 }),
  ];

  it("stacks parked and quiet tiles, but never one that is waiting on you", () => {
    const { onGrid, stacked } = partitionStack(
      rows,
      { parked: ["parked", "waiting-parked"], kept: ["kept"] },
      NOW_MS,
    );
    expect(onGrid.map((r) => r.id)).toEqual(["busy", "kept", "waiting-parked"]);
    expect(stacked.map((r) => r.id)).toEqual(["parked", "quiet"]);
  });
});

describe("park and unstack", () => {
  it("parks once, and unstacking a quiet tile keeps it out of the Stack", () => {
    let state = parkTile({ parked: [], kept: ["a"] }, "a");
    state = parkTile(state, "a");
    expect(state).toEqual({ parked: ["a"], kept: [] });
    expect(unstackTile(state, "a", false)).toEqual({ parked: [], kept: [] });
    expect(unstackTile(state, "a", true)).toEqual({ parked: [], kept: ["a"] });
  });

  it("prunes ids that are no longer sessions", () => {
    expect(pruneStack({ parked: ["a", "gone"], kept: ["gone", "b"] }, new Set(["a", "b"]))).toEqual(
      { parked: ["a"], kept: ["b"] },
    );
  });
});

describe("persistence", () => {
  it("stores the Stack as JSON", () => {
    writeTileStack({ parked: ["a"], kept: ["b"] });
    expect(JSON.parse(localStorage.getItem("omnigent:tiles-stack") ?? "{}")).toEqual({
      parked: ["a"],
      kept: ["b"],
    });
  });
});
