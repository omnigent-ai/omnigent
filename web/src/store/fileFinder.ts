import { type RefObject, useEffect, useLayoutEffect, useRef } from "react";
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
  /** Whether that session's host also searches its files' text. */
  textSearchAvailable: boolean;
  request: FileFinderRequest | null;
  /** A shown Search page should take the keyboard, once its field is on screen. */
  textSearchFocus: { sessionId: string; id: number } | null;
  pane: FileFinderPane | null;
  setAvailableSession: (sessionId: string | null, options?: { textSearch?: boolean }) => void;
  /** Clears `id` once shown; a newer request is kept. */
  finishRequest: (id: number) => void;
}

let nextRequestId = 1;

export const useFileFinderStore = create<FileFinderState>((set) => ({
  availableSessionId: null,
  textSearchAvailable: false,
  request: null,
  textSearchFocus: null,
  pane: null,
  setAvailableSession: (sessionId, options = {}) =>
    set({
      availableSessionId: sessionId,
      textSearchAvailable: sessionId !== null && options.textSearch === true,
    }),
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
  if (store.availableSessionId !== sessionId || !store.textSearchAvailable) return false;
  useFileFinderStore.setState({
    request: { id: nextRequestId++, sessionId, ...options, kind: "text" },
  });
  return true;
}

/** Asks `sessionId`'s Search page to take the keyboard once it shows, as a text search wants. */
export function requestTextSearchFocus(sessionId: string): void {
  useFileFinderStore.setState({ textSearchFocus: { sessionId, id: nextRequestId++ } });
}

/** How many frames a focus request waits for its hidden Search page to show, about a second. */
const TEXT_SEARCH_FOCUS_FRAMES = 60;

/**
 * Focuses `input` when a text search asked `sessionId`'s Search page for the keyboard. A page
 * kept mounted while hidden can't take focus until it's selected, so the request is kept, and
 * tried again each frame, until the field has focus or the wait ends.
 */
export function useTextSearchFocus(
  sessionId: string,
  input: RefObject<HTMLInputElement | null>,
): void {
  const focus = useFileFinderStore((state) => state.textSearchFocus);
  useEffect(() => {
    if (!focus || focus.sessionId !== sessionId) return;
    const settle = () =>
      useFileFinderStore.setState((state) =>
        state.textSearchFocus?.id === focus.id ? { textSearchFocus: null } : state,
      );
    let frame = 0;
    let frames = 0;
    const attempt = () => {
      const field = input.current;
      field?.focus();
      if ((field && document.activeElement === field) || ++frames >= TEXT_SEARCH_FOCUS_FRAMES) {
        settle();
        return;
      }
      frame = requestAnimationFrame(attempt);
    };
    attempt();
    return () => cancelAnimationFrame(frame);
  }, [focus, sessionId, input]);
}

/** Whether a text search can open on `sessionId`'s files. */
export function useTextSearchAvailable(sessionId: string | undefined): boolean {
  return useFileFinderStore(
    (state) => !!sessionId && state.availableSessionId === sessionId && state.textSearchAvailable,
  );
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
  { textSearch = false }: { textSearch?: boolean } = {},
): void {
  const showRef = useRef(show);
  useLayoutEffect(() => {
    showRef.current = show;
  });
  useEffect(() => {
    useFileFinderStore.getState().setAvailableSession(sessionId, { textSearch });
  }, [sessionId, textSearch]);
  // A host that goes away withdraws its session, unless a newer host already offers another.
  useEffect(
    () => () => {
      const store = useFileFinderStore.getState();
      if (store.availableSessionId === sessionId) store.setAvailableSession(null);
    },
    [sessionId],
  );
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
