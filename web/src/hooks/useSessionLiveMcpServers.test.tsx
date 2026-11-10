import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const authenticatedFetch = vi.fn();
vi.mock("@/lib/identity", () => ({
  authenticatedFetch: (...args: unknown[]) => authenticatedFetch(...args),
}));

import {
  liveMcpStatusNote,
  sessionLiveMcpServersQueryKey,
  useReconnectLiveMcpServer,
  useSessionLiveMcpServers,
} from "./useSessionLiveMcpServers";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function wrapperFor(client: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

let client: QueryClient;

beforeEach(() => {
  authenticatedFetch.mockReset();
  client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
});

afterEach(() => {
  vi.useRealTimers();
  client.clear();
});

describe("useSessionLiveMcpServers", () => {
  // An older server answers an unknown route with its generic 404 body.
  it.each([
    { detail: "Not Found" },
    { error: { code: "not_found", message: "Not found" } },
    { error: { code: "not_found", message: "Not found." } },
  ])("treats a server without the live route as unsupported: %j", async (body) => {
    authenticatedFetch.mockResolvedValueOnce(json(body, 404));
    const { result } = renderHook(() => useSessionLiveMcpServers("conv_a", true), {
      wrapper: wrapperFor(client),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual({ live: false, reason: "unsupported", servers: [] });
    expect(authenticatedFetch).toHaveBeenCalledWith(
      "/v1/sessions/conv_a/live-mcp-servers",
      expect.anything(),
    );
  });

  it.each([
    [404, { error: { code: "not_found", message: "Session not found" } }, "Session not found"],
    [403, { error: { code: "forbidden", message: "No access." } }, "No access."],
  ])("still reports a %i read as an error", async (status, body, message) => {
    authenticatedFetch.mockResolvedValueOnce(json(body, status));
    const { result } = renderHook(() => useSessionLiveMcpServers("conv_a", true), {
      wrapper: wrapperFor(client),
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toBe(message);
  });

  it("asks nothing while disabled and re-reads only while a server is connecting", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
    const { rerender } = renderHook(({ enabled }) => useSessionLiveMcpServers("conv_a", enabled), {
      initialProps: { enabled: false },
      wrapper: wrapperFor(client),
    });
    await act(() => vi.advanceTimersByTimeAsync(5_000));
    expect(authenticatedFetch).not.toHaveBeenCalled();

    authenticatedFetch
      .mockResolvedValueOnce(json({ live: true, servers: [{ name: "docs", status: "pending" }] }))
      .mockResolvedValueOnce(
        json({ live: true, servers: [{ name: "docs", status: "connected" }] }),
      );
    rerender({ enabled: true });
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(2_000));
    expect(authenticatedFetch).toHaveBeenCalledTimes(2);
    // Settled: no further reads.
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect(authenticatedFetch).toHaveBeenCalledTimes(2);
  });
});

describe("useReconnectLiveMcpServer", () => {
  it("writes the reconnect's snapshot and keeps the harness's refusal for its row", async () => {
    authenticatedFetch.mockResolvedValueOnce(
      json({
        live: true,
        servers: [{ name: "docs", status: "failed" }],
        reconnect_error: "token expired",
      }),
    );
    const { result } = renderHook(() => useReconnectLiveMcpServer("conv_a"), {
      wrapper: wrapperFor(client),
    });
    act(() => result.current.reconnect("docs"));
    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(result.current.error).toEqual({ name: "docs", message: "token expired" });
    expect(client.getQueryData(sessionLiveMcpServersQueryKey("conv_a"))).toEqual({
      live: true,
      servers: [{ name: "docs", status: "failed" }],
    });
    const [path, init] = authenticatedFetch.mock.calls[0]!;
    expect(path).toBe("/v1/sessions/conv_a/live-mcp-servers/reconnect");
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({ name: "docs" });
  });

  it("keeps a reconnect that settles after a session switch with its own session", async () => {
    let answer!: (response: Response) => void;
    authenticatedFetch.mockReturnValueOnce(
      new Promise<Response>((resolve) => {
        answer = resolve;
      }),
    );
    const { result, rerender } = renderHook(
      ({ sessionId }) => useReconnectLiveMcpServer(sessionId),
      { initialProps: { sessionId: "conv_a" }, wrapper: wrapperFor(client) },
    );
    act(() => result.current.reconnect("docs"));
    await waitFor(() => expect(result.current.reconnectingName).toBe("docs"));

    rerender({ sessionId: "conv_b" });
    expect(result.current.reconnectingName).toBeNull();
    await act(async () =>
      answer(json({ live: true, servers: [], reconnect_error: "still unauthorized" })),
    );
    await waitFor(() =>
      expect(client.getQueryData(sessionLiveMcpServersQueryKey("conv_a"))).toEqual({
        live: true,
        servers: [],
      }),
    );
    expect(client.getQueryData(sessionLiveMcpServersQueryKey("conv_b"))).toBeUndefined();
    expect(result.current.error).toBeNull();

    rerender({ sessionId: "conv_a" });
    expect(result.current.error).toEqual({ name: "docs", message: "still unauthorized" });

    // A reconnect in another session leaves this session's refusal in place.
    authenticatedFetch.mockResolvedValueOnce(json({ live: true, servers: [] }));
    rerender({ sessionId: "conv_b" });
    act(() => result.current.reconnect("wiki"));
    await waitFor(() =>
      expect(client.getQueryData(sessionLiveMcpServersQueryKey("conv_b"))).toEqual({
        live: true,
        servers: [],
      }),
    );
    rerender({ sessionId: "conv_a" });
    expect(result.current.error).toEqual({ name: "docs", message: "still unauthorized" });
  });

  it("shows each session's own reconnect while another session's runs", async () => {
    const answers: ((response: Response) => void)[] = [];
    authenticatedFetch.mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          answers.push(resolve);
        }),
    );
    const { result, rerender } = renderHook(
      ({ sessionId }) => useReconnectLiveMcpServer(sessionId),
      { initialProps: { sessionId: "conv_a" }, wrapper: wrapperFor(client) },
    );
    act(() => result.current.reconnect("docs"));
    await waitFor(() => expect(result.current.reconnectingName).toBe("docs"));
    rerender({ sessionId: "conv_b" });
    act(() => result.current.reconnect("wiki"));
    await waitFor(() => expect(result.current.reconnectingName).toBe("wiki"));

    rerender({ sessionId: "conv_a" });
    expect(result.current.reconnectingName).toBe("docs");
    await act(async () => answers[0]!(json({ live: true, servers: [] })));
    await waitFor(() => expect(result.current.reconnectingName).toBeNull());
    rerender({ sessionId: "conv_b" });
    expect(result.current.reconnectingName).toBe("wiki");
    await act(async () => answers[1]!(json({ live: true, servers: [] })));
  });

  it("keeps the reconnect's snapshot over a read that was already in flight", async () => {
    let answerRead!: (response: Response) => void;
    authenticatedFetch
      .mockResolvedValueOnce(json({ live: true, servers: [{ name: "docs", status: "failed" }] }))
      .mockReturnValueOnce(
        new Promise<Response>((resolve) => {
          answerRead = resolve;
        }),
      )
      .mockResolvedValueOnce(
        json({ live: true, servers: [{ name: "docs", status: "connected" }] }),
      );
    const { result } = renderHook(
      () => ({
        live: useSessionLiveMcpServers("conv_a", true),
        reconnect: useReconnectLiveMcpServer("conv_a"),
      }),
      { wrapper: wrapperFor(client) },
    );
    await waitFor(() => expect(result.current.live.isSuccess).toBe(true));
    act(() => void result.current.live.refetch());
    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalledTimes(2));

    act(() => result.current.reconnect.reconnect("docs"));
    await waitFor(() => expect(result.current.live.data?.servers[0]?.status).toBe("connected"));
    act(() => answerRead(json({ live: true, servers: [{ name: "docs", status: "failed" }] })));
    await waitFor(() =>
      expect(client.getQueryState(sessionLiveMcpServersQueryKey("conv_a"))?.fetchStatus).toBe(
        "idle",
      ),
    );
    expect(result.current.live.data?.servers[0]?.status).toBe("connected");
    expect(result.current.reconnect.error).toBeNull();
  });

  it("re-reads the session's connections after a failed reconnect", async () => {
    authenticatedFetch.mockImplementation((path: string) =>
      Promise.resolve(
        path.endsWith("/reconnect")
          ? json({ error: { code: "runner_unavailable", message: "Host busy." } }, 503)
          : json({ live: true, servers: [{ name: "docs", status: "connected" }] }),
      ),
    );
    const { result } = renderHook(
      () => ({
        live: useSessionLiveMcpServers("conv_a", true),
        reconnect: useReconnectLiveMcpServer("conv_a"),
      }),
      { wrapper: wrapperFor(client) },
    );
    await waitFor(() => expect(result.current.live.isSuccess).toBe(true));
    client.setQueryData(sessionLiveMcpServersQueryKey("conv_a"), {
      live: true,
      servers: [{ name: "docs", status: "failed" }],
    });

    act(() => result.current.reconnect.reconnect("docs"));
    await waitFor(() =>
      expect(result.current.reconnect.error).toEqual({ name: "docs", message: "Host busy." }),
    );
    await waitFor(() => expect(result.current.live.data?.servers[0]?.status).toBe("connected"));
    expect(authenticatedFetch.mock.calls.map(([path]) => path)).toEqual([
      "/v1/sessions/conv_a/live-mcp-servers",
      "/v1/sessions/conv_a/live-mcp-servers/reconnect",
      "/v1/sessions/conv_a/live-mcp-servers",
    ]);
  });
});

describe("liveMcpStatusNote", () => {
  it.each([
    [
      { live: false, reason: "not_running" as const, servers: [] },
      false,
      "Live connection status appears once this session’s agent is running; send a message to start it.",
    ],
    [
      { live: false, reason: "unavailable" as const, detail: "Host offline.", servers: [] },
      false,
      "Couldn’t read this session’s live MCP connections: Host offline.",
    ],
    [
      { live: false, reason: "unavailable" as const, servers: [] },
      false,
      "Couldn’t read this session’s live MCP connections.",
    ],
    [undefined, true, "Couldn’t read this session’s live MCP connections."],
    [{ live: false, reason: "unsupported" as const, servers: [] }, false, undefined],
    [{ live: true, servers: [] }, false, undefined],
    [undefined, false, undefined],
  ])("explains %j (failed read: %s)", (result, failed, note) => {
    expect(liveMcpStatusNote(result, failed)).toBe(note);
  });
});
