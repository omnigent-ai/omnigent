import { ClockIcon } from "lucide-react";

import type { QueuedMessage } from "@/store/chatStore";
import { cn } from "@/lib/utils";

interface QueuedMessagesStripProps {
  /** Messages waiting to be flushed, in FIFO order (head first). */
  messages: QueuedMessage[];
  /** Column-width class so the strip lines up with the composer card. */
  widthClassName?: string;
}

/**
 * Docked strip above the composer listing messages queued while the agent is
 * busy. Peeks above the composer card (`-mb-4` + bottom padding), mirroring
 * `SubagentComposerTray`. Renders nothing when the queue is empty.
 *
 * Read-only in this iteration — per-row actions (delete / edit / steer /
 * reorder) land in later changes.
 */
export function QueuedMessagesStrip({ messages, widthClassName }: QueuedMessagesStripProps) {
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
          </div>
        ))}
      </div>
    </div>
  );
}
