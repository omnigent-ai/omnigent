// Unit tests for the Web Push subscribe flow (#8): feature detection, the
// register → fetch-VAPID → subscribe → POST happy path, and the no-op when the
// server hasn't configured push (vapid-public-key 404). The service worker /
// PushManager globals are stubbed; modules are reset per test so the module's
// cached registration doesn't leak across cases.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// A real-shaped (decodable) base64url VAPID public key — RFC 8291's example
// sender key — so urlBase64ToUint8Array doesn't choke in the happy path.
const VAPID_PUB =
  "BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8";

const fetchMock = vi.fn();

function res(body: unknown, ok = true, status = 200): Response {
  return { ok, status, json: async () => body } as unknown as Response;
}

function installPushEnv() {
  const fakeSub = {
    endpoint: "https://push.example/abc",
    toJSON: () => ({
      endpoint: "https://push.example/abc",
      keys: { p256dh: "PUB", auth: "AUTH" },
    }),
    unsubscribe: vi.fn().mockResolvedValue(undefined),
  };
  const getSubscription = vi.fn().mockResolvedValue(null);
  const subscribe = vi.fn().mockResolvedValue(fakeSub);
  const register = vi.fn().mockResolvedValue({ pushManager: { getSubscription, subscribe } });
  vi.stubGlobal("navigator", { serviceWorker: { register } });
  vi.stubGlobal("PushManager", function PushManager() {});
  return { register, subscribe, getSubscription };
}

beforeEach(() => {
  vi.resetModules();
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("webPush", () => {
  it("reports unsupported without a service worker", async () => {
    vi.stubGlobal("navigator", {});
    vi.stubGlobal("PushManager", function PushManager() {});
    const { isWebPushSupported } = await import("./webPush");
    expect(isWebPushSupported()).toBe(false);
  });

  it("subscribes and registers the subscription server-side", async () => {
    const { subscribe } = installPushEnv();
    fetchMock
      .mockResolvedValueOnce(res({ key: VAPID_PUB })) // GET /v1/push/vapid-public-key
      .mockResolvedValueOnce(res({ id: "push_1" })); // POST /v1/push/subscriptions

    const { enablePushNotifications } = await import("./webPush");
    const ok = await enablePushNotifications();

    expect(ok).toBe(true);
    expect(subscribe).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(url).toBe("/v1/push/subscriptions");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      endpoint: "https://push.example/abc",
      keys: { p256dh: "PUB", auth: "AUTH" },
    });
  });

  it("no-ops when the server has no VAPID key (404)", async () => {
    const { subscribe } = installPushEnv();
    fetchMock.mockResolvedValueOnce(res({}, false, 404));

    const { enablePushNotifications } = await import("./webPush");
    expect(await enablePushNotifications()).toBe(false);
    expect(subscribe).not.toHaveBeenCalled();
  });
});
