// Pure helpers for the Design page (`/design`): pick the workspaces to scan
// from recent sessions and turn their deck searches and kit reads into
// render-ready groups. Free of React and fetch so it is unit-testable.

import type { Conversation, ProjectSummary } from "@/hooks/useConversations";
import { isTopLevelActive } from "@/canvas/canvasSessions";
import { DESIGN_KIT_MAX_BYTES, parseDesignKit, type KitFile } from "@/shell/codeViewerHelpers";
import { conversationDisplayLabel, sessionBelongsToProject } from "@/shell/sidebarNav";
import type { DesignSystemKind } from "./designSystem";

/** Only this many recent sessions are scanned; the server index lists the rest. */
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

/** The group header badge: a design-system pointer when present, else the kit. */
export type KitIndicator =
  | { status: "none" }
  | { status: "ok"; name: string }
  | { status: "system"; name: string; kind: DesignSystemKind }
  | { status: "invalid"; reason: string; system?: true };

export type KitIndicatorState = KitIndicator | { status: "loading" };

export type DeckSearchState =
  | { status: "loading" }
  | { status: "ok"; paths: string[]; truncated: boolean }
  | { status: "unavailable" }
  | { status: "error"; message: string };

export interface DesignGroup {
  workspace: DesignWorkspace;
  /** `indexed`: listed from the server index, not scanned. */
  status: "loading" | "ready" | "unavailable" | "error" | "indexed";
  decks: DesignDeck[];
  kit: KitIndicatorState;
  /** True when the workspace search may have missed decks past the result cap. */
  truncated: boolean;
  error?: string;
  /** An indexed group whose session has no live runner or host. */
  offline?: boolean;
}

/** One deck from the server index (`GET /v1/design/artifacts`). */
export interface DesignIndexEntry {
  session_id: string;
  path: string;
  kind: "deck" | "wireframe";
  updated_at: number;
  session_title: string | null;
  workspace: string | null;
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
    byPath.set(path, { path, session, label: workspaceLabel(session, path, projects, viewerId) });
  }
  return [...byPath.values()];
}

/** Whether a session's files can be read now; unknown liveness counts as live. */
export function isSessionLive(session: Conversation): boolean {
  return session.runner_online !== false || session.host_online === true;
}

function workspaceLabel(
  session: Conversation,
  path: string,
  projects: readonly ProjectSummary[],
  viewerId: string | null,
): string {
  const project = projects.find((p) => sessionBelongsToProject(session, p, viewerId));
  return project?.name ?? folderName(path);
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
        truncated: false,
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
    // Keep a truncated empty group so the user sees the search hit its cap.
    if (decks.length > 0 || search.truncated) {
      groups.push({
        workspace,
        status: "ready",
        decks,
        kit,
        truncated: search.truncated,
      });
    }
  });
  return groups;
}

/**
 * Groups for indexed decks in workspaces the scan does not cover, in index
 * order (newest first). A deck found through several sessions is listed once,
 * through the newest. No reads run for these groups.
 */
export function indexDesignGroups(
  entries: readonly DesignIndexEntry[],
  scanned: readonly DesignWorkspace[],
  sessions: readonly Conversation[],
  projects: readonly ProjectSummary[],
  viewerId: string | null,
): DesignGroup[] {
  const covered = new Set(scanned.map((w) => w.path));
  const loaded = new Map(sessions.map((s) => [s.id, s]));
  const groups = new Map<string, DesignGroup>();
  for (const entry of entries) {
    if (!entry.workspace || !isDeckPath(entry.path)) continue;
    const path = trimSlashes(entry.workspace);
    if (covered.has(path)) continue;
    const known = loaded.get(entry.session_id);
    const session: Conversation = known ?? {
      id: entry.session_id,
      object: "conversation",
      title: entry.session_title,
      created_at: 0,
      updated_at: entry.updated_at,
      labels: {},
      permission_level: null,
      workspace: entry.workspace,
    };
    let group = groups.get(path);
    if (!group) {
      const label = workspaceLabel(session, path, projects, viewerId);
      group = {
        workspace: { path, session, label },
        status: "indexed",
        decks: [],
        kit: { status: "none" },
        offline: !known || !isSessionLive(known),
      };
      groups.set(path, group);
    }
    if (group.decks.some((deck) => deck.path === entry.path)) continue;
    group.decks.push({
      sessionId: entry.session_id,
      path: entry.path,
      name: deckName(entry.path),
      sessionTitle: conversationDisplayLabel(session),
    });
  }
  for (const group of groups.values()) group.decks.sort((a, b) => a.path.localeCompare(b.path));
  return [...groups.values()];
}

/**
 * Landing search: a group whose workspace label or session title matches keeps
 * all its decks, otherwise only decks whose name matches. Non-ready groups have
 * nothing to match and are dropped while searching.
 */
export function filterDesignGroups(groups: DesignGroup[], query: string): DesignGroup[] {
  const q = query.trim().toLowerCase();
  if (!q) return groups;
  const has = (text: string) => text.toLowerCase().includes(q);
  return groups.flatMap((group) => {
    if (group.status !== "ready" && group.status !== "indexed") return [];
    if (has(group.workspace.label)) return [group];
    const decks = group.decks.filter((deck) => has(deck.name) || has(deck.sessionTitle));
    return decks.length > 0 ? [{ ...group, decks }] : [];
  });
}

/** "No decks yet" shows only once the session list settled and nothing is left to show. */
export function isDesignListEmpty(
  groups: readonly DesignGroup[],
  sessionsSettled: boolean,
): boolean {
  return sessionsSettled && groups.length === 0;
}
