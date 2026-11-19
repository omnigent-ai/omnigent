import { lazy, useSyncExternalStore, type ComponentType } from "react";

/** After a chunk first fails to load, how long before trying it again. */
export const CHUNK_RETRY_MS = 5_000;
/** The longest wait between tries: each failed one doubles it, and a load resets it. */
export const CHUNK_RETRY_MAX_MS = 5 * 60_000;

export interface RetryingChunk<P> {
  /** Whether the chunk's last load failed, so another try is due. */
  missing: () => boolean;
  /** Calls `listener` when the chunk fails to load, and when a later try loads it. */
  subscribe: (listener: () => void) => () => void;
  /** The chunk's component, kept up to date; it renders nothing while the chunk can't load. */
  useComponent: () => ComponentType<P>;
}

/**
 * A component loaded in its own chunk, off the app's first paint. A chunk that can't load
 * (a deploy replaced it, the network dropped) is tried again later, less often each time,
 * and only while something shows it.
 */
export function retryingChunk<P extends object>(
  load: () => Promise<ComponentType<P>>,
): RetryingChunk<P> {
  let retryMs = CHUNK_RETRY_MS;
  let missing = false;
  // A try came due while nothing showed the chunk: the next subscriber makes it.
  let due = false;
  const listeners = new Set<() => void>();
  const Nothing: ComponentType<P> = () => null;
  const changed = () => {
    for (const listener of [...listeners]) listener();
  };

  function tryAgain(): void {
    if (listeners.size === 0) {
      due = true;
      return;
    }
    load().then((loaded) => {
      Component = loaded;
      missing = false;
      retryMs = CHUNK_RETRY_MS;
      changed();
    }, failedAgain);
  }

  // Said once: each retry that fails again would otherwise flood the log.
  let warned = false;
  function failedAgain(error: unknown): void {
    if (!warned) console.warn("A part of the app couldn't load; trying again later.", error);
    warned = true;
    retryLater();
  }

  function retryLater(): void {
    const waitMs = retryMs;
    retryMs = Math.min(retryMs * 2, CHUNK_RETRY_MAX_MS);
    setTimeout(tryAgain, waitMs);
  }

  let Component: ComponentType<P> = lazy(() =>
    load().then(
      (loaded) => ({ default: loaded }),
      (error: unknown) => {
        missing = true;
        changed();
        failedAgain(error);
        return { default: Nothing };
      },
    ),
  );

  function subscribe(listener: () => void): () => void {
    listeners.add(listener);
    if (due) {
      due = false;
      tryAgain();
    }
    return () => {
      listeners.delete(listener);
    };
  }

  return {
    missing: () => missing,
    subscribe,
    useComponent: () => useSyncExternalStore(subscribe, () => Component),
  };
}
