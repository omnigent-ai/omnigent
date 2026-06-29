// Omnigent service worker — PWA install + Web Push (#8).
//
// Plain JS (no build step): served from /sw.js so its scope is the whole app.
// The push handler renders the notification the server encrypted for this
// client; notificationclick focuses an existing tab (or opens one) and routes
// to the conversation the notification points at.

self.addEventListener("install", () => {
  // Activate immediately so a freshly-deployed worker takes over without a
  // manual reload — fine here since the SW only does push + click routing.
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { body: event.data ? event.data.text() : "" };
  }
  const title = data.title || "Omnigent";
  const options = {
    body: data.body || "",
    tag: data.tag,
    data: { navigatePath: data.navigatePath || "/" },
    icon: "/favicon.svg",
    badge: "/favicon.svg",
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const path = (event.notification.data && event.notification.data.navigatePath) || "/";
  event.waitUntil(
    (async () => {
      const clientList = await self.clients.matchAll({
        type: "window",
        includeUncontrolled: true,
      });
      for (const client of clientList) {
        if ("focus" in client) {
          await client.focus();
          if ("navigate" in client) {
            try {
              await client.navigate(path);
            } catch {
              // Cross-origin / detached client — focusing is enough.
            }
          }
          return;
        }
      }
      if (self.clients.openWindow) {
        await self.clients.openWindow(path);
      }
    })(),
  );
});
