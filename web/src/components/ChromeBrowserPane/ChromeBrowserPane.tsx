/**
 * ChromeBrowserPane — a live view of a Chrome running on the session's host.
 *
 * The page arrives as a stream of JPEG frames over a WebSocket and is drawn on
 * a canvas; pointer and keyboard input on the canvas go back the same way. It
 * needs no desktop shell, so it works in a plain browser tab too.
 */

import { ChevronLeftIcon, ChevronRightIcon, RotateCwIcon } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { isDatabricksWorkspace, isHostRoutingEnabled, resolveWebSocketUrl } from "@/lib/host";
import { normalizeTypedUrl } from "@/lib/normalizeTypedUrl";
import { nextReconnectDelay } from "@/lib/reconnectBackoff";
import { getSessionHost, isHostKeyless } from "@/lib/sessionHost";
import { cn } from "@/lib/utils";
import {
  decodeFrame,
  fitPage,
  isRetryableClose,
  keyMessage,
  modifierMask,
  mouseButtonName,
  pagePoint,
  parseServerMessage,
  chromeBrowserAttachPath,
  WS_CLOSE_NOT_CONFIGURED,
  type ChromeBrowserClientMessage,
  type ChromeBrowserState,
} from "./chromeBrowserProtocol";

type Status = "connecting" | "live" | "reconnecting" | "not-configured" | "refused";

/** Wait this long after the pane stops resizing before asking Chrome to lay out again. */
const RESIZE_SETTLE_MS = 150;
const NOTICE_MS = 6_000;
/** Pixels per wheel "line", for mice that report lines instead of pixels. */
const WHEEL_LINE_PX = 16;

const STATUS_TEXT: Record<Exclude<Status, "live">, string> = {
  connecting: "Connecting to the Chrome browser…",
  reconnecting: "Reconnecting to the Chrome browser…",
  "not-configured": "No Chrome browser is set up on this session's host.",
  refused: "You don't have access to this session's Chrome browser.",
};

const TOOLBAR_BUTTON =
  "flex size-6 items-center justify-center rounded text-foreground hover:bg-muted disabled:pointer-events-none disabled:opacity-40";

export interface ChromeBrowserPaneProps {
  conversationId: string;
  /** Show the page without sending input; for viewers who don't own the session. */
  readOnly?: boolean;
  /** Whether the pane is on screen; an inactive pane holds no connection. */
  active?: boolean;
  className?: string;
}

function attachUrl(conversationId: string, readOnly: boolean): string {
  // Mirrors the terminal's routing: the socket must reach the replica holding the runner tunnel.
  const hostId = isHostRoutingEnabled() ? getSessionHost(conversationId) : null;
  const unkeyed = hostId !== null && isDatabricksWorkspace() && isHostKeyless(hostId);
  return resolveWebSocketUrl(
    chromeBrowserAttachPath(conversationId, readOnly, unkeyed ? undefined : (hostId ?? undefined)),
    { unkeyed },
  );
}

export function ChromeBrowserPane({
  conversationId,
  readOnly = false,
  active = true,
  className,
}: ChromeBrowserPaneProps) {
  const [status, setStatus] = useState<Status>("connecting");
  const [browser, setBrowser] = useState<ChromeBrowserState | null>(null);
  const [viewOnly, setViewOnly] = useState(readOnly);
  const [notice, setNotice] = useState<string | null>(null);
  const [address, setAddress] = useState("");
  const [pageSize, setPageSize] = useState({ width: 0, height: 0 });
  const [stageSize, setStageSize] = useState({ width: 0, height: 0 });

  const socketRef = useRef<WebSocket | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const stageRef = useRef<HTMLDivElement | null>(null);
  const addressEditingRef = useRef(false);
  const pageSizeRef = useRef(pageSize);
  const stageSizeRef = useRef(stageSize);
  const drawnSequenceRef = useRef(0);
  const pendingMoveRef = useRef<ChromeBrowserClientMessage | null>(null);
  const moveFrameRef = useRef<number | null>(null);

  const send = useCallback((message: ChromeBrowserClientMessage) => {
    const socket = socketRef.current;
    if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
  }, []);

  const sendViewport = useCallback(() => {
    const { width, height } = stageSizeRef.current;
    if (width > 0 && height > 0) send({ type: "resize", width, height });
  }, [send]);

  const drawFrame = useCallback(
    async (buffer: ArrayBuffer) => {
      const frame = decodeFrame(buffer);
      if (!frame) return;
      try {
        const bitmap = await createImageBitmap(new Blob([frame.jpeg], { type: "image/jpeg" }));
        const canvas = canvasRef.current;
        // Decodes finish out of order; never draw an older frame over a newer one.
        if (canvas && frame.seq > drawnSequenceRef.current) {
          drawnSequenceRef.current = frame.seq;
          if (canvas.width !== bitmap.width) canvas.width = bitmap.width;
          if (canvas.height !== bitmap.height) canvas.height = bitmap.height;
          canvas.getContext("2d")?.drawImage(bitmap, 0, 0);
          const current = pageSizeRef.current;
          if (current.width !== frame.width || current.height !== frame.height) {
            pageSizeRef.current = { width: frame.width, height: frame.height };
            setPageSize(pageSizeRef.current);
          }
        }
        bitmap.close();
      } finally {
        // Acknowledged after drawing, so the runner sends no faster than this pane paints.
        send({ type: "frame_ack", seq: frame.seq });
      }
    },
    [send],
  );

  useEffect(() => {
    if (!active) return;
    let disposed = false;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let failedAttempts = 0;

    const connect = () => {
      const socket = new WebSocket(attachUrl(conversationId, readOnly));
      socket.binaryType = "arraybuffer";
      socketRef.current = socket;
      drawnSequenceRef.current = 0;

      socket.addEventListener("message", (event) => {
        if (typeof event.data !== "string") {
          void drawFrame(event.data as ArrayBuffer);
          return;
        }
        const message = parseServerMessage(event.data);
        if (!message) return;
        if (message.type === "ready") {
          failedAttempts = 0;
          setStatus("live");
          setViewOnly(message.readOnly);
          sendViewport();
        } else if (message.type === "state") {
          setBrowser(message.state);
          if (!addressEditingRef.current) setAddress(message.state.url);
        } else {
          setNotice(message.message);
        }
      });
      socket.addEventListener("close", (event) => {
        if (disposed || socketRef.current !== socket) return;
        socketRef.current = null;
        if (!isRetryableClose(event.code)) {
          setStatus(event.code === WS_CLOSE_NOT_CONFIGURED ? "not-configured" : "refused");
          return;
        }
        failedAttempts += 1;
        setStatus("reconnecting");
        retryTimer = setTimeout(connect, nextReconnectDelay(failedAttempts));
      });
    };

    setStatus("connecting");
    connect();
    return () => {
      disposed = true;
      if (retryTimer !== null) clearTimeout(retryTimer);
      socketRef.current?.close(1000);
      socketRef.current = null;
    };
  }, [active, conversationId, readOnly, drawFrame, sendViewport]);

  // Lay the remote page out at the size of the space it is shown in.
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    let settleTimer: ReturnType<typeof setTimeout> | null = null;
    const observer = new ResizeObserver(([entry]) => {
      const next = {
        width: Math.round(entry.contentRect.width),
        height: Math.round(entry.contentRect.height),
      };
      stageSizeRef.current = next;
      setStageSize(next);
      if (settleTimer !== null) clearTimeout(settleTimer);
      settleTimer = setTimeout(sendViewport, RESIZE_SETTLE_MS);
    });
    observer.observe(stage);
    return () => {
      observer.disconnect();
      if (settleTimer !== null) clearTimeout(settleTimer);
    };
  }, [sendViewport]);

  useEffect(() => {
    if (notice === null) return;
    const timer = setTimeout(() => setNotice(null), NOTICE_MS);
    return () => clearTimeout(timer);
  }, [notice]);

  const interactive = status === "live" && !viewOnly;

  const pointFor = useCallback((event: { clientX: number; clientY: number }) => {
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!rect) return { x: 0, y: 0 };
    return pagePoint({ x: event.clientX, y: event.clientY }, rect, pageSizeRef.current);
  }, []);

  // React registers wheel listeners as passive, which cannot stop the app from scrolling.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !interactive) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const scale = event.deltaMode === WheelEvent.DOM_DELTA_LINE ? WHEEL_LINE_PX : 1;
      send({
        type: "mouse",
        event: "wheel",
        ...pointFor(event),
        button: "none",
        buttons: event.buttons,
        modifiers: modifierMask(event),
        delta_x: event.deltaX * scale,
        delta_y: event.deltaY * scale,
      });
    };
    canvas.addEventListener("wheel", onWheel, { passive: false });
    return () => canvas.removeEventListener("wheel", onWheel);
  }, [interactive, pointFor, send]);

  useEffect(
    () => () => {
      if (moveFrameRef.current !== null) cancelAnimationFrame(moveFrameRef.current);
    },
    [],
  );

  const flushPendingMove = () => {
    if (pendingMoveRef.current) send(pendingMoveRef.current);
    pendingMoveRef.current = null;
  };

  const sendButton = (event: React.MouseEvent<HTMLCanvasElement>, kind: "down" | "up") => {
    event.preventDefault();
    // A queued move must land before the press it led up to.
    flushPendingMove();
    send({
      type: "mouse",
      event: kind,
      ...pointFor(event),
      button: mouseButtonName(event.button),
      buttons: event.buttons,
      modifiers: modifierMask(event),
      click_count: Math.min(Math.max(event.detail, 1), 3),
    });
  };

  const sendMove = (event: React.MouseEvent<HTMLCanvasElement>) => {
    // At most one move per painted frame; the newest position wins.
    pendingMoveRef.current = {
      type: "mouse",
      event: "move",
      ...pointFor(event),
      button: "none",
      buttons: event.buttons,
      modifiers: modifierMask(event),
    };
    if (moveFrameRef.current !== null) return;
    moveFrameRef.current = requestAnimationFrame(() => {
      moveFrameRef.current = null;
      flushPendingMove();
    });
  };

  const sendKey = (event: React.KeyboardEvent<HTMLCanvasElement>, kind: "down" | "up") => {
    // Let the local paste shortcut through, so its paste event carries this machine's clipboard.
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "v") return;
    event.preventDefault();
    send(keyMessage(event.nativeEvent, kind));
  };

  const submitAddress = () => {
    const typed = address.trim();
    if (typed) send({ type: "navigate", url: normalizeTypedUrl(typed) });
  };

  const drawn = fitPage(pageSize, stageSize);
  const tabs = browser?.tabs ?? [];

  return (
    <div
      className={cn("flex min-h-0 min-w-0 flex-col", className)}
      data-chrome-browser-pane={conversationId}
    >
      <div className="flex shrink-0 items-center gap-1 border-border border-b bg-card px-2 py-1.5">
        <button
          type="button"
          onClick={() => send({ type: "back" })}
          disabled={!interactive || !browser?.canGoBack}
          aria-label="Go back"
          className={TOOLBAR_BUTTON}
        >
          <ChevronLeftIcon className="size-4" />
        </button>
        <button
          type="button"
          onClick={() => send({ type: "forward" })}
          disabled={!interactive || !browser?.canGoForward}
          aria-label="Go forward"
          className={TOOLBAR_BUTTON}
        >
          <ChevronRightIcon className="size-4" />
        </button>
        <button
          type="button"
          onClick={() => send({ type: "reload" })}
          disabled={!interactive}
          aria-label="Reload"
          className={TOOLBAR_BUTTON}
        >
          <RotateCwIcon className={cn("size-4", browser?.loading && "animate-spin")} />
        </button>
        <input
          type="text"
          value={address}
          spellCheck={false}
          autoCorrect="off"
          autoCapitalize="off"
          placeholder="Enter a URL"
          aria-label="Address bar"
          readOnly={!interactive}
          onChange={(event) => setAddress(event.target.value)}
          onFocus={() => {
            addressEditingRef.current = true;
          }}
          onBlur={() => {
            addressEditingRef.current = false;
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              submitAddress();
              event.currentTarget.blur();
            }
          }}
          className="h-6 min-w-0 flex-1 rounded-md border border-input bg-transparent px-2 text-foreground text-sm outline-none placeholder:text-muted-foreground focus-visible:border-ring dark:bg-input"
        />
        {tabs.length > 1 && (
          <Select
            value={browser?.tabId ?? undefined}
            onValueChange={(id) => send({ type: "select_tab", id })}
            disabled={!interactive}
          >
            <SelectTrigger size="sm" aria-label="Browser tab" className="h-6 max-w-40 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {tabs.map((tab) => (
                <SelectItem key={tab.id} value={tab.id}>
                  {tab.title || tab.url || "Untitled"}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
        {status === "live" && viewOnly && (
          <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-muted-foreground text-xs">
            View only
          </span>
        )}
      </div>
      {notice !== null && (
        <p role="status" className="shrink-0 border-border border-b px-2 py-1 text-xs">
          {notice}
        </p>
      )}
      <div
        ref={stageRef}
        className="relative flex min-h-0 min-w-0 flex-1 items-center justify-center overflow-hidden bg-muted"
      >
        <canvas
          ref={canvasRef}
          tabIndex={interactive ? 0 : -1}
          role={interactive ? "application" : "img"}
          aria-label={browser?.title ? `Chrome browser: ${browser.title}` : "Chrome browser"}
          style={{ width: drawn.width, height: drawn.height }}
          className={cn("outline-none", status !== "live" && "opacity-40")}
          onMouseDown={
            interactive
              ? (event) => {
                  event.currentTarget.focus();
                  sendButton(event, "down");
                }
              : undefined
          }
          onMouseUp={interactive ? (event) => sendButton(event, "up") : undefined}
          onMouseMove={interactive ? sendMove : undefined}
          onContextMenu={interactive ? (event) => event.preventDefault() : undefined}
          onKeyDown={interactive ? (event) => sendKey(event, "down") : undefined}
          onKeyUp={interactive ? (event) => sendKey(event, "up") : undefined}
          onPaste={
            interactive
              ? (event) => {
                  event.preventDefault();
                  const text = event.clipboardData.getData("text");
                  if (text) send({ type: "text", text });
                }
              : undefined
          }
        />
        {status !== "live" && (
          <p
            role="status"
            className="absolute inset-0 flex items-center justify-center px-6 text-center text-muted-foreground text-sm"
          >
            {STATUS_TEXT[status]}
          </p>
        )}
      </div>
    </div>
  );
}
