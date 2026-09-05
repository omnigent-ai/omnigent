import type {
  ExtensionContext,
  ExtensionProjectSummary,
  ExtensionSessionPage,
  ExtensionSessionSummary,
} from "@omnigent/extension-sdk";

const INITIAL_SESSION_PAGE_LIMIT = 25;
const SESSION_PAGE_LIMIT = 1_000;
const MAX_SESSION_PAGES = 200;
const MAX_SESSIONS = 5_000;

export interface SessionLoadProgress {
  sessions: ExtensionSessionSummary[];
  hasMore: boolean;
}

export function canReadSessions(context: ExtensionContext): boolean {
  return context.capabilities.includes("sessions.listPage");
}

export function canReadProjects(context: ExtensionContext): boolean {
  return context.capabilities.includes("projects.list");
}

export function canCreateProjects(context: ExtensionContext): boolean {
  return context.capabilities.includes("projects.create");
}

export async function loadSessions(
  context: ExtensionContext,
  onProgress?: (progress: SessionLoadProgress) => void | Promise<void>,
): Promise<ExtensionSessionSummary[]> {
  if (!canReadSessions(context)) {
    throw new Error("Canvas requires the sessions.read permission");
  }
  const sessions: ExtensionSessionSummary[] = [];
  const seenCursors = new Set<string>();
  let after: string | null = null;
  for (let pageNumber = 0; pageNumber < MAX_SESSION_PAGES; pageNumber += 1) {
    const page: ExtensionSessionPage = await context.sessions.listPage({
      after,
      limit: pageNumber === 0 ? INITIAL_SESSION_PAGE_LIMIT : SESSION_PAGE_LIMIT,
    });
    sessions.push(...page.sessions);
    if (sessions.length > MAX_SESSIONS) {
      throw new Error("Canvas session load exceeded 5,000 sessions");
    }
    await onProgress?.({ sessions: [...sessions], hasMore: page.hasMore });
    if (!page.hasMore) return sessions;
    if (!page.nextCursor) {
      throw new Error("Canvas session load received no next cursor");
    }
    if (seenCursors.has(page.nextCursor)) {
      throw new Error("Canvas session load received a repeated cursor");
    }
    seenCursors.add(page.nextCursor);
    after = page.nextCursor;
  }
  throw new Error("Canvas session load exceeded 200 pages");
}

// Without the projects capability every session lands on the Main canvas.
export async function loadProjects(
  context: ExtensionContext,
): Promise<ExtensionProjectSummary[]> {
  if (!canReadProjects(context)) return [];
  return context.projects.list();
}
