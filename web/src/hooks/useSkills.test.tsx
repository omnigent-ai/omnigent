import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { useState, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSkills } from "./useSkills";

const fetchMock = vi.fn();
const target = { hostId: "host", harness: "claude-native", path: "/repo" };
const skills = [{ name: "review", description: "Review changes" }];
const response = (catalog = skills, status = 200) =>
  new Response(JSON.stringify({ skills: catalog }), { status });
const wrapper = function QueryWrapper({ children }: { children: ReactNode }) {
  const [client] = useState(
    () => new QueryClient({ defaultOptions: { queries: { retry: false } } }),
  );
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
};
beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("useSkills", () => {
  it.each([undefined, "session-a"])(
    "settles directly from the discovery response (session: %s)",
    async (sessionId) => {
      let resolve!: (value: Response) => void;
      fetchMock.mockImplementation(
        () =>
          new Promise<Response>((done) => {
            resolve = done;
          }),
      );
      const { result } = renderHook(() => useSkills({ ...target, sessionId, starting: true }), {
        wrapper,
      });
      expect(result.current.skillsStatus).toBe("loading");
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
      const [url, init] = fetchMock.mock.calls[0]!;
      const parsed = new URL(url, "http://test");
      expect(parsed.pathname).toBe("/v1/skills");
      expect(Object.fromEntries(parsed.searchParams)).toEqual(
        sessionId
          ? { session_id: sessionId }
          : {
              host_id: target.hostId,
              harness: target.harness,
              path: target.path,
            },
      );
      expect(init.signal).toBeInstanceOf(AbortSignal);
      await act(async () => resolve(response()));
      await waitFor(() => expect(result.current.skillsStatus).toBe("ready"));
      expect(result.current.skills).toEqual(skills);
    },
  );

  it.each([{ hostId: null }, { harness: null }, { path: "" }, { enabled: false }])(
    "does not discover with incomplete or disabled input: %j",
    async (change) => {
      const { result } = renderHook(() => useSkills({ ...target, ...change }), { wrapper });
      await act(async () => {});
      expect(result.current.skillsStatus).toBe("unavailable");
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );

  it("lets the server determine the session's harness", async () => {
    fetchMock.mockResolvedValue(response());
    const { result } = renderHook(
      () => useSkills({ ...target, sessionId: "session-a", harness: null }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.skillsStatus).toBe("ready"));
  });

  it("waits for a sandbox host binding and starts when it arrives", async () => {
    fetchMock.mockResolvedValue(response());
    const { result, rerender } = renderHook(
      ({ hostId }) => useSkills({ ...target, hostId, sessionId: "session-a", starting: true }),
      { wrapper, initialProps: { hostId: null as string | null } },
    );
    expect(result.current.skillsStatus).toBe("loading");
    expect(fetchMock).not.toHaveBeenCalled();
    rerender({ hostId: "sandbox-host" });
    await waitFor(() => expect(result.current.skillsStatus).toBe("ready"));
  });

  it("stops loading when launch fails and no host is available", () => {
    const { result, rerender } = renderHook(
      ({ starting }) => useSkills({ sessionId: "session-a", starting }),
      { wrapper, initialProps: { starting: true } },
    );
    expect(result.current.skillsStatus).toBe("loading");
    rerender({ starting: false });
    expect(result.current.skillsStatus).toBe("unavailable");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([
    { hostId: "other" },
    { harness: "codex-native" },
    { path: "/other" },
    { sessionId: "session-a" },
    { agentId: "other" },
    { subAgentName: "child" },
  ])("aborts stale discovery when the target changes: %j", async (change) => {
    let resolveOld!: (value: Response) => void;
    fetchMock
      .mockImplementationOnce(
        () =>
          new Promise<Response>((done) => {
            resolveOld = done;
          }),
      )
      .mockResolvedValueOnce(response());
    const { result, rerender } = renderHook(
      (options: Parameters<typeof useSkills>[0]) => useSkills(options),
      { wrapper, initialProps: target },
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const signal = fetchMock.mock.calls[0][1].signal as AbortSignal;
    rerender({ ...target, ...change });
    expect(signal.aborted).toBe(true);
    await waitFor(() => expect(result.current.skillsStatus).toBe("ready"));
    await act(async () => resolveOld(response([{ name: "old", description: "Old target" }])));
    expect(result.current.skills).toEqual(skills);
  });

  it("skips discovery for a read-only composer and hides cached skills when disabled", async () => {
    fetchMock.mockResolvedValue(response());
    const { result, rerender } = renderHook(
      ({ enabled }) => useSkills({ ...target, sessionId: "shared-session", enabled }),
      { wrapper, initialProps: { enabled: false } },
    );
    await act(async () => {});
    expect(fetchMock).not.toHaveBeenCalled();
    rerender({ enabled: true });
    await waitFor(() => expect(result.current.skills).toEqual(skills));
    rerender({ enabled: false });
    expect(result.current.skills).toEqual([]);
    expect(result.current.skillsStatus).toBe("unavailable");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("retries a failed request and accepts an empty catalog", async () => {
    fetchMock.mockResolvedValueOnce(response([], 502));
    const { result } = renderHook(() => useSkills(target), { wrapper });
    await waitFor(() => expect(result.current.skillsStatus).toBe("error"));
    fetchMock.mockResolvedValueOnce(response([]));
    await act(async () => {
      await result.current.refetch();
    });
    await waitFor(() => expect(result.current.skillsStatus).toBe("ready"));
    expect(result.current.skills).toEqual([]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("rejects malformed catalogs", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({})));
    const { result } = renderHook(() => useSkills(target), { wrapper });
    await waitFor(() => expect(result.current.skillsStatus).toBe("error"));
  });
});
