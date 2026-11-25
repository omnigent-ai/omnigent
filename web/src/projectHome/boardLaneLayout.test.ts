import { describe, expect, it } from "vitest";
import type { TicketStage } from "@/lib/projectTicketsApi";
import { collapsedLanes, LANE_GAP_PX, LANE_MIN_PX, LANE_STRIP_PX } from "./boardLaneLayout";

const stage = (id: string, is_complete = false) => ({ id, name: id, is_complete }) as TicketStage;
const STAGES = [
  stage("pending"),
  stage("working"),
  stage("review"),
  stage("implemented"),
  stage("verified"),
  stage("merged", true),
];
/** The width that holds `open` full lanes and `strips` collapsed ones. */
const widthFor = (open: number, strips: number) =>
  open * LANE_MIN_PX + strips * LANE_STRIP_PX + (open + strips - 1) * LANE_GAP_PX;

describe("collapsedLanes", () => {
  it("collapses only the completed stage when every other lane fits", () => {
    expect([...collapsedLanes(STAGES, {}, "pending", widthFor(5, 1))]).toEqual(["merged"]);
  });

  it("collapses from the right until the open lanes fit, keeping the first stage open", () => {
    expect([...collapsedLanes(STAGES, {}, "pending", widthFor(3, 3))].sort()).toEqual(
      ["implemented", "merged", "verified"].sort(),
    );
    const narrow = collapsedLanes(STAGES, {}, "pending", 440);
    expect(narrow.has("pending")).toBe(false);
    expect(narrow.size).toBe(5);
  });

  it("keeps a lane the person opened, and collapses one they closed", () => {
    const layout = collapsedLanes(
      STAGES,
      { verified: false, merged: false, working: true },
      "pending",
      widthFor(3, 3),
    );
    expect(layout.has("verified")).toBe(false);
    expect(layout.has("merged")).toBe(false);
    expect(layout.has("working")).toBe(true);
  });

  it("leaves a phone-width board to scroll instead of folding its active lanes", () => {
    expect([...collapsedLanes(STAGES, {}, "pending", 358)]).toEqual(["merged"]);
  });

  it("uses only stored choices before the board has been measured", () => {
    expect([...collapsedLanes(STAGES, { review: true }, "pending", null)].sort()).toEqual([
      "merged",
      "review",
    ]);
  });
});
