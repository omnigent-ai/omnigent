import { describe, expect, it } from "vitest";
import type { Conversation, ProjectSummary } from "@/hooks/useConversations";
import { PROJECT_LABEL_KEY } from "@/lib/sessionListCache";
import {
  CARD_HEIGHT,
  CARD_WIDTH,
  GRID_STEP,
  MAIN_CANVAS_ID,
  canvasIdFor,
  gridPosition,
  mergeCanvasPositions,
  mergeSessionPositions,
  projectCanvasId,
  samePositions,
  sessionsOnCanvas,
} from "./canvasLayout";

function session(
  id: string,
  updatedAt: number,
  overrides: Partial<Conversation> = {},
): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 1,
    updated_at: updatedAt,
    labels: {},
    permission_level: null,
    status: "idle",
    ...overrides,
  };
}

describe("mergeSessionPositions", () => {
  it("seeds a deterministic non-overlapping grid ordered by recency", () => {
    const sessions = [session("old", 1), session("new", 3), session("middle", 2)];
    const first = mergeSessionPositions(sessions, {});
    const second = mergeSessionPositions([...sessions].reverse(), {});

    expect(second).toEqual(first);
    expect(first.new).toEqual({ x: 0, y: 0 });
    expect(first.middle).toEqual({ x: GRID_STEP * 10, y: 0 });
    const unique = new Set(
      Object.values(first).map(({ x, y }) => `${x / GRID_STEP}:${y / GRID_STEP}`),
    );
    expect(unique.size).toBe(3);
  });

  it("preserves rounded saved positions and places new cards elsewhere", () => {
    const result = mergeSessionPositions([session("saved", 1), session("new", 2)], {
      saved: { x: 1.4, y: 2.6 },
      removed: { x: 99, y: 99 },
    });

    expect(result.saved).toEqual({ x: 1, y: 3 });
    expect(result.removed).toBeUndefined();
    expect(result.new).not.toEqual(result.saved);
  });
});

describe("grid lattice", () => {
  it("lays cards out in whole snap steps so dragged cards line up with the grid", () => {
    const firstRow = gridPosition(0, 2);
    const secondColumn = gridPosition(1, 2);
    const secondRow = gridPosition(2, 2);
    expect(secondColumn.x - firstRow.x).toBeGreaterThan(CARD_WIDTH);
    expect(secondRow.y - firstRow.y).toBeGreaterThan(CARD_HEIGHT);
    expect(secondColumn.x % GRID_STEP).toBe(0);
    expect(secondRow.y % GRID_STEP).toBe(0);
  });

  it("drops saved spots of sessions that are gone", () => {
    const merged = mergeSessionPositions([session("two", 1)], {
      one: { x: 1, y: 2 },
      two: { x: 3, y: 4 },
    });
    expect(merged).toEqual({ two: { x: 3, y: 4 } });
  });
});

describe("samePositions", () => {
  it("compares card sets and coordinates", () => {
    const left = { a: { x: 1, y: 2 }, b: { x: 3, y: 4 } };
    expect(samePositions(left, { b: { x: 3, y: 4 }, a: { x: 1, y: 2 } })).toBe(true);
    expect(samePositions(left, { a: { x: 1, y: 2 } })).toBe(false);
    expect(samePositions(left, { a: { x: 1, y: 2 }, b: { x: 3, y: 5 } })).toBe(false);
    expect(samePositions(left, { a: { x: 1, y: 2 }, c: { x: 3, y: 4 } })).toBe(false);
  });
});

describe("canvases", () => {
  const projects: ProjectSummary[] = [
    { id: "proj_a", name: "Alpha" },
    { id: null, name: "Legacy" },
  ];
  const inProject = session("in_project", 4, { project_id: "proj_a" });
  const labelled = session("labelled", 3, { labels: { [PROJECT_LABEL_KEY]: "Legacy" } });
  const orphan = session("orphan", 2, { project_id: "proj_gone" });
  const loose = session("loose", 1);
  const all = [inProject, labelled, orphan, loose];

  it("keys a project canvas by id, or by name for a label-only folder", () => {
    expect(projectCanvasId(projects[0])).toBe("proj_a");
    expect(projectCanvasId(projects[1])).toBe("name:Legacy");
  });

  it("files sessions by first-class id or legacy label and the rest on Main", () => {
    expect(canvasIdFor(inProject, projects, null)).toBe("proj_a");
    expect(canvasIdFor(labelled, projects, null)).toBe("name:Legacy");
    expect(canvasIdFor(orphan, projects, null)).toBe(MAIN_CANVAS_ID);
    expect(sessionsOnCanvas(all, MAIN_CANVAS_ID, projects, null)).toEqual([orphan, loose]);
    expect(sessionsOnCanvas(all, "proj_a", projects, null)).toEqual([inProject]);
    expect(sessionsOnCanvas(all, "name:Legacy", projects, null)).toEqual([labelled]);
  });

  it("keeps a shared session the viewer does not own on Main, like the sidebar", () => {
    const shared = session("shared", 5, { project_id: "proj_a", owner: "someone-else" });
    const mine = session("mine", 4, { project_id: "proj_a", owner: "me" });
    expect(canvasIdFor(shared, projects, "me")).toBe(MAIN_CANVAS_ID);
    expect(canvasIdFor(mine, projects, "me")).toBe("proj_a");
    // Until identity resolves, shared rows are not filed either.
    expect(canvasIdFor(shared, projects, null)).toBe(MAIN_CANVAS_ID);
  });

  it("starts every canvas at its own grid origin while keeping saved spots", () => {
    const positions = mergeCanvasPositions(all, projects, { loose: { x: 640, y: 0 } }, null);
    expect(positions.in_project).toEqual({ x: 0, y: 0 });
    expect(positions.labelled).toEqual({ x: 0, y: 0 });
    expect(positions.orphan).toEqual({ x: 0, y: 0 });
    expect(positions.loose).toEqual({ x: 640, y: 0 });
  });
});
