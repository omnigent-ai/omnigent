import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const fetchMock = vi.fn();
const isWorkspace = vi.fn(() => true);
const SLICE_KEY = "X-Databricks-Omnigent-Slice-Key";
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
  isWorkspace.mockReturnValue(true);
  vi.doUnmock("./sessionHost");
  vi.doMock("./host", () => ({
    getOmnigentHostConfig: () => ({ fetcher: fetchMock }),
    hostFetch: fetchMock,
    isDatabricksWorkspace: isWorkspace,
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
    expect(requestHeaders(0).has(SLICE_KEY)).toBe(false);
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
    expect(requestHeaders(0).has(SLICE_KEY)).toBe(false);
    expect(requestHeaders(1).get(SLICE_KEY)).toBe("new-host");
  });

  it("recovers workspace dev requests without an embedded fetcher", async () => {
    vi.doMock("./host", () => ({
      getOmnigentHostConfig: () => ({}),
      hostFetch: fetchMock,
      isDatabricksWorkspace: isWorkspace,
    }));
    const { authenticatedFetch, resolve } = await setup();
    fetchMock.mockResolvedValueOnce(wrongReplica()).mockResolvedValueOnce(Response.json({}));

    expect((await authenticatedFetch(EVENTS_URL)).ok).toBe(true);

    expect(resolve).toHaveBeenCalledExactlyOnceWith(SESSION_ID, { force: true });
    expect(requestHeaders(0).has(SLICE_KEY)).toBe(false);
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
    "preserves the original error when refresh is %s",
    async (mode) => {
      const { authenticatedFetch, resolve } = await setup();
      resolve.mockImplementation(async (_sessionId, options) => {
        if (mode === "failed" && options?.force) throw new Error("snapshot unavailable");
      });
      const original = wrongReplica();
      fetchMock.mockResolvedValue(original);

      const response = await authenticatedFetch(EVENTS_URL, { method: "POST", body: "{}" });

      expect(response).toBe(original);
      expect(await response.json()).toEqual({ error: { code: "wrong_replica" } });
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(resolve).toHaveBeenCalledTimes(2);
    },
  );

  it("does not loop when the keyed retry also reports wrong_replica", async () => {
    const { authenticatedFetch, resolve } = await setup();
    fetchMock.mockImplementation(async () => wrongReplica());

    const response = await authenticatedFetch(EVENTS_URL, { method: "POST", body: "{}" });

    expect(response.status).toBe(400);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(resolve).toHaveBeenCalledTimes(2);
    expect(requestHeaders(1).get(SLICE_KEY)).toBe("new-host");
  });

  it("clears a stale keyless demotion before trying the newly resolved host", async () => {
    const { authenticatedFetch, markHostKeyless, isHostKeyless } = await setup();
    markHostKeyless("new-host");
    fetchMock.mockResolvedValueOnce(wrongReplica()).mockResolvedValueOnce(Response.json({}));

    await authenticatedFetch(EVENTS_URL);

    expect(requestHeaders(1).get(SLICE_KEY)).toBe("new-host");
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
      new Headers(init.headers).has(SLICE_KEY) ? Response.json({ queued: true }) : wrongReplica(),
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

  it.each(["explicit-key", "standalone", "create", "snapshot"])(
    "leaves %s requests outside managed first-message recovery",
    async (mode) => {
      const { authenticatedFetch, resolve } = await setup();
      if (mode === "standalone") isWorkspace.mockReturnValue(false);
      const url =
        mode === "create"
          ? "/v1/sessions"
          : mode === "snapshot"
            ? `/v1/sessions/${SESSION_ID}`
            : EVENTS_URL;
      const headers = mode === "explicit-key" ? { [SLICE_KEY]: "caller-host" } : undefined;
      const original = wrongReplica();
      fetchMock.mockResolvedValueOnce(original);

      expect(await authenticatedFetch(url, { headers })).toBe(original);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(resolve.mock.calls.every((call) => call[1]?.force !== true)).toBe(true);
    },
  );
});
