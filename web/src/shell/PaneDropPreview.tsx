// Where a dragged pane or tab would land among the session's panes: the half it would take,
// edged in the accent on the side it meets.

import type { TerminalPaneSide } from "@/lib/paneSplitLayout";
import { cn } from "@/lib/utils";

import { type AppPaneDrop, usePendingAppPaneDrop } from "./appPanes";
import { splitPanesRoot } from "./paneMove";

/** The drop half's edge against the rest of its target, where the two panes will meet. */
const DROP_EDGE: Record<TerminalPaneSide, string> = {
  left: "border-r-2",
  right: "border-l-2",
  top: "border-b-2",
  bottom: "border-t-2",
};

/** The spot `drop` takes, placed in percent of the panes' view it sits in. */
export function PaneDropPreview({
  drop,
}: {
  drop: Pick<AppPaneDrop, "targetKey" | "side" | "area">;
}) {
  return (
    <div
      aria-hidden
      data-pane-drop={`${drop.targetKey}:${drop.side}`}
      className={cn(
        "pointer-events-none absolute z-20 border-primary bg-primary/15",
        DROP_EDGE[drop.side],
      )}
      style={{
        left: `${drop.area.left}%`,
        top: `${drop.area.top}%`,
        width: `${drop.area.width}%`,
        height: `${drop.area.height}%`,
      }}
    />
  );
}

/**
 * A tab's drop over a chat shown alone. The session's panes wait hidden under the chat until a
 * split shows them, so their view can't draw the preview: it draws here instead, over the spot
 * the hidden panes' view takes on screen.
 */
export function LoneChatPaneDropPreview({ conversationId }: { conversationId: string }) {
  const drop = usePendingAppPaneDrop(conversationId);
  const root = drop ? splitPanesRoot(conversationId) : null;
  if (!drop || !root) return null;
  const box = root.getBoundingClientRect();
  return (
    <div
      aria-hidden
      data-lone-chat-pane-drop=""
      className="pointer-events-none fixed z-30"
      style={{ left: box.left, top: box.top, width: box.width, height: box.height }}
    >
      <PaneDropPreview drop={drop} />
    </div>
  );
}
