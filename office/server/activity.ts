// Meaningful-interaction discovery: the latest human-authored message in a
// session, found by paging its history newest-first.

import type { FailureDetail } from "../shared/troubleshoot.ts";

export interface WireItem {
  id: string;
  type: string;
  role?: string;
  created_at?: number;
  is_meta?: boolean;
  user_authored?: boolean;
  [key: string]: unknown;
}

export interface ItemsPage {
  data: WireItem[];
  has_more: boolean;
  last_id?: string | null;
}

export type FetchItemsPage = (sessionId: string, after: string | null) => Promise<ItemsPage>;

export interface SessionScan {
  /** `updated_at` of the session when it was scanned; a newer value triggers a rescan. */
  scannedUpdatedAt: number;
  latestHumanItemId: string | null;
  latestHumanAt: number | null;
  /** True when the scan found the latest human message or reached the start of history. */
  complete: boolean;
  /** The error the latest turn ended with, or null when a later item followed it. */
  lastTurnError?: FailureDetail | null;
}

export const MAX_SCAN_PAGES = 5;

/** Human input only: agent-to-agent traffic and injected context never count. */
export function isHumanMessage(item: WireItem): boolean {
  return (
    item.type === "message" &&
    item.role === "user" &&
    item.is_meta !== true &&
    item.user_authored !== false &&
    typeof item.created_at === "number"
  );
}

// Transcript metadata that neither ends nor continues a turn.
const NEUTRAL_ITEM_TYPES = new Set(["reasoning", "compaction", "resource_event", "routing_decision", "slash_command", "terminal_command"]);

/** The error banner that ended the latest turn, from a newest-first page. */
export function latestTurnError(items: WireItem[]): FailureDetail | null {
  for (const item of items) {
    if (item.type === "error") {
      if (item.level === "info") continue;
      const text = (value: unknown) => (typeof value === "string" && value ? value : undefined);
      return {
        code: text(item.code) ?? "unknown_error",
        message: text(item.message) ?? "The turn failed without a message.",
        source: text(item.source),
        title: text(item.title),
        cause: text(item.cause),
        remediation: text(item.remediation),
        at: typeof item.created_at === "number" ? item.created_at : undefined,
      };
    }
    if (NEUTRAL_ITEM_TYPES.has(item.type) || item.is_meta === true) continue;
    return null;
  }
  return null;
}

export async function scanSession(
  sessionId: string,
  updatedAt: number,
  fetchPage: FetchItemsPage,
  maxPages = MAX_SCAN_PAGES,
  isRelayed: (itemId: string) => boolean = () => false,
): Promise<SessionScan> {
  let after: string | null = null;
  let lastTurnError: FailureDetail | null = null;
  for (let page = 0; page < maxPages; page++) {
    const result = await fetchPage(sessionId, after);
    if (page === 0) lastTurnError = latestTurnError(result.data);
    // Messages the office relayed between agents are user-role items but not human input.
    const human = result.data.find((item) => isHumanMessage(item) && !isRelayed(item.id));
    if (human) {
      return {
        scannedUpdatedAt: updatedAt,
        latestHumanItemId: human.id,
        latestHumanAt: human.created_at as number,
        complete: true,
        lastTurnError,
      };
    }
    if (!result.has_more || result.data.length === 0) {
      return { scannedUpdatedAt: updatedAt, latestHumanItemId: null, latestHumanAt: null, complete: true, lastTurnError };
    }
    after = result.last_id ?? result.data[result.data.length - 1].id;
  }
  return { scannedUpdatedAt: updatedAt, latestHumanItemId: null, latestHumanAt: null, complete: false, lastTurnError };
}

/**
 * Keep the newer of a fresh scan and a previous one. An incomplete rescan
 * never erases an interaction found earlier, so replays and reconnects
 * cannot reset activity.
 */
export function mergeScan(previous: SessionScan | undefined, next: SessionScan): SessionScan {
  if (!previous || previous.latestHumanAt === null) return next;
  if (next.latestHumanAt !== null && next.latestHumanAt >= previous.latestHumanAt) return next;
  return {
    scannedUpdatedAt: next.scannedUpdatedAt,
    latestHumanItemId: previous.latestHumanItemId,
    latestHumanAt: previous.latestHumanAt,
    complete: previous.complete || next.complete,
    lastTurnError: next.lastTurnError,
  };
}
