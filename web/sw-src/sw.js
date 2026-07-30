// Omnigent installability / update-only service worker (hand-rolled).
//
// Omnigent is a cloud app with NO offline mode, so this worker deliberately:
//   - does NOT precache or serve the app shell, and
//   - does NOT intercept navigations — every navigation hits the network, so a
//     deploy is never masked behind a stale cached shell.
// It exists only to (a) make the app installable and (b) drive the in-app
// "new version → Reload" prompt (see src/components/pwa/useServiceWorkerUpdate).
//
// BUILD_VERSION is replaced at build time (vite.config.ts → emitPwaAssets) with
// a fingerprint of the hashed JS/CSS outputs, so this file's bytes change on
// every code/style deploy. That byte change is what the browser's update
// algorithm (via workbox-window in the page) detects to fire the prompt.
const BUILD_VERSION = "__BUILD_VERSION__";
const CACHE_NAME = `omnigent-pwa-${BUILD_VERSION}`;

self.addEventListener("install", (event) => {
  // Precache ONLY version.json. Two reasons: it gives the worker a real
  // (non-empty) fetch handler — Chrome's automatic install prompt ignores
  // no-op handlers — and the per-build cache name means each deploy starts a
  // fresh cache. We do NOT call skipWaiting(): a new build waits in the
  // background until the user accepts the prompt.
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.add("/version.json")));
});

self.addEventListener("activate", (event) => {
  // Drop caches from prior builds. No clients.claim(): in prompt mode the new
  // worker must not take control of open pages until the user accepts.
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))),
      ),
  );
});

self.addEventListener("message", (event) => {
  // workbox-window's messageSkipWaiting() posts this when the user clicks Reload.
  if (event.data && event.data.type === "SKIP_WAITING") self.skipWaiting();
});

self.addEventListener("fetch", (event) => {
  // Respond ONLY for the version sentinel (cache-first, network fallback).
  // Everything else — navigations, hashed assets, everything — falls through
  // with no respondWith(), i.e. straight to the network. This keeps a real
  // fetch handler without ever serving a stale app shell.
  const url = new URL(event.request.url);
  if (url.pathname === "/version.json") {
    event.respondWith(caches.match(event.request).then((cached) => cached || fetch(event.request)));
  }
});
