// Read-only list of the sessions the caller can access (owned + shared with
// them), used by the new-session dialog to warn when a new session would
// share an on-disk working directory with an existing one (write-conflict
// hint). `GET /v1/sessions` is scoped server-side to what the caller can
// access, so this never observes sessions they can't already see — no
// cross-user disclosure. See NewChatDialog.

import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";
import type { Conversation } from "./useConversations";

// Single-page cap on the directory scan. The hint counts only sessions the
// caller can access, so 200 covers any realistic per-user count without a
// paginating loop. A user past this many sessions would undercount the hint
// (best-effort warning), never overcount — and the `updated_at desc` sort
// below keeps the most-recently-active (likeliest still-connected) sessions
// within the cap.
const DIRECTORY_SCAN_LIMIT = 200;

/**
 * Fetch up to {@link DIRECTORY_SCAN_LIMIT} sessions the caller can access for
 * the new-session directory-conflict hint.
 *
 * `enabled` gates the request to when the dialog is open so a closed dialog
 * costs nothing. A short `staleTime` lets repeated opens reuse the cache
 * while still picking up sessions created elsewhere within a few seconds.
 *
 * @param enabled Whether to issue the request (the dialog is open).
 * @returns The TanStack Query result; `data` is the session list.
 */
export function useDirectorySessions(enabled: boolean) {
  return useQuery({
    queryKey: ["directory-sessions"],
    enabled,
    queryFn: async (): Promise<Conversation[]> => {
      const res = await authenticatedFetch(
        // `updated_at desc` matches the sessions-list convention in
        // `useConversations` and biases the capped page toward the most
        // recently active sessions, the ones most likely still connected.
        `/v1/sessions?order=desc&sort_by=updated_at&limit=${DIRECTORY_SCAN_LIMIT}`,
      );
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const body = (await res.json()) as { data: Conversation[] };
      return body.data;
    },
    staleTime: 4_000,
  });
}
