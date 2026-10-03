// Pure helpers for the Design page (`/design`): pick the workspaces to scan
// from recent sessions and turn their deck searches and kit reads into
// render-ready groups. Free of React and fetch so it is unit-testable.

import type { Conversation, ProjectSummary } from "@/hooks/useConversations";
import { isTopLevelActive } from "@/canvas/canvasSessions";
import { DESIGN_KIT_MAX_BYTES, parseDesignKit, type KitFile } from "@/shell/codeViewerHelpers";
import { conversationDisplayLabel, sessionBelongsToProject } from "@/shell/sidebarNav";

/** Only this many recent sessions are scanned; a server index replaces the scan later. */
export const DESIGN_SESSION_CAP = 50;
export const DECK_SUFFIX = ".slides.html";
/** The search endpoint needs a name substring; the glob narrows it to decks. */
export const DECK_SEARCH_QUERY = DECK_SUFFIX;
export const DECK_INCLUDE_GLOB = `**/*${DECK_SUFFIX}`;
const EXCLUDED_SEGMENTS = new Set([".worktrees", "node_modules"]);

/** One workspace to scan, read through its most recent session. */
export interface DesignWorkspace {
  /** Workspace path without a trailing slash; the group key. */
  path: string;
  session: Conversation;
  /** The session's project name, else the workspace folder name. */
  label: string;
}

export interface DesignDeck {
  sessionId: string;
  /** Path relative to the workspace. */
  path: string;
  name: string;
  sessionTitle: string;
}

export type KitIndicator =
  { status: "none" } | { status: "ok"; name: string } | { status: "invalid"; reason: string };

export type KitIndicatorState = KitIndicator | { status: "loading" };

export type DeckSearchState =
  | { status: "loading" }
  | { status: "ok"; paths: string[] }
  | { status: "unavailable" }
  | { status: "error"; message: string };

export interface DesignGroup {
  workspace: DesignWorkspace;
  status: "loading" | "ready" | "unavailable" | "error";
  decks: DesignDeck[];
  kit: KitIndicatorState;
  error?: string;
}

function trimSlashes(path: string): string {
  return path.length > 1 ? path.replace(/[/\\]+$/, "") : path;
}

function folderName(path: string): string {
  return path.split(/[/\\]/).filter(Boolean).at(-1) ?? path;
}

/**
 * The workspaces of the most recent top-level, non-archived sessions, newest
 * first. The cap applies to sessions before deduping, and the most recent
 * session of each workspace is the one every read goes through.
 */
export function selectDesignWorkspaces(
  sessions: readonly Conversation[],
  projects: readonly ProjectSummary[],
  viewerId: string | null,
  cap: number = DESIGN_SESSION_CAP,
): DesignWorkspace[] {
  const recent = sessions
    .filter(isTopLevelActive)
    .sort((a, b) => b.updated_at - a.updated_at || a.id.localeCompare(b.id))
    .slice(0, cap);
  const byPath = new Map<string, DesignWorkspace>();
  for (const session of recent) {
    if (!session.workspace) continue;
    const path = trimSlashes(session.workspace);
    if (byPath.has(path)) continue;
    const project = projects.find((p) => sessionBelongsToProject(session, p, viewerId));
    byPath.set(path, { path, session, label: project?.name ?? folderName(path) });
  }
  return [...byPath.values()];
}

/** A `*.slides.html` file outside nested worktrees and dependencies. */
export function isDeckPath(path: string): boolean {
  const segments = path.split("/");
  const name = segments.at(-1) ?? "";
  if (!name.endsWith(DECK_SUFFIX) || name === DECK_SUFFIX) return false;
  return !segments.some((segment) => EXCLUDED_SEGMENTS.has(segment));
}

export function deckName(path: string): string {
  return (path.split("/").at(-1) ?? path).slice(0, -DECK_SUFFIX.length);
}

/** Kit indicator from `kit.json` alone (`null` when the file does not exist). */
export function kitIndicator(file: KitFile | null): KitIndicator {
  if (!file) return { status: "none" };
  if (file.truncated || file.bytes > DESIGN_KIT_MAX_BYTES) {
    return {
      status: "invalid",
      reason: `kit.json is larger than ${DESIGN_KIT_MAX_BYTES / 1024 / 1024} MB`,
    };
  }
  if (file.encoding !== "utf-8")
    return { status: "invalid", reason: "kit.json is not a text file" };
  try {
    return { status: "ok", name: parseDesignKit(file.content).name };
  } catch (e) {
    return { status: "invalid", reason: e instanceof Error ? e.message : String(e) };
  }
}

/**
 * One group per workspace, in workspace order. A finished search with no decks
 * drops its group; loading, unavailable, and failed searches keep theirs so
 * the user sees why a workspace has nothing listed.
 */
export function buildDesignGroups(
  workspaces: readonly DesignWorkspace[],
  searches: readonly (DeckSearchState | undefined)[],
  kits: readonly (KitIndicatorState | undefined)[],
): DesignGroup[] {
  const groups: DesignGroup[] = [];
  workspaces.forEach((workspace, i) => {
    const search = searches[i] ?? { status: "loading" };
    const kit = kits[i] ?? { status: "loading" };
    if (search.status !== "ok") {
      groups.push({
        workspace,
        status: search.status,
        decks: [],
        kit,
        ...(search.status === "error" ? { error: search.message } : {}),
      });
      return;
    }
    const sessionTitle = conversationDisplayLabel(workspace.session);
    const decks = search.paths
      .filter(isDeckPath)
      .sort((a, b) => a.localeCompare(b))
      .map((path) => ({
        sessionId: workspace.session.id,
        path,
        name: deckName(path),
        sessionTitle,
      }));
    if (decks.length > 0) groups.push({ workspace, status: "ready", decks, kit });
  });
  return groups;
}

/** "No decks yet" shows only once the session list settled and nothing is left to show. */
export function isDesignListEmpty(
  groups: readonly DesignGroup[],
  sessionsSettled: boolean,
): boolean {
  return sessionsSettled && groups.length === 0;
}
