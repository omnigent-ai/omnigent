import { describe, expect, it } from "vitest";
import type { ExtensionSessionSummary } from "@omnigent/extension-sdk";
import {
  CARD_GAP,
  CARD_HEIGHT,
  CARD_WIDTH,
  MAIN_CANVAS_ID,
  mergeCanvasPositions,
  mergeSessionPositions,
  prunePositions,
  sessionsOnCanvas,
} from "./canvasLayout";

function session(id: string, updatedAt: number): ExtensionSessionSummary {
  return {
    id,
    title: id,
    status: "idle",
    unread: false,
    titleProvisional: false,
    gitBranch: null,
    projectId: null,
    workspace: null,
    createdAt: 1,
    updatedAt,
  };
}

describe("mergeSessionPositions", () => {
  it("seeds a deterministic non-overlapping grid ordered by recency", () => {
    const sessions = [
      session("old", 1),
      session("new", 3),
      session("middle", 2),
    ];
    const first = mergeSessionPositions(sessions, {});
    const second = mergeSessionPositions([...sessions].reverse(), {});

    expect(second).toEqual(first);
    expect(first.new).toEqual({ x: 0, y: 0 });
    expect(first.middle).toEqual({ x: CARD_WIDTH + CARD_GAP, y: 0 });
    const unique = new Set(
      Object.values(first).map(
        ({ x, y }) =>
          `${x / (CARD_WIDTH + CARD_GAP)}:${y / (CARD_HEIGHT + CARD_GAP)}`,
      ),
    );
    expect(unique.size).toBe(3);
  });

  it("preserves rounded saved positions and places new cards elsewhere", () => {
    const result = mergeSessionPositions(
      [session("saved", 1), session("new", 2)],
      { saved: { x: 1.4, y: 2.6 }, removed: { x: 99, y: 99 } },
    );

    expect(result.saved).toEqual({ x: 1, y: 3 });
    expect(result.removed).toBeUndefined();
    expect(result.new).not.toEqual(result.saved);
  });
});

describe("prunePositions", () => {
  it("keeps only IDs from a complete live session set", () => {
    expect(
      prunePositions({ one: { x: 1, y: 2 }, two: { x: 3, y: 4 } }, ["two"]),
    ).toEqual({ two: { x: 3, y: 4 } });
  });
});

describe("canvases", () => {
  const projectIds = new Set(["proj_a"]);
  const inProject = { ...session("in_project", 3), projectId: "proj_a" };
  const orphan = { ...session("orphan", 2), projectId: "proj_gone" };
  const loose = session("loose", 1);

  it("places sessions on their project's canvas and the rest on Main", () => {
    expect(
      sessionsOnCanvas([inProject, orphan, loose], MAIN_CANVAS_ID, projectIds),
    ).toEqual([orphan, loose]);
    expect(
      sessionsOnCanvas([inProject, orphan, loose], "proj_a", projectIds),
    ).toEqual([inProject]);
  });

  it("starts every canvas at its own grid origin while keeping saved spots", () => {
    const positions = mergeCanvasPositions(
      [inProject, orphan, loose],
      projectIds,
      {
        loose: { x: 640, y: 0 },
      },
    );
    expect(positions.in_project).toEqual({ x: 0, y: 0 });
    expect(positions.orphan).toEqual({ x: 0, y: 0 });
    expect(positions.loose).toEqual({ x: 640, y: 0 });
  });
});
