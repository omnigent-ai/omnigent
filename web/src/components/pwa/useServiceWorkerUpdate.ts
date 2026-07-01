import { useEffect, useState } from "react";
import { Workbox } from "workbox-window";

/**
 * Service-worker registration + update detection for the standalone PWA.
 *
 * Thin wrapper around `workbox-window` (kept instead of `vite-plugin-pwa` so we
 * ship ~3 small packages, not the ~250-package `workbox-build` toolchain — see
 * the service worker at `sw-src/sw.js`). `workbox-window` is the part with real
 * value: it handles the lifecycle edge cases a hand-rolled registration gets
 * wrong — notably a worker that was ALREADY `waiting` when the page loaded.
 *
 * The wiring lives at module scope, not inside the React effect, on purpose:
 *   - registration happens exactly once even though `<PWAUpdateBanner>` mounts
 *     under `<StrictMode>` (which double-invokes effects), and
 *   - the `waiting`/`controlling` listeners are attached once and never churn,
 *     so an event can't slip through between an effect's cleanup and re-run.
 * The hook is a pure subscriber to that module-level state.
 */

type Subscriber = (needRefresh: boolean) => void;

let workbox: Workbox | undefined;
let started = false;
let reloading = false;
let needRefreshState = false;
const subscribers = new Set<Subscriber>();

function publish(needRefresh: boolean): void {
  needRefreshState = needRefresh;
  for (const subscriber of subscribers) subscriber(needRefresh);
}

function start(): void {
  if (started) return;
  started = true;
  // No service worker in dev: `sw.js` is only emitted by the production build, so
  // in dev the path resolves to the index.html SPA fallback (wrong MIME) and
  // registration would fail — and a dev SW fights HMR anyway. Standard PWA
  // practice; vite-plugin-pwa keeps its dev service worker off for the same reason.
  if (!import.meta.env.PROD) return;
  if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return;

  workbox = new Workbox("/sw.js");

  // `waiting` fires when a new worker is installed and waiting to activate —
  // INCLUDING the case where it was already waiting at page load (workbox-window
  // inspects `registration.waiting` on register). It does NOT fire on first
  // install (nothing to wait behind), so we only prompt on a genuine update.
  workbox.addEventListener("waiting", () => publish(true));

  // `controlling` fires when a new worker takes control of the page — either
  // because THIS tab accepted the update (messageSkipWaiting) or because another
  // tab did (skipWaiting is process-wide, so the new worker controls every open
  // tab at once). Either way this tab is now running old code under a new worker,
  // so reload into the fresh build; the `reloading` flag guards a double fire.
  // This never fires on first install: with no clients.claim() and no
  // skipWaiting() until the user accepts, a brand-new worker does not take
  // control of already-open pages.
  workbox.addEventListener("controlling", () => {
    if (reloading) return;
    reloading = true;
    window.location.reload();
  });

  void workbox.register();
}

/** Apply the waiting update: ask the worker to skip waiting, then reload. */
function applyUpdate(): void {
  // Tells the waiting worker to `skipWaiting()`; activation triggers
  // `controlling` above, which reloads into the new build.
  if (workbox) void workbox.messageSkipWaiting();
}

export interface ServiceWorkerUpdate {
  /** A new build is installed and waiting; show the reload prompt. */
  needRefresh: boolean;
  /** Accept the update: activate the new worker and reload into it. */
  reload: () => void;
  /** Dismiss the prompt for this session (the worker keeps waiting). */
  dismiss: () => void;
}

export function useServiceWorkerUpdate(): ServiceWorkerUpdate {
  const [needRefresh, setNeedRefresh] = useState(needRefreshState);

  useEffect(() => {
    start();
    const subscriber: Subscriber = (value) => setNeedRefresh(value);
    subscribers.add(subscriber);
    // Sync to any state published before this component subscribed.
    setNeedRefresh(needRefreshState);
    return () => {
      subscribers.delete(subscriber);
    };
  }, []);

  return {
    needRefresh,
    reload: applyUpdate,
    dismiss: () => publish(false),
  };
}
