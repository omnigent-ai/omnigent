// The design tools' layers over a Browser tab's page: Annotate (pick an element, ask for a
// change) and Draw (mark up the page, send a screenshot). They draw over `rect` in the
// overlay document they render in, and turn the pointer and keys into core's actions.
import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Textarea } from "@/components/ui/textarea";
import { isImeCompositionKeyEvent } from "@/lib/ime";
import { cn } from "@/lib/utils";
import { runCommand, type CommandResult, type DesignCommand } from "./core/designCommands";
import { drawShape, MARK_COLORS } from "./core/marks";
import {
  errorText,
  hitAt,
  isSendingNotice,
  pickElement,
  sendAnnotation,
  sendDrawing,
} from "./core/toolActions";
import { isSessionEnded, toolSession, type ToolSession } from "./core/toolSession";
import {
  cardPosition,
  ColorSwatch,
  DrawDock,
  ElementOutline,
  isSendShortcut,
  LAYER_Z,
  type LayerRect,
  NOTE_FIELD_CLASS,
  PALETTE_CLASS,
  PaletteColor,
  PickHint,
  prepareMarksCanvas,
  REQUEST_FIELD_CLASS,
  RequestCard,
  SHAPE_TOOLS,
  TEXT_DRAFT_CLASS,
} from "./layerParts";
import {
  closeTool,
  useTabTools,
  type Hit,
  type Shape,
  type ShapeKind,
} from "./core/designToolsStore";

/** The window an element is in (the overlay's, for the desktop app's layers). */
const windowOf = (element: Element): Window => element.ownerDocument.defaultView ?? window;

// Wheel deltas in lines or pages, as pixels (what scrollBy takes).
const WHEEL_LINE_PX = 16;

/** Forward a wheel turn over a tool's layer to what's under it in the (inert) page. */
function scrollPage(session: ToolSession, event: React.WheelEvent, frame: LayerRect): void {
  const unit = event.deltaMode === 1 ? WHEEL_LINE_PX : event.deltaMode === 2 ? frame.height : 1;
  const [x, y] = [event.clientX - frame.left, event.clientY - frame.top];
  const [dx, dy] = [event.deltaX * unit, event.deltaY * unit];
  // Held keys too: Ctrl+wheel is the page's zoom, not a scroll.
  const modifiers = [
    event.ctrlKey && "control",
    event.metaKey && "meta",
    event.shiftKey && "shift",
    event.altKey && "alt",
  ].filter((key): key is string => typeof key === "string");
  void session.run((page) => page.scroll(x, y, dx, dy, modifiers)).catch(() => undefined);
}

export interface LayerProps {
  tabId: string;
  url: string;
  /** The page's viewport, in the coordinates of the document the layers are in. */
  rect: LayerRect | null;
  onNotice: (message: string) => void;
  /** Where the layers' popovers open: in the layers' own document. Default: the app's. */
  popoverContainer?: HTMLElement | null;
}

/** A Send's outcome, from its notice: queued, or why not. */
function sendOutcome(notice: string | null, tool: string): CommandResult {
  if (isSendingNotice(notice)) return { ok: true };
  return { ok: false, error: notice ?? `${tool} closed before it sent.` };
}

/**
 * The open tool's session (see toolSession.ts). A tool that works in the page (`usesPage`)
 * closes, saying why, when the page can't run it.
 */
function useToolSession(
  tabId: string,
  onNotice: (message: string) => void,
  usesPage: boolean,
): ToolSession | null {
  const { session: id } = useTabTools(tabId);
  const live = toolSession(tabId);
  const session = live?.id === id ? live : null;
  useEffect(() => {
    if (!session || !usesPage) return;
    session.page().catch((error: unknown) => {
      if (isSessionEnded(error)) return;
      session.close();
      onNotice(errorText(error));
    });
  }, [session, onNotice, usesPage]);
  return session;
}

export function AnnotateLayer({ tabId, rect, onNotice }: LayerProps) {
  const { picked, request } = useTabTools(tabId);
  const session = useToolSession(tabId, onNotice, true);
  const hovering = useRef(false);
  const nextPoint = useRef<{ x: number; y: number } | null>(null);
  const [hover, setHover] = useState<Hit | null>(null);
  const cardRef = useRef<HTMLFormElement | null>(null);
  const [cardHeight, setCardHeight] = useState<number | undefined>(undefined);
  // Placed by its real height: the field grows as the request does, and wraps to the width.
  const frameWidth = rect?.width;
  useLayoutEffect(() => {
    const height = cardRef.current?.offsetHeight;
    if (height) setCardHeight(height);
  }, [picked, request, frameWidth]);

  // One hit test at a time, each at the latest point the pointer moved to.
  const updateHover = async (event: ReactPointerEvent<HTMLDivElement>) => {
    if (picked || !session || !rect) return;
    nextPoint.current = { x: event.clientX - rect.left, y: event.clientY - rect.top };
    if (hovering.current) return;
    hovering.current = true;
    try {
      while (nextPoint.current && !session.isEnded) {
        const { x, y } = nextPoint.current;
        nextPoint.current = null;
        // oxlint-disable-next-line no-await-in-loop -- one hit test at a time
        const hit = await hitAt(session, x, y);
        if (!session.isEnded) setHover(hit);
      }
    } finally {
      hovering.current = false;
    }
  };
  const pick = (event: React.MouseEvent<HTMLDivElement>) => {
    if (session && rect)
      void pickElement(session, event.clientX - rect.left, event.clientY - rect.top);
  };

  // Hands the request to the outbox, which sends (and retries) it; the tool is done.
  const send = async (): Promise<CommandResult> => {
    const notice = session ? await sendAnnotation(session) : null;
    if (notice) onNotice(notice);
    return sendOutcome(notice, "Annotate");
  };

  const sendCommand: DesignCommand = {
    blocked: !picked
      ? "Point at an element on the page first."
      : !request.trim()
        ? "Say what should change first."
        : undefined,
    run: send,
  };

  if (!rect) return null;
  const shown = picked ?? hover;
  const card = picked ? cardPosition(picked.rect, rect, cardHeight) : null;
  return (
    <>
      <div
        data-testid="annotate-layer"
        className={cn(LAYER_Z, "fixed", picked ? "cursor-default" : "cursor-crosshair")}
        style={{ left: rect.left, top: rect.top, width: rect.width, height: rect.height }}
        onPointerMove={(event) => void updateHover(event)}
        onClick={pick}
        onWheel={(event) => {
          if (!picked && session) scrollPage(session, event, rect);
        }}
      >
        {shown && <ElementOutline hit={shown} picked={!!picked} />}
      </div>
      {!picked && <PickHint rect={rect} onClose={() => closeTool(tabId)} />}
      {picked && card && (
        <RequestCard
          cardRef={cardRef}
          style={{ left: rect.left + card.left, top: rect.top + card.top, width: card.width }}
          sendDisabled={sendCommand.blocked !== undefined}
          onCancel={() => closeTool(tabId)}
          onSubmit={() => runCommand(sendCommand)}
          field={
            <Textarea
              autoFocus
              rows={2}
              value={request}
              aria-label="What should change?"
              placeholder="What should change?"
              onChange={(event) => session?.edit({ request: event.target.value })}
              onKeyDown={(event) => {
                // Enter that confirms an input method's composition isn't a send.
                if (event.key === "Enter" && !event.shiftKey && !isImeCompositionKeyEvent(event)) {
                  event.preventDefault();
                  runCommand(sendCommand);
                }
              }}
              className={REQUEST_FIELD_CLASS}
            />
          }
        />
      )}
    </>
  );
}

interface TextDraft {
  x: number;
  y: number;
  value: string;
}

export function DrawLayer({ tabId, url, rect, onNotice, popoverContainer }: LayerProps) {
  const { shapes, note, phase } = useTabTools(tabId);
  const session = useToolSession(tabId, onNotice, false);
  // Frozen while the screenshot is taken: what's sent is what was on screen.
  const frozen = phase !== "editing";
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  // The shape being drawn, read by pointer events between renders.
  const drawingRef = useRef<Shape | null>(null);
  const [current, setCurrent] = useState<Shape | null>(null);
  const [kind, setKind] = useState<ShapeKind>("pen");
  // Each tool keeps its own color.
  const [colors, setColors] = useState<Record<ShapeKind, string>>({
    pen: MARK_COLORS[0].value,
    rect: MARK_COLORS[0].value,
    arrow: MARK_COLORS[0].value,
    text: MARK_COLORS[0].value,
  });
  const [textDraft, setTextDraftState] = useState<TextDraft | null>(null);
  // Also kept in a ref: a blur after Enter or Escape (the field unmounting) must see none.
  const draftRef = useRef<TextDraft | null>(null);
  const setTextDraft = (draft: TextDraft | null) => {
    draftRef.current = draft;
    setTextDraftState(draft);
  };
  const width = rect?.width ?? 0;
  const height = rect?.height ?? 0;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    // One scale for the backing store and the marks, read now: the window may have changed displays.
    // Resized only when that changes the size, as resizing on each pointer move would stutter.
    const ratio = windowOf(canvas).devicePixelRatio || 1;
    const backingWidth = Math.round(width * ratio);
    const backingHeight = Math.round(height * ratio);
    if (canvas.width !== backingWidth) canvas.width = backingWidth;
    if (canvas.height !== backingHeight) canvas.height = backingHeight;
    const context = canvas.getContext("2d");
    if (!context) return;
    prepareMarksCanvas(context, width, height, ratio);
    for (const shape of current ? [...shapes, current] : shapes) drawShape(context, shape);
  }, [shapes, current, width, height]);

  const drafted = !!textDraft?.value.trim();
  const sendCommand: DesignCommand = {
    blocked: frozen
      ? "Draw is still taking its screenshot."
      : shapes.length === 0 && !drafted
        ? "Draw on the page first."
        : undefined,
    // Words still being typed go on the canvas first, then it sends.
    run: () => (drafted ? commitText(true) : send()),
  };
  const undoCommand: DesignCommand = {
    blocked: frozen
      ? "Draw is still taking its screenshot."
      : shapes.length === 0
        ? "There's nothing to undo."
        : undefined,
    run: () => {
      session?.edit((all) => ({ shapes: all.shapes.slice(0, -1) }));
    },
  };
  // Keys on screen send as the button does, saying why when they can't.
  const sendFromKeys = () => {
    const result = runCommand(sendCommand);
    if (!result.ok) onNotice(result.error);
  };

  /** Put the words being typed on the canvas; with `andSend`, then send (how that went). */
  const commitText = (andSend: boolean): Promise<CommandResult> | undefined => {
    const draft = draftRef.current;
    setTextDraft(null);
    if (!draft?.value.trim()) {
      if (andSend) sendFromKeys();
      return undefined;
    }
    const mark: Shape = {
      kind: "text",
      points: [{ x: draft.x, y: draft.y }],
      color: colors.text,
      text: draft.value.trim(),
    };
    const added = session?.edit((all) => ({ shapes: [...all.shapes, mark] }));
    if (!andSend) return undefined;
    // The screenshot is of the session's marks, this one among them now.
    return added ? send() : Promise.resolve(sendOutcome(null, "Draw"));
  };

  const point = (event: { clientX: number; clientY: number }) => ({
    x: event.clientX - (rect?.left ?? 0),
    y: event.clientY - (rect?.top ?? 0),
  });

  // The screenshot shows the marks frozen at Send, never the live canvas.
  const send = async (): Promise<CommandResult> => {
    const notice = session ? await sendDrawing(session, url) : null;
    if (notice) onNotice(notice);
    return sendOutcome(notice, "Draw");
  };

  if (!rect) return null;
  return (
    <>
      <canvas
        ref={canvasRef}
        data-testid="draw-layer"
        className={cn(LAYER_Z, "fixed touch-none", frozen ? "cursor-wait" : "cursor-crosshair")}
        style={{ left: rect.left, top: rect.top, width, height }}
        onPointerDown={(event) => {
          // Only the primary button draws, as only it presses a button.
          if (frozen || event.button !== 0) return;
          if (kind === "text") {
            // Keep focus in the text field that's about to open.
            event.preventDefault();
            if (textDraft) commitText(false);
            setTextDraft({ ...point(event), value: "" });
            return;
          }
          if (!session) return;
          const started: Shape = { kind, points: [point(event)], color: colors[kind] };
          drawingRef.current = started;
          setCurrent(started);
          // Finish on any pointer up, even one that lands outside the canvas, where it
          // lands. The session drops these listeners, and the stroke, if the tool closes
          // first.
          const canvas = event.currentTarget;
          const finish = (end: PointerEvent) => {
            stopUp();
            stopCancel();
            const drawn = drawingRef.current;
            drawingRef.current = null;
            setCurrent(null);
            if (!drawn) return;
            const released = end.type === "pointerup" && Number.isFinite(end.clientX);
            const shape = released ? extend(drawn, point(end)) : drawn;
            // Kept if it went anywhere: a loop back to where it started has points between.
            const first = shape.points[0];
            if (first && shape.points.some((p) => p.x !== first.x || p.y !== first.y)) {
              session.edit((all) => ({ shapes: [...all.shapes, shape] }));
            }
          };
          const stopUp = session.listen(windowOf(canvas), "pointerup", finish);
          const stopCancel = session.listen(windowOf(canvas), "pointercancel", finish);
        }}
        onPointerMove={(event) => {
          const shape = drawingRef.current;
          if (!shape) return;
          drawingRef.current = extend(shape, point(event));
          setCurrent(drawingRef.current);
        }}
      />
      {textDraft && !frozen && (
        <Input
          autoFocus
          value={textDraft.value}
          aria-label="Text to add"
          placeholder="Type, then Enter"
          onChange={(event) => {
            setTextDraft({ ...textDraft, value: event.target.value });
          }}
          onKeyDown={(event) => {
            // Keys while composing are the input method's (Escape cancels its candidate).
            if (isImeCompositionKeyEvent(event)) return;
            if (event.key === "Escape") {
              event.preventDefault();
              setTextDraft(null);
            } else if (event.key === "Enter") {
              event.preventDefault();
              commitText(isSendShortcut(event));
            }
          }}
          onBlur={() => commitText(false)}
          className={cn(LAYER_Z, TEXT_DRAFT_CLASS)}
          style={{
            left: rect.left + textDraft.x,
            top: rect.top + textDraft.y - 16,
            color: colors.text,
          }}
        />
      )}
      <DrawDock
        rect={rect}
        kind={kind}
        frozen={frozen}
        undoDisabled={undoCommand.blocked !== undefined}
        sendDisabled={sendCommand.blocked !== undefined}
        onKind={setKind}
        onUndo={() => runCommand(undoCommand)}
        onSend={() => runCommand(sendCommand)}
        onClose={() => closeTool(tabId)}
        color={
          <Popover>
            <PopoverTrigger asChild>
              <Button
                componentId="browserDesignTools.draw.color"
                type="button"
                variant="ghost"
                size="icon-xs"
                aria-label={`Color for ${SHAPE_TOOLS.find((tool) => tool.kind === kind)?.label ?? "marks"}`}
                title="Color"
                className="shrink-0 rounded-full"
              >
                <ColorSwatch color={colors[kind]} />
              </Button>
            </PopoverTrigger>
            <PopoverContent
              side="bottom"
              container={popoverContainer}
              className={cn("z-50", PALETTE_CLASS)}
            >
              {MARK_COLORS.map((color) => (
                <PaletteColor
                  key={color.value}
                  color={color}
                  selected={colors[kind] === color.value}
                  onPick={() => setColors((all) => ({ ...all, [kind]: color.value }))}
                />
              ))}
            </PopoverContent>
          </Popover>
        }
        note={
          <Input
            value={note}
            readOnly={frozen}
            aria-label="Note for the agent"
            placeholder="Add a note for the agent"
            onChange={(event) => session?.edit({ note: event.target.value })}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !isImeCompositionKeyEvent(event)) sendFromKeys();
            }}
            className={NOTE_FIELD_CLASS}
          />
        }
      />
    </>
  );
}

/** `shape` drawn on to `next`: a pen adds the point, a rect or arrow ends there. */
function extend(shape: Shape, next: { x: number; y: number }): Shape {
  const last = shape.points.at(-1);
  if (last && last.x === next.x && last.y === next.y) return shape;
  return {
    ...shape,
    points: shape.kind === "pen" ? [...shape.points, next] : [shape.points[0], next],
  };
}
