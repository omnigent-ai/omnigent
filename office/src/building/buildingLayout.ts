// Geometry of the building cutaway: floors stacked top to bottom with an
// elevator shaft on the left and up to three room slots per floor.

import type { Placement } from "../../shared/ranking.ts";

export const SHAFT_WIDTH = 72;
export const SLOT_WIDTH = 210;
export const SLOT_GAP = 12;
export const FLOOR_HEIGHT = 128;
export const BUILDING_WIDTH = SHAFT_WIDTH + 3 * SLOT_WIDTH + 4 * SLOT_GAP;
export const GROUND_GAP = 18;

export type FloorKind = "upstairs" | "lobby" | "basement";

export interface FloorRow {
  kind: FloorKind;
  label: string;
  projectIds: string[];
}

/** Highest floor first, then the lobby, then B1 downward. */
export function buildingRows(placement: Placement): FloorRow[] {
  const rows: FloorRow[] = [];
  for (let floor = placement.upstairs.length; floor >= 1; floor--) {
    rows.push({ kind: "upstairs", label: String(floor), projectIds: placement.upstairs[floor - 1] });
  }
  rows.push({ kind: "lobby", label: "Lobby", projectIds: [] });
  placement.basement.forEach((projectIds, index) => {
    rows.push({ kind: "basement", label: `B${index + 1}`, projectIds });
  });
  return rows;
}

export function rowTop(rows: FloorRow[], rowIndex: number): number {
  const lobbyIndex = rows.findIndex((row) => row.kind === "lobby");
  return rowIndex * FLOOR_HEIGHT + (rowIndex > lobbyIndex ? GROUND_GAP : 0);
}

export function buildingHeight(rows: FloorRow[]): number {
  return rows.length * FLOOR_HEIGHT + (rows.some((row) => row.kind === "basement") ? GROUND_GAP : 0);
}

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export function slotRect(rows: FloorRow[], rowIndex: number, slotIndex: number): Rect {
  return {
    x: SHAFT_WIDTH + SLOT_GAP + slotIndex * (SLOT_WIDTH + SLOT_GAP),
    y: rowTop(rows, rowIndex) + 14,
    width: SLOT_WIDTH,
    height: FLOOR_HEIGHT - 22,
  };
}

/** Where each project's room currently sits, for movement animation. */
export function slotPositions(rows: FloorRow[]): Map<string, Rect> {
  const positions = new Map<string, Rect>();
  rows.forEach((row, rowIndex) =>
    row.projectIds.forEach((projectId, slotIndex) => positions.set(projectId, slotRect(rows, rowIndex, slotIndex))),
  );
  return positions;
}

export function rowIndexOfProject(rows: FloorRow[], projectId: string): number {
  return rows.findIndex((row) => row.projectIds.includes(projectId));
}
