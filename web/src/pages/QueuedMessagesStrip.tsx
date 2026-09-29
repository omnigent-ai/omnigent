import {
  closestCenter,
  DndContext,
  type CollisionDetection,
  type DragEndEvent,
  KeyboardSensor,
  MouseSensor,
  pointerWithin,
  TouchSensor,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import {
  ArrowUpIcon,
  ClockIcon,
  GripVerticalIcon,
  ImageIcon,
  PaperclipIcon,
  PencilIcon,
  XIcon,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { QueuedMessage } from "@/store/chatStore";
import { attachmentFilename } from "@/lib/attachments";
import { cn } from "@/lib/utils";

/** Keep touch targets large without enlarging the visible glyphs. */
const ACTION_BUTTON_CLASS =
  "shrink-0 text-muted-foreground transition-colors hover:text-foreground focus-visible:text-foreground max-md:size-11";

const ACTION_ICON_CLASS = "size-3.5 max-md:size-4";

export const queuedMessageCollisionDetection: CollisionDetection = (args) =>
  args.pointerCoordinates ? pointerWithin(args) : closestCenter(args);

interface QueuedMessagesStripProps {
  /** Messages waiting to be flushed, in FIFO order (head first). */
  messages: QueuedMessage[];
  /** Remove a queued message by id (per-row delete). */
  onDelete: (queueId: string) => void;
  /** Pull a queued message back into the composer for editing. */
  onEdit: (queueId: string) => void;
  /**
   * Send a queued message now (steer), instead of waiting for the idle flush.
   * Omitted when the session can't steer mid-turn (e.g. native terminals),
   * in which case no steer button is shown.
   */
  onSteer?: (queueId: string) => void;
  /**
   * Move `queueId` so it sits before `beforeQueueId` (or to the end when null).
   * Drives drag-to-reorder; omit to render a non-reorderable strip.
   */
  onReorder?: (queueId: string, beforeQueueId: string | null) => void;
  /**
   * Layout class aligning the strip with the composer surface it docks
   * onto (its tuck only hides behind a surface at least as wide, so match
   * that surface's width — e.g. the workspace bar's inset).
   */
  widthClassName?: string;
}

/** A single queued-message row, draggable by its grip when reordering is on. */
function QueuedRow({
  message,
  onDelete,
  onEdit,
  onSteer,
  reorderable,
}: {
  message: QueuedMessage;
  onDelete: (queueId: string) => void;
  onEdit: (queueId: string) => void;
  onSteer?: (queueId: string) => void;
  reorderable: boolean;
}) {
  const {
    attributes,
    listeners,
    setNodeRef: setDragRef,
    isDragging,
  } = useDraggable({
    id: message.queueId,
    disabled: !reorderable,
  });
  // The whole row is the drop target so dropping anywhere on it reorders.
  const { setNodeRef: setDropRef, isOver } = useDroppable({
    id: message.queueId,
    disabled: !reorderable,
  });
  const hasText = message.text.trim().length > 0;
  const files = message.files ?? [];
  const attachmentNames = files.map(attachmentFilename);
  const AttachmentIcon = files.every((file) => file.type.startsWith("image/"))
    ? ImageIcon
    : PaperclipIcon;

  return (
    <div
      ref={setDropRef}
      role="listitem"
      className={cn(
        "relative flex min-w-0 items-center gap-1 py-0.5 text-sm text-foreground",
        isDragging && "opacity-40",
        isOver &&
          !isDragging &&
          "after:absolute after:inset-x-0 after:-bottom-0.5 after:h-px after:bg-muted-foreground/50",
      )}
    >
      {reorderable ? (
        <Button
          ref={setDragRef}
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label="Reorder queued message"
          className={cn(ACTION_BUTTON_CLASS, "cursor-grab touch-none active:cursor-grabbing")}
          {...attributes}
          {...listeners}
        >
          <GripVerticalIcon className={ACTION_ICON_CLASS} aria-hidden="true" />
        </Button>
      ) : (
        <ClockIcon
          className={cn(ACTION_ICON_CLASS, "mx-1 shrink-0 text-muted-foreground")}
          aria-hidden="true"
        />
      )}
      <div className="flex min-w-0 flex-1 items-center gap-2 max-md:flex-col max-md:items-start max-md:gap-0.5">
        {hasText && (
          <span className="min-w-0 max-w-full truncate" title={message.text}>
            {message.text}
          </span>
        )}
        {attachmentNames.length > 0 && (
          <Badge
            variant="outline"
            data-testid="queued-message-attachments"
            title={attachmentNames.join("\n")}
            className={cn(
              "min-w-0 max-w-[min(16rem,100%)] gap-1.5 rounded-md border-border/60 bg-background/50 px-1.5 font-normal text-muted-foreground transition-none max-sm:gap-1 max-sm:px-1",
              hasText && "md:max-w-[min(14rem,55%)]",
            )}
          >
            <AttachmentIcon
              className={cn("shrink-0", attachmentNames.length > 1 && "max-sm:hidden")}
              aria-hidden="true"
            />
            <span className="truncate">{attachmentNames[0]}</span>
            {attachmentNames.length > 1 && (
              <>
                <span
                  className="shrink-0 border-l border-border/60 pl-1.5 tabular-nums max-sm:border-0 max-sm:pl-0"
                  aria-hidden="true"
                >
                  +{attachmentNames.length - 1}
                </span>
                <span className="sr-only">, {attachmentNames.slice(1).join(", ")}</span>
              </>
            )}
          </Badge>
        )}
      </div>
      {message.requiresRetry && (
        <span className="shrink-0 text-xs text-destructive">Send failed</span>
      )}
      {/* Always visible (not hover-gated) so the actions are discoverable;
          they brighten on hover/focus. */}
      <span className="flex shrink-0 items-center gap-0">
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label="Edit queued message"
          className={ACTION_BUTTON_CLASS}
          onClick={() => onEdit(message.queueId)}
        >
          <PencilIcon className={ACTION_ICON_CLASS} aria-hidden="true" />
        </Button>
        {onSteer ? (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="icon-xs"
                aria-label={
                  message.requiresRetry ? "Retry queued message" : "Send queued message now"
                }
                className={ACTION_BUTTON_CLASS}
                onClick={() => onSteer(message.queueId)}
              >
                <ArrowUpIcon className="size-4 max-md:size-4" aria-hidden="true" />
              </Button>
            </TooltipTrigger>
            <TooltipContent side="top">
              {message.requiresRetry ? "Retry" : "Send now"}
            </TooltipContent>
          </Tooltip>
        ) : null}
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label="Remove queued message"
          className={ACTION_BUTTON_CLASS}
          onClick={() => onDelete(message.queueId)}
        >
          <XIcon className="size-4 max-md:size-4" aria-hidden="true" />
        </Button>
      </span>
    </div>
  );
}

/**
 * Docked strip above the composer listing messages queued while the agent is
 * busy. Peeks above the composer stack's top surface (`-mb-4` + bottom
 * padding tuck its square bottom corners behind it), mirroring
 * `SubagentComposerTray`. Renders nothing when the queue is empty.
 *
 * Each row can be steered (sent now), edited (pulled back into the composer),
 * deleted, or — when `onReorder` is provided — dragged by its grip to reorder
 * the queue (drains FIFO, so order is the send order).
 */
export function QueuedMessagesStrip({
  messages,
  onDelete,
  onEdit,
  onSteer,
  onReorder,
  widthClassName,
}: QueuedMessagesStripProps) {
  // Pointer sensors use activation constraints so pressing the grip does not
  // accidentally start a drag. KeyboardSensor keeps reordering operable
  // without a mouse or touch input.
  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 5 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 8 } }),
    useSensor(KeyboardSensor),
  );

  if (messages.length === 0) return null;

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (onReorder === undefined || over === null || active.id === over.id) return;
    const from = messages.findIndex((m) => m.queueId === active.id);
    const to = messages.findIndex((m) => m.queueId === over.id);
    if (from === -1 || to === -1) return;
    // Dragging down past the target lands after it (before the next row, or the
    // end); dragging up lands before it. Mirrors dnd-kit sortable's semantics
    // and lets a drag reach the very end of the list.
    const beforeQueueId = from < to ? (messages[to + 1]?.queueId ?? null) : messages[to]!.queueId;
    onReorder(String(active.id), beforeQueueId);
  };

  const rows = messages.map((message) => (
    <QueuedRow
      key={message.queueId}
      message={message}
      onDelete={onDelete}
      onEdit={onEdit}
      onSteer={onSteer}
      reorderable={onReorder !== undefined}
    />
  ));

  // Desktop shares the card's content inset (COMPOSER_CONTENT_INSET_CLASS);
  // the literal md:px-3 repeats it because the token has no responsive
  // variant. Narrow phones keep the compact px-2.
  return (
    <div
      data-testid="composer-queued-strip"
      className={cn(
        "composer-queued-surface mx-auto -mb-4 flex w-full flex-col rounded-t-2xl px-2 pt-1.5 pb-5.5 md:px-3",
        widthClassName,
      )}
    >
      {/* Cap the list height and scroll when the queue is long, so a big
          backlog never pushes the composer off-screen. ~5 rows tall. */}
      <div
        role="list"
        aria-label="Queued messages"
        className="flex max-h-32 flex-col gap-1 overflow-y-auto overscroll-contain"
      >
        {onReorder === undefined ? (
          rows
        ) : (
          <DndContext
            sensors={sensors}
            collisionDetection={queuedMessageCollisionDetection}
            onDragEnd={handleDragEnd}
          >
            {rows}
          </DndContext>
        )}
      </div>
    </div>
  );
}
