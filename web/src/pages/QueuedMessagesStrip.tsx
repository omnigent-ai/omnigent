import { ClockIcon, PencilIcon, Trash2Icon } from "lucide-react";

import type { QueuedMessage } from "@/store/chatStore";
import { cn } from "@/lib/utils";

interface QueuedMessagesStripProps {
  /** Messages waiting to be flushed, in FIFO order (head first). */
  messages: QueuedMessage[];
  /** Remove a queued message by id (per-row delete). */
  onDelete: (queueId: string) => void;
  /** Pull a queued message back into the composer for editing. */
  onEdit: (queueId: string) => void;
  /** Column-width class so the strip lines up with the composer card. */
  widthClassName?: string;
}

/**
 * Docked strip above the composer listing messages queued while the agent is
 * busy. Peeks above the composer card (`-mb-4` + bottom padding), mirroring
 * `SubagentComposerTray`. Renders nothing when the queue is empty.
 *
 * Each row can be edited (pulled back into the composer) or deleted; steer /
 * reorder land in later changes.
 */
export function QueuedMessagesStrip({
  messages,
  onDelete,
  onEdit,
  widthClassName,
}: QueuedMessagesStripProps) {
  if (messages.length === 0) return null;
  return (
    <div
      data-testid="composer-queued-strip"
      className={cn(
        "mx-auto -mb-4 flex w-full flex-col rounded-t-2xl bg-tray/40 px-4 pt-1.5 pb-5.5",
        widthClassName,
      )}
    >
      {/* Cap the list height and scroll when the queue is long, so a big
          backlog never pushes the composer off-screen. ~5 rows tall. */}
      <div className="flex max-h-32 flex-col gap-1 overflow-y-auto">
        {messages.map((message) => (
          <div
            key={message.queueId}
            className="flex items-center gap-1.5 text-xs text-muted-foreground"
          >
            <ClockIcon className="size-3.5 shrink-0" aria-hidden="true" />
            <span className="min-w-0 flex-1 truncate">{message.text}</span>
            <span className="shrink-0 text-muted-foreground/70">Queued</span>
            {/* Always visible (not hover-gated) so the actions are
                discoverable; they brighten on hover/focus. */}
            <button
              type="button"
              aria-label="Edit queued message"
              className="shrink-0 rounded p-0.5 text-muted-foreground/60 transition hover:text-foreground focus-visible:text-foreground"
              onClick={() => onEdit(message.queueId)}
            >
              <PencilIcon className="size-3.5" aria-hidden="true" />
            </button>
            <button
              type="button"
              aria-label="Remove queued message"
              className="shrink-0 rounded p-0.5 text-muted-foreground/60 transition hover:text-foreground focus-visible:text-foreground"
              onClick={() => onDelete(message.queueId)}
            >
              <Trash2Icon className="size-3.5" aria-hidden="true" />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
