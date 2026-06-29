// Context that lets any component in the chat tree open a file in the
// FileViewer panel without prop-drilling through AppShell → Outlet → ChatPage
// → BlockRenderer → ToolCard.

import { createContext, useContext } from "react";

interface FileViewerContextType {
  openFile: (path: string) => void;
  /**
   * Returns true when `path` is a known workspace file (present in the
   * session's changed-files list). Used as the synchronous fast path for
   * clickable file-path links in the chat; paths not in this list are
   * verified against the filesystem API via the session id below.
   */
  isChangedPath: (path: string) => boolean;
  /**
   * The focused session id, so chat consumers can verify whether an
   * arbitrary referenced path exists in the workspace (not just the
   * agent-changed ones). Undefined outside AppShell.
   */
  conversationId: string | undefined;
  /**
   * Absolute workspace root, so chat consumers can collapse an absolute or
   * home-relative path the agent mentions onto a workspace-relative one.
   * Null when the session has no filesystem or it isn't loaded yet.
   */
  workspaceRoot: string | null;
  /**
   * Absolute runner home, used to expand a leading ``~`` before resolving
   * against {@link workspaceRoot}. Null when unknown.
   */
  workspaceHome: string | null;
}

export const FileViewerContext = createContext<FileViewerContextType | null>(null);

/**
 * Returns the `openFile` callback when rendered inside AppShell, or
 * `null` when used outside of it (tests, Storybook, etc.).
 */
export function useFileViewer(): ((path: string) => void) | null {
  return useContext(FileViewerContext)?.openFile ?? null;
}

// Stable fallback used when the context is absent (tests, Storybook).
// A module-level constant avoids allocating a new function on every render.
const ALWAYS_FALSE = () => false;

/**
 * Returns the `isChangedPath` predicate when rendered inside AppShell, or
 * a function that always returns `false` when used outside of it
 * (tests, Storybook, etc.).
 */
export function useIsChangedPath(): (path: string) => boolean {
  return useContext(FileViewerContext)?.isChangedPath ?? ALWAYS_FALSE;
}

/**
 * Returns the focused session id when rendered inside AppShell, or
 * `undefined` when used outside of it (tests, Storybook). Lets chat consumers
 * verify path existence against the runner filesystem API.
 */
export function useFileViewerConversationId(): string | undefined {
  return useContext(FileViewerContext)?.conversationId;
}

/**
 * Returns the workspace root and runner home for the focused session, so chat
 * consumers can collapse an absolute / home-relative path the agent mentions
 * onto a workspace-relative one. Both are null outside AppShell or before the
 * environment resolves.
 */
export function useWorkspacePaths(): { root: string | null; home: string | null } {
  const ctx = useContext(FileViewerContext);
  return { root: ctx?.workspaceRoot ?? null, home: ctx?.workspaceHome ?? null };
}
