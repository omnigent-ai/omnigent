// Unit tests for `identity.ts` — `resolveIdentity()` discovery and
// `authenticatedFetch()` header injection.
//
// `identity.ts` keeps its cached user id at module scope (the entire
// app shares one identity), so each test calls `vi.resetModules()` and
// re-imports to start from a clean slate. Otherwise tests would leak
// state into each other through the cached `_currentUserId`.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

function mockJsonResponse(body: unknown, init?: { ok?: boolean; status?: number }): Response {
  const res = {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    statusText: "OK",
    json: async () => body,
    // authenticatedFetch peeks at a clone to detect host_unavailable without
    // consuming the caller's body; the mock body is re-readable so clone→self.
    clone() {
      return res;
    },
  };
  return res as unknown as Response;
}

// The server's wrong-replica signal: HTTP 503 with error.code "host_unavailable".
function mockHostUnavailableResponse(): Response {
  return mockJsonResponse({ error: { code: "host_unavailable", message: "wrong replica" } }, {
    ok: false,
    status: 503,
  });
}

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.resetModules();
});

afterEach(() => {
  vi.unstubAllGlobals();
  // The always-send fallback reads the persisted last-host choice; clear it so
  // a case that sets it can't leak into the "omits when hostless" cases (which
  // rely on empty storage → no fallback key).
  try {
    window.localStorage.clear();
  } catch {
    /* no storage in this env */
  }
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

  // The slice key only rides on the workspace-embedded (managed) UI, which
  // installs a host fetcher; a standalone/self-hosted server has none. The
  // embedded cases install a fetcher that delegates to the mocked global fetch,
  // so assertions still read `fetchMock`.
  const embedFetcher = (path: string, init?: RequestInit) => fetch(path, init);

  it("stamps the omnigent slice-key header with the host_id on /v1/hosts routes", async () => {
    // Host-control requests carry the host in the path, so the slice key
    // (the host_id) is derived from the URL directly.
    const { setOmnigentHostConfig } = await import("./host");
    setOmnigentHostConfig({ fetcher: embedFetcher });
    const { authenticatedFetch } = await import("./identity");
    fetchMock.mockResolvedValueOnce(mockJsonResponse({}));
    await authenticatedFetch("/v1/hosts/host_abc/runners", { method: "POST" });

    const headers = new Headers((fetchMock.mock.calls[0][1] as RequestInit).headers);
    expect(headers.get("X-Databricks-Omnigent-Slice-Key")).toBe("host_abc");
  });

  it("stamps a session route's host_id from the session→host map", async () => {
    // A session's requests must reach the replica holding its runner tunnel,
    // keyed by the session's host_id — recorded when the session was parsed.
    const { setSessionHost } = await import("./sessionHost");
    setSessionHost("conv_sess1", "host_xyz");
    const { setOmnigentHostConfig } = await import("./host");
    setOmnigentHostConfig({ fetcher: embedFetcher });
    const { authenticatedFetch } = await import("./identity");
    fetchMock.mockResolvedValueOnce(mockJsonResponse({}));
    await authenticatedFetch("/v1/sessions/conv_sess1/events", { method: "POST" });

    const headers = new Headers((fetchMock.mock.calls[0][1] as RequestInit).headers);
    expect(headers.get("X-Databricks-Omnigent-Slice-Key")).toBe("host_xyz");
  });

  it("omits the slice-key header on a standalone (non-embedded) UI", async () => {
    // No host fetcher installed = standalone/self-hosted server (no Dicer);
    // the key must not ride, even on a host-scoped route.
    const { authenticatedFetch } = await import("./identity");
    fetchMock.mockResolvedValueOnce(mockJsonResponse({}));
    await authenticatedFetch("/v1/hosts/host_abc/runners", { method: "POST" });

    const init = fetchMock.mock.calls[0][1] as RequestInit | undefined;
    if (init?.headers) {
      expect(new Headers(init.headers).has("X-Databricks-Omnigent-Slice-Key")).toBe(false);
    }
  });

  it("omits the slice-key header when the session's host is unknown", async () => {
    const { setOmnigentHostConfig } = await import("./host");
    setOmnigentHostConfig({ fetcher: embedFetcher });
    const { authenticatedFetch } = await import("./identity");
    fetchMock.mockResolvedValueOnce(mockJsonResponse({}));
    await authenticatedFetch("/v1/sessions/conv_unregistered/events", { method: "POST" });

    const init = fetchMock.mock.calls[0][1] as RequestInit | undefined;
    if (init?.headers) {
      expect(new Headers(init.headers).has("X-Databricks-Omnigent-Slice-Key")).toBe(false);
    }
  });

  it("omits the slice-key header on non-host-scoped routes (session list)", async () => {
    const { setOmnigentHostConfig } = await import("./host");
    setOmnigentHostConfig({ fetcher: embedFetcher });
    const { authenticatedFetch } = await import("./identity");
    fetchMock.mockResolvedValueOnce(mockJsonResponse({}));
    await authenticatedFetch("/v1/sessions");

    const init = fetchMock.mock.calls[0][1] as RequestInit | undefined;
    if (init?.headers) {
      expect(new Headers(init.headers).has("X-Databricks-Omnigent-Slice-Key")).toBe(false);
    }
  });

  it("keys a create POST /v1/sessions from the host_id in the request body", async () => {
    // Create carries its host in the body, not the URL. The create notifies
    // the runner inline over the pod-local tunnel, so it must reach the host's
    // replica — recover the host_id from the JSON body.
    const { setOmnigentHostConfig } = await import("./host");
    setOmnigentHostConfig({ fetcher: embedFetcher });
    const { authenticatedFetch } = await import("./identity");
    fetchMock.mockResolvedValueOnce(mockJsonResponse({}));
    await authenticatedFetch("/v1/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent_id: "ag_1", host_id: "host_create" }),
    });

    const headers = new Headers((fetchMock.mock.calls[0][1] as RequestInit).headers);
    expect(headers.get("X-Databricks-Omnigent-Slice-Key")).toBe("host_create");
  });

  it("omits the slice-key header on a hostless create (sandbox / bundled)", async () => {
    // A sandbox create carries host_type but no host_id; a bundled (multipart)
    // create has no JSON body. Neither can be keyed — the runner is bound later
    // via POST /v1/hosts/{id}/runners, which keys itself from the URL.
    const { setOmnigentHostConfig } = await import("./host");
    setOmnigentHostConfig({ fetcher: embedFetcher });
    const { authenticatedFetch } = await import("./identity");
    fetchMock.mockResolvedValueOnce(mockJsonResponse({}));
    await authenticatedFetch("/v1/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent_id: "ag_1", host_type: "managed" }),
    });

    const init = fetchMock.mock.calls[0][1] as RequestInit | undefined;
    if (init?.headers) {
      expect(new Headers(init.headers).has("X-Databricks-Omnigent-Slice-Key")).toBe(false);
    }
  });

  it("does not overwrite a slice-key header the caller set explicitly", async () => {
    const { setOmnigentHostConfig } = await import("./host");
    setOmnigentHostConfig({ fetcher: embedFetcher });
    const { authenticatedFetch } = await import("./identity");
    fetchMock.mockResolvedValueOnce(mockJsonResponse({}));
    await authenticatedFetch("/v1/hosts/host_abc/runners", {
      method: "POST",
      headers: { "X-Databricks-Omnigent-Slice-Key": "explicit" },
    });

    const headers = new Headers((fetchMock.mock.calls[0][1] as RequestInit).headers);
    expect(headers.get("X-Databricks-Omnigent-Slice-Key")).toBe("explicit");
  });

  // Wrong-replica fallback: a slice-keyed request that lands on a replica
  // without the host's tunnel comes back 503 host_unavailable; the client
  // retries ONCE without the key so it routes by the workspace-id default.
  it("retries without the slice key on a 503 host_unavailable response", async () => {
    const { setSessionHost } = await import("./sessionHost");
    setSessionHost("conv_mis", "host_gone");
    const { setOmnigentHostConfig } = await import("./host");
    setOmnigentHostConfig({ fetcher: embedFetcher });
    const { authenticatedFetch } = await import("./identity");
    // First attempt (keyed) misroutes; retry (keyless) succeeds.
    fetchMock.mockResolvedValueOnce(mockHostUnavailableResponse());
    fetchMock.mockResolvedValueOnce(mockJsonResponse({ ok: true }));

    const res = await authenticatedFetch("/v1/sessions/conv_mis/events", { method: "POST" });

    expect(res.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    // First carried the key; the retry dropped it.
    const first = new Headers((fetchMock.mock.calls[0][1] as RequestInit).headers);
    const second = new Headers((fetchMock.mock.calls[1][1] as RequestInit).headers);
    expect(first.get("X-Databricks-Omnigent-Slice-Key")).toBe("host_gone");
    expect(second.has("X-Databricks-Omnigent-Slice-Key")).toBe(false);
  });

  it("retries at most once — a second host_unavailable surfaces to the caller", async () => {
    const { setSessionHost } = await import("./sessionHost");
    setSessionHost("conv_mis2", "host_gone");
    const { setOmnigentHostConfig } = await import("./host");
    setOmnigentHostConfig({ fetcher: embedFetcher });
    const { authenticatedFetch } = await import("./identity");
    fetchMock.mockResolvedValue(mockHostUnavailableResponse());

    const res = await authenticatedFetch("/v1/sessions/conv_mis2/events", { method: "POST" });

    expect(res.status).toBe(503);
    // Keyed attempt + one keyless retry, then give up (no infinite loop).
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does not retry a 503 that is not host_unavailable (e.g. runner_unavailable)", async () => {
    const { setSessionHost } = await import("./sessionHost");
    setSessionHost("conv_off", "host_a");
    const { setOmnigentHostConfig } = await import("./host");
    setOmnigentHostConfig({ fetcher: embedFetcher });
    const { authenticatedFetch } = await import("./identity");
    fetchMock.mockResolvedValueOnce(
      mockJsonResponse({ error: { code: "runner_unavailable" } }, { ok: false, status: 503 }),
    );

    const res = await authenticatedFetch("/v1/sessions/conv_off/events", { method: "POST" });

    expect(res.status).toBe(503);
    // A genuinely offline runner: a keyless retry would not help, so we don't.
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not retry when no slice key was sent (standalone / non-host route)", async () => {
    const { setOmnigentHostConfig } = await import("./host");
    setOmnigentHostConfig({ fetcher: embedFetcher });
    const { authenticatedFetch } = await import("./identity");
    // Non-host-scoped route → no key stamped (empty last-host storage) →
    // host_unavailable (however it arose) is surfaced as-is, not retried.
    fetchMock.mockResolvedValueOnce(mockHostUnavailableResponse());

    const res = await authenticatedFetch("/v1/sessions");

    expect(res.status).toBe(503);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  // Always-send invariant: a host-less / cross-host request (no host in the URL
  // or create body) falls back to the resolved MODAL host so EVERY workspace
  // request carries SOME key. Safe: these routes serve from any replica, and a
  // wrong guess self-heals via the host_unavailable keyless retry. The modal is
  // seeded here by recording a session host + resolving (the app does this on
  // the first session-list settle, in SessionUpdatesProvider).
  it("falls back to the modal host on a host-less route (session list)", async () => {
    const { setSessionHost, resolveModalHost, _resetModalHostForTest } =
      await import("./sessionHost");
    _resetModalHostForTest();
    setSessionHost("conv_a", "host_modal");
    resolveModalHost(() => null);
    const { setOmnigentHostConfig } = await import("./host");
    setOmnigentHostConfig({ fetcher: embedFetcher });
    const { authenticatedFetch } = await import("./identity");
    fetchMock.mockResolvedValueOnce(mockJsonResponse({}));
    await authenticatedFetch("/v1/sessions");

    const headers = new Headers((fetchMock.mock.calls[0][1] as RequestInit).headers);
    expect(headers.get("X-Databricks-Omnigent-Slice-Key")).toBe("host_modal");
  });

  it("prefers the request's own host over the modal fallback", async () => {
    // A host-scoped route resolves its OWN host from the path; the modal
    // fallback must not override it.
    const { setSessionHost, resolveModalHost, _resetModalHostForTest } =
      await import("./sessionHost");
    _resetModalHostForTest();
    setSessionHost("conv_a", "host_modal");
    resolveModalHost(() => null);
    const { setOmnigentHostConfig } = await import("./host");
    setOmnigentHostConfig({ fetcher: embedFetcher });
    const { authenticatedFetch } = await import("./identity");
    fetchMock.mockResolvedValueOnce(mockJsonResponse({}));
    await authenticatedFetch("/v1/hosts/host_path/runners", { method: "POST" });

    const headers = new Headers((fetchMock.mock.calls[0][1] as RequestInit).headers);
    expect(headers.get("X-Databricks-Omnigent-Slice-Key")).toBe("host_path");
  });

  it("omits the key when the modal host is unresolved (no key before first settle)", async () => {
    const { _resetModalHostForTest } = await import("./sessionHost");
    _resetModalHostForTest(); // resolve NOT called → modalHostId() is null
    const { setOmnigentHostConfig } = await import("./host");
    setOmnigentHostConfig({ fetcher: embedFetcher });
    const { authenticatedFetch } = await import("./identity");
    fetchMock.mockResolvedValueOnce(mockJsonResponse({}));
    await authenticatedFetch("/v1/sessions");

    const init = fetchMock.mock.calls[0][1] as RequestInit | undefined;
    if (init?.headers) {
      expect(new Headers(init.headers).has("X-Databricks-Omnigent-Slice-Key")).toBe(false);
    }
  });

  it("does not fall back on a standalone (non-embedded) UI even with a resolved modal", async () => {
    const { setSessionHost, resolveModalHost, _resetModalHostForTest } =
      await import("./sessionHost");
    _resetModalHostForTest();
    setSessionHost("conv_a", "host_modal");
    resolveModalHost(() => null);
    const { authenticatedFetch } = await import("./identity");
    fetchMock.mockResolvedValueOnce(mockJsonResponse({}));
    await authenticatedFetch("/v1/sessions");

    const init = fetchMock.mock.calls[0][1] as RequestInit | undefined;
    if (init?.headers) {
      expect(new Headers(init.headers).has("X-Databricks-Omnigent-Slice-Key")).toBe(false);
    }
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
