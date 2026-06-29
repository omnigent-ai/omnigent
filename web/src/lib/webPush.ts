// Web Push (#8): register the service worker, subscribe via the PushManager
// using the server's VAPID key, and persist the subscription server-side so
// the backend can deliver notifications when the app is backgrounded/closed.
//
// All real push delivery requires a secure context (HTTPS or localhost) and a
// browser push service, so these functions feature-detect and no-op safely
// where unsupported (e.g. the Electron shell, which uses native notifications).

import { authenticatedFetch } from "./identity";

/** True when the browser can register a service worker AND do Web Push. */
export function isWebPushSupported(): boolean {
  return (
    typeof navigator !== "undefined" &&
    "serviceWorker" in navigator &&
    typeof window !== "undefined" &&
    "PushManager" in window
  );
}

let cachedRegistration: ServiceWorkerRegistration | null = null;

/**
 * Register the app's service worker (idempotent). Returns the registration,
 * or null when service workers aren't available.
 */
export async function registerServiceWorker(): Promise<ServiceWorkerRegistration | null> {
  if (!isWebPushSupported()) return null;
  if (cachedRegistration) return cachedRegistration;
  cachedRegistration = await navigator.serviceWorker.register("/sw.js");
  return cachedRegistration;
}

/** Decode a base64url VAPID key to the Uint8Array `applicationServerKey` wants. */
function urlBase64ToUint8Array(base64Url: string): Uint8Array<ArrayBuffer> {
  const padding = "=".repeat((4 - (base64Url.length % 4)) % 4);
  const base64 = (base64Url + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  // Back the view with a concrete ArrayBuffer (not ArrayBufferLike) so it
  // satisfies the BufferSource type `applicationServerKey` expects.
  const out = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i += 1) out[i] = raw.charCodeAt(i);
  return out;
}

/**
 * Subscribe this browser to Web Push and register the subscription with the
 * server. Idempotent (reuses an existing PushManager subscription).
 *
 * @returns true when a subscription is active and registered; false when push
 *   is unsupported here or not configured on the server (no VAPID key).
 */
export async function enablePushNotifications(): Promise<boolean> {
  const registration = await registerServiceWorker();
  if (!registration) return false;

  // The server advertises its VAPID public key; a 404 means push isn't
  // configured server-side, so there's nothing to subscribe to.
  const keyRes = await authenticatedFetch("/v1/push/vapid-public-key");
  if (!keyRes.ok) return false;
  const { key } = (await keyRes.json()) as { key: string };

  let subscription = await registration.pushManager.getSubscription();
  if (!subscription) {
    subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(key),
    });
  }

  const json = subscription.toJSON() as { endpoint?: string; keys?: Record<string, string> };
  if (!json.endpoint || !json.keys?.p256dh || !json.keys?.auth) return false;

  const res = await authenticatedFetch("/v1/push/subscriptions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ endpoint: json.endpoint, keys: json.keys }),
  });
  return res.ok;
}

/** Unsubscribe this browser and drop the server-side registration. */
export async function disablePushNotifications(): Promise<void> {
  const registration = await registerServiceWorker();
  const subscription = registration ? await registration.pushManager.getSubscription() : null;
  if (!subscription) return;
  await authenticatedFetch("/v1/push/subscriptions", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ endpoint: subscription.endpoint }),
  });
  await subscription.unsubscribe();
}
