import {
  createContext,
  lazy,
  Suspense,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import { Spinner } from "@/components/ui/spinner";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useNavigate, useSearchParams } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { SidebarOpenButton } from "@/shell/SidebarOpenButton";
import { MAIN_CANVAS_ID } from "./canvasLayout";
import { CANVAS_QUERY_PARAM, canvasLocation } from "./canvasNavigation";

const CanvasPage = lazy(() =>
  import("@/pages/CanvasPage").then((m) => ({ default: m.CanvasPage })),
);
const RATIO_KEY = "omnigent:canvas-split-ratio";
const DEFAULT_RATIO = 0.54;
const MIN_CANVAS_WIDTH = 320;
export const CANVAS_CONVERSATION_MIN_WIDTH = 380;
const DIVIDER_WIDTH = 8;
const COMPACT_WIDTH = 760;

function readRatio(): number {
  try {
    const value = Number(localStorage.getItem(RATIO_KEY));
    if (Number.isFinite(value) && value > 0 && value < 1) return value;
  } catch {
    // Storage can be unavailable in embedded previews.
  }
  return DEFAULT_RATIO;
}

interface CanvasControls {
  compact: boolean;
  focused: boolean;
  closeConversation: () => void;
  toggleFocus: () => void;
}

const CanvasWorkspaceContext = createContext<CanvasControls | null>(null);
export const useCanvasWorkspace = () => useContext(CanvasWorkspaceContext);

/** Keeps the same board mounted while the route selects a conversation. */
export function CanvasWorkspace({
  active,
  conversationId,
  minConversationWidth = CANVAS_CONVERSATION_MIN_WIDTH,
  onCanvasWidthChange,
  onOpenSidebar,
  children,
}: {
  active: boolean;
  conversationId?: string;
  minConversationWidth?: number;
  onCanvasWidthChange?: (width: number) => void;
  onOpenSidebar?: (peek?: boolean) => void;
  children: ReactNode;
}) {
  const navigate = useNavigate();
  const isMobile = useIsMobileViewport();
  const [searchParams] = useSearchParams();
  const container = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(() => window.innerWidth);
  const [ratio, setRatio] = useState(readRatio);
  const ratioRef = useRef(ratio);
  ratioRef.current = ratio;
  const [focused, setFocused] = useState(false);
  const drag = useRef<{ pointerId: number; x: number; width: number } | null>(null);
  const compact = isMobile || width < COMPACT_WIDTH;
  const available = Math.max(1, width - DIVIDER_WIDTH);
  const maximum = Math.max(MIN_CANVAS_WIDTH, available - minConversationWidth);
  const canvasWidth = Math.round(Math.max(MIN_CANVAS_WIDTH, Math.min(maximum, available * ratio)));
  const canvasVisible = !conversationId || (!compact && !focused);

  useLayoutEffect(() => {
    onCanvasWidthChange?.(
      active && conversationId && canvasVisible ? canvasWidth + DIVIDER_WIDTH : 0,
    );
  }, [active, conversationId, canvasVisible, canvasWidth, onCanvasWidthChange]);

  useLayoutEffect(() => {
    const node = container.current;
    if (!active || !node) return;
    const measure = () => {
      const next = node.getBoundingClientRect().width;
      if (next > 0) setWidth(next);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [active]);

  useEffect(() => setFocused(false), [conversationId, active]);
  const showCanvas = useCallback(() => setFocused(false), []);
  useEffect(() => {
    if (!focused) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.defaultPrevented) showCanvas();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [focused, showCanvas]);

  const closeConversation = useCallback(() => {
    navigate(canvasLocation(searchParams.get(CANVAS_QUERY_PARAM) ?? MAIN_CANVAS_ID));
  }, [navigate, searchParams]);
  const controls = useMemo(
    () => ({
      compact,
      focused,
      closeConversation,
      toggleFocus: () => (focused ? showCanvas() : setFocused(true)),
    }),
    [compact, focused, closeConversation, showCanvas],
  );

  const resize = (next: number) => {
    const value = Math.max(MIN_CANVAS_WIDTH, Math.min(maximum, next)) / available;
    ratioRef.current = value;
    setRatio(value);
  };
  const saveRatio = () => {
    try {
      localStorage.setItem(RATIO_KEY, String(ratioRef.current));
    } catch {
      // Resizing still works for this visit.
    }
    drag.current = null;
  };

  if (!active) return <div className="relative flex min-h-0 min-w-0 flex-1">{children}</div>;
  return (
    <CanvasWorkspaceContext.Provider value={controls}>
      <div
        ref={container}
        className="canvas-workspace relative flex min-h-0 min-w-0 flex-1"
        data-testid="canvas-workspace"
      >
        <section
          aria-label="Canvas pane"
          className={cn(
            "relative min-h-0 min-w-0 shrink-0 flex-col",
            canvasVisible ? "flex" : "hidden",
          )}
          style={
            {
              width: conversationId && !compact ? canvasWidth : "100%",
              "--omnigent-header-height": "0px",
            } as CSSProperties
          }
        >
          {/* Keep navigation available while the board is loading. */}
          {onOpenSidebar && (
            <div className="chat-header pointer-events-none absolute inset-x-0 top-0 z-30 flex h-14 items-center px-2 md:h-12 md:px-4">
              <SidebarOpenButton componentId="canvas.open_sidebar" onOpenSidebar={onOpenSidebar} />
            </div>
          )}
          <Suspense
            fallback={
              <div className="flex flex-1 items-center justify-center">
                <Spinner aria-label="Loading Canvas" />
              </div>
            }
          >
            <CanvasPage selectedSessionId={conversationId ?? null} />
          </Suspense>
        </section>
        {conversationId && canvasVisible && (
          <div
            role="separator"
            tabIndex={0}
            aria-label="Resize canvas and conversation"
            aria-orientation="vertical"
            aria-valuemin={MIN_CANVAS_WIDTH}
            aria-valuemax={maximum}
            aria-valuenow={canvasWidth}
            className="group flex w-2 shrink-0 touch-none cursor-col-resize items-center justify-center border-x border-border/60 bg-muted/30 hover:bg-brand-accent/10 focus-visible:outline-2 focus-visible:outline-brand-accent"
            onPointerDown={(event) => {
              if (event.button !== 0) return;
              event.preventDefault();
              event.currentTarget.focus();
              event.currentTarget.setPointerCapture(event.pointerId);
              drag.current = { pointerId: event.pointerId, x: event.clientX, width: canvasWidth };
            }}
            onPointerMove={(event) => {
              if (drag.current?.pointerId === event.pointerId)
                resize(drag.current.width + event.clientX - drag.current.x);
            }}
            onPointerUp={saveRatio}
            onPointerCancel={saveRatio}
            onLostPointerCapture={saveRatio}
            onDoubleClick={() => {
              ratioRef.current = DEFAULT_RATIO;
              setRatio(DEFAULT_RATIO);
              saveRatio();
            }}
            onKeyDown={(event) => {
              if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
              event.preventDefault();
              let next = canvasWidth + (event.key === "ArrowLeft" ? -24 : 24);
              if (event.key === "Home") next = MIN_CANVAS_WIDTH;
              else if (event.key === "End") next = maximum;
              resize(next);
              saveRatio();
            }}
          >
            <span className="h-9 w-0.5 rounded-full bg-muted-foreground/30 group-hover:bg-brand-accent" />
          </div>
        )}
        <section
          aria-label="Canvas conversation"
          className={cn("relative min-h-0 min-w-0 flex-1", conversationId ? "flex" : "hidden")}
        >
          {children}
        </section>
      </div>
    </CanvasWorkspaceContext.Provider>
  );
}
