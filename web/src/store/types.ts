// Shared types for the chat store and renderer.

/**
 * Lifecycle of the most recently sent response.
 *
 * The store keeps a single `activeResponse` — the response currently
 * in flight or the most recent terminal one — and the renderer reads
 * its `state` to decorate the matching assistant bubble (showing
 * "Working…" while streaming, an error banner on failure, a
 * cancelled marker when stopped).
 *
 * Cleared (set to `null`) on the next send or on `switchTo`, so
 * non-terminal lifecycle never leaks across conversations or
 * follow-up sends. On terminal states (cancelled / failed /
 * incomplete) the value is kept around long enough for the user to
 * see the result; only the next send clears it.
 */
export interface ActiveResponse {
  responseId: string;
  state: "streaming" | "completed" | "cancelled" | "failed" | "incomplete";
  /** Free-form error message (HTTP error, parse error, abort). Empty
   *  for non-failure states. */
  error: string | null;
  /** Epoch ms when a terminal edge finalized the turn. Bounds the
   *  stray-idle revive: deltas shortly after a finalize contradict it
   *  (revive); a scheduled wake's deltas arrive minutes later and
   *  belong to the NEXT turn (no revive). */
  completedAt?: number;
}

/** The attachment whose upload failed, the server's reason, and the user's two choices. */
export interface UploadFailure {
  /** The failed file's pending id, as its chip names it; absent when the message as a whole failed. */
  fileId?: string;
  /** The failure's text, as the send's error shows it. */
  message: string;
  /** The send's id: its reload record is the parked entry's, and recovery must leave it alone. */
  stableId: string;
  /** Re-uploads the same files and sends the message once they're in. */
  retry: () => void;
  /** Drops the message: it is not restored anywhere. */
  remove: () => void;
}
