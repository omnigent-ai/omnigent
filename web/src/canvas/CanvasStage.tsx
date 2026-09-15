import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import { ArrowLeftIcon, ExternalLinkIcon, PanelLeftIcon, XIcon } from "lucide-react";
import { Link } from "@/lib/routing";
import { sessionPageHref, useNavigateToSession } from "@/lib/sessionNavigation";
import { cn } from "@/lib/utils";

const MIN_BOARD = 320;
const MIN_CHAT = 480;
const DIVIDER = 6;
const MIN_SPLIT = 960;

export function useCanvasSplitLayout(enabled: boolean, selected: boolean) {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const [preferred, setPreferred] = useState<number | null>(null);
  const dragging = useRef<number | null>(null);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!enabled || !element) return;
    const measure = () => setWidth(element.getBoundingClientRect().width);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [enabled]);
  const split = enabled && selected && width >= MIN_SPLIT;
  const maxBoard = Math.max(MIN_BOARD, width - MIN_CHAT - DIVIDER);
  const clamp = (value: number) => Math.max(MIN_BOARD, Math.min(maxBoard, value));
  const boardWidth = split ? clamp(preferred ?? width * 0.45) : 0;
  const reservedWidth = split ? boardWidth + DIVIDER : 0;
  return {
    ref,
    split,
    boardWidth,
    reservedWidth,
    sessionWidth: Math.max(0, width - reservedWidth),
    handleProps: {
      role: "separator" as const,
      tabIndex: 0,
      "aria-label": "Resize Canvas and chat",
      "aria-orientation": "vertical" as const,
      "aria-valuemin": MIN_BOARD,
      "aria-valuemax": Math.round(maxBoard),
      "aria-valuenow": Math.round(boardWidth),
      onPointerDown: (event: React.PointerEvent<HTMLDivElement>) => {
        if (event.button !== 0) return;
        dragging.current = event.pointerId;
        event.currentTarget.setPointerCapture(event.pointerId);
        event.preventDefault();
      },
      onPointerMove: (event: React.PointerEvent<HTMLDivElement>) => {
        if (dragging.current !== event.pointerId || !ref.current) return;
        setPreferred(clamp(event.clientX - ref.current.getBoundingClientRect().left));
      },
      onPointerUp: (event: React.PointerEvent<HTMLDivElement>) => {
        if (dragging.current !== event.pointerId) return;
        dragging.current = null;
        event.currentTarget.releasePointerCapture(event.pointerId);
      },
      onLostPointerCapture: () => {
        dragging.current = null;
      },
      onKeyDown: (event: React.KeyboardEvent<HTMLDivElement>) => {
        const next =
          event.key === "ArrowLeft"
            ? boardWidth - 32
            : event.key === "ArrowRight"
              ? boardWidth + 32
              : event.key === "Home"
                ? MIN_BOARD
                : event.key === "End"
                  ? maxBoard
                  : null;
        if (next === null) return;
        event.preventDefault();
        setPreferred(clamp(next));
      },
    },
  };
}

/** Keep the board and one session surface mounted in stable slots through every resize. */
export function CanvasStage({
  enabled,
  conversationId,
  layout,
  board,
  children,
  sidebarOpen,
  onOpenSidebar,
  onBackToChat,
}: {
  enabled: boolean;
  conversationId: string | undefined;
  layout: ReturnType<typeof useCanvasSplitLayout>;
  board: ReactNode;
  children: ReactNode;
  sidebarOpen: boolean;
  onOpenSidebar: () => void;
  onBackToChat?: () => void;
}) {
  const navigateToSession = useNavigateToSession();
  const boardRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const previous = useRef(conversationId);
  useEffect(() => {
    const outgoing = previous.current;
    previous.current = conversationId;
    if (!enabled) return;
    if (conversationId && !outgoing) closeRef.current?.focus({ preventScroll: true });
    if (!conversationId && outgoing) {
      const card = Array.from(
        boardRef.current?.querySelectorAll<HTMLElement>("[data-canvas-session-id]") ?? [],
      ).find((element) => element.dataset.canvasSessionId === outgoing);
      (card ?? boardRef.current)?.focus({ preventScroll: true });
    }
  }, [conversationId, enabled]);

  if (!enabled) return <div className="relative flex min-h-0 min-w-0 flex-1">{children}</div>;
  return (
    <div
      ref={layout.ref}
      className="relative flex min-h-0 min-w-0 flex-1 overflow-hidden"
      data-testid="canvas-stage"
    >
      <section
        ref={boardRef}
        tabIndex={-1}
        aria-label="Canvas board"
        className={cn("relative min-h-0 min-w-0 flex-col", layout.split ? "shrink-0" : "flex-1")}
        style={{
          display: conversationId && !layout.split ? "none" : "flex",
          width: layout.split ? layout.boardWidth : undefined,
        }}
      >
        {!sidebarOpen && (
          <button
            type="button"
            className="absolute left-3 top-3 z-30 rounded p-1 hover:bg-accent"
            aria-label="Show navigation"
            onClick={onOpenSidebar}
          >
            <PanelLeftIcon className="size-4" />
          </button>
        )}
        {board}
      </section>
      {layout.split && (
        <div
          {...layout.handleProps}
          className="z-40 w-1.5 shrink-0 touch-none cursor-col-resize bg-border/50 hover:bg-brand-accent/50 focus-visible:bg-brand-accent"
        />
      )}
      <section
        aria-label="Session panel"
        className="relative flex min-h-0 min-w-0 flex-1 flex-col"
        style={{ display: conversationId ? "flex" : "none" }}
      >
        {conversationId && (
          <>
            <div
              className="flex shrink-0 items-center gap-2 border-b px-3 py-2 text-ui"
              style={{ paddingTop: "max(0.5rem, var(--omnigent-inset-top))" }}
            >
              <button
                ref={closeRef}
                type="button"
                onClick={() => navigateToSession(null)}
                className="flex items-center gap-1 rounded px-1 py-0.5 hover:bg-accent"
                aria-label="Close session panel"
              >
                {layout.split ? <XIcon className="size-4" /> : <ArrowLeftIcon className="size-4" />}
                {layout.split ? "Close" : "Back to Canvas"}
              </button>
              {onBackToChat && (
                <button
                  type="button"
                  onClick={onBackToChat}
                  className="rounded px-2 py-0.5 hover:bg-accent"
                >
                  Back to chat
                </button>
              )}
              <Link
                to={sessionPageHref(conversationId)}
                className="ml-auto flex items-center gap-1 rounded px-1 py-0.5 hover:bg-accent"
                aria-label="Open session full page"
              >
                <ExternalLinkIcon className="size-4" /> Full page
              </Link>
            </div>
            <div
              className="relative flex min-h-0 min-w-0 flex-1 overflow-hidden"
              style={{ "--omnigent-inset-top": "0px" } as CSSProperties}
            >
              {children}
            </div>
          </>
        )}
      </section>
    </div>
  );
}
