// Tile filters: status (All / Favorites / Needs you / Working), project, and a
// search where every word must match. Favorites sort first.

import type { Conversation } from "@/hooks/useConversations";
import { isSessionPinned } from "@/lib/sessionFamilies";
import { needsYou } from "./tileSelection";

export type TileStatusFilter = "all" | "favorites" | "needs-you" | "working";
export const TILE_STATUS_FILTERS: readonly TileStatusFilter[] = [
  "all",
  "favorites",
  "needs-you",
  "working",
];

export interface TileFilters {
  status: TileStatusFilter;
  /** Project name from `projectOf`, or null for every project. */
  project: string | null;
  query: string;
}

/** The project a session belongs to: its working directory's last segment. */
export function projectOf(row: Pick<Conversation, "workspace">): string | null {
  const trimmed = row.workspace?.trim().replace(/\/+$/, "");
  if (!trimmed) return null;
  return trimmed.slice(trimmed.lastIndexOf("/") + 1) || trimmed;
}

export function matchesStatus(row: Conversation, status: TileStatusFilter): boolean {
  switch (status) {
    case "favorites":
      return isSessionPinned(row);
    case "needs-you":
      return needsYou(row);
    case "working":
      return row.status === "running";
    default:
      return true;
  }
}

/** Case-insensitive; every whitespace-separated word must appear somewhere. */
export function matchesQuery(row: Conversation, query: string): boolean {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return true;
  const haystack = [row.title, row.harness, row.agent_name, row.workspace, row.git_branch]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return words.every((word) => haystack.includes(word));
}

export function filterTiles<T extends Conversation>(rows: readonly T[], filters: TileFilters): T[] {
  return rows.filter(
    (row) =>
      matchesStatus(row, filters.status) &&
      (filters.project === null || projectOf(row) === filters.project) &&
      matchesQuery(row, filters.query),
  );
}

/** Favorites first, otherwise keeping the given order. */
export function favoritesFirst<T extends Conversation>(rows: readonly T[]): T[] {
  const favorites = rows.filter((row) => isSessionPinned(row));
  return favorites.length === 0
    ? [...rows]
    : [...favorites, ...rows.filter((row) => !isSessionPinned(row))];
}

/** Count per status filter, for the filter bar. */
export function statusCounts(rows: readonly Conversation[]): Record<TileStatusFilter, number> {
  const counts = { all: rows.length, favorites: 0, "needs-you": 0, working: 0 };
  for (const row of rows) {
    if (isSessionPinned(row)) counts.favorites += 1;
    if (needsYou(row)) counts["needs-you"] += 1;
    if (row.status === "running") counts.working += 1;
  }
  return counts;
}

/** Projects present among the rows, sorted, for the project menu. */
export function projectsOf(rows: readonly Conversation[]): string[] {
  return [...new Set(rows.map(projectOf).filter((name): name is string => name !== null))].sort();
}
