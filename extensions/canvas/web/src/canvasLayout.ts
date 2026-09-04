import type { ExtensionSessionSummary } from "@omnigent/extension-sdk";

export const CARD_WIDTH = 280;
export const CARD_HEIGHT = 132;
export const CARD_GAP = 32;
// Sessions outside any project (or whose project is gone) live on this canvas.
export const MAIN_CANVAS_ID = "main";
const CELL_WIDTH = CARD_WIDTH + CARD_GAP;
const CELL_HEIGHT = CARD_HEIGHT + CARD_GAP;

export interface CanvasPosition {
  x: number;
  y: number;
}

export type CanvasPositions = Record<string, CanvasPosition>;

function cellKey(position: CanvasPosition): string {
  return `${Math.round(position.x / CELL_WIDTH)}:${Math.round(position.y / CELL_HEIGHT)}`;
}

export function gridPosition(index: number, columns: number): CanvasPosition {
  return {
    x: (index % columns) * CELL_WIDTH,
    y: Math.floor(index / columns) * CELL_HEIGHT,
  };
}

export function mergeSessionPositions(
  sessions: ExtensionSessionSummary[],
  saved: CanvasPositions,
): CanvasPositions {
  const ordered = [...sessions].sort(
    (left, right) =>
      right.updatedAt - left.updatedAt || left.id.localeCompare(right.id),
  );
  const liveIds = new Set(ordered.map((session) => session.id));
  const positions: CanvasPositions = {};
  const occupied = new Set<string>();
  for (const [id, position] of Object.entries(saved)) {
    if (
      !liveIds.has(id) ||
      !Number.isFinite(position.x) ||
      !Number.isFinite(position.y)
    )
      continue;
    const normalized = { x: Math.round(position.x), y: Math.round(position.y) };
    positions[id] = normalized;
    occupied.add(cellKey(normalized));
  }
  const columns = Math.max(1, Math.ceil(Math.sqrt(ordered.length)));
  let candidate = 0;
  for (const session of ordered) {
    if (positions[session.id]) continue;
    let position = gridPosition(candidate++, columns);
    while (occupied.has(cellKey(position)))
      position = gridPosition(candidate++, columns);
    positions[session.id] = position;
    occupied.add(cellKey(position));
  }
  return positions;
}

export function prunePositions(
  positions: CanvasPositions,
  sessionIds: Iterable<string>,
): CanvasPositions {
  const live = new Set(sessionIds);
  return Object.fromEntries(
    Object.entries(positions).filter(([id]) => live.has(id)),
  );
}

export function canvasIdFor(
  session: ExtensionSessionSummary,
  projectIds: ReadonlySet<string>,
): string {
  return session.projectId !== null && projectIds.has(session.projectId)
    ? session.projectId
    : MAIN_CANVAS_ID;
}

export function sessionsOnCanvas(
  sessions: ExtensionSessionSummary[],
  canvasId: string,
  projectIds: ReadonlySet<string>,
): ExtensionSessionSummary[] {
  return sessions.filter(
    (session) => canvasIdFor(session, projectIds) === canvasId,
  );
}

// Each canvas lays out its unsaved cards in its own grid, so a project's cards
// never start in slots taken by cards on another canvas.
export function mergeCanvasPositions(
  sessions: ExtensionSessionSummary[],
  projectIds: ReadonlySet<string>,
  saved: CanvasPositions,
): CanvasPositions {
  const groups = new Map<string, ExtensionSessionSummary[]>();
  for (const session of sessions) {
    const canvasId = canvasIdFor(session, projectIds);
    groups.set(canvasId, [...(groups.get(canvasId) ?? []), session]);
  }
  let positions: CanvasPositions = {};
  for (const group of groups.values()) {
    positions = { ...positions, ...mergeSessionPositions(group, saved) };
  }
  return positions;
}
