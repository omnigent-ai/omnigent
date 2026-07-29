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
 * Persist and restore a scroll container's position across key changes
 * (conversation/file switches) and unmount/remount.
 *
 * Restoring is not a one-shot write: while new content loads the container
 * is a short placeholder and the browser clamps scrollTop to 0, and the
 * content then grows in steps, each of which can clamp again. So the
 * restore waits for `ready`, then re-asserts the saved offset on every
 * render and via an animation-frame loop until the container is tall
 * enough to hold it — giving up (accepting the clamp) once the content's
 * height stops changing. Saving stays off until the restore settles so
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
  const pendingRef = useRef<{ target: number; lastHeight: number } | null>(null);
  const keyRef = useRef<string | null>(null);
  if (key !== keyRef.current) {
    keyRef.current = key;
    pendingRef.current = key ? { target: scrollTopCache.get(key) ?? 0, lastHeight: -1 } : null;
  }

  // No dependency array: intentionally runs after every render — each
  // content-growth step is another chance to reach the saved offset.
  useLayoutEffect(() => {
    const el = ref.current;
    const pending = pendingRef.current;
    if (!el || !pending || !ready) return;
    let frame = 0;
    const attempt = () => {
      el.scrollTop = pending.target;
      const maxScroll = el.scrollHeight - el.clientHeight;
      if (maxScroll >= pending.target || el.scrollHeight === pending.lastHeight) {
        pendingRef.current = null;
        return;
      }
      pending.lastHeight = el.scrollHeight;
      frame = requestAnimationFrame(attempt);
    };
    attempt();
    return () => cancelAnimationFrame(frame);
  });

  return useCallback((event: UIEvent<HTMLElement>) => {
    if (keyRef.current && pendingRef.current === null) {
      scrollTopCache.set(keyRef.current, event.currentTarget.scrollTop);
    }
  }, []);
}
