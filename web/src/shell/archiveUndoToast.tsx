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

/** Stable id so repeated archives update ONE pill (merge) and reset its timer. */
const ARCHIVE_UNDO_TOAST_ID = "archive-undo";

// The sessions the visible pill would undo. Archives in quick succession
// (while the pill is still up) merge into this one batch; it's cleared when the
// pill auto-closes, is dismissed, or Undo runs. Module-level so the three
// archive entry points (row menu, header menu, bulk selection) share a single
// pill rather than stacking one each. Full rows (not just ids) so Undo can
// re-inject them into the sidebar even after a refetch evicted the archived
// rows (see `undoArchiveConversations`).
let batched: Conversation[] = [];
// The most recent caller's QueryClient. Every entry point resolves the same
// app-level client, so the latest one correctly unarchives the whole batch.
let activeQueryClient: QueryClient | null = null;
let activeNavigate: NavigateFn | null = null;

function clearBatch(): void {
  batched = [];
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
 * same pill and reset its countdown, so undoing restores every session archived
 * since the pill first appeared. A failed archive reconciles its own row back
 * and the extra row in the batch is harmless — unarchiving a session that never
 * archived is a no-op.
 */
export function showArchiveUndoToast(
  queryClient: QueryClient,
  conversations: readonly Conversation[],
  navigate: NavigateFn,
): void {
  if (conversations.length === 0) return;
  activeQueryClient = queryClient;
  activeNavigate = navigate;
  const seen = new Set(batched.map((c) => c.id));
  for (const conv of conversations) {
    if (!seen.has(conv.id)) {
      batched.push(conv);
      seen.add(conv.id);
    }
  }
  const count = batched.length;
  toast(`Archived ${count} ${count === 1 ? "session" : "sessions"}`, {
    id: ARCHIVE_UNDO_TOAST_ID,
    duration: ARCHIVE_UNDO_DURATION_MS,
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
