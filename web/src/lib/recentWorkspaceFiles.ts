// Files recently opened in each session, newest first, which Go to File lists before a
// query. Kept for the most recently used sessions only, so storage stays bounded.

export const RECENT_WORKSPACE_FILES_STORAGE_KEY = "omnigent.recentWorkspaceFiles";
export const RECENT_FILES_PER_SESSION = 50;
export const RECENT_FILE_SESSIONS = 30;

interface SessionRecentFiles {
  sessionId: string;
  paths: string[];
}

function readAll(): SessionRecentFiles[] {
  try {
    const raw = window.localStorage.getItem(RECENT_WORKSPACE_FILES_STORAGE_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter(
        (entry): entry is SessionRecentFiles =>
          typeof entry?.sessionId === "string" && Array.isArray(entry.paths),
      )
      .map(({ sessionId, paths }) => ({
        sessionId,
        paths: paths
          .filter((path): path is string => typeof path === "string")
          .slice(0, RECENT_FILES_PER_SESSION),
      }))
      .slice(0, RECENT_FILE_SESSIONS);
  } catch {
    // Corrupt or inaccessible storage only loses recents.
    return [];
  }
}

/** The session's recently opened files, most recent first. */
export function readRecentWorkspaceFiles(sessionId: string): string[] {
  return readAll().find((entry) => entry.sessionId === sessionId)?.paths ?? [];
}

/** Notes that `path` was just opened in the session. */
export function recordRecentWorkspaceFile(sessionId: string, path: string): void {
  const sessions = readAll();
  const previous = sessions.find((entry) => entry.sessionId === sessionId)?.paths ?? [];
  const paths = [path, ...previous.filter((existing) => existing !== path)].slice(
    0,
    RECENT_FILES_PER_SESSION,
  );
  const next = [
    { sessionId, paths },
    ...sessions.filter((entry) => entry.sessionId !== sessionId),
  ].slice(0, RECENT_FILE_SESSIONS);
  try {
    window.localStorage.setItem(RECENT_WORKSPACE_FILES_STORAGE_KEY, JSON.stringify(next));
  } catch {
    // Quota or access errors shouldn't break opening files.
  }
}
