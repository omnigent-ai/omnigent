// Module-scope store for the in-flight local session import, so the sidebar
// can show it and the Settings panel can pick it back up after a remount.

import type { QueryClient } from "@tanstack/react-query";
import { create } from "zustand";
import {
  ApiError,
  importErrorFromException,
  importLocalSessions,
  type ImportedSessionRef,
  type ImportErrorInfo,
  type ImportProgress,
  type ImportSourceSelector,
  type LocalImportResult,
} from "@/lib/sessionsApi";

export interface SessionImportRequest {
  hostId: string;
  source: ImportSourceSelector;
  limit: number;
  /** Import one known harness session instead of the recent ones. */
  sessionId?: string;
  /** Resolved display name of the chosen host, for an offline-import message. */
  hostName?: string | null;
}

// Import codes that mean the picker's view of the host is stale: refetch the
// host list so an offline machine drops out instead of failing again.
const HOST_STALE_IMPORT_CODES = new Set([
  "host_offline",
  "host_unreachable",
  "host_disconnected",
  "host_unresponsive",
]);

/** Whether an import error should refresh the host list. */
function hostListLooksStale(error: ImportErrorInfo, thrown: unknown): boolean {
  if (error.code !== null) return HOST_STALE_IMPORT_CODES.has(error.code);
  // A server that predates import codes sends its "host is offline" as a bare 409.
  return thrown instanceof ApiError && thrown.status === 409;
}

interface SessionImportState {
  /** True from the Import click until the stream settles with a result or error. */
  importing: boolean;
  importedCount: number;
  /** Sessions imported this run, in arrival order; drives the inline list. */
  streamed: ImportedSessionRef[];
  /** Latest `progress` event, for the "7 of 20" line; null before the first one. */
  progress: ImportProgress | null;
  request: SessionImportRequest | null;
  result: LocalImportResult | null;
  /** Why the whole import stopped; null while running or on a clean finish. */
  error: ImportErrorInfo | null;
  /** Run one import; resolves when the stream settles. Ignored while one is in flight. */
  start: (request: SessionImportRequest, queryClient: QueryClient) => Promise<void>;
}

// The server persists each session as its frame arrives, so the sidebar list
// is refreshed on this cadence while the import is in flight.
const LIST_REFRESH_MS = 5000;

export const useSessionImportStore = create<SessionImportState>((set, get) => ({
  importing: false,
  importedCount: 0,
  streamed: [],
  progress: null,
  request: null,
  result: null,
  error: null,
  start: async (request, queryClient) => {
    if (get().importing) return;
    set({
      importing: true,
      importedCount: 0,
      streamed: [],
      progress: null,
      request,
      result: null,
      error: null,
    });
    const refreshList = () => void queryClient.invalidateQueries({ queryKey: ["conversations"] });
    const refresh = setInterval(refreshList, LIST_REFRESH_MS);
    // Why the whole import stopped: a stream `error`, a dropped connection, or a
    // pre-stream HTTP error. Kept alongside whatever partial tally arrived.
    let importError: ImportErrorInfo | null = null;
    let thrown: unknown = null;
    try {
      const { hostId, source, limit, sessionId } = request;
      const onSession = (session: ImportedSessionRef) =>
        set((state) => ({
          importedCount: state.importedCount + 1,
          streamed: [...state.streamed, session],
        }));
      const result = await importLocalSessions(hostId, source, limit, onSession, sessionId, {
        onProgress: (progress) => set({ progress }),
      });
      set({ result });
      importError = result.error;
    } catch (e) {
      thrown = e;
      importError = importErrorFromException(e, { hostName: request.hostName ?? null });
    } finally {
      clearInterval(refresh);
      set({ error: importError, importing: false });
    }
    if (importError !== null && hostListLooksStale(importError, thrown)) {
      void queryClient.invalidateQueries({ queryKey: ["hosts"] });
    }
    // Newly imported sessions land in the sidebar list.
    await queryClient.invalidateQueries({ queryKey: ["conversations"] });
  },
}));
