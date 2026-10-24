// Building ranking and floor placement. Pure functions over RoomSummary so
// the service and the browser apply exactly the same rules.

import type { Priority, RoomSummary } from "./types.ts";

export const ROOMS_PER_FLOOR = 3;
export const BASEMENT_AFTER_SECONDS = 30 * 24 * 60 * 60;

const PRIORITY_ORDER: Record<Priority, number> = { critical: 0, high: 1, normal: 2 };

export const PRIORITY_LABELS: Record<Priority, string> = {
  critical: "Critical",
  high: "High",
  normal: "Normal",
};

/** Priority, then latest interaction (unknown last), then newest creation, then id. */
export function compareRooms(a: RoomSummary, b: RoomSummary): number {
  const byPriority = PRIORITY_ORDER[a.priority] - PRIORITY_ORDER[b.priority];
  if (byPriority !== 0) return byPriority;
  if (a.latestInteractionAt !== b.latestInteractionAt) {
    if (a.latestInteractionAt === null) return 1;
    if (b.latestInteractionAt === null) return -1;
    return b.latestInteractionAt - a.latestInteractionAt;
  }
  if (a.createdAt !== b.createdAt) return b.createdAt - a.createdAt;
  return a.projectId < b.projectId ? -1 : a.projectId > b.projectId ? 1 : 0;
}

/**
 * The instant inactivity is measured from: the latest of the last known
 * interaction and creation, plus the first sync when history is incomplete.
 */
export function inactivityAnchor(room: RoomSummary): number {
  let anchor = Math.max(room.createdAt, room.latestInteractionAt ?? -Infinity);
  if (!room.historyComplete) anchor = Math.max(anchor, room.firstSyncAt);
  return anchor;
}

export function basementDeadline(room: RoomSummary): number {
  return inactivityAnchor(room) + BASEMENT_AFTER_SECONDS;
}

export type BasementExemption = "priority" | "keepUpstairs" | "pendingRequest";

export function basementExemption(room: RoomSummary): BasementExemption | null {
  if (room.priority !== "normal") return "priority";
  if (room.keepUpstairs) return "keepUpstairs";
  if (room.pendingRequestCount > 0) return "pendingRequest";
  return null;
}

export function isBasementEligible(room: RoomSummary, now: number): boolean {
  return basementExemption(room) === null && now >= basementDeadline(room);
}

export interface Placement {
  /** Above-ground floors, index 0 = floor 1. Each floor lists project ids left to right. */
  upstairs: string[][];
  /** Basement floors, index 0 = B1 (nearest the lobby). */
  basement: string[][];
}

function chunk<T>(items: T[], size: number): T[][] {
  const chunks: T[][] = [];
  for (let i = 0; i < items.length; i += size) chunks.push(items.slice(i, i + size));
  return chunks;
}

/** Highest-ranked upstairs room goes on the highest floor; highest basement room on B1. */
export function computePlacement(rooms: RoomSummary[], now: number): Placement {
  const ranked = [...rooms].sort(compareRooms);
  const upstairsRanked = ranked.filter((room) => !isBasementEligible(room, now));
  const basementRanked = ranked.filter((room) => isBasementEligible(room, now));
  const upstairsTopDown = chunk(
    upstairsRanked.map((room) => room.projectId),
    ROOMS_PER_FLOOR,
  );
  return {
    upstairs: upstairsTopDown.reverse(),
    basement: chunk(
      basementRanked.map((room) => room.projectId),
      ROOMS_PER_FLOOR,
    ),
  };
}

export function placementsEqual(a: Placement, b: Placement): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

/** Keep a displayed placement's rooms in place but drop rooms that no longer exist. */
export function prunePlacement(placement: Placement, knownIds: Set<string>): Placement {
  const prune = (floors: string[][]) =>
    floors.map((floor) => floor.filter((id) => knownIds.has(id))).filter((floor) => floor.length);
  return { upstairs: prune(placement.upstairs), basement: prune(placement.basement) };
}

const DAY = 24 * 60 * 60;

function wholeDays(seconds: number): number {
  return Math.floor(seconds / DAY);
}

function describeAgo(at: number, now: number): string {
  const days = wholeDays(now - at);
  if (days <= 0) {
    const time = new Date(at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return `today at ${time}`;
  }
  return days === 1 ? "1 day ago" : `${days} days ago`;
}

export interface RankExplanation {
  headline: string;
  details: string[];
}

/** Human-readable reason for a room's position, per the building ranking rules. */
export function explainRank(
  room: RoomSummary,
  rooms: RoomSummary[],
  now: number,
  placementWaiting: boolean,
): RankExplanation {
  const samePriority = rooms.filter((other) => other.priority === room.priority).sort(compareRooms);
  const position = samePriority.findIndex((other) => other.projectId === room.projectId) + 1;
  const label = PRIORITY_LABELS[room.priority];
  const inBasement = isBasementEligible(room, now);
  const where =
    room.priority === "normal" ? (inBasement ? "Basement" : "Upstairs") : `#${position} of ${samePriority.length} ${label} projects`;
  const details: string[] = [];

  if (room.latestInteractionAt !== null) {
    details.push(`Last human interaction: ${describeAgo(room.latestInteractionAt, now)}.`);
  } else if (room.historyComplete) {
    details.push("No human interaction yet; inactivity is measured from creation.");
  }
  if (!room.historyComplete) {
    details.push(
      `History is incomplete; known coverage begins at the first sync ` +
        `(${new Date(room.firstSyncAt * 1000).toLocaleDateString()}), which sets the grace period.`,
    );
  }

  const exemption = basementExemption(room);
  if (exemption === "priority") {
    details.push(`${label} projects always remain upstairs.`);
  } else if (exemption === "keepUpstairs") {
    details.push("Keep upstairs is enabled, so this room never moves to the basement.");
  } else if (exemption === "pendingRequest") {
    details.push("A pending human request keeps this room upstairs.");
  } else if (inBasement) {
    details.push(
      `No human interaction for ${wholeDays(now - inactivityAnchor(room))} days. ` +
        "Send a message or enable Keep upstairs to return upstairs.",
    );
  } else {
    const remaining = Math.max(0, Math.ceil((basementDeadline(room) - now) / DAY));
    details.push(`Basement eligibility begins in ${remaining} days unless Keep upstairs is enabled.`);
  }
  if (placementWaiting) details.push("A newer placement is waiting; select Refresh placement to apply it.");

  return { headline: `${label} · ${where}`, details };
}
