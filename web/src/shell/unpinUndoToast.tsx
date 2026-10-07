import { toast } from "sonner";

import type { Conversation } from "@/hooks/useConversations";
import { PINNED_LABEL_KEY } from "@/lib/sessionListCache";

/** How long the post-unpin Undo pill stays up — long enough to catch a mis-click. */
const UNPIN_UNDO_DURATION_MS = 5000;

/** Stable id so a newer unpin replaces the pill instead of stacking another. */
const UNPIN_UNDO_TOAST_ID = "unpin-undo";

/** The pin toggle's `mutateAsync` (`useTogglePinnedConversation`). */
type WritePin = (vars: { id: string; pinned: boolean; pinnedAt?: number }) => Promise<unknown>;

/**
 * Unpin a session and, once the write lands, offer an Undo pill. Undo re-pins
 * at the previous pin value, so the session returns to its old Pinned slot (and
 * keeps its numeric jump shortcut). A refused or failed unpin rolls itself back
 * and offers nothing. A newer unpin replaces the pill rather than merging, since
 * pin writes run one at a time.
 *
 * @param writePin - The pin toggle's `mutateAsync`.
 * @param id - The session to unpin.
 * @param before - The session's cached row before the unpin, if any.
 */
export function unpinWithUndo(
  writePin: WritePin,
  id: string,
  before: Pick<Conversation, "title" | "labels"> | undefined,
): void {
  const previous = Number(before?.labels?.[PINNED_LABEL_KEY]);
  const pinnedAt = Number.isFinite(previous) && previous > 0 ? previous : undefined;
  writePin({ id, pinned: false })
    .then(() => {
      toast("Unpinned session", {
        id: UNPIN_UNDO_TOAST_ID,
        description: before?.title || undefined,
        duration: UNPIN_UNDO_DURATION_MS,
        action: {
          label: "Undo",
          onClick: () => void writePin({ id, pinned: true, pinnedAt }).catch(() => {}),
        },
        testId: "unpin-undo-toast-item",
      });
    })
    // The toggle's own onError already rolls the row back.
    .catch(() => {});
}
