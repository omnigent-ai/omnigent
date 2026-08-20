// Tombstone service worker: unregisters the retired Omnigent PWA worker.
//
// @deprecated Delete this file (and its emit in vite.config.ts) in 0.11.0.
//
// The retired worker was installability/update-only — it never cached the app
// shell — and its sole consumer, the in-app "new version → Reload" prompt, is
// gone. A worker already registered in a browser stays registered even after we
// stop shipping one, so this final version exists only to remove itself.
//
// Deliberately no skipWaiting() on install: this parks in `waiting`, so a tab
// still running the old bundle shows that bundle's update prompt one last time.
// Accepting it posts SKIP_WAITING below, which activates this worker (and
// reloads the tab into the PWA-free build). Otherwise it activates once the last
// old client goes away. Either path is unprompted-reload-free.
//
// The purge matches the retired worker's exact cache-name shape rather than
// clearing Cache Storage wholesale or trusting a bare prefix: it only ever
// created `omnigent-pwa-<8 lowercase hex>` (a uint32 build fingerprint). Pinning
// the full shape means a tombstone lingering in some browser cannot delete a
// future feature's caches even if that feature reuses this prefix.
const RETIRED_CACHE_NAME = /^omnigent-pwa-[0-9a-f]{8}$/;

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys.filter((key) => RETIRED_CACHE_NAME.test(key)).map((key) => caches.delete(key)),
        ),
      )
      // Removes the registration itself. This leaves no persistent browser
      // state, so registering a service worker at /sw.js again later is clean.
      .then(() => self.registration.unregister()),
  );
});

self.addEventListener("message", (event) => {
  // The old bundle's Reload button posts this (workbox messageSkipWaiting).
  if (event.data && event.data.type === "SKIP_WAITING") self.skipWaiting();
});
