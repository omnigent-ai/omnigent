import { describe, expect, it } from "vitest";
import type { Conversation, ProjectSummary } from "@/hooks/useConversations";
import { PROJECT_LABEL_KEY } from "@/lib/sessionListCache";
import {
  CARD_GAP,
  CARD_HEIGHT,
  CARD_WIDTH,
  MAIN_CANVAS_ID,
  canvasIdFor,
  mergeCanvasPositions,
  mergeSessionPositions,
  projectCanvasId,
  prunePositions,
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
    expect(first.middle).toEqual({ x: CARD_WIDTH + CARD_GAP, y: 0 });
    const unique = new Set(
      Object.values(first).map(
        ({ x, y }) => `${x / (CARD_WIDTH + CARD_GAP)}:${y / (CARD_HEIGHT + CARD_GAP)}`,
      ),
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

describe("prunePositions", () => {
  it("keeps only IDs from a complete live session set", () => {
    expect(prunePositions({ one: { x: 1, y: 2 }, two: { x: 3, y: 4 } }, ["two"])).toEqual({
      two: { x: 3, y: 4 },
    });
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
    expect(canvasIdFor(inProject, projects)).toBe("proj_a");
    expect(canvasIdFor(labelled, projects)).toBe("name:Legacy");
    expect(canvasIdFor(orphan, projects)).toBe(MAIN_CANVAS_ID);
    expect(sessionsOnCanvas(all, MAIN_CANVAS_ID, projects)).toEqual([orphan, loose]);
    expect(sessionsOnCanvas(all, "proj_a", projects)).toEqual([inProject]);
    expect(sessionsOnCanvas(all, "name:Legacy", projects)).toEqual([labelled]);
  });

  it("starts every canvas at its own grid origin while keeping saved spots", () => {
    const positions = mergeCanvasPositions(all, projects, { loose: { x: 640, y: 0 } });
    expect(positions.in_project).toEqual({ x: 0, y: 0 });
    expect(positions.labelled).toEqual({ x: 0, y: 0 });
    expect(positions.orphan).toEqual({ x: 0, y: 0 });
    expect(positions.loose).toEqual({ x: 640, y: 0 });
  });
});
