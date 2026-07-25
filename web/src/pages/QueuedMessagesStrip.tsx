import {
  DndContext,
  type DragEndEvent,
  MouseSensor,
  pointerWithin,
  TouchSensor,
  useDraggable,
  useDroppable,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import {
  ClockIcon,
  CornerDownRightIcon,
  GripVerticalIcon,
  PencilIcon,
  Trash2Icon,
} from "lucide-react";

import type { QueuedMessage } from "@/store/chatStore";
import { cn } from "@/lib/utils";

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
  /** Column-width class so the strip lines up with the composer card. */
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

  return (
    <div
      ref={setDropRef}
      className={cn(
        "flex items-center gap-1.5 text-xs text-muted-foreground",
        isDragging && "opacity-40",
        isOver && "rounded bg-foreground/5",
      )}
    >
      {reorderable ? (
        <button
          type="button"
          ref={setDragRef}
          aria-label="Reorder queued message"
          className="shrink-0 cursor-grab touch-none rounded p-0.5 text-muted-foreground/50 transition hover:text-foreground focus-visible:text-foreground active:cursor-grabbing"
          {...attributes}
          {...listeners}
        >
          <GripVerticalIcon className="size-3.5" aria-hidden="true" />
        </button>
      ) : (
        <ClockIcon className="size-3.5 shrink-0" aria-hidden="true" />
      )}
      <span className="min-w-0 flex-1 truncate">{message.text}</span>
      {/* Always visible (not hover-gated) so the actions are discoverable;
          they brighten on hover/focus. */}
      {onSteer ? (
        <button
          type="button"
          aria-label="Send queued message now"
          className="flex shrink-0 items-center gap-1 rounded px-1 py-0.5 text-muted-foreground/60 transition hover:text-foreground focus-visible:text-foreground"
          onClick={() => onSteer(message.queueId)}
        >
          <CornerDownRightIcon className="size-3.5" aria-hidden="true" />
          Steer
        </button>
      ) : null}
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
  );
}

/**
 * Docked strip above the composer listing messages queued while the agent is
 * busy. Peeks above the composer card (`-mb-4` + bottom padding), mirroring
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
  // Pointer-only sensors with a small activation distance, matching the
  // sidebar's DnD, so a click on the grip still reaches the row's buttons.
  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 5 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 8 } }),
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
        {onReorder === undefined ? (
          rows
        ) : (
          <DndContext
            sensors={sensors}
            collisionDetection={pointerWithin}
            onDragEnd={handleDragEnd}
          >
            {rows}
          </DndContext>
        )}
      </div>
    </div>
  );
}
