import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { useState, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Session } from "@/lib/types";
import { useSessionSkills } from "./useSessionSkills";

const fetchMock = vi.fn();
const session = {
  id: "session-a",
  hostId: "host",
  workspace: "/repo",
  agentId: "agent",
  harness: "claude-sdk",
} as Session;
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

describe("useSessionSkills", () => {
  it("settles directly from the host response while the runner is starting", async () => {
    let resolve!: (value: Response) => void;
    fetchMock.mockImplementation(
      () =>
        new Promise<Response>((done) => {
          resolve = done;
        }),
    );
    const { result } = renderHook(() => useSessionSkills(session, true, true), { wrapper });
    expect(result.current.skillsStatus).toBe("loading");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/sessions/session-a/skills");
    await act(async () => resolve(response()));
    await waitFor(() => expect(result.current.skillsStatus).toBe("ready"));
    expect(result.current.skills).toEqual(skills);
  });

  it("waits for a sandbox host binding and starts when it arrives", async () => {
    fetchMock.mockResolvedValue(response());
    const { result, rerender } = renderHook(
      ({ current, online }) => useSessionSkills(current, online, true),
      {
        wrapper,
        initialProps: { current: { ...session, hostId: null } as Session, online: false },
      },
    );
    expect(result.current.skillsStatus).toBe("loading");
    expect(fetchMock).not.toHaveBeenCalled();
    rerender({ current: session, online: true });
    await waitFor(() => expect(result.current.skillsStatus).toBe("ready"));
  });

  it("stops loading when launch fails and no host is available", () => {
    const { result, rerender } = renderHook(
      ({ starting }) => useSessionSkills(null, false, starting),
      { wrapper, initialProps: { starting: true } },
    );
    expect(result.current.skillsStatus).toBe("loading");
    rerender({ starting: false });
    expect(result.current.skillsStatus).toBe("unavailable");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([
    { id: "session-b" },
    { workspace: "/other" },
    { agentId: "agent-b" },
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
      ({ current }) => useSessionSkills(current, true, false),
      { wrapper, initialProps: { current: session } },
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const signal = fetchMock.mock.calls[0][1].signal as AbortSignal;
    rerender({ current: { ...session, ...change } });
    expect(signal.aborted).toBe(true);
    await waitFor(() => expect(result.current.skillsStatus).toBe("ready"));
    await act(async () => resolveOld(response([{ name: "old", description: "Old target" }])));
    expect(result.current.skills).toEqual(skills);
  });

  it("retries a failed request and accepts an empty catalog", async () => {
    fetchMock.mockResolvedValueOnce(response([], 502));
    const { result } = renderHook(() => useSessionSkills(session, true, false), { wrapper });
    await waitFor(() => expect(result.current.skillsStatus).toBe("error"));
    fetchMock.mockResolvedValueOnce(response([]));
    await act(async () => {
      await result.current.refetch();
    });
    await waitFor(() => expect(result.current.skillsStatus).toBe("ready"));
    expect(result.current.skills).toEqual([]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
