import { describe, expect, it } from "vitest";
import { tilesToggleDestination } from "@/shell/Sidebar";
import {
  isInteractiveTarget,
  parseTilesView,
  partitionActive,
  pickLiveIds,
  QUIET_AFTER_MS,
  resolveActiveId,
  tileStreamBudget,
} from "./tileSelection";

const NOW_MS = 1_800_000_000_000;
const secondsAgo = (ms: number) => (NOW_MS - ms) / 1000;

function row(id: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    status: "idle" as const,
    updated_at: secondsAgo(60_000),
    archived: false,
    pending_elicitations_count: 0,
    ...overrides,
  };
}

describe("partitionActive", () => {
  it("keeps running, waiting and recent sessions as tiles and the rest dormant", () => {
    const old = secondsAgo(QUIET_AFTER_MS + 1);
    const { active, dormant } = partitionActive(
      [
        row("recent"),
        row("running-old", { status: "running", updated_at: old }),
        row("waiting-old", { updated_at: old, pending_elicitations_count: 1 }),
        row("quiet", { updated_at: old }),
        row("archived", { archived: true }),
      ],
      NOW_MS,
    );
    expect(active.map((r) => r.id)).toEqual(["recent", "running-old", "waiting-old"]);
    expect(dormant.map((r) => r.id)).toEqual(["quiet"]);
  });

  it("gives each conversation one tile", () => {
    const { active } = partitionActive([row("a"), row("a"), row("b")], NOW_MS);
    expect(active.map((r) => r.id)).toEqual(["a", "b"]);
  });
});

describe("resolveActiveId", () => {
  const tiles = [row("a"), row("b", { pending_elicitations_count: 2 }), row("c")];

  it("honors the chosen tile while it exists", () => {
    expect(resolveActiveId(tiles, "c")).toBe("c");
  });

  it("falls back to the first tile needing you, then the first tile", () => {
    expect(resolveActiveId(tiles, "gone")).toBe("b");
    expect(resolveActiveId([row("a"), row("c")], null)).toBe("a");
    expect(resolveActiveId([], null)).toBeNull();
  });
});

describe("pickLiveIds", () => {
  it("always streams the active tile, then fills the budget in order", () => {
    expect([...pickLiveIds(["a", "b", "c", "d"], "d", 2)]).toEqual(["d", "a"]);
    expect([...pickLiveIds(["a", "b"], null, 4)]).toEqual(["a", "b"]);
    expect(pickLiveIds(["a"], "a", 0).size).toBe(0);
  });
});

describe("parseTilesView", () => {
  it("accepts only known views", () => {
    expect(parseTilesView("focus")).toBe("focus");
    expect(parseTilesView("metrics")).toBeNull();
    expect(parseTilesView(null)).toBeNull();
  });
});

describe("isInteractiveTarget", () => {
  it("ignores clicks on controls but not on plain content", () => {
    const tile = document.createElement("section");
    tile.innerHTML = `<p id="text">hi</p><button id="btn"><span id="icon"></span></button>`;
    expect(isInteractiveTarget(tile.querySelector("#text"))).toBe(false);
    expect(isInteractiveTarget(tile.querySelector("#icon"))).toBe(true);
  });
});

describe("tilesToggleDestination", () => {
  it("carries the active agent between single and tile views", () => {
    expect(tilesToggleDestination(false, null, "abc")).toBe("/tiles?active=abc");
    expect(tilesToggleDestination(false, null, undefined)).toBe("/tiles");
    expect(tilesToggleDestination(true, "abc", undefined)).toBe("/c/abc");
    expect(tilesToggleDestination(true, null, undefined)).toBe("/");
  });
});

describe("tileStreamBudget", () => {
  it("keeps two live tiles over HTTP/1.1 and the full budget over HTTP/2", () => {
    expect(tileStreamBudget("serial", 4)).toBe(2);
    expect(tileStreamBudget("serial", 1)).toBe(1);
    expect(tileStreamBudget("multiplexed", 30)).toBe(30);
  });
});
