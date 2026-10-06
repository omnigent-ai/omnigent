// Wireframe viewer for `*.wireframe.html`: one screen at a time in a device
// frame. The frame renders at the device size, so the wireframe's media
// queries respond to it, and is scaled to fit. Same sandbox and branding gate
// as the deck viewer; the parent only talks to the frame over postMessage.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CodeIcon,
  Maximize2Icon,
  Minimize2Icon,
  MonitorIcon,
  PaletteIcon,
  SmartphoneIcon,
  TabletIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import type { Comment } from "@/hooks/useComments";
import { cn } from "@/lib/utils";
import { HTML_PREVIEW_SANDBOX, type ActiveSelection } from "./codeViewerHelpers";
import { useDesignBranding, useFullscreen } from "./designViewer";
import { appendCommentBridge, wireframeScreenIdForSourceOffset } from "./htmlCommentBridge";
import { TruncatedBanner } from "./TruncatedBanner";
import { useHtmlCommentBridge } from "./useHtmlCommentBridge";
import {
  WIREFRAME_DEVICES,
  WIREFRAME_MSG_SOURCE,
  listWireframeScreens,
  prepareWireframeDoc,
  type WireframeDevice,
} from "./wireframeDoc";

const DEVICE_ICONS = { desktop: MonitorIcon, tablet: TabletIcon, phone: SmartphoneIcon };

const EMPTY_COMMENTS: Comment[] = [];
const noopSetActiveSelection = (_sel: ActiveSelection | null) => {};

/** Fit the device into the container, never above its real size; 0 before layout. */
export function fitScale(width: number, height: number, device: WireframeDevice): number {
  return Math.min(width / device.width, height / device.height, 1) || 0;
}

export interface WireframeViewerProps {
  content: string;
  truncated?: boolean;
  /** Session whose workspace may hold a design kit or design-system pointer. */
  conversationId?: string;
  /** Switches the file viewer to the existing source view. */
  onRequestSourceMode?: () => void;
  comments?: Comment[];
  activeSelection?: ActiveSelection | null;
  onSetActiveSelection?: (sel: ActiveSelection | null) => void;
}

export function WireframeViewer({
  content,
  truncated = false,
  conversationId,
  onRequestSourceMode,
  comments = EMPTY_COMMENTS,
  activeSelection = null,
  onSetActiveSelection = noopSetActiveSelection,
}: WireframeViewerProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const screens = useMemo(() => listWireframeScreens(content), [content]);
  const branding = useDesignBranding(conversationId, content, false);
  const brandingReady = branding !== null;
  const doc = branding?.content ?? content;
  const kitStyle = branding?.kitStyle ?? "";
  const systemStyle = branding?.systemStyle ?? "";
  const preparedDoc = useMemo(
    () => (brandingReady ? prepareWireframeDoc(doc, kitStyle, systemStyle) : ""),
    [doc, brandingReady, kitStyle, systemStyle],
  );
  const [device, setDevice] = useState<WireframeDevice>(WIREFRAME_DEVICES[0]);
  const [picked, setPicked] = useState<string | null>(null);
  const current = screens.some((s) => s.id === picked) ? picked : (screens[0]?.id ?? null);
  const [scale, setScale] = useState(0);
  const { nonce, iframeRef, addCommentPortal } = useHtmlCommentBridge({
    conversationId: conversationId ?? "",
    content,
    docKey: preparedDoc,
    comments,
    activeSelection,
    onSetActiveSelection,
    scale,
  });
  const srcDoc = useMemo(
    () => (preparedDoc ? appendCommentBridge(preparedDoc, nonce) : ""),
    [preparedDoc, nonce],
  );
  const { isFullscreen, supported: fullscreenSupported, toggle } = useFullscreen(rootRef);

  const post = useCallback(
    (msg: Record<string, unknown>) => {
      iframeRef.current?.contentWindow?.postMessage({ source: WIREFRAME_MSG_SOURCE, ...msg }, "*");
    },
    [iframeRef],
  );

  useEffect(() => {
    if (current) post({ type: "goto", id: current });
  }, [current, post]);

  // Activate a comment on another screen by jumping to the section that holds it.
  useEffect(() => {
    if (!activeSelection) return;
    const id = wireframeScreenIdForSourceOffset(content, activeSelection.start_index);
    if (id && id !== current) setPicked(id);
  }, [activeSelection, content, current]);

  // Link clicks inside the frame report the new screen; trust only our iframe.
  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      const win = iframeRef.current?.contentWindow;
      if (!win || e.source !== win || e.data?.source !== WIREFRAME_MSG_SOURCE) return;
      const { type, id } = e.data;
      if (type === "screen" && screens.some((s) => s.id === id)) setPicked(id);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [screens, iframeRef]);

  useEffect(() => {
    const el = stageRef.current;
    if (!el) return;
    const measure = () => setScale(fitScale(el.clientWidth, el.clientHeight, device));
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [device]);

  return (
    <div
      ref={rootRef}
      role="region"
      aria-roledescription="wireframe"
      aria-label="Wireframe"
      className="flex h-full flex-col bg-background"
    >
      {truncated && <TruncatedBanner />}
      {branding?.notice && (
        <div
          role="status"
          className="shrink-0 truncate border-b border-border bg-muted px-3 py-1 text-ui text-muted-foreground"
          title={branding.notice}
        >
          {branding.notice}
        </div>
      )}
      <div
        ref={stageRef}
        className="relative flex min-h-0 flex-1 items-center justify-center overflow-hidden bg-muted"
      >
        <div style={{ width: device.width * scale, height: device.height * scale }}>
          {/* A new iframe per document loads without adding a history entry. */}
          <iframe
            key={srcDoc}
            ref={iframeRef}
            srcDoc={srcDoc}
            sandbox={HTML_PREVIEW_SANDBOX}
            title="Wireframe"
            onLoad={() => current && post({ type: "goto", id: current })}
            className="origin-top-left border-0 bg-white shadow-sm"
            style={{ width: device.width, height: device.height, transform: `scale(${scale})` }}
          />
        </div>
      </div>
      <div className="flex shrink-0 items-center justify-between gap-2 border-t border-border px-2 py-1 text-ui text-muted-foreground">
        <div className="flex min-w-0 items-center gap-2">
          <div role="group" aria-label="Device" className="flex shrink-0 gap-0.5">
            {WIREFRAME_DEVICES.map((d) => {
              const Icon = DEVICE_ICONS[d.id];
              return (
                <Button
                  key={d.id}
                  type="button"
                  variant="ghost"
                  size="sm"
                  aria-label={d.label}
                  aria-pressed={device.id === d.id}
                  title={`${d.label} ${d.width}x${d.height}`}
                  onClick={() => setDevice(d)}
                  className={cn(
                    "h-8 gap-1.5 px-2",
                    device.id === d.id && "bg-muted text-foreground",
                  )}
                >
                  <Icon className="size-4" />
                  <span className="hidden md:inline">{d.label}</span>
                </Button>
              );
            })}
          </div>
          {screens.length > 1 && current && (
            <select
              aria-label="Screen"
              value={current}
              onChange={(e) => setPicked(e.target.value)}
              className="h-8 min-w-0 max-w-48 truncate rounded-md border border-input bg-transparent px-2 text-ui text-foreground"
            >
              {screens.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.title}
                </option>
              ))}
            </select>
          )}
        </div>
        <div className="flex min-w-0 items-center gap-1">
          {branding?.badge && (
            <span
              className="flex min-w-0 items-center gap-1 px-1"
              title={`${branding.badge.kind === "kit" ? "Design kit" : "Design system"}: ${branding.badge.name}`}
            >
              <PaletteIcon className="size-3.5 shrink-0" aria-hidden />
              <span className="max-w-32 truncate max-sm:sr-only">{branding.badge.name}</span>
            </span>
          )}
          {onRequestSourceMode && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              aria-label="View wireframe source"
              title="View wireframe source"
              onClick={onRequestSourceMode}
              className="h-8 gap-1.5 px-2"
            >
              <CodeIcon className="size-4" />
              Source
            </Button>
          )}
          {fullscreenSupported && (
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label={isFullscreen ? "Exit fullscreen" : "Enter fullscreen"}
              title={isFullscreen ? "Exit fullscreen" : "Enter fullscreen"}
              onClick={toggle}
              className="size-8"
            >
              {isFullscreen ? (
                <Minimize2Icon className="size-4" />
              ) : (
                <Maximize2Icon className="size-4" />
              )}
            </Button>
          )}
        </div>
      </div>
      {addCommentPortal}
    </div>
  );
}
