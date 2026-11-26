// Context that lets any component in the chat tree open a file in the
// FileViewer panel without prop-drilling through AppShell → Outlet → ChatPage
// → BlockRenderer → ToolCard.

import { createContext, useContext, useMemo } from "react";

import type { PullRequestSubtab } from "./pullRequestPanelView";

/**
 * A 1-based citation target. Object identity is the navigation request key:
 * preserve it when forwarding or remounting; create a fresh object for each click.
 */
export interface FilePosition {
  line: number;
  column?: number;
  /** Last line of a cited range (`file.ts:10-20`), greater than `line`. */
  endLine?: number;
  /** Column just past a cited range's end, on `endLine` (or `line`). */
  endColumn?: number;
}

/**
 * A request to show a file in its diff, as a Changes-list click makes. Object
 * identity is the request key, as for `FilePosition`.
 */
export interface FileDiffRequest {
  readonly view: "diff";
}

export type OpenFileOptions = Partial<FilePosition>;

/** `path` is where the viewer goes next; null when its tab closes or the session is left. */
export type FileNavigationGuard = (
  path: string | null,
  options: OpenFileOptions | undefined,
  navigate: () => void,
) => void;

/** What to show of a session's pull requests: all of them, or one pull request. */
export type PullRequestsTarget =
  | { kind: "overview" }
  /**
   * One PR; with `list`, in the tab that lists them beside it, as choosing it there does.
   * With `overview`, a tab already showing it returns to its Overview too. A new tab opens on
   * `view` unless the URL names a section; one already showing the PR keeps its own.
   */
  | { kind: "pull"; url: string; list?: true; overview?: true; view?: PullRequestSubtab };

/**
 * `classic: "open"` opens Classic's Pull Requests tab as `openGithubTab` does;
 * `"stay"` leaves Classic as it is, for callers whose panel already shows the change.
 */
export interface ShowPullRequestsOptions {
  classic: "open" | "stay";
  /** In the tabs sidebar, open or keep the tab behind the current one, as a browser's ⌘-click does. */
  background?: boolean;
}

interface FileViewerContextType {
  openFile: (path: string, options?: OpenFileOptions) => void;
  /** The mounted viewer confirms dirty navigation before selection or URL changes. */
  registerNavigationGuard?: (guard: FileNavigationGuard) => () => void;
  /** Runs the registered guards, then `commit` once the viewer allows leaving its file. */
  guardFileNavigation?: FileNavigationGuard;
  /** Open Pull Requests in the workspace rail or mobile drawer. */
  openGithubTab: (prUrl?: string) => void;
  /**
   * Show all pull requests or one of them: in the tabs sidebar, its own tab
   * (revealing the sidebar); elsewhere as `options` says. Absent where only
   * `openGithubTab` exists, such as a grid pane.
   */
  showPullRequests?: (target: PullRequestsTarget, options: ShowPullRequestsOptions) => void;
  /** Show a host-absolute folder in the Files tree; absent where no Files panel exists. */
  openFolder?: (absolutePath: string) => void;
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
  /** Host running the session, so desktop-only file actions can check it is this machine. */
  sessionHostId?: string | null;
}

export const FileViewerContext = createContext<FileViewerContextType | null>(null);

/**
 * Returns the `openFile` callback when rendered inside AppShell, or
 * `null` when used outside of it (tests, Storybook, etc.).
 */
export function useFileViewer(): ((path: string, options?: OpenFileOptions) => void) | null {
  return useContext(FileViewerContext)?.openFile ?? null;
}

/** Returns the callback that shows a folder in the Files tree, or `null` when there is none. */
export function useFolderOpener(): ((absolutePath: string) => void) | null {
  return useContext(FileViewerContext)?.openFolder ?? null;
}

/**
 * Returns a callback that opens the Pull Requests rail tab or mobile drawer, or `null`
 * when used outside AppShell (tests, Storybook).
 */
export function useOpenGithubTab(): ((prUrl?: string) => void) | null {
  return useContext(FileViewerContext)?.openGithubTab ?? null;
}

/** Show a session's pull requests from this chat; falls back to `openGithubTab` where that's all there is. */
export function useShowPullRequests():
  ((target: PullRequestsTarget, options: ShowPullRequestsOptions) => void) | null {
  const context = useContext(FileViewerContext);
  return useMemo(() => {
    if (!context) return null;
    if (context.showPullRequests) return context.showPullRequests;
    const { openGithubTab } = context;
    return (target: PullRequestsTarget, options: ShowPullRequestsOptions) => {
      if (options.classic === "open")
        openGithubTab(target.kind === "pull" ? target.url : undefined);
    };
  }, [context]);
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
