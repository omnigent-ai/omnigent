import type { QueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { Link } from "@/lib/routing";
import { undoArchiveConversations, type Conversation } from "@/hooks/useConversations";

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

function clearBatch(): void {
  batched = [];
  activeQueryClient = null;
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

/** The pill body: "Archived N session(s). Undo" plus a small Settings link. */
function ArchiveUndoToast({ count }: { count: number }) {
  return (
    <div
      data-testid="archive-undo-toast"
      className="flex items-center gap-3 rounded-full border border-border bg-card px-4 py-2 text-sm text-foreground shadow-composer dark:bg-card-solid"
    >
      <span>
        Archived {count} {count === 1 ? "session" : "sessions"}.{" "}
        <button
          type="button"
          data-testid="archive-undo-button"
          onClick={runUndo}
          className="font-bold underline underline-offset-2 hover:text-primary"
        >
          Undo
        </button>
      </span>
      <Link
        to="/settings/archived"
        onClick={() => toast.dismiss(ARCHIVE_UNDO_TOAST_ID)}
        className="border-l border-border pl-3 text-xs text-muted-foreground hover:text-foreground hover:underline"
      >
        View in Settings
      </Link>
    </div>
  );
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
): void {
  if (conversations.length === 0) return;
  activeQueryClient = queryClient;
  const seen = new Set(batched.map((c) => c.id));
  for (const conv of conversations) {
    if (!seen.has(conv.id)) {
      batched.push(conv);
      seen.add(conv.id);
    }
  }
  const count = batched.length;
  toast.custom(() => <ArchiveUndoToast count={count} />, {
    id: ARCHIVE_UNDO_TOAST_ID,
    duration: ARCHIVE_UNDO_DURATION_MS,
    unstyled: true,
    testId: "archive-undo-toast-item",
    onAutoClose: clearBatch,
    onDismiss: clearBatch,
  });
}

/** Test-only: drop the pending Undo batch so cases don't leak module state. */
export function resetArchiveUndoBatchForTests(): void {
  clearBatch();
}
