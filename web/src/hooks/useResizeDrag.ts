import { useCallback, useEffect, useMemo, useRef } from "react";

interface ResizeDragOptions<T extends Element> {
  enabled?: boolean;
  /** Fires when a drag claims the pointer — snapshot the pre-drag width here. */
  onStart?: (event: React.PointerEvent<T>) => void;
  onMove: (event: React.PointerEvent<T>) => void;
  onCommit?: () => void;
  /**
   * Fires instead of `onCommit` when the drag aborts (Escape, blur, …).
   * Every `onMove` has already applied its width live, so cancellation must
   * actively restore the `onStart` snapshot or the abort silently keeps the
   * dragged width on screen while storage still holds the old one.
   */
  onCancel?: () => void;
  overlay?: boolean;
  observeHandleRemoval?: boolean;
}

const OVERLAY_STYLE =
  "position:fixed;inset:0;z-index:2147483647;cursor:col-resize;background:transparent;";

const bodyStyleOwners = new Set<symbol>();
let previousBodyCursor = "";
let previousBodyUserSelect = "";

function acquireBodyStyles(owner: symbol): void {
  if (bodyStyleOwners.size === 0) {
    previousBodyCursor = document.body.style.cursor;
    previousBodyUserSelect = document.body.style.userSelect;
  }
  bodyStyleOwners.add(owner);
  document.body.style.cursor = "col-resize";
  document.body.style.userSelect = "none";
}

function releaseBodyStyles(owner: symbol): void {
  bodyStyleOwners.delete(owner);
  if (bodyStyleOwners.size !== 0) return;
  document.body.style.cursor = previousBodyCursor;
  document.body.style.userSelect = previousBodyUserSelect;
}

/** Shared pointer lifecycle for resize handles; callers provide only sizing policy. */
export function useResizeDrag<T extends Element = Element>({
  enabled = true,
  onStart,
  onMove,
  onCommit,
  onCancel,
  overlay = false,
  observeHandleRemoval = false,
}: ResizeDragOptions<T>) {
  const activePointerId = useRef<number | null>(null);
  const activeHandle = useRef<T | null>(null);
  const cleanup = useRef<(() => void) | null>(null);
  const overlayElement = useRef<HTMLDivElement | null>(null);
  const bodyStyleOwner = useRef(Symbol("resize-drag"));
  const onStartRef = useRef(onStart);
  const onMoveRef = useRef(onMove);
  const onCommitRef = useRef(onCommit);
  const onCancelRef = useRef(onCancel);
  onStartRef.current = onStart;
  onMoveRef.current = onMove;
  onCommitRef.current = onCommit;
  onCancelRef.current = onCancel;

  const finishDrag = useCallback((commit: boolean) => {
    const pointerId = activePointerId.current;
    if (pointerId === null) return;

    const handle = activeHandle.current;
    activePointerId.current = null;
    activeHandle.current = null;
    cleanup.current?.();
    cleanup.current = null;
    overlayElement.current?.remove();
    overlayElement.current = null;

    try {
      handle?.releasePointerCapture(pointerId);
    } catch {
      // The handle may have detached or already lost capture.
    }

    releaseBodyStyles(bodyStyleOwner.current);
    if (commit) onCommitRef.current?.();
    else onCancelRef.current?.();
  }, []);

  const cancelDrag = useCallback(() => finishDrag(false), [finishDrag]);

  const onPointerDown = useCallback(
    (event: React.PointerEvent<T>) => {
      if (!enabled || event.button !== 0 || activePointerId.current !== null) return;

      const capture = event.currentTarget.setPointerCapture;
      if (!capture) return;
      try {
        capture.call(event.currentTarget, event.pointerId);
      } catch {
        return;
      }

      event.preventDefault();
      activePointerId.current = event.pointerId;
      activeHandle.current = event.currentTarget;
      onStartRef.current?.(event);

      const onDocumentPointerUp = (documentEvent: PointerEvent) => {
        if (documentEvent.pointerId === activePointerId.current) {
          finishDrag(true);
        }
      };
      const onDocumentPointerCancel = (documentEvent: PointerEvent) => {
        if (documentEvent.pointerId === activePointerId.current) {
          cancelDrag();
        }
      };
      const onDocumentKeyDown = (documentEvent: KeyboardEvent) => {
        if (documentEvent.key === "Escape") cancelDrag();
      };
      const onContextMenu = cancelDrag;
      const onWindowBlur = cancelDrag;
      const onVisibilityChange = cancelDrag;
      const observer = observeHandleRemoval
        ? new MutationObserver(() => {
            if (activeHandle.current && !activeHandle.current.isConnected) cancelDrag();
          })
        : null;
      observer?.observe(document.documentElement, { childList: true, subtree: true });
      document.addEventListener("pointerup", onDocumentPointerUp);
      document.addEventListener("pointercancel", onDocumentPointerCancel);
      document.addEventListener("keydown", onDocumentKeyDown);
      document.addEventListener("contextmenu", onContextMenu);
      window.addEventListener("blur", onWindowBlur);
      document.addEventListener("visibilitychange", onVisibilityChange);
      cleanup.current = () => {
        observer?.disconnect();
        document.removeEventListener("pointerup", onDocumentPointerUp);
        document.removeEventListener("pointercancel", onDocumentPointerCancel);
        document.removeEventListener("keydown", onDocumentKeyDown);
        document.removeEventListener("contextmenu", onContextMenu);
        window.removeEventListener("blur", onWindowBlur);
        document.removeEventListener("visibilitychange", onVisibilityChange);
      };

      if (overlay) {
        const element = document.createElement("div");
        element.style.cssText = OVERLAY_STYLE;
        document.body.appendChild(element);
        overlayElement.current = element;
      }
      acquireBodyStyles(bodyStyleOwner.current);
    },
    [cancelDrag, enabled, finishDrag, observeHandleRemoval, overlay],
  );

  const onPointerMove = useCallback((event: React.PointerEvent<T>) => {
    if (event.pointerId === activePointerId.current) onMoveRef.current(event);
  }, []);

  const onPointerUp = useCallback(
    (event: React.PointerEvent<T>) => {
      if (event.pointerId === activePointerId.current) finishDrag(true);
    },
    [finishDrag],
  );

  const onPointerCancel = useCallback(
    (event: React.PointerEvent<T>) => {
      if (event.pointerId === activePointerId.current) cancelDrag();
    },
    [cancelDrag],
  );

  useEffect(() => {
    if (!enabled) cancelDrag();
  }, [cancelDrag, enabled]);
  useEffect(() => cancelDrag, [cancelDrag]);

  const handleProps = useMemo(
    () => ({
      onPointerDown,
      onPointerMove,
      onPointerUp,
      onPointerCancel,
      onLostPointerCapture: onPointerCancel,
    }),
    [onPointerDown, onPointerMove, onPointerUp, onPointerCancel],
  );

  return { cancelDrag, handleProps };
}
