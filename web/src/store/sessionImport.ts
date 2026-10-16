// Module-scope store for the in-flight local session import, so the sidebar
// can show it and the Settings panel can pick it back up after a remount.

import type { QueryClient } from "@tanstack/react-query";
import { create } from "zustand";
import {
  importLocalSessions,
  type ImportSourceSelector,
  type LocalImportResult,
} from "@/lib/sessionsApi";

export interface SessionImportRequest {
  hostId: string;
  source: ImportSourceSelector;
  limit: number;
  /** Import one known harness session instead of the recent ones. */
  sessionId?: string;
}

interface SessionImportState {
  /** True from the Import click until the stream settles with a result or error. */
  importing: boolean;
  importedCount: number;
  request: SessionImportRequest | null;
  result: LocalImportResult | null;
  error: string | null;
  /** Run one import; resolves when the stream settles. Ignored while one is in flight. */
  start: (request: SessionImportRequest, queryClient: QueryClient) => Promise<void>;
}

// The server persists each session as its frame arrives, so the sidebar list
// is refreshed on this cadence while the import is in flight.
const LIST_REFRESH_MS = 5000;

export const useSessionImportStore = create<SessionImportState>((set, get) => ({
  importing: false,
  importedCount: 0,
  request: null,
  result: null,
  error: null,
  start: async (request, queryClient) => {
    if (get().importing) return;
    set({ importing: true, importedCount: 0, request, result: null, error: null });
    const refreshList = () => void queryClient.invalidateQueries({ queryKey: ["conversations"] });
    const refresh = setInterval(refreshList, LIST_REFRESH_MS);
    try {
      const { hostId, source, limit, sessionId } = request;
      const onSession = () => set((state) => ({ importedCount: state.importedCount + 1 }));
      const result =
        sessionId !== undefined
          ? await importLocalSessions(hostId, source, limit, onSession, sessionId)
          : await importLocalSessions(hostId, source, limit, onSession);
      set({ result });
    } catch (e) {
      set({ error: e instanceof Error ? e.message : "Import failed. Try again." });
    } finally {
      clearInterval(refresh);
      set({ importing: false });
    }
    // Newly imported sessions land in the sidebar list.
    await queryClient.invalidateQueries({ queryKey: ["conversations"] });
  },
}));
