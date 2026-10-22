import { describe, expect, it } from "vitest";
import { sessionMessageEdges } from "./sessionMessages";
import { sessionMessagePath } from "./SessionMessageEdge";
import { Position } from "@xyflow/react";
import { CARD_HEIGHT } from "./canvasLayout";
import type { SessionMessage } from "@/lib/sessionCommunicationApi";

function link(source: string, target: string): SessionMessage {
  return {
    source_id: source,
    target_id: target,
    source_title: source,
    target_title: target,
    last_sent_at: 1,
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
    const [, forwardX, forwardY] = sessionMessagePath({ ...positions, sourceX: 280, targetX: 320 });
    const [, reverseX, reverseY] = sessionMessagePath({ ...positions, sourceX: 600, targetX: 0 });
    expect(forwardX).toBeGreaterThan(280);
    expect(forwardX).toBeLessThan(320);
    expect(forwardY).toBeLessThan(80);
    expect(reverseX).toBeGreaterThan(0);
    expect(reverseX).toBeLessThan(600);
    expect(reverseY).toBeLessThan(80 - CARD_HEIGHT / 2);
  });
  it("keeps reverse activity independently selectable without drawing hidden peers", () => {
    const edges = sessionMessageEdges(
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
    const edges = sessionMessageEdges([link("A", "B")], new Set(["A", "B"]));
    expect(edges[0].label).toBe("Sent");
    expect(edges[0].ariaLabel).toContain("communication activity");
    expect(sessionMessageEdges([], new Set(["A", "B"]))).toEqual([]);
  });
});
