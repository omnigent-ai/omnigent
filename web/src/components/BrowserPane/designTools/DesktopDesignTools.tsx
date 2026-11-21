// Design tools for the desktop app's Browser pane: Annotate and Draw. The page is a native
// view that paints over the app, so while a tool is open its layers render into an overlay
// document main stacks just above the page (desktopOverlay.ts), which takes the user's input.
import { MousePointerClickIcon, PencilLineIcon } from "lucide-react";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import { Separator } from "@/components/ui/separator";
import { isImeCompositionKeyEvent } from "@/lib/ime";
import { closeTool, tabTools, toggleTool, useTabTools } from "./core/designToolsStore";
import { desktopDesignTools } from "./desktopBridge";
import { setTabDesignPlatform } from "./core/designPlatform";
import { desktopDesignPlatform } from "./desktopDesignPlatform";
import { DesignOverlay, type OverlayDocument } from "./desktopOverlay";
import { Notice, NOTICE_MS, ToolButton, useShownRef, type LayerRect } from "./layerParts";
import { useOutboxNotices } from "./outboxNotices";
import { AnnotateLayer, DrawLayer } from "./toolLayers";

const PAGE_CHANGED = "The page changed, so the tool closed.";
const OPEN_FAILED = "The tool couldn't open on this page. Try again once it has loaded.";
// A notice's own overlay leaves room round it for its shadow (main places the overlay so
// the notice sits 12px above the page's bottom edge).
const NOTICE_MARGIN = 8;

export interface DesktopDesignToolsProps {
  /** The Browser pane's view. */
  viewId: string;
  /** A page is open in the view. */
  hasView: boolean;
  /**
   * The pane is shown (a collapsed workspace rail keeps it mounted, but inactive, with its
   * view detached). Default true.
   */
  active?: boolean;
  url: string;
  /** The pane's box that the native view covers. */
  container: RefObject<HTMLDivElement | null>;
}

/**
 * Whether `element` has room to show anything (a pane squeezed to nothing hasn't). The
 * ref's element comes with the pane's page (`hasView`), after the tools' first render, so
 * it's looked for again then.
 */
function useHasRoom(element: RefObject<HTMLElement | null>, hasView: boolean): boolean {
  const [room, setRoom] = useState(true);
  useEffect(() => {
    const target = hasView ? element.current : null;
    if (!target) return;
    const check = () => {
      const { width, height } = target.getBoundingClientRect();
      setRoom(width >= 1 && height >= 1);
    };
    check();
    const observer = new ResizeObserver(check);
    observer.observe(target);
    return () => observer.disconnect();
  }, [element, hasView]);
  return room;
}

/** The overlay's viewport (the page's, at its zoom), or null while it has none. */
function useOverlayRect(overlay: OverlayDocument | null): LayerRect | null {
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  useEffect(() => {
    if (!overlay) {
      setSize(null);
      return;
    }
    const { win } = overlay;
    const measure = () => setSize({ width: win.innerWidth, height: win.innerHeight });
    measure();
    win.addEventListener("resize", measure);
    return () => win.removeEventListener("resize", measure);
  }, [overlay]);
  if (!size || size.width < 1 || size.height < 1) return null;
  return { left: 0, top: 0, ...size };
}

const noSubscribe = () => () => {};
const noOverlay = () => null;

/** A notice alone, in an overlay of its own size: it measures itself for main. */
function OverlayNotice({ overlay, text }: { overlay: DesignOverlay; text: string }) {
  const ref = useRef<HTMLDivElement | null>(null);
  // The page's width, read while the overlay is still the page's size to measure in
  // (main shrinks it to the notice after): the notice wraps within it.
  const [room, setRoom] = useState<number | null>(null);
  useLayoutEffect(() => {
    const view = ref.current?.ownerDocument.defaultView;
    if (view && view.innerWidth > 0) setRoom(view.innerWidth - 2 * NOTICE_MARGIN);
  }, [overlay]);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const fit = () => {
      const box = element.getBoundingClientRect();
      overlay.fit({
        width: Math.ceil(box.width) + 2 * NOTICE_MARGIN,
        height: Math.ceil(box.height) + 2 * NOTICE_MARGIN,
      });
    };
    fit();
    const view = element.ownerDocument.defaultView;
    if (!view) return;
    const Observer = (view as typeof globalThis).ResizeObserver ?? ResizeObserver;
    const observer = new Observer(fit);
    observer.observe(element);
    return () => observer.disconnect();
  }, [overlay, text, room]);
  return (
    <div
      ref={ref}
      className="fixed"
      style={{
        left: NOTICE_MARGIN,
        top: NOTICE_MARGIN,
        width: "max-content",
        ...(room !== null ? { maxWidth: room } : {}),
      }}
    >
      <Notice
        rect={null}
        style={{
          position: "static",
          width: "max-content",
          ...(room !== null ? { maxWidth: room } : {}),
        }}
      >
        {text}
      </Notice>
    </div>
  );
}

/** The design tools' toolbar buttons, and the open tool. */
export function DesktopDesignTools({
  viewId,
  hasView,
  active = true,
  url,
  container,
}: DesktopDesignToolsProps) {
  const bridge = useMemo(desktopDesignTools, []);
  const overlay = useMemo(
    () => (bridge ? new DesignOverlay(bridge, viewId) : null),
    [bridge, viewId],
  );
  // This view's page is the tools' platform for its tab (core/designPlatform.ts). Set in
  // a layout effect, so it's there before any tool's effect does something in the page.
  useLayoutEffect(
    () => (overlay ? setTabDesignPlatform(viewId, desktopDesignPlatform(overlay)) : undefined),
    [overlay, viewId],
  );
  const { tool, session } = useTabTools(viewId);
  // Each its own object, so the same message again shows its full time.
  const [shownNotice, setShownNotice] = useState<{ text: string } | null>(null);
  const notice = shownNotice?.text ?? null;
  const setNotice = useCallback(
    (text: string | null) => setShownNotice(text === null ? null : { text }),
    [],
  );
  const noticeRef = useRef(notice);
  useLayoutEffect(() => {
    noticeRef.current = notice;
  }, [notice]);
  // A notice whose overlay couldn't open shows in the toolbar instead (an object, as above).
  const [unplaced, setUnplaced] = useState<{ text: string } | null>(null);
  const [toolbarRef, toolbarShown] = useShownRef();
  const room = useHasRoom(container, hasView);
  // The tools work only while the page is there to see: shown, in a pane with room.
  const usable = hasView && active && room;
  const mode = tool !== null ? "tool" : notice !== null ? "notice" : null;
  const shown = useSyncExternalStore(
    overlay?.subscribe ?? noSubscribe,
    overlay?.current ?? noOverlay,
  );
  const rect = useOverlayRect(shown);

  // The view went away, or the pane was hidden or squeezed to nothing: so does the tool,
  // and its fields with it (nothing hidden is left to focus or type in).
  useEffect(() => {
    if (usable) return;
    closeTool(viewId);
    setNotice(null);
  }, [usable, viewId, setNotice]);
  useEffect(() => () => closeTool(viewId), [viewId]);
  useEffect(() => {
    if (!shownNotice) return;
    const timer = setTimeout(() => setShownNotice(null), NOTICE_MS);
    return () => clearTimeout(timer);
  }, [shownNotice]);
  useEffect(() => {
    if (!unplaced) return;
    const timer = setTimeout(() => setUnplaced(null), NOTICE_MS);
    return () => clearTimeout(timer);
  }, [unplaced]);
  useOutboxNotices(viewId, setNotice);

  useEffect(() => {
    if (!overlay || mode === null) return;
    overlay.start(mode === "tool");
    return () => overlay.stop();
  }, [overlay, mode]);
  // The overlay couldn't open (the page was loading, say): a tool that looks open but
  // can't be used closes, and says so; a notice that can't show says it in the toolbar.
  useEffect(
    () =>
      overlay?.onFailed(() => {
        if (tabTools(viewId).tool !== null) {
          closeTool(viewId);
          setNotice(OPEN_FAILED);
          return;
        }
        if (noticeRef.current !== null) setUnplaced({ text: noticeRef.current });
        setNotice(null);
      }),
    [overlay, viewId, setNotice],
  );
  // Main ended the opening (the page changed, or went), or its overlay went.
  useEffect(() => {
    if (!bridge || !overlay) return;
    const stopEvents = bridge.onBrowserDesignToolsEvent((event) => {
      if (event.viewId !== viewId || event.type !== "ended") return;
      // An older opening's end does nothing to the one open now.
      if (!overlay.ended(event.session)) return;
      // A notice's opening ended: it goes too, so the next notice opens afresh.
      if (tabTools(viewId).tool === null) {
        setNotice(null);
        return;
      }
      closeTool(viewId);
      if (event.reason === "navigated") setNotice(PAGE_CHANGED);
    });
    const stopLost = overlay.onLost(() => {
      if (tabTools(viewId).tool === null) setNotice(null);
      else closeTool(viewId);
    });
    return () => {
      stopEvents();
      stopLost();
    };
  }, [bridge, overlay, viewId, setNotice]);
  // The overlay's document resized: main lays it over the page again, at its zoom.
  const width = rect?.width;
  const height = rect?.height;
  useEffect(() => {
    if (overlay && width !== undefined && height !== undefined && mode === "tool") overlay.fit();
  }, [overlay, width, height, mode]);

  // Esc closes the tool from the overlay, where the keys go while the user works in it, or
  // from its own toolbar button; Escape elsewhere in the app belongs to what has focus.
  const escape = useCallback(
    (event: KeyboardEvent) => {
      // An input method's Escape cancels its candidate, not the tool.
      if (event.key !== "Escape" || event.defaultPrevented) return;
      if (isImeCompositionKeyEvent({ nativeEvent: event })) return;
      closeTool(viewId);
    },
    [viewId],
  );
  useEffect(() => {
    if (tool === null || !toolbarShown || !shown) return;
    const { win } = shown;
    win.addEventListener("keydown", escape);
    return () => win.removeEventListener("keydown", escape);
  }, [tool, toolbarShown, shown, escape]);

  const layer = {
    tabId: viewId,
    url,
    rect,
    onNotice: setNotice,
    popoverContainer: shown?.root ?? null,
  };
  return (
    <div
      ref={toolbarRef}
      role="group"
      aria-label="Design tools"
      className="flex items-center gap-0.5"
    >
      {/* Apart from the browser's own buttons before it, like the tab strip's divider. */}
      <Separator
        orientation="vertical"
        className="mx-1 h-3.5 bg-border-strong data-vertical:self-center"
      />
      <ToolButton
        label="Annotate an element"
        icon={MousePointerClickIcon}
        active={tool === "annotate"}
        disabled={!usable}
        onClick={() => toggleTool(viewId, "annotate")}
        onEscape={() => closeTool(viewId)}
      />
      <ToolButton
        label="Draw on the page"
        icon={PencilLineIcon}
        active={tool === "draw"}
        disabled={!usable}
        onClick={() => toggleTool(viewId, "draw")}
        onEscape={() => closeTool(viewId)}
      />
      {unplaced && (
        <span
          role="status"
          className="max-w-48 truncate text-muted-foreground text-ui"
          title={unplaced.text}
        >
          {unplaced.text}
        </span>
      )}
      {/* Keyed by the tool session, so each opening starts with fresh local state. */}
      {shown &&
        overlay &&
        createPortal(
          mode === "tool" ? (
            <>
              {tool === "annotate" && <AnnotateLayer key={session} {...layer} />}
              {tool === "draw" && <DrawLayer key={session} {...layer} />}
              {notice && <Notice rect={rect}>{notice}</Notice>}
            </>
          ) : (
            notice && <OverlayNotice overlay={overlay} text={notice} />
          ),
          shown.root,
        )}
    </div>
  );
}
