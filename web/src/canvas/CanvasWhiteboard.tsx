// The Canvas whiteboard: a toolbar plus drawings rendered in React Flow's own
// viewport, so strokes, frames, notes and arrows pan and zoom with the session
// cards. Drawing tools cover the board with a capture surface; the Select tool
// lets cards and drawings be moved as before. Drawings are saved per canvas in
// localStorage (`whiteboardStorage.ts`).

import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ComponentType,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { useNodes, useReactFlow, ViewportPortal } from "@xyflow/react";
import {
  ArrowUpRightIcon,
  MousePointer2Icon,
  PencilIcon,
  SquareIcon,
  StickyNoteIcon,
  Trash2Icon,
} from "lucide-react";
import { useOmnigentAnalytics } from "@/lib/analytics";
import { isTextEntry } from "@/lib/hotkeys";
import { cn } from "@/lib/utils";
import { CARD_HEIGHT, CARD_WIDTH } from "./canvasLayout";
import {
  addElement,
  appendStrokePoint,
  boxAt,
  boxFromCorners,
  isBoundEnd,
  isBoxed,
  MAX_TEXT_LENGTH,
  MIN_DRAG_SIZE,
  moveElement,
  newWhiteboardElementId,
  noteBoxAt,
  rectangleBox,
  removeElement,
  snapToGrid,
  resolveArrow,
  strokePath,
  withText,
  type ArrowElement,
  type BoxedElement,
  type WhiteboardBox,
  type WhiteboardElement,
  type WhiteboardPoint,
  type WhiteboardTool,
} from "./whiteboardScene";
import { readWhiteboard, whiteboardStorageKey, writeWhiteboard } from "./whiteboardStorage";

const TOOLS: {
  tool: WhiteboardTool;
  label: string;
  Icon: ComponentType<{ className?: string }>;
}[] = [
  { tool: "select", label: "Select", Icon: MousePointer2Icon },
  { tool: "pen", label: "Draw", Icon: PencilIcon },
  { tool: "rectangle", label: "Frame", Icon: SquareIcon },
  { tool: "note", label: "Note", Icon: StickyNoteIcon },
  { tool: "arrow", label: "Arrow", Icon: ArrowUpRightIcon },
];
const TOOL_CLASS =
  "flex size-7 mobile-touch-target items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground aria-pressed:bg-brand-accent/10 aria-pressed:text-foreground disabled:pointer-events-none disabled:opacity-40";
// React Flow skips panning, dragging and wheel zoom for targets inside these classes.
const NO_FLOW_GESTURES = "nopan nodrag nowheel";
const INTERACTIVE_CLASS = `${NO_FLOW_GESTURES} pointer-events-auto`;
const MOVE_THRESHOLD = 3;
const STROKE_WIDTH = 3;
const HIT_WIDTH = 14;
const INK = "var(--foreground)";
const NO_ELEMENTS: WhiteboardElement[] = [];
const SELECTED_INK = "var(--brand-accent)";

interface MoveDrag {
  pointerId: number;
  id: string;
  clientX: number;
  clientY: number;
  original: WhiteboardElement;
  moved: boolean;
}

interface DrawGesture {
  pointerId: number;
  anchor: WhiteboardPoint;
}

function rounded(point: WhiteboardPoint): WhiteboardPoint {
  return { x: Math.round(point.x), y: Math.round(point.y) };
}

export function CanvasWhiteboard({
  canvasId,
  viewerId,
}: {
  canvasId: string;
  viewerId: string | null;
}) {
  const { screenToFlowPosition, getZoom } = useReactFlow();
  const { trackClick } = useOmnigentAnalytics();
  const nodes = useNodes();
  const markerId = `whiteboard-arrow-${useId().replace(/[^\w-]/g, "")}`;
  const storageKey = whiteboardStorageKey(viewerId, canvasId);
  const [board, setBoard] = useState(() => ({
    key: storageKey,
    elements: readWhiteboard(viewerId, canvasId),
  }));
  const [tool, setTool] = useState<WhiteboardTool>("select");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraftState] = useState<WhiteboardElement | null>(null);
  const draftRef = useRef<WhiteboardElement | null>(null);
  const setDraft = useCallback((next: WhiteboardElement | null) => {
    draftRef.current = next;
    setDraftState(next);
  }, []);
  const [saveFailed, setSaveFailed] = useState(false);
  const moveRef = useRef<MoveDrag | null>(null);
  const drawRef = useRef<DrawGesture | null>(null);

  // Each canvas has its own drawing; switching tabs (or identity resolving)
  // swaps it in during render so the previous board never flashes.
  if (board.key !== storageKey) {
    setBoard({ key: storageKey, elements: readWhiteboard(viewerId, canvasId) });
    setSelectedId(null);
    setEditingId(null);
    setDraft(null);
  }
  const elements = board.key === storageKey ? board.elements : NO_ELEMENTS;
  const elementsRef = useRef(elements);
  elementsRef.current = elements;

  const show = useCallback(
    (next: WhiteboardElement[]) => {
      elementsRef.current = next;
      setBoard({ key: storageKey, elements: next });
    },
    [storageKey],
  );

  const save = useCallback(
    (next: WhiteboardElement[]) => {
      show(next);
      try {
        writeWhiteboard(next, viewerId, canvasId);
        setSaveFailed(false);
      } catch {
        setSaveFailed(true);
      }
    },
    [canvasId, show, viewerId],
  );

  // Bind targets in drawing order (frames, cards, notes) so the topmost one wins.
  const bindTargets = useMemo(() => {
    const targets: (readonly [string, WhiteboardBox])[] = [];
    const shapes = elements.filter(isBoxed);
    for (const frame of shapes.filter((element) => element.kind === "rectangle")) {
      targets.push([frame.id, frame]);
    }
    for (const node of nodes) {
      targets.push([
        node.id,
        {
          x: node.position.x,
          y: node.position.y,
          width: node.measured?.width ?? node.width ?? node.initialWidth ?? CARD_WIDTH,
          height: node.measured?.height ?? node.height ?? node.initialHeight ?? CARD_HEIGHT,
        },
      ]);
    }
    for (const note of shapes.filter((element) => element.kind === "note")) {
      targets.push([note.id, note]);
    }
    return targets;
  }, [elements, nodes]);
  const resolveBox = useMemo(() => {
    const boxes = new Map(bindTargets);
    return (id: string) => boxes.get(id) ?? null;
  }, [bindTargets]);

  const deleteSelected = useCallback(() => {
    if (!selectedId) return;
    trackClick("canvas.whiteboard.delete");
    save(removeElement(elementsRef.current, selectedId, resolveBox));
    setSelectedId(null);
    setEditingId(null);
  }, [resolveBox, save, selectedId, trackClick]);

  const chooseTool = (next: WhiteboardTool) => {
    trackClick(`canvas.whiteboard.${next}`);
    setTool(next);
    setSelectedId(null);
    setEditingId(null);
  };

  const selectedRef = useRef(selectedId);
  selectedRef.current = selectedId;
  const toolRef = useRef(tool);
  toolRef.current = tool;
  // Capture phase, so Delete never also reaches React Flow's own delete key.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      if (event.target instanceof HTMLElement && isTextEntry(event.target)) return;
      if ((event.key === "Delete" || event.key === "Backspace") && selectedRef.current) {
        event.preventDefault();
        event.stopPropagation();
        deleteSelected();
      } else if (event.key === "Escape" && (toolRef.current !== "select" || selectedRef.current)) {
        event.preventDefault();
        event.stopPropagation();
        drawRef.current = null;
        setDraft(null);
        setTool("select");
        setSelectedId(null);
      }
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [deleteSelected, setDraft]);

  // Clicking anywhere outside a drawing clears its selection.
  useEffect(() => {
    if (!selectedId) return;
    const onPointerDown = (event: PointerEvent) => {
      if (event.target instanceof Element && event.target.closest("[data-whiteboard]")) return;
      setSelectedId(null);
    };
    document.addEventListener("pointerdown", onPointerDown, true);
    return () => document.removeEventListener("pointerdown", onPointerDown, true);
  }, [selectedId]);

  // Frames and notes land on the cards' grid; pen strokes and arrow ends follow the pointer.
  const snapsToGrid = tool === "rectangle" || tool === "note";
  const flowPoint = (event: { clientX: number; clientY: number }) =>
    screenToFlowPosition({ x: event.clientX, y: event.clientY }, { snapToGrid: snapsToGrid });

  const startDrawing = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture?.(event.pointerId);
    const point = rounded(flowPoint(event));
    drawRef.current = { pointerId: event.pointerId, anchor: point };
    const id = newWhiteboardElementId();
    if (tool === "pen") setDraft({ id, kind: "stroke", points: [point] });
    else if (tool === "rectangle") {
      setDraft({ id, kind: "rectangle", ...boxFromCorners(point, point), text: "" });
    } else if (tool === "arrow") {
      const bind = boxAt(point, bindTargets);
      setDraft({ id, kind: "arrow", from: bind ? { bind } : point, to: point });
    }
  };

  const continueDrawing = (event: ReactPointerEvent<HTMLDivElement>) => {
    const gesture = drawRef.current;
    if (!gesture || gesture.pointerId !== event.pointerId) return;
    const point = flowPoint(event);
    const current = draftRef.current;
    if (current?.kind === "stroke") {
      // Browsers merge fast pointer moves into one event per frame; the merged
      // samples keep a quick stroke from turning into straight segments.
      const samples = event.nativeEvent.getCoalescedEvents?.() ?? [];
      let points = current.points;
      for (const sample of samples.length > 0 ? samples : [event]) {
        points = appendStrokePoint(points, flowPoint(sample));
      }
      setDraft({ ...current, points });
    } else if (current?.kind === "rectangle") {
      setDraft({ ...current, ...boxFromCorners(gesture.anchor, rounded(point)) });
    } else if (current?.kind === "arrow") {
      setDraft({ ...current, to: rounded(point) });
    }
  };

  const finishArrow = (arrow: ArrowElement, point: WhiteboardPoint): ArrowElement | null => {
    const bind = boxAt(point, bindTargets);
    const sameTarget = bind !== null && isBoundEnd(arrow.from) && arrow.from.bind === bind;
    const finished: ArrowElement = { ...arrow, to: bind && !sameTarget ? { bind } : point };
    const drawn = resolveArrow(finished, resolveBox);
    if (!drawn || sameTarget) return null;
    const length = Math.hypot(drawn.end.x - drawn.start.x, drawn.end.y - drawn.start.y);
    return length >= MIN_DRAG_SIZE ? finished : null;
  };

  const finishDrawing = (event: ReactPointerEvent<HTMLDivElement>) => {
    const gesture = drawRef.current;
    if (!gesture || gesture.pointerId !== event.pointerId) return;
    drawRef.current = null;
    const point = rounded(flowPoint(event));
    const current = draftRef.current;
    setDraft(null);
    let created: WhiteboardElement | null = null;
    if (tool === "note") {
      created = { id: newWhiteboardElementId(), kind: "note", ...noteBoxAt(point), text: "" };
    } else if (current?.kind === "stroke") {
      created = { ...current, points: appendStrokePoint(current.points, point) };
    } else if (current?.kind === "rectangle") {
      created = { ...current, ...rectangleBox(gesture.anchor, point) };
    } else if (current?.kind === "arrow") {
      created = finishArrow(current, point);
    }
    if (!created) return;
    save(addElement(elementsRef.current, created));
    // The pen keeps drawing; shapes hand the board back to Select like most whiteboards.
    if (tool === "pen") return;
    setTool("select");
    setSelectedId(created.id);
    if (created.kind === "note") setEditingId(created.id);
  };

  const cancelDrawing = () => {
    drawRef.current = null;
    setDraft(null);
  };

  const startMove = (event: ReactPointerEvent<Element>, element: WhiteboardElement) => {
    if (event.button !== 0 || editingId === element.id) return;
    event.stopPropagation();
    setSelectedId(element.id);
    event.currentTarget.setPointerCapture?.(event.pointerId);
    moveRef.current = {
      pointerId: event.pointerId,
      id: element.id,
      clientX: event.clientX,
      clientY: event.clientY,
      original: element,
      moved: false,
    };
  };

  const continueMove = (event: ReactPointerEvent<Element>) => {
    const drag = moveRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const screenDx = event.clientX - drag.clientX;
    const screenDy = event.clientY - drag.clientY;
    if (!drag.moved && Math.hypot(screenDx, screenDy) < MOVE_THRESHOLD) return;
    drag.moved = true;
    const zoom = getZoom() || 1;
    const moved = snapToGrid(moveElement(drag.original, screenDx / zoom, screenDy / zoom));
    show(elementsRef.current.map((element) => (element.id === drag.id ? moved : element)));
  };

  const finishMove = (event: ReactPointerEvent<Element>) => {
    const drag = moveRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    moveRef.current = null;
    if (drag.moved) save(elementsRef.current);
  };

  const moveHandlers = (element: WhiteboardElement) => ({
    onPointerDown: (event: ReactPointerEvent<Element>) => startMove(event, element),
    onPointerMove: continueMove,
    onPointerUp: finishMove,
    onPointerCancel: finishMove,
  });

  const commitText = (id: string, text: string) => {
    setEditingId(null);
    const current = elementsRef.current.find((element) => element.id === id);
    if (!current || !("text" in current) || current.text === text) return;
    save(withText(elementsRef.current, id, text));
  };

  const ink = (id: string) => (id === selectedId ? SELECTED_INK : INK);

  const renderArrow = (arrow: ArrowElement, interactive: boolean) => {
    const drawn = resolveArrow(arrow, resolveBox);
    if (!drawn) return null;
    const selected = arrow.id === selectedId;
    return (
      <g key={arrow.id} data-testid="whiteboard-arrow">
        <line
          x1={drawn.start.x}
          y1={drawn.start.y}
          x2={drawn.end.x}
          y2={drawn.end.y}
          stroke={ink(arrow.id)}
          strokeWidth={STROKE_WIDTH}
          strokeLinecap="round"
          markerEnd={`url(#${markerId}${selected ? "-selected" : ""})`}
        />
        {interactive && (
          <line
            x1={drawn.start.x}
            y1={drawn.start.y}
            x2={drawn.end.x}
            y2={drawn.end.y}
            stroke="transparent"
            strokeWidth={HIT_WIDTH}
            pointerEvents="stroke"
            className={cn(NO_FLOW_GESTURES, "cursor-move")}
            data-whiteboard=""
            aria-label="Arrow"
            {...moveHandlers(arrow)}
          />
        )}
      </g>
    );
  };

  const renderVector = (element: WhiteboardElement, interactive: boolean) => {
    if (element.kind === "arrow") return renderArrow(element, interactive);
    if (element.kind === "stroke") {
      const path = strokePath(element.points);
      return (
        <g key={element.id} data-testid="whiteboard-stroke">
          <path
            d={path}
            fill="none"
            stroke={ink(element.id)}
            strokeWidth={STROKE_WIDTH}
            strokeLinecap="round"
            strokeLinejoin="round"
          />
          {interactive && (
            <path
              d={path}
              fill="none"
              stroke="transparent"
              strokeWidth={HIT_WIDTH}
              strokeLinecap="round"
              pointerEvents="stroke"
              className={cn(NO_FLOW_GESTURES, "cursor-move")}
              data-whiteboard=""
              aria-label="Drawing"
              {...moveHandlers(element)}
            />
          )}
        </g>
      );
    }
    if (element.kind === "rectangle") {
      return (
        <g key={element.id} data-testid="whiteboard-frame">
          <rect
            x={element.x}
            y={element.y}
            width={element.width}
            height={element.height}
            rx={8}
            fill="none"
            stroke={ink(element.id)}
            strokeWidth={STROKE_WIDTH}
            strokeOpacity={element.id === selectedId ? 1 : 0.55}
          />
          {interactive && (
            <rect
              x={element.x}
              y={element.y}
              width={element.width}
              height={element.height}
              rx={8}
              fill="none"
              stroke="transparent"
              strokeWidth={HIT_WIDTH}
              pointerEvents="stroke"
              className={cn(NO_FLOW_GESTURES, "cursor-move")}
              data-whiteboard=""
              aria-label={element.text ? `Frame ${element.text}` : "Frame"}
              onDoubleClick={() => setEditingId(element.id)}
              {...moveHandlers(element)}
            />
          )}
        </g>
      );
    }
    return null;
  };

  const renderFrameLabel = (frame: BoxedElement) => {
    const editing = editingId === frame.id;
    if (!editing && !frame.text && frame.id !== selectedId) return null;
    return (
      <div
        key={`${frame.id}-label`}
        className={cn(
          "absolute flex h-6 max-w-[320px] items-end text-ui font-medium text-muted-foreground",
          !editing && cn(INTERACTIVE_CLASS, "cursor-move"),
        )}
        style={{ left: frame.x, top: frame.y - 28 }}
        data-whiteboard=""
        onDoubleClick={() => setEditingId(frame.id)}
        {...(editing ? {} : moveHandlers(frame))}
      >
        {editing ? (
          <input
            autoFocus
            aria-label="Frame name"
            defaultValue={frame.text}
            maxLength={MAX_TEXT_LENGTH}
            className={cn(
              INTERACTIVE_CLASS,
              "h-6 w-56 rounded border bg-background px-1.5 text-ui text-foreground outline-none",
            )}
            onBlur={(event) => commitText(frame.id, event.currentTarget.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === "Escape") event.currentTarget.blur();
            }}
          />
        ) : (
          <span className="truncate">{frame.text || "Untitled frame"}</span>
        )}
      </div>
    );
  };

  const renderNote = (note: BoxedElement) => {
    const editing = editingId === note.id;
    return (
      <div
        key={note.id}
        data-testid="whiteboard-note"
        data-whiteboard=""
        role="note"
        aria-label={note.text || "Empty note"}
        className={cn(
          INTERACTIVE_CLASS,
          "absolute flex cursor-move rounded-md border border-amber-300 bg-amber-100 p-2 text-[13px] text-amber-950 shadow-sm",
          "dark:border-amber-700/60 dark:bg-amber-950 dark:text-amber-100",
          note.id === selectedId && "ring-2 ring-brand-accent",
        )}
        style={{ left: note.x, top: note.y, width: note.width, height: note.height }}
        onDoubleClick={() => setEditingId(note.id)}
        {...moveHandlers(note)}
      >
        {editing ? (
          <textarea
            autoFocus
            aria-label="Note text"
            defaultValue={note.text}
            maxLength={MAX_TEXT_LENGTH}
            className="size-full resize-none bg-transparent outline-none"
            onBlur={(event) => commitText(note.id, event.currentTarget.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") event.currentTarget.blur();
            }}
          />
        ) : (
          <span className="size-full overflow-hidden whitespace-pre-wrap break-words">
            {note.text}
          </span>
        )}
      </div>
    );
  };

  const interactive = tool === "select";
  const shapes = elements.filter(isBoxed);

  return (
    <>
      <ViewportPortal>
        <div className="pointer-events-none absolute left-0 top-0" data-testid="whiteboard-layer">
          <svg
            aria-hidden={elements.length === 0}
            className="pointer-events-none absolute left-0 top-0 overflow-visible"
            width={1}
            height={1}
          >
            <defs>
              {[
                [markerId, INK],
                [`${markerId}-selected`, SELECTED_INK],
              ].map(([id, color]) => (
                <marker
                  key={id}
                  id={id}
                  viewBox="0 0 10 10"
                  refX={8}
                  refY={5}
                  markerWidth={5}
                  markerHeight={5}
                  orient="auto-start-reverse"
                >
                  <path d="M 0 0 L 10 5 L 0 10 z" fill={color} />
                </marker>
              ))}
            </defs>
            {elements.map((element) => renderVector(element, interactive))}
            {draft && renderVector(draft, false)}
          </svg>
          {shapes.map((shape) =>
            shape.kind === "note" ? renderNote(shape) : renderFrameLabel(shape),
          )}
        </div>
      </ViewportPortal>
      {tool !== "select" && (
        <div
          data-testid="whiteboard-surface"
          data-tool={tool}
          aria-hidden
          className="absolute inset-0 z-[5] cursor-crosshair touch-none"
          onPointerDown={startDrawing}
          onPointerMove={continueDrawing}
          onPointerUp={finishDrawing}
          onPointerCancel={cancelDrawing}
        />
      )}
      <div
        role="toolbar"
        aria-label="Whiteboard"
        data-whiteboard=""
        className="absolute left-3 top-3 z-10 flex items-center gap-0.5 rounded-md border bg-card p-0.5 shadow-sm"
      >
        {TOOLS.map(({ tool: option, label, Icon }) => (
          <button
            key={option}
            type="button"
            className={TOOL_CLASS}
            aria-label={label}
            title={label}
            aria-pressed={tool === option}
            onClick={() => chooseTool(option)}
          >
            <Icon className="size-4" />
          </button>
        ))}
        <span aria-hidden className="mx-0.5 h-5 w-px bg-border" />
        <button
          type="button"
          className={TOOL_CLASS}
          aria-label="Delete selected drawing"
          title="Delete selected drawing"
          disabled={!selectedId}
          onClick={deleteSelected}
        >
          <Trash2Icon className="size-4" />
        </button>
      </div>
      {saveFailed && (
        <div
          role="status"
          className="absolute left-3 top-14 z-10 rounded-md border bg-card px-2 py-1 text-xs text-muted-foreground"
        >
          Drawing could not be saved.
        </div>
      )}
    </>
  );
}
