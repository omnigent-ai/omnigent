import { useCallback, useEffect, useRef, useState } from "react";

const KEYBOARD_STEP_PX = 16;

/** Clamp to [minWidth, maxWidth]; a maxWidth below minWidth resolves to minWidth. */
function clampWidth(width: number, minWidth: number, maxWidth: number): number {
  return Math.max(minWidth, Math.min(maxWidth, width));
}

/** Stored width for `storageKey`, or null when missing, invalid, or storage is unavailable. */
function readStoredWidth(storageKey: string | undefined): number | null {
  if (storageKey === undefined) return null;
  try {
    // Number(null) is 0, so a missing key fails the positive check.
    const value = Number(window.localStorage.getItem(storageKey));
    return Number.isFinite(value) && value > 0 ? value : null;
  } catch {
    return null;
  }
}

function writeStoredWidth(storageKey: string | undefined, width: number): void {
  if (storageKey === undefined) return;
  try {
    window.localStorage.setItem(storageKey, String(width));
  } catch {
    // Quota or privacy-mode failures keep the width in memory only.
  }
}

/**
 * Resizable left-column width, dragged from its right edge or stepped with the arrow keys.
 *
 * The user's preferred width is kept apart from the rendered width (the preference
 * clamped to [minWidth, maxWidth]), so a shrinking container clamps the rendered width
 * without erasing the preference. Pass `storageKey` to persist the preference.
 */
export function useResizableColumn(
  defaultWidth = 176,
  minWidth = 100,
  maxWidth = 480,
  storageKey?: string,
) {
  const [preferredWidth, setPreferredWidth] = useState(
    () => readStoredWidth(storageKey) ?? defaultWidth,
  );
  // Pointer x relative to the container while dragging; null when no move has happened.
  const [dragPointerX, setDragPointerX] = useState<number | null>(null);
  const width = clampWidth(dragPointerX ?? preferredWidth, minWidth, maxWidth);

  const dragging = useRef(false);
  const dragStartWidth = useRef(width);
  const latestPointerX = useRef<number | null>(null);
  const containerRef = useRef<HTMLElement | null>(null);
  const minRef = useRef(minWidth);
  const maxRef = useRef(maxWidth);
  const storageKeyRef = useRef(storageKey);
  minRef.current = minWidth;
  maxRef.current = maxWidth;
  storageKeyRef.current = storageKey;

  const commitWidth = useCallback((next: number) => {
    setPreferredWidth(next);
    writeStoredWidth(storageKeyRef.current, next);
  }, []);

  const onMouseDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      dragging.current = true;
      dragStartWidth.current = width;
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
    },
    [width],
  );

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      const delta = e.key === "ArrowRight" ? KEYBOARD_STEP_PX : -KEYBOARD_STEP_PX;
      const next = clampWidth(width + delta, minWidth, maxWidth);
      // A step at a clamp changes nothing, so the stored preference is left alone.
      if (next !== width) commitWidth(next);
    },
    [width, minWidth, maxWidth, commitWidth],
  );

  useEffect(() => {
    function onMouseMove(e: MouseEvent) {
      if (!dragging.current || !containerRef.current) return;
      const pointerX = e.clientX - containerRef.current.getBoundingClientRect().left;
      latestPointerX.current = pointerX;
      setDragPointerX(pointerX);
    }
    function onMouseUp() {
      if (!dragging.current) return;
      dragging.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      const pointerX = latestPointerX.current;
      latestPointerX.current = null;
      setDragPointerX(null);
      if (pointerX === null) return;
      // Commit only when the drag changed the rendered width; a drag that returns
      // to its starting width keeps the original preference.
      const released = clampWidth(pointerX, minRef.current, maxRef.current);
      if (released !== dragStartWidth.current) commitWidth(released);
    }
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    return () => {
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", onMouseUp);
      if (dragging.current) {
        dragging.current = false;
        document.body.style.cursor = "";
        document.body.style.userSelect = "";
      }
    };
  }, [commitWidth]);

  return {
    /** Rendered pixel width for the left column (apply as inline style). */
    width,
    /** Attach to the flex-row container to anchor drag calculations. */
    containerRef,
    /** Spread onto the resize handle element at the right edge of the left column. */
    handleProps: {
      onMouseDown,
      onKeyDown,
      tabIndex: 0,
      role: "separator" as const,
      "aria-orientation": "vertical" as const,
      "aria-label": "Resize terminal list",
      "aria-valuenow": width,
      "aria-valuemin": minWidth,
      "aria-valuemax": Math.max(minWidth, maxWidth),
    },
  };
}
