// Unit tests for `identity.ts` — `resolveIdentity()` discovery and
// `authenticatedFetch()` header injection.
//
// `identity.ts` keeps its cached user id at module scope (the entire
// app shares one identity), so each test calls `vi.resetModules()` and
// re-imports to start from a clean slate. Otherwise tests would leak
// state into each other through the cached `_currentUserId`.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

function mockJsonResponse(body: unknown, init?: { ok?: boolean; status?: number }): Response {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    statusText: "OK",
    json: async () => body,
  } as unknown as Response;
}

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.resetModules();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("resolveIdentity", () => {
  it("calls GET /v1/me and caches the user id", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ user_id: "alice@example.com" }));
    const { resolveIdentity, getCurrentUserId } = await import("./identity");

    const userId = await resolveIdentity();

    expect(userId).toBe("alice@example.com");
    expect(getCurrentUserId()).toBe("alice@example.com");
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/me");
  });

  it("returns the cached value on subsequent calls without re-fetching", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ user_id: "bob" }));
    const { resolveIdentity } = await import("./identity");

    const first = await resolveIdentity();
    const second = await resolveIdentity();

    expect(first).toBe("bob");
    expect(second).toBe("bob");
    // Critical: a second call MUST NOT hit the network. If this fires
    // twice we're paying a round-trip on every component mount.
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("dedupes concurrent calls into a single in-flight request", async () => {
    // Two callers race resolveIdentity() before the first has settled.
    // Both should resolve to the same user id from one fetch — without
    // dedupe, the cache would get populated twice and `fetch` would
    // fire twice.
    let resolveBody: ((r: Response) => void) | null = null;
    fetchMock.mockReturnValueOnce(
      new Promise<Response>((r) => {
        resolveBody = r;
      }),
    );
    const { resolveIdentity } = await import("./identity");

    const a = resolveIdentity();
    const b = resolveIdentity();
    expect(fetchMock).toHaveBeenCalledOnce();

    resolveBody!(mockJsonResponse({ user_id: "carol" }));
    expect(await a).toBe("carol");
    expect(await b).toBe("carol");
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("returns null when the server responds with user_id: null", async () => {
    // Server signals "no auth provider configured" with user_id: null.
    // Resolution should still complete (not throw) so the app can
    // continue without sending the header.
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ user_id: null }));
    const { resolveIdentity, getCurrentUserId } = await import("./identity");

    const userId = await resolveIdentity();

    expect(userId).toBeNull();
    expect(getCurrentUserId()).toBeNull();
  });

  it("swallows network errors and resolves to null", async () => {
    // If the server is unreachable we can't block app startup. The
    // promise must resolve (not reject) and `getCurrentUserId` returns
    // null. authenticatedFetch then becomes a passthrough.
    fetchMock.mockRejectedValueOnce(new Error("network"));
    const { resolveIdentity, getCurrentUserId } = await import("./identity");

    const userId = await resolveIdentity();

    expect(userId).toBeNull();
    expect(getCurrentUserId()).toBeNull();
  });

  it("treats non-2xx as null without throwing", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({}, { ok: false, status: 500 }));
    const { resolveIdentity } = await import("./identity");

    await expect(resolveIdentity()).resolves.toBeNull();
  });
});

describe("getCurrentUserId", () => {
  it("returns null before resolveIdentity has been called", async () => {
    const { getCurrentUserId } = await import("./identity");
    expect(getCurrentUserId()).toBeNull();
  });
});

describe("resolveIdentity base-path login redirect", () => {
  let originalLocation: Location;

  beforeEach(() => {
    originalLocation = window.location;
  });

  afterEach(() => {
    Object.defineProperty(window, "location", { configurable: true, value: originalLocation });
    delete window.__OMNIGENT_BASE_PATH__;
  });

  function mockLocation(pathname: string): void {
    Object.defineProperty(window, "location", {
      configurable: true,
      value: {
        pathname,
        search: "",
        href: "",
        origin: "http://localhost",
        host: "localhost",
        protocol: "http:",
      },
    });
  }

  it("redirects to the base-prefixed login URL on 401", async () => {
    window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";
    mockLocation("/proxy/6767/c/abc");
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({ user_id: null, login_url: "/login" }, { ok: false, status: 401 }),
    );
    const { resolveIdentity } = await import("./identity");

    await resolveIdentity();

    expect(window.location.href).toBe(
      `/proxy/6767/login?return_to=${encodeURIComponent("/proxy/6767/c/abc")}`,
    );
  });

  it("does not redirect when already on the base-prefixed login path", async () => {
    window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";
    mockLocation("/proxy/6767/login");
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({ user_id: null, login_url: "/login" }, { ok: false, status: 401 }),
    );
    const { resolveIdentity } = await import("./identity");

    await resolveIdentity();

    expect(window.location.href).toBe("");
  });
});

describe("authenticatedFetch", () => {
  it("injects X-Forwarded-Email header once the identity is resolved", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ user_id: "alice" }));
    const { resolveIdentity, authenticatedFetch } = await import("./identity");
    await resolveIdentity();

    fetchMock.mockResolvedValueOnce(mockJsonResponse({}));
    await authenticatedFetch("/v1/sessions");

    const init = fetchMock.mock.calls[1][1] as RequestInit;
    const headers = new Headers(init.headers);
    expect(headers.get("X-Forwarded-Email")).toBe("alice");
  });

  it("does NOT inject the header when identity is unresolved", async () => {
    // Before `resolveIdentity()` runs, the cache is null. We must not
    // send `X-Forwarded-Email: null` (which the server would reject in
    // multi-user mode) — pass the request through untouched.
    const { authenticatedFetch } = await import("./identity");

    fetchMock.mockResolvedValueOnce(mockJsonResponse({}));
    await authenticatedFetch("/v1/sessions");

    const init = fetchMock.mock.calls[0][1] as RequestInit | undefined;
    if (init?.headers) {
      const headers = new Headers(init.headers);
      expect(headers.has("X-Forwarded-Email")).toBe(false);
    }
  });

  it("preserves caller-supplied headers when injecting", async () => {
    // The caller may already pass Content-Type, Accept, etc. Those
    // must survive the merge with the auth header.
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ user_id: "alice" }));
    const { resolveIdentity, authenticatedFetch } = await import("./identity");
    await resolveIdentity();

    fetchMock.mockResolvedValueOnce(mockJsonResponse({}));
    await authenticatedFetch("/v1/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: "{}",
    });

    const init = fetchMock.mock.calls[1][1] as RequestInit;
    const headers = new Headers(init.headers);
    expect(headers.get("X-Forwarded-Email")).toBe("alice");
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(headers.get("Accept")).toBe("text/event-stream");
    expect(init.method).toBe("POST");
    expect(init.body).toBe("{}");
  });

  it("does not overwrite an explicit X-Forwarded-Email the caller set", async () => {
    // Edge case: a caller (test, debug tool, future explicit-impersonate
    // flow) may set X-Forwarded-Email itself. Don't clobber it — the
    // identity layer is a default, not an override.
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ user_id: "alice" }));
    const { resolveIdentity, authenticatedFetch } = await import("./identity");
    await resolveIdentity();

    fetchMock.mockResolvedValueOnce(mockJsonResponse({}));
    await authenticatedFetch("/v1/sessions", {
      headers: { "X-Forwarded-Email": "explicit-override" },
    });

    const init = fetchMock.mock.calls[1][1] as RequestInit;
    const headers = new Headers(init.headers);
    expect(headers.get("X-Forwarded-Email")).toBe("explicit-override");
  });

  it("forwards method, body, and signal", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ user_id: "alice" }));
    const { resolveIdentity, authenticatedFetch } = await import("./identity");
    await resolveIdentity();

    const controller = new AbortController();
    fetchMock.mockResolvedValueOnce(mockJsonResponse({}));
    await authenticatedFetch("/v1/sessions/x", {
      method: "DELETE",
      signal: controller.signal,
    });

    const init = fetchMock.mock.calls[1][1] as RequestInit;
    expect(init.method).toBe("DELETE");
    expect(init.signal).toBe(controller.signal);
  });
});

describe("getCurrentAuthorId", () => {
  it("returns a resolved real identity for self-attribution", async () => {
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ user_id: "alice@example.com" }));
    const { resolveIdentity, getCurrentAuthorId } = await import("./identity");
    await resolveIdentity();
    expect(getCurrentAuthorId()).toBe("alice@example.com");
  });

  it("returns null for the single-user 'local' sentinel", async () => {
    // /v1/me returns "local" when auth is disabled; it is not a distinct
    // actor, so optimistic bubbles must stay unlabeled (no "local" flash).
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ user_id: "local" }));
    const { resolveIdentity, getCurrentAuthorId } = await import("./identity");
    await resolveIdentity();
    expect(getCurrentAuthorId()).toBeNull();
  });

  it("returns null before identity resolves", async () => {
    const { getCurrentAuthorId } = await import("./identity");
    // No resolveIdentity() call: cache is still null, so no label.
    expect(getCurrentAuthorId()).toBeNull();
  });
});
