import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { OmnigentHostConfig } from "./host";

const fetchMock = vi.fn();
const getHostConfig = vi.fn<() => OmnigentHostConfig>();
const isWorkspace = vi.fn(() => true);
const SLICE_KEY = "X-Databricks-Omnigent-Slice-Key";
const CLIENT_KEY_RE = /^[0-9a-f]{32}$/;
const SESSION_ID = "managed-session";
const EVENTS_URL = `/v1/sessions/${SESSION_ID}/events`;

function wrongReplica(): Response {
  return Response.json({ error: { code: "wrong_replica" } }, { status: 400 });
}

function requestHeaders(index: number): Headers {
  return new Headers((fetchMock.mock.calls[index][1] as RequestInit).headers);
}

beforeEach(() => {
  vi.resetModules();
  fetchMock.mockReset();
  getHostConfig.mockReset().mockReturnValue({ fetcher: fetchMock });
  isWorkspace.mockReturnValue(true);
  vi.doUnmock("./sessionHost");
  vi.doMock("./host", () => ({
    getOmnigentHostConfig: getHostConfig,
    hostFetch: fetchMock,
    isDatabricksWorkspace: isWorkspace,
    isHostRoutingEnabled: isWorkspace,
  }));
});

afterEach(() => {
  vi.doUnmock("./host");
});

async function setup() {
  const identity = await import("./identity");
  const hosts = await import("./sessionHost");
  const resolve = vi.fn(async (sessionId: string, options?: { force?: boolean }) => {
    if (options?.force) hosts.setSessionHost(sessionId, "new-host");
  });
  identity.setSessionHostResolver(resolve);
  return { ...identity, ...hosts, resolve };
}

describe("managed first-message routing", () => {
  it("refreshes an attempted hostless lookup and replays the same message with its new host", async () => {
    const { authenticatedFetch, resolve, resolveSessionHost } = await setup();
    await resolveSessionHost(SESSION_ID);
    const controller = new AbortController();
    const init: RequestInit = {
      method: "POST",
      body: JSON.stringify({ type: "message", data: { content: "Hey" } }),
      headers: { "Content-Type": "application/json", "X-Forwarded-Email": "alice@example.com" },
      signal: controller.signal,
    };
    const success = Response.json({ queued: true });
    fetchMock.mockResolvedValueOnce(wrongReplica()).mockResolvedValueOnce(success);

    const response = await authenticatedFetch(EVENTS_URL, init);

    expect(response).toBe(success);
    expect(resolve).toHaveBeenNthCalledWith(1, SESSION_ID);
    expect(resolve).toHaveBeenNthCalledWith(2, SESSION_ID, { force: true });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(requestHeaders(0).get(SLICE_KEY)).toMatch(CLIENT_KEY_RE);
    expect(requestHeaders(1).get(SLICE_KEY)).toBe("new-host");
    expect(requestHeaders(1).get("Content-Type")).toBe("application/json");
    expect(requestHeaders(1).get("X-Forwarded-Email")).toBe("alice@example.com");
    for (const [url, request] of fetchMock.mock.calls) {
      expect(url).toBe(EVENTS_URL);
      expect(request).toMatchObject({
        method: init.method,
        body: init.body,
        signal: controller.signal,
        cache: "no-store",
      });
    }
  });

  it("uses a host learned while the original request was in flight", async () => {
    const { authenticatedFetch, resolve, setSessionHost } = await setup();
    fetchMock.mockImplementationOnce(async () => {
      setSessionHost(SESSION_ID, "new-host");
      return wrongReplica();
    });
    fetchMock.mockResolvedValueOnce(Response.json({ queued: true }));

    expect((await authenticatedFetch(EVENTS_URL, { method: "POST", body: "{}" })).ok).toBe(true);

    expect(resolve).toHaveBeenCalledTimes(1);
    expect(requestHeaders(0).get(SLICE_KEY)).toMatch(CLIENT_KEY_RE);
    expect(requestHeaders(1).get(SLICE_KEY)).toBe("new-host");
  });

  it("recovers workspace dev requests without an embedded fetcher", async () => {
    getHostConfig.mockReturnValue({});
    const { authenticatedFetch, resolve } = await setup();
    fetchMock.mockResolvedValueOnce(wrongReplica()).mockResolvedValueOnce(Response.json({}));

    expect((await authenticatedFetch(EVENTS_URL)).ok).toBe(true);

    expect(resolve).toHaveBeenCalledExactlyOnceWith(SESSION_ID, { force: true });
    expect(requestHeaders(0).get(SLICE_KEY)).toMatch(CLIENT_KEY_RE);
    expect(requestHeaders(1).get(SLICE_KEY)).toBe("new-host");
  });

  it("waits for an old bootstrap before starting the forced lookup", async () => {
    const { resolve, resolveSessionHost, setSessionHost, getSessionHost } = await setup();
    let finishBootstrap!: () => void;
    resolve.mockImplementation(async (sessionId, options) => {
      if (options?.force) {
        setSessionHost(sessionId, "new-host");
      } else {
        await new Promise<void>((done) => {
          finishBootstrap = done;
        });
      }
    });

    const bootstrap = resolveSessionHost(SESSION_ID);
    const refresh = resolveSessionHost(SESSION_ID, { force: true });
    expect(resolve).toHaveBeenCalledTimes(1);
    finishBootstrap();
    await bootstrap;
    await refresh;

    expect(resolve).toHaveBeenNthCalledWith(2, SESSION_ID, { force: true });
    expect(getSessionHost(SESSION_ID)).toBe("new-host");
  });

  it.each(["hostless", "failed"])(
    "falls back to the unkeyed default replica when refresh is %s",
    async (mode) => {
      const { authenticatedFetch, resolve } = await setup();
      resolve.mockImplementation(async (_sessionId, options) => {
        if (mode === "failed" && options?.force) throw new Error("snapshot unavailable");
      });
      const success = Response.json({ queued: true });
      fetchMock.mockResolvedValueOnce(wrongReplica()).mockResolvedValueOnce(success);

      const response = await authenticatedFetch(EVENTS_URL, { method: "POST", body: "{}" });

      expect(response).toBe(success);
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(resolve).toHaveBeenCalledTimes(2);
      expect(requestHeaders(1).has(SLICE_KEY)).toBe(false);
      expect(fetchMock.mock.calls[1][2]).toEqual({ unkeyed: true });
    },
  );

  it("does not loop when every retry reports wrong_replica", async () => {
    const { authenticatedFetch, resolve } = await setup();
    fetchMock.mockImplementation(async () => wrongReplica());

    const response = await authenticatedFetch(EVENTS_URL, { method: "POST", body: "{}" });

    expect(response.status).toBe(400);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(resolve).toHaveBeenCalledTimes(2);
    expect(requestHeaders(1).get(SLICE_KEY)).toBe("new-host");
    expect(requestHeaders(2).has(SLICE_KEY)).toBe(false);
  });

  it("marks a discovered host keyless when only the default replica reaches it", async () => {
    const { authenticatedFetch, isHostKeyless } = await setup();
    fetchMock
      .mockResolvedValueOnce(wrongReplica())
      .mockResolvedValueOnce(wrongReplica())
      .mockResolvedValueOnce(Response.json({}));

    expect((await authenticatedFetch(EVENTS_URL)).ok).toBe(true);

    expect(requestHeaders(1).get(SLICE_KEY)).toBe("new-host");
    expect(requestHeaders(2).has(SLICE_KEY)).toBe(false);
    expect(isHostKeyless("new-host")).toBe(true);
  });

  it("keeps a demoted host's request unkeyed when the default replica reaches it", async () => {
    const { authenticatedFetch, markHostKeyless, isHostKeyless } = await setup();
    markHostKeyless("new-host");
    fetchMock.mockResolvedValueOnce(wrongReplica()).mockResolvedValueOnce(Response.json({}));

    expect((await authenticatedFetch(EVENTS_URL)).ok).toBe(true);

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(requestHeaders(1).has(SLICE_KEY)).toBe(false);
    expect(isHostKeyless("new-host")).toBe(true);
  });

  it("clears a stale keyless demotion and tries the newly resolved host", async () => {
    const { authenticatedFetch, markHostKeyless, isHostKeyless } = await setup();
    markHostKeyless("new-host");
    fetchMock
      .mockResolvedValueOnce(wrongReplica())
      .mockResolvedValueOnce(wrongReplica())
      .mockResolvedValueOnce(Response.json({}));

    expect((await authenticatedFetch(EVENTS_URL)).ok).toBe(true);

    expect(requestHeaders(1).has(SLICE_KEY)).toBe(false);
    expect(requestHeaders(2).get(SLICE_KEY)).toBe("new-host");
    expect(isHostKeyless("new-host")).toBe(false);
  });

  it("deduplicates concurrent forced host refreshes", async () => {
    const { authenticatedFetch, resolve, setSessionHost, resolveSessionHost } = await setup();
    await resolveSessionHost(SESSION_ID);
    let finishRefresh!: () => void;
    resolve.mockImplementation(async (sessionId, options) => {
      if (!options?.force) return;
      await new Promise<void>((done) => {
        finishRefresh = done;
      });
      setSessionHost(sessionId, "new-host");
    });
    fetchMock.mockImplementation(async (_url, init: RequestInit) =>
      new Headers(init.headers).get(SLICE_KEY) === "new-host"
        ? Response.json({ queued: true })
        : wrongReplica(),
    );

    const first = authenticatedFetch(EVENTS_URL, { method: "POST", body: "first" });
    const second = authenticatedFetch(EVENTS_URL, { method: "POST", body: "second" });
    await vi.waitFor(() => expect(resolve).toHaveBeenCalledTimes(2));
    finishRefresh();

    expect((await first).ok).toBe(true);
    expect((await second).ok).toBe(true);
    expect(resolve).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("does not replay an aborted message after the shared refresh completes", async () => {
    const { authenticatedFetch, resolve, setSessionHost } = await setup();
    const controller = new AbortController();
    resolve.mockImplementation(async (sessionId, options) => {
      if (!options?.force) return;
      controller.abort();
      setSessionHost(sessionId, "new-host");
    });
    fetchMock.mockResolvedValueOnce(wrongReplica());

    await expect(
      authenticatedFetch(EVENTS_URL, { signal: controller.signal }),
    ).rejects.toMatchObject({
      name: "AbortError",
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each([
    { code: "invalid_argument", status: 400 },
    { code: "runner_unavailable", status: 503 },
    { code: "wrong_replica", status: 500 },
  ])("does not retry $status $code", async ({ code, status }) => {
    const { authenticatedFetch, resolve } = await setup();
    const original = Response.json({ error: { code } }, { status });
    fetchMock.mockResolvedValueOnce(original);

    expect(await authenticatedFetch(EVENTS_URL)).toBe(original);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(resolve).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["explicit-key", 1],
    ["standalone", 1],
    ["create", 2],
    ["snapshot", 2],
  ])("leaves %s requests outside the forced host refresh", async (mode, calls) => {
    const { authenticatedFetch, resolve } = await setup();
    if (mode === "standalone") isWorkspace.mockReturnValue(false);
    const url =
      mode === "create"
        ? "/v1/sessions"
        : mode === "snapshot"
          ? `/v1/sessions/${SESSION_ID}`
          : EVENTS_URL;
    const headers = mode === "explicit-key" ? { [SLICE_KEY]: "caller-host" } : undefined;
    const init = mode === "create" ? { method: "POST", body: "{}" } : {};
    fetchMock.mockImplementation(async () => wrongReplica());

    expect((await authenticatedFetch(url, { ...init, headers })).status).toBe(400);
    // A client-keyed create or snapshot still tries the unkeyed default replica once.
    expect(fetchMock).toHaveBeenCalledTimes(calls);
    if (calls === 2) expect(requestHeaders(1).has(SLICE_KEY)).toBe(false);
    expect(resolve.mock.calls.every((call) => call[1]?.force !== true)).toBe(true);
  });

  it("recovers an unknown-host archive PATCH on the host, then the default replica", async () => {
    const { authenticatedFetch, isHostKeyless } = await setup();
    const body = JSON.stringify({ archived: true });
    fetchMock
      .mockResolvedValueOnce(wrongReplica())
      .mockResolvedValueOnce(wrongReplica())
      .mockResolvedValueOnce(Response.json({}));

    const response = await authenticatedFetch(`/v1/sessions/${SESSION_ID}`, {
      method: "PATCH",
      body,
    });

    expect(response.ok).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(requestHeaders(0).get(SLICE_KEY)).toMatch(CLIENT_KEY_RE);
    expect(requestHeaders(1).get(SLICE_KEY)).toBe("new-host");
    expect(requestHeaders(2).has(SLICE_KEY)).toBe(false);
    for (const [, request] of fetchMock.mock.calls) {
      expect(request).toMatchObject({ method: "PATCH", body });
    }
    expect(isHostKeyless("new-host")).toBe(true);
  });

  it("returns a scheduled-task create's wrong_replica as-is on the modal host key", async () => {
    const { authenticatedFetch, setSessionHost, resolveModalHost, isHostKeyless } = await setup();
    setSessionHost("other-session", "host_modal");
    resolveModalHost(["host_modal"]);
    fetchMock.mockResolvedValueOnce(wrongReplica());

    const response = await authenticatedFetch("/v1/scheduled-tasks", {
      method: "POST",
      body: JSON.stringify({ name: "Nightly" }),
    });

    expect(response.status).toBe(400);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(requestHeaders(0).get(SLICE_KEY)).toBe("host_modal");
    expect(isHostKeyless("host_modal")).toBe(false);
  });

  it("looks up an unknown session before a root PATCH and keys it by its host", async () => {
    const { authenticatedFetch, setSessionHost, setSessionHostResolver } = await setup();
    const lookup = vi.fn(async (sessionId: string) => setSessionHost(sessionId, "host_known"));
    setSessionHostResolver(lookup);
    fetchMock.mockResolvedValueOnce(Response.json({}));

    await authenticatedFetch(`/v1/sessions/${SESSION_ID}`, {
      method: "PATCH",
      body: JSON.stringify({ archived: true }),
    });

    expect(lookup).toHaveBeenCalledExactlyOnceWith(SESSION_ID);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(requestHeaders(0).get(SLICE_KEY)).toBe("host_known");
  });

  it("keeps the client key for an unknown session when its lookup fails", async () => {
    const { authenticatedFetch, setSessionHostResolver } = await setup();
    setSessionHostResolver(async () => {
      throw new Error("lookup down");
    });
    fetchMock.mockResolvedValueOnce(Response.json({}));

    const response = await authenticatedFetch(`/v1/sessions/${SESSION_ID}`, {
      method: "PATCH",
      body: JSON.stringify({ archived: true }),
    });

    expect(response.ok).toBe(true);
    expect(requestHeaders(0).get(SLICE_KEY)).toMatch(CLIENT_KEY_RE);
  });
});

describe("host-less requests", () => {
  it.each([
    "/v1/sessions?order=desc&limit=30",
    "/v1/sessions/projects",
    "/v1/sessions/orchestrated",
    "/v1/sessions/updates",
    "/v1/hosts",
    "/health?session_ids=session-a,session-b",
  ])("keys %s by the modal host once it resolves", async (url) => {
    const { authenticatedFetch, setSessionHost, resolveModalHost } = await setup();
    setSessionHost("session-a", "host_modal");
    setSessionHost("session-b", "host_modal");
    setSessionHost("session-c", "host_other");
    resolveModalHost(["host_modal"]);
    fetchMock.mockResolvedValueOnce(Response.json({}));

    await authenticatedFetch(url);

    expect(requestHeaders(0).get(SLICE_KEY)).toBe("host_modal");
  });

  it("keys a host-less request by this client's key before the modal host resolves", async () => {
    const { authenticatedFetch } = await setup();
    fetchMock.mockResolvedValueOnce(Response.json({}));

    await authenticatedFetch("/v1/sessions?limit=30");

    expect(requestHeaders(0).get(SLICE_KEY)).toMatch(CLIENT_KEY_RE);
  });

  it("returns a host-less wrong_replica as-is without demoting the modal host", async () => {
    const { authenticatedFetch, setSessionHost, resolveModalHost, isHostKeyless } = await setup();
    setSessionHost("session-a", "host_modal");
    resolveModalHost(["host_modal"]);
    fetchMock.mockResolvedValueOnce(wrongReplica());

    const response = await authenticatedFetch("/v1/sessions?limit=30");

    expect(response.status).toBe(400);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(isHostKeyless("host_modal")).toBe(false);
  });

  it("sends no slice key and writes no routing key when host routing is off", async () => {
    window.localStorage.clear();
    isWorkspace.mockReturnValue(false);
    const { authenticatedFetch } = await setup();
    fetchMock.mockResolvedValue(Response.json({}));

    await authenticatedFetch("/v1/sessions?limit=30");
    await authenticatedFetch(`/v1/sessions/${SESSION_ID}/events`, { method: "POST", body: "{}" });

    expect(requestHeaders(0).has(SLICE_KEY)).toBe(false);
    expect(requestHeaders(1).has(SLICE_KEY)).toBe(false);
    expect(window.localStorage.getItem("omnigent:routing-key")).toBeNull();
  });
});

describe("managed create routing by a chosen host id", () => {
  it.each([
    [
      { host_type: "managed", host_id: "0123456789abcdef0123456789abcdef" },
      "0123456789abcdef0123456789abcdef",
    ],
    [{ host_type: "managed" }, expect.stringMatching(CLIENT_KEY_RE)],
  ])("keys a managed POST /v1/sessions by the body's host_id (%o)", async (body, key) => {
    const { authenticatedFetch } = await setup();
    fetchMock.mockResolvedValueOnce(Response.json({ id: "conv_new" }, { status: 201 }));

    await authenticatedFetch("/v1/sessions", { method: "POST", body: JSON.stringify(body) });

    expect(requestHeaders(0).get(SLICE_KEY)).toEqual(key);
  });

  it("routes a session by its chosen host id until the host binds", async () => {
    const { authenticatedFetch, sessionRoutingHostId, setSessionHost } = await setup();
    const labels = { "omnigent.sandbox.requested_host_id": "0123456789abcdef0123456789abcdef" };
    setSessionHost(SESSION_ID, sessionRoutingHostId(null, labels));
    fetchMock.mockResolvedValue(Response.json({ queued: true }));

    await authenticatedFetch(EVENTS_URL, { method: "POST", body: "{}" });
    setSessionHost(SESSION_ID, sessionRoutingHostId("bound-host", labels));
    await authenticatedFetch(EVENTS_URL, { method: "POST", body: "{}" });

    expect(requestHeaders(0).get(SLICE_KEY)).toBe("0123456789abcdef0123456789abcdef");
    expect(requestHeaders(1).get(SLICE_KEY)).toBe("bound-host");
  });
});
