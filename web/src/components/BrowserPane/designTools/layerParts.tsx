// What the design tools show over a Browser tab's page: the parts the tools' layers
// (toolLayers.tsx) draw in the overlay document main stacks above the native view.
import {
  ArrowUpIcon,
  ArrowUpRightIcon,
  CaseSensitiveIcon,
  MousePointerClickIcon,
  PencilLineIcon,
  SquareIcon,
  Undo2Icon,
  XIcon,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useState, type CSSProperties, type ReactNode, type Ref } from "react";
import { Button } from "@/components/ui/button";
import { isImeCompositionKeyEvent } from "@/lib/ime";
import { cn } from "@/lib/utils";
import type { CssRect, Hit, ShapeKind } from "./core/designToolsStore";
import { elementTag } from "./core/toolActions";
import type { MARK_COLORS } from "./core/marks";

export const NOTICE_MS = 5000;
// Below the popovers (z-50) the layers open, such as Draw's palette.
export const LAYER_Z = "z-40";

export const SHAPE_TOOLS: { kind: ShapeKind; label: string; icon: LucideIcon }[] = [
  { kind: "pen", label: "Pen", icon: PencilLineIcon },
  { kind: "rect", label: "Rectangle", icon: SquareIcon },
  { kind: "arrow", label: "Arrow", icon: ArrowUpRightIcon },
  { kind: "text", label: "Text", icon: CaseSensitiveIcon },
];

export const isSendShortcut = (event: { key: string; metaKey: boolean; ctrlKey: boolean }) =>
  event.key === "Enter" && (event.metaKey || event.ctrlKey);

/** Where a layer sits: the page's rect, in the host's coordinates. */
export interface LayerRect {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** Set a canvas up for marks: device pixels, round 3px strokes. */
export function prepareMarksCanvas(
  context: CanvasRenderingContext2D,
  width: number,
  height: number,
  ratio: number,
): void {
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  context.lineWidth = 3;
  context.lineCap = "round";
  context.lineJoin = "round";
}

const CARD_WIDTH = 320;
const CARD_HEIGHT = 120;
const CARD_GAP = 10;
const CARD_MARGIN = 8;

/**
 * Where the request card goes, and how wide: beside the picked element, inside the frame.
 * `height` is the card's own, measured: its field grows with the request.
 */
export function cardPosition(
  element: CssRect,
  frame: { width: number; height: number },
  height = CARD_HEIGHT,
): { left: number; top: number; width: number } {
  // Narrower than usual in a narrow frame, so all of it stays on the page.
  const width = Math.max(0, Math.min(CARD_WIDTH, frame.width - 2 * CARD_MARGIN));
  const clampX = (x: number) =>
    Math.max(CARD_MARGIN, Math.min(x, frame.width - width - CARD_MARGIN));
  const clampY = (y: number) =>
    Math.max(CARD_MARGIN, Math.min(y, frame.height - height - CARD_MARGIN));
  const below = element.y + element.height + CARD_GAP;
  const above = element.y - CARD_GAP - height;
  if (below + height <= frame.height) return { left: clampX(element.x), top: below, width };
  if (above >= 0) return { left: clampX(element.x), top: above, width };
  const right = element.x + element.width + CARD_GAP;
  const fitsRight = right + width <= frame.width - CARD_MARGIN;
  const left = fitsRight ? right : element.x - CARD_GAP - width;
  return { left: clampX(left), top: clampY(element.y), width };
}

export function ToolButton({
  label,
  icon: Icon,
  active,
  disabled,
  onClick,
  onEscape,
}: {
  label: string;
  icon: LucideIcon;
  active?: boolean;
  disabled?: boolean;
  onClick?: () => void;
  /** While it's active and focused, Esc here calls this, ahead of the app's own Esc. */
  onEscape?: () => void;
}) {
  const [focused, setFocused] = useState(false);
  const ownsEscape = !!onEscape && !!active && focused;
  return (
    <Button
      componentId={`browserDesignTools.${label.replace(/[^a-z]+/gi, "-").toLowerCase()}`}
      type="button"
      aria-label={label}
      title={label}
      aria-pressed={active}
      disabled={disabled}
      onClick={onClick}
      onFocus={() => setFocused(true)}
      onBlur={() => setFocused(false)}
      data-escape-owner={ownsEscape ? "" : undefined}
      onKeyDown={(event) => {
        if (!ownsEscape || event.key !== "Escape" || isImeCompositionKeyEvent(event)) return;
        event.preventDefault();
        event.stopPropagation();
        onEscape();
      }}
      variant="ghost"
      size="icon-xs"
      className={cn("shrink-0 rounded-full", active && "bg-background text-primary shadow-sm")}
    >
      <Icon className="size-4" />
    </Button>
  );
}

/** The active tool's dock: one floating bar, centered along the top of the page. */
export function ToolBar({
  rect,
  icon: Icon,
  closeLabel,
  onClose,
  children,
}: {
  rect: LayerRect;
  icon?: LucideIcon;
  /** The x at the bar's end, which closes the tool (as Esc does): "Close Annotate". */
  closeLabel?: string;
  onClose?: () => void;
  children: ReactNode;
}) {
  return (
    <div
      className={cn(
        LAYER_Z,
        "fixed flex items-center gap-2 rounded-full border border-border bg-popover/95 py-1 pr-1 pl-3 text-popover-foreground text-ui shadow-lg backdrop-blur",
      )}
      style={{
        left: rect.left + rect.width / 2,
        top: rect.top + 12,
        maxWidth: rect.width - 24,
        transform: "translateX(-50%)",
      }}
    >
      {/* A page too narrow for it all scrolls these; the x stays in view at the end. */}
      <div className="flex min-w-0 items-center gap-2 overflow-x-auto">
        {Icon && <Icon className="size-4 shrink-0 text-primary" />}
        {children}
      </div>
      {onClose && closeLabel && <ToolButton label={closeLabel} icon={XIcon} onClick={onClose} />}
    </div>
  );
}

/** A keyboard key in a hint. */
export function Key({ children }: { children: ReactNode }) {
  return (
    <kbd className="rounded border border-border px-1.5 text-muted-foreground text-sm">
      {children}
    </kbd>
  );
}

/** Annotate's hint while nothing is picked. */
export function PickHint({ rect, onClose }: { rect: LayerRect; onClose?: () => void }) {
  return (
    <ToolBar rect={rect} icon={MousePointerClickIcon} closeLabel="Close Annotate" onClose={onClose}>
      <span className="truncate">Pick an element to change</span>
      <span className={onClose ? undefined : "pr-2"}>
        <Key>Esc</Key>
      </span>
    </ToolBar>
  );
}

/** The outline round a hovered or picked element, with its tag. */
export function ElementOutline({ hit, picked }: { hit: Hit; picked: boolean }) {
  return (
    <div
      className={cn(
        "pointer-events-none absolute rounded-md outline-2 outline-status-blue outline-offset-2 motion-safe:transition-all motion-safe:duration-100",
        picked ? "bg-status-blue/5" : "bg-status-blue/10",
      )}
      style={{ left: hit.rect.x, top: hit.rect.y, width: hit.rect.width, height: hit.rect.height }}
    >
      <span
        className={cn(
          "absolute left-0 max-w-64 truncate rounded-md bg-status-blue px-1.5 py-0.5 font-medium text-background text-sm shadow-sm",
          hit.rect.y >= 28 ? "-top-7" : "top-1",
        )}
      >
        {elementTag(hit.element)}
      </span>
    </div>
  );
}

export const REQUEST_FIELD_CLASS =
  "field-sizing-content max-h-40 min-h-0 resize-none border-0 bg-transparent px-1 py-1 shadow-none focus-visible:ring-0 dark:bg-transparent";

/** Annotate's request card: the field, Cancel and the round Send. */
export function RequestCard({
  cardRef,
  style,
  field,
  sendDisabled,
  onCancel,
  onSubmit,
}: {
  cardRef?: Ref<HTMLFormElement>;
  style: CSSProperties;
  field: ReactNode;
  sendDisabled: boolean;
  onCancel?: () => void;
  onSubmit?: () => void;
}) {
  return (
    <form
      ref={cardRef}
      aria-label="Annotate"
      className={cn(
        LAYER_Z,
        "fixed flex flex-col gap-1 rounded-xl border border-border bg-popover p-2 text-popover-foreground shadow-xl",
      )}
      style={{ width: CARD_WIDTH, ...style }}
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit?.();
      }}
    >
      {field}
      <div className="flex items-center gap-2">
        <Button
          componentId="browserDesignTools.annotate.cancel"
          type="button"
          variant="ghost"
          size="xs"
          className="ml-auto"
          onClick={onCancel}
        >
          Cancel
        </Button>
        <Button
          componentId="browserDesignTools.annotate.send"
          type="submit"
          aria-label="Send to agent"
          title="Send to agent"
          disabled={sendDisabled}
          variant="default"
          size="icon-xs"
          className="rounded-full"
        >
          <ArrowUpIcon className="size-4" />
        </Button>
      </div>
    </form>
  );
}

/** Draw's color swatch button: the current tool's color. */
export function ColorSwatch({ color }: { color: string }) {
  return (
    <span className="size-4 rounded-full border border-border" style={{ backgroundColor: color }} />
  );
}

/** One color in Draw's palette. */
export function PaletteColor({
  color,
  selected,
  onPick,
}: {
  color: (typeof MARK_COLORS)[number];
  selected: boolean;
  onPick?: () => void;
}) {
  return (
    <Button
      componentId="browserDesignTools.draw.color.pick"
      type="button"
      variant="ghost"
      size="icon-xs"
      aria-label={color.name}
      aria-pressed={selected}
      title={color.name}
      className="rounded-full"
      onClick={onPick}
    >
      <span
        className={cn(
          "size-4 rounded-full border border-border",
          selected && "ring-2 ring-ring ring-offset-1 ring-offset-popover",
        )}
        style={{ backgroundColor: color.value }}
      />
    </Button>
  );
}

export const PALETTE_CLASS = "flex w-auto gap-1 rounded-full p-1";

/** Draw's dock: shapes, color, Undo, the note, and Send. */
export function DrawDock({
  rect,
  kind,
  frozen,
  undoDisabled,
  sendDisabled,
  color,
  note,
  onKind,
  onUndo,
  onSend,
  onClose,
}: {
  rect: LayerRect;
  kind: ShapeKind;
  frozen: boolean;
  undoDisabled: boolean;
  sendDisabled: boolean;
  /** The color control: a swatch that opens the palette. */
  color: ReactNode;
  note: ReactNode;
  onKind?: (kind: ShapeKind) => void;
  onUndo?: () => void;
  onSend?: () => void;
  onClose?: () => void;
}) {
  return (
    <ToolBar rect={rect} closeLabel="Close Draw" onClose={onClose}>
      <div role="group" aria-label="Shape" className="flex shrink-0 rounded-full bg-muted p-0.5">
        {SHAPE_TOOLS.map((tool) => (
          <ToolButton
            key={tool.kind}
            label={tool.label}
            icon={tool.icon}
            active={kind === tool.kind}
            onClick={onKind && (() => onKind(tool.kind))}
          />
        ))}
      </div>
      {color}
      <ToolButton label="Undo" icon={Undo2Icon} disabled={undoDisabled} onClick={onUndo} />
      {note}
      <Button
        componentId="browserDesignTools.draw.send"
        type="button"
        disabled={sendDisabled}
        onClick={onSend}
        variant="default"
        size="xs"
        className="shrink-0 rounded-full"
      >
        {frozen ? "Capturing…" : "Send to agent"}
      </Button>
    </ToolBar>
  );
}

export const NOTE_FIELD_CLASS =
  "h-7 w-44 min-w-0 border-0 bg-transparent shadow-none focus-visible:ring-0 dark:bg-transparent";
export const TEXT_DRAFT_CLASS = "fixed h-8 w-56 bg-popover font-semibold shadow-lg";

/** A notice along the bottom of the page (or the window's corner, before it's measured). */
export function Notice({
  rect,
  style,
  children,
}: {
  rect: LayerRect | null;
  /** Placed by its caller instead (the desktop app's notice overlay is its own size). */
  style?: CSSProperties;
  children: ReactNode;
}) {
  return (
    <div
      role="status"
      className={cn(
        LAYER_Z,
        "fixed max-w-sm rounded-full border border-border bg-popover px-3 py-1.5 text-popover-foreground text-ui shadow-lg",
      )}
      style={
        style ??
        (rect
          ? {
              left: rect.left + rect.width / 2,
              top: rect.top + rect.height - 12,
              transform: "translate(-50%, -100%)",
            }
          : { right: 16, bottom: 16 })
      }
    >
      {children}
    </div>
  );
}

/** Whether the toolbar (and so its Browser pane) is laid out on screen. */
export function useShownRef(): [(element: HTMLElement | null) => void, boolean] {
  const [element, setElement] = useState<HTMLElement | null>(null);
  const [shown, setShown] = useState(false);
  useEffect(() => {
    if (!element) return;
    const check = () => setShown(element.getClientRects().length > 0);
    check();
    const observer = new ResizeObserver(check);
    observer.observe(element);
    return () => observer.disconnect();
  }, [element]);
  return [setElement, shown];
}
