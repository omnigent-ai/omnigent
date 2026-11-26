// Serves the finder's requests: Go to File opens its dialog on Files, a text search on Text,
// and Search in Sessions… on Sessions. Every file entry point (the Files page's button, ⌘K, the
// chords, the New tab page) asks through the open session's request; Sessions needs no session.

import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import {
  type FileFinderRequest,
  requestFileFinder,
  requestTextSearch,
  requestTextSearchFocus,
  useFileFinderHost,
} from "@/store/fileFinder";
import { useSessionSearchStore } from "@/store/sessionSearch";

import { handSearchToSearchPage } from "../contentSearchCache";
import type { OpenFileOptions } from "../FileViewerContext";
import { finderModes, GoToFileDialog, type GoToFileMode } from "./GoToFileDialog";
import { useGoToFileCommands } from "./goToFileCommands";
import { useSessionSearchPaletteCommand } from "./sessionSearchCommand";

export interface GoToFileHostProps {
  /**
   * The session whose files are searched; null hosts no file search, so every file entry point
   * is unavailable, and the finder offers Sessions alone.
   */
  sessionId: string | null;
  openFile: (path: string, options?: OpenFileOptions) => void;
  /** Shows the Search page for the session's files, for a text search opened there. */
  showTextSearch: () => void;
  /** Whether the session's files can be searched for text; without it Search Text isn't offered. */
  textSearch: boolean;
}

export interface OpenRequest {
  /**
   * Changes with each opening, so a new request starts the dialog fresh. Named for the store
   * the request came from, since file and session requests count separately.
   */
  key: string;
  query: string | undefined;
  mode: GoToFileMode;
}

/** The dialog's key for `request`: its store, then its id there. */
export function openRequestKey(request: {
  id: number;
  kind: FileFinderRequest["kind"] | "sessions";
}): string {
  return `${request.kind === "sessions" ? "sessions" : "files"}:${request.id}`;
}

/**
 * What the dialog shows after `request`. One that brings a query, even an empty one, opens
 * afresh on it. Otherwise a request for the mode showing toggles it closed, and one for the
 * other mode switches to it and keeps what was typed.
 */
export function nextOpenRequest(
  current: OpenRequest | null,
  request: Pick<FileFinderRequest, "id" | "query" | "toggle"> & {
    kind: FileFinderRequest["kind"] | "sessions";
  },
): OpenRequest | null {
  const mode: GoToFileMode = request.kind;
  if (!current || request.query !== undefined) {
    return { key: openRequestKey(request), query: request.query, mode };
  }
  if (current.mode !== mode) return { ...current, mode };
  return request.toggle ? null : current;
}

export function GoToFileHost({
  sessionId,
  openFile,
  showTextSearch,
  textSearch,
}: GoToFileHostProps) {
  const queryClient = useQueryClient();
  const [opened, setOpened] = useState<OpenRequest | null>(null);
  useFileFinderHost(
    sessionId,
    (request) => setOpened((current) => nextOpenRequest(current, request)),
    { textSearch },
  );
  // Search in Sessions… asks here, open session or not.
  const sessionSearch = useSessionSearchStore((state) => state.request);
  useEffect(() => {
    if (!sessionSearch) return;
    const sessionRequest = {
      id: sessionSearch.id,
      kind: "sessions" as const,
      query: sessionSearch.query,
      toggle: false,
    };
    setOpened((current) => nextOpenRequest(current, sessionRequest));
    // Taken once, so a host that mounts later, such as on the next session, doesn't reopen it.
    useSessionSearchStore.setState((state) =>
      state.request?.id === sessionSearch.id ? { request: null } : state,
    );
  }, [sessionSearch]);
  useSessionSearchPaletteCommand();
  useGoToFileCommands(sessionId, {
    textSearch,
    goToFile: ({ toggle }) => {
      if (sessionId) requestFileFinder(sessionId, { toggle });
    },
    searchText: ({ toggle }) => {
      if (sessionId) requestTextSearch(sessionId, { toggle });
    },
  });
  if (!opened) return null;
  const modes = finderModes(sessionId !== null, textSearch);
  // A tab this host can't serve, such as Text where nothing is searchable, shows the first one.
  const mode = modes.includes(opened.mode) ? opened.mode : modes[0];
  return (
    <GoToFileDialog
      key={opened.key}
      sessionId={sessionId}
      open
      onOpenChange={(open) => {
        if (!open) setOpened(null);
      }}
      mode={mode}
      onModeChange={(next) => setOpened((current) => current && { ...current, mode: next })}
      textSearch={textSearch}
      initialQuery={opened.query}
      openFile={openFile}
      openInSearch={(input, result) => {
        if (!sessionId) return;
        // The page shows at once; the search lands once any search it still runs is cancelled.
        void handSearchToSearchPage(queryClient, sessionId, input, result);
        requestTextSearchFocus(sessionId);
        showTextSearch();
      }}
    />
  );
}
