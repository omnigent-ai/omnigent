import { useCallback, useLayoutEffect, useRef } from "react";
import type { RefObject, UIEvent } from "react";

/**
 * Module-level cache so scroll positions survive unmount/remount and
 * conversation switches within a JS session. Shared by the Files panel
 * (keyed per conversation + view) and the file viewer surfaces (keyed per
 * conversation + path).
 */
const scrollTopCache = new Map<string, number>();

/** Read a saved scroll offset (e.g. to seed a Monaco editor on mount). */
export function getSavedScrollTop(key: string): number | undefined {
  return scrollTopCache.get(key);
}

/** Record a scroll offset (e.g. from Monaco's onDidScrollChange). */
export function saveScrollTop(key: string, scrollTop: number): void {
  scrollTopCache.set(key, scrollTop);
}

/**
 * How long a restore keeps re-asserting the saved offset while the content is
 * still too short to hold it. Async work (syntax highlighting, image decode,
 * lazy cells) grows the container in bursts with stalls in between, so the
 * window has to outlast a stall. Shared with the Monaco surfaces.
 */
export const SCROLL_RESTORE_BUDGET_MS = 1500;

/** Pointer/touch/wheel input that means the user has taken over scrolling. */
const USER_SCROLL_EVENTS = ["wheel", "touchstart", "pointerdown"] as const;

/** The slice of a Monaco code editor the scroll restore needs. */
interface ScrollableEditor {
  setScrollTop: (top: number) => void;
  onDidScrollChange: (listener: (e: { scrollTop: number }) => void) => { dispose: () => void };
  getDomNode?: () => HTMLElement | null;
}

/**
 * Restore and persist a Monaco editor's own scroll offset (Monaco scrolls
 * internally, so the DOM hook can't drive it).
 *
 * Same shape as `useScrollRestore`: the editor isn't laid out at mount, so the
 * offset clamps to 0 and Monaco reports that clamp as a scroll event. Saving
 * stays off — and the target is re-asserted — until the offset is actually
 * reached, the user scrolls, or the restore budget runs out. Without that, the
 * clamp would overwrite the reader's saved position with 0.
 *
 * @param editor The editor to restore (the modified side, for a diff).
 * @param getKey Reads the current cache key (files can switch under one editor).
 * @param isCurrent False once the editor has been replaced or torn down.
 */
export function attachEditorScrollRestore(
  editor: ScrollableEditor,
  getKey: () => string,
  isCurrent: () => boolean,
): void {
  const saved = getSavedScrollTop(getKey());
  let pending =
    saved !== undefined && saved > 0
      ? { target: saved, deadline: performance.now() + SCROLL_RESTORE_BUDGET_MS }
      : null;
  const dom = pending ? (editor.getDomNode?.() ?? null) : null;
  const settle = () => {
    pending = null;
    for (const type of USER_SCROLL_EVENTS) dom?.removeEventListener(type, settle);
  };
  if (pending) {
    editor.setScrollTop(pending.target);
    requestAnimationFrame(() => {
      if (pending && isCurrent()) editor.setScrollTop(pending.target);
    });
    for (const type of USER_SCROLL_EVENTS) dom?.addEventListener(type, settle, { passive: true });
  }
  editor.onDidScrollChange((e) => {
    if (pending) {
      if (Math.abs(e.scrollTop - pending.target) > 1 && performance.now() < pending.deadline) {
        editor.setScrollTop(pending.target);
        return;
      }
      settle();
    }
    saveScrollTop(getKey(), e.scrollTop);
  });
}

/**
 * Persist and restore a scroll container's position across key changes
 * (conversation/file switches) and unmount/remount.
 *
 * Restoring is not a one-shot write: while new content loads the container
 * is a short placeholder and the browser clamps scrollTop to 0, and the
 * content then grows in steps, each of which can clamp again. So the
 * restore waits for `ready`, then re-asserts the saved offset on every
 * render and via an animation-frame loop until the container is tall
 * enough to hold it, giving up once `SCROLL_RESTORE_BUDGET_MS` has passed.
 * Any wheel/touch/pointer input settles it immediately so the user is never
 * fought for the offset. Saving stays off until the restore settles so
 * clamp-induced scroll events can't overwrite the cached value.
 *
 * @param ref The scrollable element.
 * @param key Cache key for the current content (null disables persistence).
 * @param ready True once the content backing the container is present.
 * @returns An onScroll handler to attach to the container.
 */
export function useScrollRestore(
  ref: RefObject<HTMLElement | null>,
  key: string | null,
  ready: boolean,
): (event: UIEvent<HTMLElement>) => void {
  const pendingRef = useRef<{ target: number; deadline: number } | null>(null);
  const keyRef = useRef<string | null>(null);
  if (key !== keyRef.current) {
    keyRef.current = key;
    // The deadline belongs to the pending entry, not to an effect run, so
    // re-renders during the restore can't keep extending the window.
    pendingRef.current = key
      ? {
          target: scrollTopCache.get(key) ?? 0,
          deadline: performance.now() + SCROLL_RESTORE_BUDGET_MS,
        }
      : null;
  }

  // No dependency array: intentionally runs after every render — each
  // content-growth step is another chance to reach the saved offset. Cleanup
  // cancels the previous run's frame, so only one loop is ever live.
  useLayoutEffect(() => {
    const el = ref.current;
    const pending = pendingRef.current;
    if (!el || !pending || !ready) return;
    let frame = 0;
    function settleForUser() {
      pendingRef.current = null;
      cancelAnimationFrame(frame);
      detach();
    }
    const detach = () => {
      for (const type of USER_SCROLL_EVENTS) el.removeEventListener(type, settleForUser);
    };
    const attempt = () => {
      // A user gesture (or a later render's loop) may have already settled it.
      if (pendingRef.current !== pending) {
        detach();
        return;
      }
      el.scrollTop = pending.target;
      const maxScroll = el.scrollHeight - el.clientHeight;
      if (maxScroll >= pending.target || performance.now() >= pending.deadline) {
        pendingRef.current = null;
        detach();
        return;
      }
      frame = requestAnimationFrame(attempt);
    };
    for (const type of USER_SCROLL_EVENTS) {
      el.addEventListener(type, settleForUser, { passive: true });
    }
    attempt();
    return () => {
      cancelAnimationFrame(frame);
      detach();
    };
  });

  return useCallback((event: UIEvent<HTMLElement>) => {
    if (keyRef.current && pendingRef.current === null) {
      scrollTopCache.set(keyRef.current, event.currentTarget.scrollTop);
    }
  }, []);
}
