// Module-scope store for the in-flight local session import, so the sidebar
// can show it and the Settings panel can pick it back up after a remount.

import type { QueryClient } from "@tanstack/react-query";
import { create } from "zustand";
import { markConversationRead } from "@/hooks/useUnseenConversations";
import {
  getSessionSlim,
  importLocalSessions,
  type ImportedSessionRef,
  type ImportSourceSelector,
  type LocalImportResult,
} from "@/lib/sessionsApi";
import { reloadConversation } from "@/store/chatStore";

export interface SessionImportRequest {
  hostId: string;
  source: ImportSourceSelector;
  limit: number;
  /** Import one known harness session instead of the recent ones. */
  sessionId?: string;
  /** Replace an earlier import of `sessionId` with its current transcript. */
  force?: boolean;
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

// Replacing is the user's own update, so it must not flag the session unread.
async function markReplacedSessionSeen(id: string): Promise<void> {
  try {
    // Use the server's timestamp; this client's clock can lag behind it.
    const session = await getSessionSlim(id);
    markConversationRead(id, session.updatedAt ?? session.createdAt);
  } catch {
    // Read state is cosmetic; the replacement itself already succeeded.
  }
}

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
      const force = sessionId !== undefined && request.force === true;
      const seenMarks: Promise<void>[] = [];
      const onSession = ({ id }: ImportedSessionRef) => {
        set((state) => ({ importedCount: state.importedCount + 1 }));
        // A replaced session keeps its id, so cached copies of the old transcript must go.
        if (force) {
          reloadConversation(id).catch(() => {
            // The replacement succeeded; opening the session again reloads it.
          });
          void queryClient.invalidateQueries({ queryKey: ["session", id] });
          seenMarks.push(markReplacedSessionSeen(id));
        }
      };
      const result =
        sessionId !== undefined
          ? await importLocalSessions(hostId, source, limit, onSession, sessionId, force)
          : await importLocalSessions(hostId, source, limit, onSession);
      await Promise.all(seenMarks);
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
