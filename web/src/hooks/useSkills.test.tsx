import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { useState, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, expectTypeOf, it, vi } from "vitest";
import { useSkills, type SkillsTarget } from "./useSkills";

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
  it("requires either a session ID or a complete host target", () => {
    expectTypeOf<{ sessionId: string }>().toExtend<SkillsTarget>();
    expectTypeOf<typeof target>().toExtend<SkillsTarget>();
    expectTypeOf<{}>().not.toExtend<SkillsTarget>();
    expectTypeOf<Omit<typeof target, "hostId">>().not.toExtend<SkillsTarget>();
    expectTypeOf<Omit<typeof target, "harness">>().not.toExtend<SkillsTarget>();
    expectTypeOf<Omit<typeof target, "path">>().not.toExtend<SkillsTarget>();
    expectTypeOf<typeof target & { sessionId: string }>().not.toExtend<SkillsTarget>();
    expectTypeOf<{ sessionId: string; agentId: string }>().not.toExtend<SkillsTarget>();
    expectTypeOf<{ hostId: null; harness: string; path: string }>().not.toExtend<SkillsTarget>();
  });

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
      const { result } = renderHook(
        () => useSkills({ target: sessionId ? { sessionId } : target, starting: true }),
        { wrapper },
      );
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

  it.each([{ target: null }, { target, enabled: false }])(
    "does not discover with an unavailable or disabled target: %j",
    async (options) => {
      const { result } = renderHook(() => useSkills(options), { wrapper });
      await act(async () => {});
      expect(result.current.skillsStatus).toBe("unavailable");
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );

  it("keeps session cache dependencies out of the request", async () => {
    fetchMock.mockResolvedValue(response());
    const { result } = renderHook(
      () =>
        useSkills({
          target: {
            sessionId: "session-a",
            scope: {
              hostId: "host",
              workspace: "/repo",
              harness: null,
              agentId: "agent",
              subAgentName: null,
            },
          },
        }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.skillsStatus).toBe("ready"));
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/skills?session_id=session-a");
  });

  it("requests the selected agent's pre-session catalog", async () => {
    fetchMock.mockResolvedValue(response());
    const { result } = renderHook(() => useSkills({ target: { ...target, agentId: "filtered" } }), {
      wrapper,
    });
    await waitFor(() => expect(result.current.skillsStatus).toBe("ready"));
    expect(new URL(fetchMock.mock.calls[0][0], "http://test").searchParams.get("agent_id")).toBe(
      "filtered",
    );
  });

  it("waits for a sandbox host binding and starts when it arrives", async () => {
    fetchMock.mockResolvedValue(response());
    const { result, rerender } = renderHook(
      ({ hostId }) =>
        useSkills({ target: hostId ? { sessionId: "session-a" } : null, starting: true }),
      { wrapper, initialProps: { hostId: null as string | null } },
    );
    expect(result.current.skillsStatus).toBe("loading");
    expect(fetchMock).not.toHaveBeenCalled();
    rerender({ hostId: "sandbox-host" });
    await waitFor(() => expect(result.current.skillsStatus).toBe("ready"));
  });

  it("stops loading when launch fails and no host is available", () => {
    const { result, rerender } = renderHook(
      ({ starting }) => useSkills({ target: null, starting }),
      { wrapper, initialProps: { starting: true } },
    );
    expect(result.current.skillsStatus).toBe("loading");
    rerender({ starting: false });
    expect(result.current.skillsStatus).toBe("unavailable");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([
    { initial: target, next: { ...target, hostId: "other" } },
    { initial: target, next: { ...target, harness: "codex-native" } },
    { initial: target, next: { ...target, path: "/other" } },
    { initial: { ...target, agentId: "agent-a" }, next: { ...target, agentId: "agent-b" } },
    { initial: { sessionId: "session-a" }, next: { sessionId: "session-b" } },
    ...[
      { hostId: "other" },
      { harness: "codex-native" },
      { workspace: "/other" },
      { agentId: "other" },
      { subAgentName: "child" },
    ].map((change) => {
      const scope = {
        hostId: "host",
        harness: "claude-native",
        workspace: "/repo",
        agentId: "agent",
        subAgentName: null,
      };
      return {
        initial: { sessionId: "session-a", scope },
        next: { sessionId: "session-a", scope: { ...scope, ...change } },
      };
    }),
  ])("aborts stale discovery when the target changes: %j", async ({ initial, next }) => {
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
      (discoveryTarget: SkillsTarget) => useSkills({ target: discoveryTarget }),
      { wrapper, initialProps: initial },
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const signal = fetchMock.mock.calls[0][1].signal as AbortSignal;
    rerender(next);
    expect(signal.aborted).toBe(true);
    await waitFor(() => expect(result.current.skillsStatus).toBe("ready"));
    await act(async () => resolveOld(response([{ name: "old", description: "Old target" }])));
    expect(result.current.skills).toEqual(skills);
  });

  it("skips discovery for a read-only composer and hides cached skills when disabled", async () => {
    fetchMock.mockResolvedValue(response());
    const { result, rerender } = renderHook(
      ({ enabled }) => useSkills({ target: { sessionId: "shared-session" }, enabled }),
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
    const { result } = renderHook(() => useSkills({ target }), { wrapper });
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
    const { result } = renderHook(() => useSkills({ target }), { wrapper });
    await waitFor(() => expect(result.current.skillsStatus).toBe("error"));
  });
});
