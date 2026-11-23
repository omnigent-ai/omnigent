// What ⌘K commands derive their availability from: the open session, the view, and what the
// user has selected, read with `useCommandContext()`; and the region keyboard focus was last in,
// kept apart so a focus move re-renders only what reads it (`useCommandRegion`).

import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { create, createStore, type StoreApi, useStore } from "zustand";

import type { Conversation } from "@/hooks/useConversations";
import { useSession } from "@/hooks/useSession";
import { hasSessionAccessCheckFailed, useSessionLookupOverdue } from "@/lib/sessionAccessCheck";
import { TAB_KEY_OWNING_DIALOG_OR_MENU } from "@/lib/hotkeys";
import { isEditorLevel, isOwnerLevel } from "@/lib/permissionsApi";
import type { Session } from "@/lib/types";
import {
  AVAILABLE,
  type CommandAvailability,
  HIDDEN,
  unavailableBecause,
} from "@/store/paletteCommands";

import type { RightRailTab } from "./railTabs";

const ACCESS_CHECK_FAILED_REASON = "Couldn’t check your access to this session.";

/** The session the route shows. */
export interface CommandSession {
  id: string;
  /** The effective level; `null` means the server reported none (single-user) or it's still loading. */
  permissionLevel: number | null;
  /** True once the session's own snapshot is loaded; until then the level is a sidebar row's guess. */
  permissionResolved: boolean;
  /** Its lookup failed or ran out of time, so its access can't be known until asked again. */
  accessCheckFailed?: boolean;
  /** `null` until the sidebar row or snapshot says whether it's a sub-agent. */
  topLevel: boolean | null;
  archived: boolean;
}

/**
 * The command session for `id`, from its own snapshot when loaded. Until then a
 * sidebar row supplies a provisional level, which never grants access.
 */
export function commandSessionFromSnapshot(
  id: string,
  snapshot: Session | null,
  listRow?: Conversation | null,
  accessCheckFailed = false,
): CommandSession {
  return {
    id,
    permissionLevel: snapshot ? snapshot.permissionLevel : (listRow?.permission_level ?? null),
    permissionResolved: snapshot != null,
    accessCheckFailed: snapshot == null && accessCheckFailed,
    // A list row can be a sub-agent nested in its root's family.
    topLevel: snapshot
      ? snapshot.parentSessionId == null
      : listRow
        ? listRow.parent_session_id == null
        : null,
    archived: snapshot?.archived ?? listRow?.archived ?? false,
  };
}

/**
 * The command session for `sessionId` from its own read, shared by every surface that gates on
 * it, so each agrees when that read is overdue or failed. `null` without a session.
 */
export function useCommandSession(
  sessionId: string | null | undefined,
  listRow?: Conversation | null,
): CommandSession | null {
  const id = sessionId ?? null;
  const { session: snapshot, error } = useSession(id);
  const overdue = useSessionLookupOverdue(id);
  return useMemo(
    () =>
      id
        ? commandSessionFromSnapshot(
            id,
            snapshot,
            listRow,
            hasSessionAccessCheckFailed({ snapshot, error, overdue }),
          )
        : null,
    [id, snapshot, listRow, error, overdue],
  );
}

/** The page the route shows. */
export type CommandView =
  "session" | "new-session" | "inbox" | "automations" | "settings" | "other";

/** The app region keyboard focus was last in, outside the palette and other dialogs. */
export type CommandRegion = "sidebar" | "conversation" | "workspace";

/** What the user has picked that commands can act on. */
export interface CommandSelection {
  /** Sidebar rows picked in selection mode, in no particular order. */
  sessionIds: readonly string[];
  /** The file open in the workspace viewer. */
  filePath: string | null;
}

export interface CommandContext {
  session: CommandSession | null;
  view: CommandView;
  /** The workspace tab on screen, such as Files or Terminal; `null` while the workspace is hidden. */
  workspaceTab: RightRailTab | null;
  selection: CommandSelection;
}

const NO_SESSION_IDS: readonly string[] = [];

const EMPTY_CONTEXT: CommandContext = {
  session: null,
  view: "other",
  workspaceTab: null,
  selection: { sessionIds: NO_SESSION_IDS, filePath: null },
};

const CommandContextValue = createContext<CommandContext>(EMPTY_CONTEXT);

export function useCommandContext(): CommandContext {
  return useContext(CommandContextValue);
}

export const CHECKING_ACCESS_REASON = "Checking your access to this session…";

/** The access a session command needs, mirroring the server's checks. */
export type SessionAccess = "view" | "edit" | "owner";

const HAS_ACCESS: Record<SessionAccess, (level: number | null) => boolean> = {
  view: () => true,
  edit: isEditorLevel,
  owner: isOwnerLevel,
};

/**
 * Availability for a command on the open session: hidden without one, greyed
 * while access is unconfirmed, and greyed with `deniedReason` below `access`.
 */
export function sessionCommandAvailability(
  session: CommandSession | null,
  access: SessionAccess,
  deniedReason: string,
): CommandAvailability {
  if (!session) return HIDDEN;
  if (access === "view") return AVAILABLE;
  // A sidebar row's level is provisional: a server's list may omit it, which reads as owner.
  if (!session.permissionResolved) {
    return unavailableBecause(
      session.accessCheckFailed ? ACCESS_CHECK_FAILED_REASON : CHECKING_ACCESS_REASON,
    );
  }
  if (!HAS_ACCESS[access](session.permissionLevel)) return unavailableBecause(deniedReason);
  return AVAILABLE;
}

/**
 * What a command built from `context`, with focus last in `region`, acts on. A row whose key no
 * longer matches was built for another session, view, focus, or selection.
 */
export function commandTargetKey(context: CommandContext, region: CommandRegion | null): string {
  return JSON.stringify([
    context.session?.id ?? null,
    context.view,
    region,
    context.workspaceTab,
    context.selection.sessionIds,
    context.selection.filePath,
  ]);
}

/** Sidebar selection, published by the sidebar while it's in selection mode. */
const useSessionSelectionStore = create<{ sessionIds: readonly string[] }>(() => ({
  sessionIds: NO_SESSION_IDS,
}));

/** Offer the sidebar's selected sessions to commands while the caller is mounted. */
export function usePublishSessionSelection(sessionIds: ReadonlySet<string>): void {
  useEffect(() => {
    useSessionSelectionStore.setState({
      sessionIds: sessionIds.size === 0 ? NO_SESSION_IDS : [...sessionIds],
    });
  }, [sessionIds]);
  useEffect(() => () => useSessionSelectionStore.setState({ sessionIds: NO_SESSION_IDS }), []);
}

const REGION_ATTRIBUTE = "data-command-region";

/** Props that mark an element as one of the app's command regions. */
export function commandRegionProps(region: CommandRegion): { [REGION_ATTRIBUTE]: CommandRegion } {
  return { [REGION_ATTRIBUTE]: region };
}

function isCommandRegion(value: string | null | undefined): value is CommandRegion {
  return value === "sidebar" || value === "conversation" || value === "workspace";
}

/** The region `target` sits in, or `undefined` when focus moved into a dialog and should be ignored. */
export function regionOfFocusTarget(target: EventTarget | null): CommandRegion | null | undefined {
  if (!(target instanceof Element)) return null;
  // The palette and other dialogs act on the region focus came from; a workspace sheet is one.
  if (target.closest(TAB_KEY_OWNING_DIALOG_OR_MENU)) return undefined;
  const region = target.closest(`[${REGION_ATTRIBUTE}]`)?.getAttribute(REGION_ATTRIBUTE);
  return isCommandRegion(region) ? region : null;
}

interface CommandRegionState {
  region: CommandRegion | null;
}

/** Outside a provider, focus is in no region. */
const NO_REGION_STORE = createStore<CommandRegionState>(() => ({ region: null }));
const CommandRegionStore = createContext<StoreApi<CommandRegionState>>(NO_REGION_STORE);

/** The provider's own region store, following focus while it's mounted. */
function useFocusedRegionStore(): StoreApi<CommandRegionState> {
  const [store] = useState(() => createStore<CommandRegionState>(() => ({ region: null })));
  useEffect(() => {
    const onFocusIn = (event: FocusEvent) => {
      const next = regionOfFocusTarget(event.target);
      // Focus moving within a region changes nothing anyone reads.
      if (next !== undefined && next !== store.getState().region) store.setState({ region: next });
    };
    document.addEventListener("focusin", onFocusIn);
    return () => document.removeEventListener("focusin", onFocusIn);
  }, [store]);
  return store;
}

/**
 * The region keyboard focus was last in. Only a caller reading it re-renders when focus moves
 * to another region; `when` false reads null and skips those re-renders.
 */
export function useCommandRegion(when = true): CommandRegion | null {
  return useStore(useContext(CommandRegionStore), (state) => (when ? state.region : null));
}

/** Reads the region focus is in at the moment it's called, as accepting a command needs. */
export function useCommandRegionReader(): () => CommandRegion | null {
  const store = useContext(CommandRegionStore);
  return useCallback(() => store.getState().region, [store]);
}

/** `rebasePath` maps an app path to the router's, for an app mounted under a base path. */
export function commandViewForPath(
  pathname: string,
  rebasePath: (path: string) => string = (path) => path,
): CommandView {
  const within = (path: string) => {
    const base = rebasePath(path);
    return pathname === base || pathname.startsWith(`${base}/`);
  };
  if (within("/c")) return "session";
  if (pathname === rebasePath("/")) return "new-session";
  if (within("/inbox")) return "inbox";
  if (within("/tasks")) return "automations";
  if (within("/settings")) return "settings";
  return "other";
}

/** Supplies the command context; the shell passes what it knows about the route and session. */
export function CommandContextProvider({
  session,
  view,
  workspaceTab,
  filePath,
  children,
}: {
  session: CommandSession | null;
  view: CommandView;
  workspaceTab: RightRailTab | null;
  filePath: string | null;
  children: ReactNode;
}) {
  const regionStore = useFocusedRegionStore();
  const sessionIds = useSessionSelectionStore((state) => state.sessionIds);
  const value = useMemo<CommandContext>(
    () => ({ session, view, workspaceTab, selection: { sessionIds, filePath } }),
    [session, view, workspaceTab, sessionIds, filePath],
  );
  return (
    <CommandRegionStore.Provider value={regionStore}>
      <CommandContextValue.Provider value={value}>{children}</CommandContextValue.Provider>
    </CommandRegionStore.Provider>
  );
}
