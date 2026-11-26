// The places a workspace tab can show, the links that address them, and the
// history visits that record them. Pure types plus the reuse key, so the link
// parser, the tab store and the history store all share one definition.

import type { ComparisonChoice } from "@/lib/comparisonChoice";

/** A workspace tab's stable ID, unique within its session. */
export type TabId = string;

/** Lines `start` through `end` (inclusive), optionally at a column of `start`. */
export interface LineRange {
  start: number;
  end?: number;
  column?: number;
}

/** A comparison other than the session's own: `base` against `head`, or the working copy. */
export interface ExplicitRange {
  base: string;
  head?: string;
}

/** The sub-views of a pull request tab. */
export type PullRequestView = "summary" | "comments" | "commits" | "changes";

export type WorkspacePlace =
  /** The new-tab page; `app: "terminal"` has the shell chooser open. */
  | { kind: "new"; app?: "terminal" }
  /** The Files tree, or a file in it. */
  | { kind: "files"; path?: string; line?: LineRange; comment?: string }
  /**
   * The session's changes (or an explicit comparison), with a file cursor. `fileTab` marks
   * a tab opened for `path` on its own, which is named for that file.
   */
  | {
      kind: "changes";
      path?: string;
      line?: LineRange;
      comment?: string;
      range?: ExplicitRange;
      /** The session's comparison this visit showed; showing the visit again applies it. */
      comparison?: ComparisonChoice;
      /** The folder, absolute, whose review showed `comparison`; absent for the workspace. */
      comparisonRoot?: string;
      fileTab?: true;
    }
  | { kind: "pulls" }
  | {
      kind: "pull";
      url: string;
      view: PullRequestView;
      focus?: "checks";
      commit?: string;
      path?: string;
      line?: LineRange;
      /** The session's pull requests show beside it, and choosing one shows it in this tab. */
      list?: true;
    }
  | { kind: "terminal"; key: string }
  | { kind: "agents" }
  /** The session's Tasks page, with its workflows. */
  | { kind: "tasks" }
  | { kind: "agent"; id: string }
  /** A side chat; no `id` is a new side chat with nothing sent yet. */
  | { kind: "chat"; id?: string }
  | { kind: "search"; query?: string }
  /** An embedded browser page; no `url` is a blank page. */
  | { kind: "web"; url?: string };

export type WorkspacePlaceKind = WorkspacePlace["kind"];

/** A place plus where to show it: another session's sidebar, or a specific tab. */
export interface WorkspaceLink {
  place: WorkspacePlace;
  session?: string;
  tab?: TabId;
}

/** Where a browser visit sits in its page's own history, when the shell reports it. */
export interface BrowserEntry {
  index: number;
}

/** One step in a session's workspace history. */
export interface Visit {
  tabId: TabId;
  place: WorkspacePlace;
  entry?: BrowserEntry;
}

/**
 * The key a tab is reused by when something opens this place, or null when the
 * place is never reused by identity (the new-tab page, a draft chat, a web page).
 * Pull request URLs must already be canonical.
 */
export function placeIdentity(place: WorkspacePlace): string | null {
  switch (place.kind) {
    case "new":
    case "web":
      return null;
    case "files":
      return place.path === undefined ? "files" : `files:${place.path}`;
    case "changes": {
      const comparison =
        place.range === undefined
          ? "changes:session"
          : `changes:${place.range.base}..${place.range.head ?? ""}`;
      // A file's own tab never stands in for the review of every file, nor it for that tab.
      return place.fileTab && place.path !== undefined ? `${comparison}:${place.path}` : comparison;
    }
    case "pulls":
      return "pulls";
    case "pull":
      // A pull request beside the list never stands in for its own tab, nor it for the list.
      return place.list ? `pull-list:${place.url}` : `pull:${place.url}`;
    case "terminal":
      return `terminal:${place.key}`;
    case "agents":
      return "agents";
    case "tasks":
      return "tasks";
    case "agent":
      return `agent:${place.id}`;
    case "chat":
      return place.id === undefined ? null : `chat:${place.id}`;
    case "search":
      return "search";
  }
}
