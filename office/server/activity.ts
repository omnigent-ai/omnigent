// Meaningful-interaction discovery: the latest human-authored message in a
// session, found by paging its history newest-first.

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

export async function scanSession(
  sessionId: string,
  updatedAt: number,
  fetchPage: FetchItemsPage,
  maxPages = MAX_SCAN_PAGES,
  isRelayed: (itemId: string) => boolean = () => false,
): Promise<SessionScan> {
  let after: string | null = null;
  for (let page = 0; page < maxPages; page++) {
    const result = await fetchPage(sessionId, after);
    // Messages the office relayed between agents are user-role items but not human input.
    const human = result.data.find((item) => isHumanMessage(item) && !isRelayed(item.id));
    if (human) {
      return {
        scannedUpdatedAt: updatedAt,
        latestHumanItemId: human.id,
        latestHumanAt: human.created_at as number,
        complete: true,
      };
    }
    if (!result.has_more || result.data.length === 0) {
      return { scannedUpdatedAt: updatedAt, latestHumanItemId: null, latestHumanAt: null, complete: true };
    }
    after = result.last_id ?? result.data[result.data.length - 1].id;
  }
  return { scannedUpdatedAt: updatedAt, latestHumanItemId: null, latestHumanAt: null, complete: false };
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
  };
}
