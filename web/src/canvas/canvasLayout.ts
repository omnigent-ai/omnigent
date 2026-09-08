// Pure layout math for the Canvas page: which canvas (Main or a project) a
// session belongs to, and where its card sits when the user has not placed it.

import type { Conversation, ProjectSummary } from "@/hooks/useConversations";
import { sessionBelongsToProject } from "@/shell/sidebarNav";

export const CARD_WIDTH = 280;
export const CARD_HEIGHT = 132;
export const CARD_GAP = 32;
/** Sessions outside any project (or whose project is gone) live on this canvas. */
export const MAIN_CANVAS_ID = "main";
const CELL_WIDTH = CARD_WIDTH + CARD_GAP;
const CELL_HEIGHT = CARD_HEIGHT + CARD_GAP;

export interface CanvasPosition {
  x: number;
  y: number;
}

export type CanvasPositions = Record<string, CanvasPosition>;

/** Stable canvas id for a project: its row id, or its name for a legacy label-only folder. */
export function projectCanvasId(project: ProjectSummary): string {
  return project.id ?? `name:${project.name}`;
}

// Same membership rule as the sidebar's folders (`sessionBelongsToProject`):
// first-class id or legacy label, and owner-only filing. A shared session the
// viewer does not own stays on Main, as it does in the sidebar.
export function canvasIdFor(
  conversation: Conversation,
  projects: readonly ProjectSummary[],
  viewerId: string | null,
): string {
  const project = projects.find((candidate) =>
    sessionBelongsToProject(conversation, candidate, viewerId),
  );
  return project ? projectCanvasId(project) : MAIN_CANVAS_ID;
}

export function sessionsOnCanvas(
  sessions: readonly Conversation[],
  canvasId: string,
  projects: readonly ProjectSummary[],
  viewerId: string | null,
): Conversation[] {
  return sessions.filter((session) => canvasIdFor(session, projects, viewerId) === canvasId);
}

function cellKey(position: CanvasPosition): string {
  return `${Math.round(position.x / CELL_WIDTH)}:${Math.round(position.y / CELL_HEIGHT)}`;
}

export function gridPosition(index: number, columns: number): CanvasPosition {
  return {
    x: (index % columns) * CELL_WIDTH,
    y: Math.floor(index / columns) * CELL_HEIGHT,
  };
}

/**
 * Keep every saved position for a live session and lay the rest out on a
 * near-square grid, newest first, skipping cells a saved card already fills.
 */
export function mergeSessionPositions(
  sessions: readonly Conversation[],
  saved: CanvasPositions,
): CanvasPositions {
  const ordered = [...sessions].sort(
    (left, right) => right.updated_at - left.updated_at || left.id.localeCompare(right.id),
  );
  const liveIds = new Set(ordered.map((session) => session.id));
  const positions: CanvasPositions = {};
  const occupied = new Set<string>();
  for (const [id, position] of Object.entries(saved)) {
    if (!liveIds.has(id) || !Number.isFinite(position.x) || !Number.isFinite(position.y)) continue;
    const normalized = { x: Math.round(position.x), y: Math.round(position.y) };
    positions[id] = normalized;
    occupied.add(cellKey(normalized));
  }
  const columns = Math.max(1, Math.ceil(Math.sqrt(ordered.length)));
  let candidate = 0;
  for (const session of ordered) {
    if (positions[session.id]) continue;
    let position = gridPosition(candidate++, columns);
    while (occupied.has(cellKey(position))) position = gridPosition(candidate++, columns);
    positions[session.id] = position;
    occupied.add(cellKey(position));
  }
  return positions;
}

/** Each canvas lays out its unplaced cards in its own grid so canvases never share slots. */
export function mergeCanvasPositions(
  sessions: readonly Conversation[],
  projects: readonly ProjectSummary[],
  saved: CanvasPositions,
  viewerId: string | null,
): CanvasPositions {
  const groups = new Map<string, Conversation[]>();
  for (const session of sessions) {
    const canvasId = canvasIdFor(session, projects, viewerId);
    groups.set(canvasId, [...(groups.get(canvasId) ?? []), session]);
  }
  let positions: CanvasPositions = {};
  for (const group of groups.values()) {
    positions = { ...positions, ...mergeSessionPositions(group, saved) };
  }
  return positions;
}

export function prunePositions(
  positions: CanvasPositions,
  sessionIds: Iterable<string>,
): CanvasPositions {
  const live = new Set(sessionIds);
  return Object.fromEntries(Object.entries(positions).filter(([id]) => live.has(id)));
}
