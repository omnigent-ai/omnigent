export interface ExtensionSessionSummary {
  id: string;
  title: string | null;
  status: "idle" | "running" | "waiting" | "failed";
  workspace: string | null;
  createdAt: number;
  updatedAt: number;
}

export interface ExtensionSessionPage {
  sessions: ExtensionSessionSummary[];
  nextCursor: string | null;
  hasMore: boolean;
}

export const SESSION_PAGE_DEFAULT_LIMIT = 25;
export const SESSION_PAGE_MAX_LIMIT = 25;
export const SESSIONS_LIST_ALL_MAX_PAGES = 200;
export const SESSIONS_LIST_ALL_MAX_SESSIONS = 5_000;

function parseRevision(value: unknown): number | null {
  if (!value || typeof value !== "object") return null;
  const revision = (value as { revision?: unknown }).revision;
  return typeof revision === "number" &&
    Number.isSafeInteger(revision) &&
    revision >= 0
    ? revision
    : null;
}

export class SessionRevisionTracker {
  private baseline: number | null = null;
  private pendingRevision = -1;

  accept(value: unknown): boolean {
    const revision = parseRevision(value);
    if (revision === null) return false;
    if (this.baseline === null) {
      this.pendingRevision = Math.max(this.pendingRevision, revision);
      return false;
    }
    if (revision <= this.baseline) return false;
    this.baseline = revision;
    return true;
  }

  initialize(
    value: unknown,
    fail: (code: string, message: string) => Error,
  ): boolean {
    const revision = parseRevision(value);
    if (revision === null) {
      throw fail("InvalidResponse", "Session subscription revision is invalid");
    }
    this.baseline = revision;
    if (this.pendingRevision <= revision) return false;
    this.baseline = this.pendingRevision;
    return true;
  }
}

export function validateSessionPageLimit(
  value: number | undefined,
  fail: (code: string, message: string) => Error,
): number {
  const limit = value ?? SESSION_PAGE_DEFAULT_LIMIT;
  if (!Number.isInteger(limit) || limit < 1 || limit > SESSION_PAGE_MAX_LIMIT) {
    throw fail(
      "InvalidParams",
      `session page limit must be an integer from 1 to ${SESSION_PAGE_MAX_LIMIT}`,
    );
  }
  return limit;
}

export async function drainSessionPages(
  fetchPage: (after: string | null) => Promise<ExtensionSessionPage>,
  fail: (code: string, message: string) => Error,
): Promise<ExtensionSessionSummary[]> {
  const sessions: ExtensionSessionSummary[] = [];
  const seenCursors = new Set<string>();
  let after: string | null = null;
  for (
    let pageNumber = 0;
    pageNumber < SESSIONS_LIST_ALL_MAX_PAGES;
    pageNumber += 1
  ) {
    const page = await fetchPage(after);
    sessions.push(...page.sessions);
    if (sessions.length > SESSIONS_LIST_ALL_MAX_SESSIONS) {
      throw fail("LimitExceeded", "sessions.listAll exceeded 5,000 sessions");
    }
    if (!page.hasMore) return sessions;
    const next = page.nextCursor;
    if (!next) {
      throw fail("InvalidResponse", "sessions.listAll received no next cursor");
    }
    if (seenCursors.has(next)) {
      throw fail(
        "InvalidResponse",
        "sessions.listAll received a repeated cursor",
      );
    }
    seenCursors.add(next);
    after = next;
  }
  throw fail("LimitExceeded", "sessions.listAll exceeded 200 pages");
}
