import { StrictMode } from "react";
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import { sessionUpdatesSocket } from "@/lib/sessionUpdatesSocket";
import { useWebUpdateNotifications } from "./useWebUpdateNotifications";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/lib/routing", () => ({
  useLocation: () => ({ pathname: "/settings", search: "?tab=general", hash: "" }),
}));
vi.mock("@/lib/sessionUpdatesSocket", () => ({
  sessionUpdatesSocket: { subscribeStatus: vi.fn(), isConnected: vi.fn() },
}));

const fetchMock = vi.mocked(authenticatedFetch);
const nativeNotify = vi.fn().mockResolvedValue(true);
const unsubscribe = vi.fn();
let socketStatus: () => void;

function respond(id: unknown) {
  fetchMock.mockImplementation(async () => new Response(JSON.stringify({ webapp_build_id: id })));
}

async function poll(id: unknown) {
  respond(id);
  await act(async () => window.dispatchEvent(new Event("online")));
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.spyOn(document, "hasFocus").mockReturnValue(true);
  vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
  Object.defineProperty(window, "omnigentNative", {
    configurable: true,
    value: { kind: "android", notify: nativeNotify },
  });
  vi.mocked(sessionUpdatesSocket.subscribeStatus).mockImplementation((cb) => {
    socketStatus = cb;
    return unsubscribe;
  });
  respond("loaded");
});

afterEach(() => {
  cleanup();
  Reflect.deleteProperty(window, "omnigentNative");
  vi.restoreAllMocks();
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe("useWebUpdateNotifications", () => {
  it("establishes the first non-null baseline and requires two differing reads", async () => {
    respond(null);
    const { result } = renderHook(useWebUpdateNotifications);
    await act(async () => {});
    await poll(undefined);
    await poll(42);
    await poll("");
    await poll("loaded");
    await poll("loaded");
    await poll("new");
    expect(result.current.availableBuildId).toBeNull();
    await poll("new");
    expect(result.current.availableBuildId).toBe("new");
    expect(fetchMock).toHaveBeenCalledWith("/api/version", {
      cache: "no-store",
      signal: expect.any(AbortSignal),
    });
    expect(nativeNotify).not.toHaveBeenCalled();
  });

  it.each(["loaded", "other", null, "network-error", "http-error", "invalid-json"])(
    "resets confirmation when %s interrupts consecutive reads",
    async (interruption) => {
      const { result } = renderHook(useWebUpdateNotifications);
      await act(async () => {});
      await poll("new");
      if (interruption === "network-error") {
        fetchMock.mockRejectedValueOnce(new Error("offline"));
      } else if (interruption === "http-error") {
        fetchMock.mockResolvedValueOnce(new Response("unavailable", { status: 503 }));
      } else if (interruption === "invalid-json") {
        fetchMock.mockResolvedValueOnce(new Response("<html>login</html>"));
      } else {
        respond(interruption);
      }
      await act(async () => window.dispatchEvent(new Event("online")));
      await poll("new");
      expect(result.current.availableBuildId).toBeNull();
      await poll("new");
      expect(result.current.availableBuildId).toBe("new");
    },
  );

  it("ignores package release staleness when the served web build is unchanged", async () => {
    const { result } = renderHook(useWebUpdateNotifications);
    await act(async () => {});
    fetchMock.mockImplementation(
      async () =>
        new Response(
          JSON.stringify({
            version: "0.15.0",
            latest_version: "0.16.0",
            update_available: true,
            webapp_build_id: "loaded",
          }),
        ),
    );
    await act(async () => window.dispatchEvent(new Event("online")));
    await act(async () => window.dispatchEvent(new Event("online")));
    expect(result.current.availableBuildId).toBeNull();
    expect(nativeNotify).not.toHaveBeenCalled();
  });

  it("recovers from a request timeout and starts confirmation again", async () => {
    const { result } = renderHook(useWebUpdateNotifications);
    await act(async () => {});
    await poll("new");
    fetchMock.mockImplementationOnce(
      (_url, options) =>
        new Promise((_resolve, reject) => {
          options?.signal?.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        }),
    );
    await act(async () => window.dispatchEvent(new Event("online")));
    await act(async () => vi.advanceTimersByTime(30_000));
    expect(fetchMock.mock.lastCall?.[1]?.signal?.aborted).toBe(true);
    await poll("new");
    expect(result.current.availableBuildId).toBeNull();
    await poll("new");
    expect(result.current.availableBuildId).toBe("new");
  });

  it.each(["absent", "denied", "rejected"])(
    "keeps the banner usable when native notifications are %s",
    async (availability) => {
      if (availability === "absent") Reflect.deleteProperty(window, "omnigentNative");
      else if (availability === "denied") nativeNotify.mockResolvedValueOnce(false);
      else nativeNotify.mockRejectedValueOnce(new Error("Notification unavailable"));
      const { result } = renderHook(useWebUpdateNotifications);
      await act(async () => {});
      window.dispatchEvent(new Event("blur"));
      await poll("new");
      await poll("new");
      expect(result.current.availableBuildId).toBe("new");
      act(() => result.current.dismiss());
      await poll("new");
      expect(result.current.availableBuildId).toBeNull();
      expect(nativeNotify).toHaveBeenCalledTimes(availability === "absent" ? 0 : 1);
    },
  );

  it("polls on mount, visible intervals, foreground, network and socket reconnect", async () => {
    const { result, unmount } = renderHook(useWebUpdateNotifications);
    await act(async () => {});
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTime(299_999));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    respond("new");
    await act(async () => vi.advanceTimersByTime(1));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    await act(async () => document.dispatchEvent(new Event("visibilitychange")));
    await act(async () => vi.advanceTimersByTime(600_000));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    await act(async () => document.dispatchEvent(new Event("visibilitychange")));
    expect(result.current.availableBuildId).toBe("new");
    vi.mocked(sessionUpdatesSocket.isConnected).mockReturnValue(false);
    await act(async () => socketStatus());
    expect(fetchMock).toHaveBeenCalledTimes(3);
    vi.mocked(sessionUpdatesSocket.isConnected).mockReturnValue(true);
    await act(async () => socketStatus());
    expect(fetchMock).toHaveBeenCalledTimes(4);
    await poll("new");
    expect(fetchMock).toHaveBeenCalledTimes(5);
    unmount();
    await act(async () => {
      window.dispatchEvent(new Event("online"));
      document.dispatchEvent(new Event("visibilitychange"));
      vi.advanceTimersByTime(600_000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(5);
    expect(unsubscribe).toHaveBeenCalledOnce();
  });

  it("notifies through the native bridge once per confirmed build while unfocused", async () => {
    const { result } = renderHook(useWebUpdateNotifications);
    await act(async () => {});
    await poll("new");
    window.dispatchEvent(new Event("blur"));
    await poll("new");
    expect(result.current.availableBuildId).toBe("new");
    expect(nativeNotify).toHaveBeenCalledExactlyOnceWith({
      title: "Update available",
      body: "Reload Omnigent to use the latest web app.",
      navigatePath: "/settings?tab=general",
    });
    act(() => result.current.dismiss());
    await poll("loaded");
    await poll("new");
    await poll("new");
    expect(result.current.availableBuildId).toBeNull();
    expect(nativeNotify).toHaveBeenCalledOnce();
    await poll("newer");
    await poll("newer");
    expect(result.current.availableBuildId).toBe("newer");
    expect(nativeNotify).toHaveBeenCalledTimes(2);
  });

  it.each(["focus", "pointerdown", "keydown"])(
    "uses %s to correct the initial focus state before confirmation",
    async (event) => {
      vi.spyOn(document, "hasFocus").mockReturnValue(false);
      const { result } = renderHook(useWebUpdateNotifications);
      await act(async () => {});
      await poll("new");
      window.dispatchEvent(new Event(event));
      await poll("new");
      expect(result.current.availableBuildId).toBe("new");
      expect(nativeNotify).not.toHaveBeenCalled();
    },
  );

  it("serializes pending requests and checks visibility when the response arrives", async () => {
    const { result, unmount } = renderHook(useWebUpdateNotifications);
    await act(async () => {});
    await poll("new");
    let finish!: (response: Response) => void;
    fetchMock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    await act(async () => window.dispatchEvent(new Event("online")));
    await act(async () => window.dispatchEvent(new Event("online")));
    expect(fetchMock).toHaveBeenCalledTimes(3);
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    await act(async () => finish(new Response(JSON.stringify({ webapp_build_id: "new" }))));
    expect(result.current.availableBuildId).toBe("new");
    expect(nativeNotify).toHaveBeenCalledOnce();
    fetchMock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    await act(async () => window.dispatchEvent(new Event("online")));
    const signal = fetchMock.mock.lastCall?.[1]?.signal;
    unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => finish(new Response(JSON.stringify({ webapp_build_id: "newer" }))));
    expect(nativeNotify).toHaveBeenCalledOnce();
  });

  it("survives StrictMode effect cleanup without using an aborted baseline", async () => {
    const { result } = renderHook(useWebUpdateNotifications, { wrapper: StrictMode });
    await act(async () => {});
    await poll("new");
    expect(result.current.availableBuildId).toBeNull();
    await poll("new");
    expect(result.current.availableBuildId).toBe("new");
  });
});
