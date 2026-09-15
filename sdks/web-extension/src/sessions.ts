export interface ExtensionSessionSummary {
  id: string;
  title: string | null;
  status: "idle" | "running" | "waiting" | "failed";
  /** A finished turn the current user has not viewed yet (the sidebar's unread rule). */
  unread: boolean;
  /** True when `title` is the shell's provisional first-message title (no server title yet). */
  titleProvisional: boolean;
  workspace: string | null;
  /** Worktree branch the session runs on, or null when it works in the workspace itself. */
  gitBranch: string | null;
  /** Owning project, or null for sessions outside any project. */
  projectId: string | null;
  createdAt: number;
  updatedAt: number;
}

export interface ExtensionSessionPage {
  sessions: ExtensionSessionSummary[];
  nextCursor: string | null;
  hasMore: boolean;
}

export const SESSION_PAGE_DEFAULT_LIMIT = 25;
export const SESSION_PAGE_MAX_LIMIT = 1_000;
export const SESSIONS_LIST_ALL_MAX_PAGES = 200;
export const SESSIONS_LIST_ALL_MAX_SESSIONS = 5_000;

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
