import type { QueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import type { useNavigate } from "@/lib/routing";
import { undoArchiveConversations, type Conversation } from "@/hooks/useConversations";

type NavigateFn = ReturnType<typeof useNavigate>;

/**
 * How long the post-archive Undo pill stays on screen, in milliseconds. 3s
 * reads as long enough to catch and act on without lingering.
 */
const ARCHIVE_UNDO_DURATION_MS = 3000;

/**
 * Absolute cap on the merged pill's total on-screen life, measured from the
 * FIRST archive in the batch. Merges shorten the remaining countdown toward
 * this cap rather than resetting a fresh 3s each time, so the pill can't outlive
 * the server's per-session teardown grace (`_ARCHIVE_STOP_UNDO_GRACE_S`, 8s).
 * If it could, the earliest-archived session's runner would be stopped at the
 * grace while the pill still offered Undo — undoing then would restore a row
 * over a dead pane. Kept below the grace with margin for the unarchive round
 * trip. MUST stay < that grace.
 */
const ARCHIVE_UNDO_MAX_LIFETIME_MS = 5000;

/** Stable id so repeated archives update ONE pill (merge). */
const ARCHIVE_UNDO_TOAST_ID = "archive-undo";

// The sessions the visible pill would undo. Archives in quick succession
// (while the pill is still up) merge into this one batch; it's cleared when the
// pill auto-closes, is dismissed, or Undo runs. Module-level so the three
// archive entry points (row menu, header menu, bulk selection) share a single
// pill rather than stacking one each. Full rows (not just ids) so Undo can
// re-inject them into the sidebar even after a refetch evicted the archived
// rows (see `undoArchiveConversations`).
let batched: Conversation[] = [];
// When the current batch's pill first appeared (epoch ms), or null if none.
let batchStartedAtMs: number | null = null;
// The most recent caller's QueryClient. Every entry point resolves the same
// app-level client, so the latest one correctly unarchives the whole batch.
let activeQueryClient: QueryClient | null = null;
let activeNavigate: NavigateFn | null = null;

function clearBatch(): void {
  batched = [];
  batchStartedAtMs = null;
  activeQueryClient = null;
  activeNavigate = null;
}

function runUndo(): void {
  const conversations = batched;
  const queryClient = activeQueryClient;
  clearBatch();
  toast.dismiss(ARCHIVE_UNDO_TOAST_ID);
  if (queryClient && conversations.length > 0) {
    void undoArchiveConversations(queryClient, conversations);
  }
}

function runViewArchived(): void {
  const navigate = activeNavigate;
  clearBatch();
  toast.dismiss(ARCHIVE_UNDO_TOAST_ID);
  navigate?.("/settings/archived");
}

/**
 * Show (or extend) the post-archive Undo pill after archiving `conversations`.
 *
 * Fire it right after kicking off the archive — like the old Settings toast, it
 * runs synchronously on the click because the archiving row unmounts on the
 * next frame (optimistic overlay). Repeated calls merge their rows into the
 * same pill, so undoing restores every session archived since the pill first
 * appeared. A failed archive reconciles its own row back and the extra row in
 * the batch is harmless — unarchiving a session that never archived is a no-op.
 *
 * A merge does NOT reset a fresh countdown: the batch has an ABSOLUTE deadline
 * (`ARCHIVE_UNDO_MAX_LIFETIME_MS` from the first archive), and a merge only
 * shortens the remaining time toward it. So the pill can't outlive the
 * earliest-archived session's server teardown grace and offer an Undo for a
 * runner already stopped. An archive arriving after the deadline starts a FRESH
 * batch rather than extending the expiring one.
 */
export function showArchiveUndoToast(
  queryClient: QueryClient,
  conversations: readonly Conversation[],
  navigate: NavigateFn,
): void {
  if (conversations.length === 0) return;
  const now = Date.now();
  // Start (or restart) the batch when there is none, or when the current one
  // has hit its absolute deadline — a late archive must not join a batch whose
  // pill is expiring, or it would extend an Undo past the server grace.
  if (batchStartedAtMs === null || now - batchStartedAtMs >= ARCHIVE_UNDO_MAX_LIFETIME_MS) {
    batched = [];
    batchStartedAtMs = now;
  }
  activeQueryClient = queryClient;
  activeNavigate = navigate;
  const seen = new Set(batched.map((c) => c.id));
  for (const conv of conversations) {
    if (!seen.has(conv.id)) {
      batched.push(conv);
      seen.add(conv.id);
    }
  }
  // Time left until the batch's absolute deadline; capped at the normal
  // single-archive duration so one archive still gets the full pill. A merge
  // shortens this toward the deadline and never adds to it.
  const remaining = Math.min(
    ARCHIVE_UNDO_DURATION_MS,
    batchStartedAtMs + ARCHIVE_UNDO_MAX_LIFETIME_MS - now,
  );
  const count = batched.length;
  toast(`Archived ${count} ${count === 1 ? "session" : "sessions"}`, {
    id: ARCHIVE_UNDO_TOAST_ID,
    duration: remaining,
    action: { label: "Undo", onClick: runUndo },
    cancel: { label: "View archived", onClick: runViewArchived },
    testId: "archive-undo-toast-item",
    onAutoClose: clearBatch,
    onDismiss: clearBatch,
  });
}

/** Test-only: drop the pending Undo batch so cases don't leak module state. */
export function resetArchiveUndoBatchForTests(): void {
  clearBatch();
}
