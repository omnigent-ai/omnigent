import { useEffect, useLayoutEffect, useRef } from "react";
import { create } from "zustand";
import type { OpenFileOptions } from "@/shell/FileViewerContext";

// One file finder serves every entry point (the Files tab's field, ⌘K, its shortcut,
// and the file viewer's menu), so they open the same finder for the same workspace.
// Text search across files rides on the same request, since one shell opens both.

export interface FileFinderOptions {
  /** Text to start the search with, such as the key typed into the Files tab's field; an empty
   * string clears what the finder had. */
  query?: string;
  /** Close the finder instead when it's already open, as its shortcut does. */
  toggle?: boolean;
}

/** A request to open the finder, waiting for the shell to show it. */
export interface FileFinderRequest extends FileFinderOptions {
  id: number;
  sessionId: string;
  /** Find a file by name, or search the files' contents. */
  kind: "files" | "text";
}

/**
 * A session shown outside the route, such as the selected cell of a grid, which the
 * finder searches and opens results in while the route has no session of its own.
 */
export interface FileFinderPane {
  sessionId: string;
  hostId: string | null;
  /** Opens a result in the pane that shows the session. */
  openFile: (path: string, options?: OpenFileOptions) => void;
}

interface FileFinderState {
  /** The session whose workspace the finder can search, or null when there's none. */
  availableSessionId: string | null;
  request: FileFinderRequest | null;
  pane: FileFinderPane | null;
  setAvailableSession: (sessionId: string | null) => void;
  /** Clears `id` once shown; a newer request is kept. */
  finishRequest: (id: number) => void;
}

let nextRequestId = 1;

export const useFileFinderStore = create<FileFinderState>((set) => ({
  availableSessionId: null,
  request: null,
  pane: null,
  setAvailableSession: (sessionId) => set({ availableSessionId: sessionId }),
  finishRequest: (id) => set((state) => (state.request?.id === id ? { request: null } : state)),
}));

/**
 * Opens the file finder on `sessionId`'s workspace; the shared action behind every
 * entry point. Returns false when the finder can't search that session.
 */
export function requestFileFinder(sessionId: string, options: FileFinderOptions = {}): boolean {
  const store = useFileFinderStore.getState();
  if (store.availableSessionId !== sessionId) return false;
  useFileFinderStore.setState({
    request: { id: nextRequestId++, sessionId, ...options, kind: "files" },
  });
  return true;
}

/**
 * Opens text search across `sessionId`'s files, as its shortcut and ⌘K do; the
 * search explains there when it can't run. Returns false when no shell serves that session.
 */
export function requestTextSearch(sessionId: string, options: FileFinderOptions = {}): boolean {
  const store = useFileFinderStore.getState();
  if (store.availableSessionId !== sessionId) return false;
  useFileFinderStore.setState({
    request: { id: nextRequestId++, sessionId, ...options, kind: "text" },
  });
  return true;
}

/** Whether the finder can search `sessionId`'s workspace. */
export function useFileFinderAvailable(sessionId: string | undefined): boolean {
  return useFileFinderStore((state) => !!sessionId && state.availableSessionId === sessionId);
}

/**
 * Serves file-finder requests for `sessionId`: the shell that shows the finder calls
 * this once, and `show` receives each request meant for that session.
 */
export function useFileFinderHost(
  sessionId: string | null,
  show: (request: FileFinderRequest) => void,
): void {
  const showRef = useRef(show);
  useLayoutEffect(() => {
    showRef.current = show;
  });
  useEffect(() => {
    useFileFinderStore.getState().setAvailableSession(sessionId);
  }, [sessionId]);
  const request = useFileFinderStore((state) => state.request);
  useEffect(() => {
    if (!request) return;
    useFileFinderStore.getState().finishRequest(request.id);
    if (request.sessionId === sessionId) showRef.current(request);
  }, [request, sessionId]);
}

/**
 * Offers `pane` to the finder while it's set; null withdraws this caller's pane. The
 * offer changes only with the session, so a fresh `openFile` each render is fine.
 */
export function useFileFinderPane(pane: FileFinderPane | null): void {
  const openFileRef = useRef(pane?.openFile);
  useLayoutEffect(() => {
    openFileRef.current = pane?.openFile;
  });
  const sessionId = pane?.sessionId;
  const hostId = pane?.hostId ?? null;
  useEffect(() => {
    if (!sessionId) return;
    const offered: FileFinderPane = {
      sessionId,
      hostId,
      openFile: (path, options) => openFileRef.current?.(path, options),
    };
    useFileFinderStore.setState({ pane: offered });
    return () => {
      if (useFileFinderStore.getState().pane === offered) {
        useFileFinderStore.setState({ pane: null });
      }
    };
  }, [sessionId, hostId]);
}

/** The pane offered to the finder, if any. */
export function useOfferedFileFinderPane(): FileFinderPane | null {
  return useFileFinderStore((state) => state.pane);
}
