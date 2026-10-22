import { describe, expect, it } from "vitest";
import { sessionLinkEdges } from "./sessionLinks";
import { sessionLinkPath } from "./SessionLinkEdge";
import { Position } from "@xyflow/react";
import { CARD_HEIGHT } from "./canvasLayout";
import type { SessionLink } from "@/lib/sessionLinksApi";

function link(source: string, target: string): SessionLink {
  return {
    source_id: source,
    target_id: target,
    source_title: source,
    target_title: target,
    last_requested_at: 1,
  };
}

describe("Canvas communication activity", () => {
  it("places a return direction above the cards and the forward direction between them", () => {
    const positions = {
      sourceY: 80,
      targetY: 80,
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
    };
    const [, forwardX, forwardY] = sessionLinkPath({ ...positions, sourceX: 280, targetX: 320 });
    const [, reverseX, reverseY] = sessionLinkPath({ ...positions, sourceX: 600, targetX: 0 });
    expect(forwardX).toBeGreaterThan(280);
    expect(forwardX).toBeLessThan(320);
    expect(forwardY).toBeLessThan(80);
    expect(reverseX).toBeGreaterThan(0);
    expect(reverseX).toBeLessThan(600);
    expect(reverseY).toBeLessThan(80 - CARD_HEIGHT / 2);
  });
  it("keeps reverse activity independently selectable without drawing hidden peers", () => {
    const edges = sessionLinkEdges(
      [link("A", "B"), link("B", "A"), link("B", "private")],
      new Set(["A", "B"]),
    );
    expect(edges.map(({ id, source, target }) => ({ id, source, target }))).toEqual([
      { id: "A:B", source: "A", target: "B" },
      { id: "B:A", source: "B", target: "A" },
    ]);
    expect(edges.every((edge) => edge.selectable && !edge.deletable && edge.markerEnd)).toBe(true);
  });

  it("labels activity without implying an active permission grant", () => {
    const edges = sessionLinkEdges([link("A", "B")], new Set(["A", "B"]));
    expect(edges[0].label).toBe("Communication");
    expect(edges[0].ariaLabel).toContain("communication activity");
    expect(sessionLinkEdges([], new Set(["A", "B"]))).toEqual([]);
  });
});
