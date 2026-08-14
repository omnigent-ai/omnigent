// Unit tests for `identity.ts` — `resolveIdentity()` discovery and
// `authenticatedFetch()` header injection.
//
// `identity.ts` keeps its cached user id at module scope (the entire
// app shares one identity), so each test calls `vi.resetModules()` and
// re-imports to start from a clean slate. Otherwise tests would leak
// state into each other through the cached `currentUserId`.

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

  describe("slice-key routing (host sharding)", () => {
    it("stamps X-Databricks-Omnigent-Slice-Key on host-scoped URLs", async () => {
      // Mock sessionHost module before importing identity
      vi.doMock("./sessionHost", () => ({
        getSessionHost: vi.fn(() => "host_123"),
        setSessionHost: vi.fn(),
        isHostKeyless: vi.fn(() => false),
        markHostKeyless: vi.fn(),
        clearHostKeyless: vi.fn(),
        modalHostId: vi.fn(() => null),
        resolveModalHost: vi.fn(),
        isModalHostResolved: vi.fn(() => true),
      }));
      vi.doMock("./host", () => ({
        getOmnigentHostConfig: vi.fn(() => ({ fetcher: () => fetch })),
        hostFetch: fetchMock,
        isDatabricksWorkspace: vi.fn(() => true),
      }));

      fetchMock.mockResolvedValueOnce(mockJsonResponse({}));
      const { authenticatedFetch } = await import("./identity");

      await authenticatedFetch("/v1/hosts/host_123/runners");

      const init = fetchMock.mock.calls[0][1] as RequestInit;
      const headers = new Headers(init.headers);
      expect(headers.get("X-Databricks-Omnigent-Slice-Key")).toBe("host_123");
    });

    it("stamps the slice-key header in standalone dev against a workspace", async () => {
      // `npm run dev` pointed at a workspace URL installs no fetcher, but it's
      // still a Databricks workspace (sharded) — the VITE_DATABRICKS_WORKSPACE
      // build flag drives the key so dev traffic reaches the right replica (no
      // manual step).
      vi.stubEnv("VITE_DATABRICKS_WORKSPACE", "true");
      vi.doMock("./sessionHost", () => ({
        getSessionHost: vi.fn(() => null),
        setSessionHost: vi.fn(),
        isHostKeyless: vi.fn(() => false),
        markHostKeyless: vi.fn(),
        clearHostKeyless: vi.fn(),
        modalHostId: vi.fn(() => null),
        resolveModalHost: vi.fn(),
        isModalHostResolved: vi.fn(() => true),
      }));
      vi.doMock("./host", () => ({
        getOmnigentHostConfig: vi.fn(() => ({})),
        hostFetch: fetchMock,
        isDatabricksWorkspace: vi.fn(() => true),
      }));

      fetchMock.mockResolvedValueOnce(mockJsonResponse({}));
      const { authenticatedFetch } = await import("./identity");

      await authenticatedFetch("/v1/hosts/host_abc/runners", { method: "POST" });

      const headers = new Headers((fetchMock.mock.calls[0][1] as RequestInit).headers);
      expect(headers.get("X-Databricks-Omnigent-Slice-Key")).toBe("host_abc");
    });

    it("retries keyless on wrong_replica 400 response", async () => {
      vi.doMock("./sessionHost", () => ({
        getSessionHost: vi.fn(() => "host_789"),
        setSessionHost: vi.fn(),
        isHostKeyless: vi.fn(() => false),
        markHostKeyless: vi.fn(),
        clearHostKeyless: vi.fn(),
        modalHostId: vi.fn(() => null),
        resolveModalHost: vi.fn(),
        isModalHostResolved: vi.fn(() => true),
      }));
      vi.doMock("./host", () => ({
        getOmnigentHostConfig: vi.fn(() => ({ fetcher: () => fetch })),
        hostFetch: fetchMock,
        isDatabricksWorkspace: vi.fn(() => true),
      }));

      const wrongReplicaResponse = {
        ok: false,
        status: 400,
        statusText: "Bad Request",
        json: async () => ({ error: { code: "wrong_replica" } }),
        clone: function () {
          return this;
        },
      } as unknown as Response;

      fetchMock.mockResolvedValueOnce(wrongReplicaResponse);
      fetchMock.mockResolvedValueOnce(mockJsonResponse({}));

      const { authenticatedFetch } = await import("./identity");
      const response = await authenticatedFetch("/v1/sessions/sess_xyz/resources/terminals");

      // Should retry after the wrong_replica response
      expect(fetchMock).toHaveBeenCalledTimes(2);
      // First call should have the slice key
      const firstInit = fetchMock.mock.calls[0][1] as RequestInit;
      const firstHeaders = new Headers(firstInit.headers);
      expect(firstHeaders.get("X-Databricks-Omnigent-Slice-Key")).toBe("host_789");
      // Second call (retry) should NOT have the slice key
      const secondInit = fetchMock.mock.calls[1][1] as RequestInit;
      const secondHeaders = new Headers(secondInit.headers);
      expect(secondHeaders.get("X-Databricks-Omnigent-Slice-Key")).toBeNull();
      expect(response.status).toBe(200);
    });
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
