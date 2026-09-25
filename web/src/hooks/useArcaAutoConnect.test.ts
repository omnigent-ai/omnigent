import { act, cleanup, renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { getArcaStatus, onArcaStatusChanged } from "@/lib/nativeBridge";
import type { ArcaStatus } from "@/lib/nativeBridge";
import { fetchHosts, type Host } from "@/hooks/useHosts";
import { writeArcaHostId } from "@/lib/arcaHost";
import { useArcaAutoConnect, useArcaStatus } from "./useArcaAutoConnect";

vi.mock("@/lib/nativeBridge", () => ({
  getArcaStatus: vi.fn(async () => null),
  onArcaStatusChanged: vi.fn(() => () => {}),
}));
vi.mock("@/hooks/useHosts", () => ({
  fetchHosts: vi.fn(async () => []),
}));
vi.mock("@/lib/arcaHost", () => ({
  writeArcaHostId: vi.fn(),
  readArcaHostId: vi.fn(() => null),
}));
function wrapperWith(queryClient: QueryClient) {
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
}

function makeQueryClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

beforeEach(() => {
  vi.clearAllMocks();
});
afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

// Helper: resolve pending microtasks + flush React state updates.
async function flushAsync() {
  await act(async () => {
    await Promise.resolve();
  });
}

describe("useArcaStatus", () => {
  it("returns null initially and then the fetched status", async () => {
    const status: ArcaStatus = { state: "idle", command: null };
    vi.mocked(getArcaStatus).mockResolvedValue(status);
    vi.mocked(onArcaStatusChanged).mockImplementation(() => () => {});

    const { result } = renderHook(() => useArcaStatus());
    expect(result.current).toBeNull();

    await flushAsync();
    expect(result.current).toEqual(status);
  });

  it("updates when onArcaStatusChanged fires", async () => {
    vi.mocked(getArcaStatus).mockResolvedValue(null);
    let pushStatus: (s: ArcaStatus) => void = () => {};
    vi.mocked(onArcaStatusChanged).mockImplementation((cb) => {
      pushStatus = cb;
      return () => {};
    });

    const { result } = renderHook(() => useArcaStatus());
    await flushAsync();

    const online: ArcaStatus = { state: "online", command: null };
    act(() => {
      pushStatus(online);
    });
    expect(result.current).toEqual(online);
  });

  it("unsubscribes on unmount", async () => {
    const unsub = vi.fn();
    vi.mocked(onArcaStatusChanged).mockReturnValue(unsub);
    vi.mocked(getArcaStatus).mockResolvedValue(null);

    const { unmount } = renderHook(() => useArcaStatus());
    await flushAsync();
    unmount();
    expect(unsub).toHaveBeenCalledOnce();
  });
});

const box = (host_id: string): Host => ({ host_id, name: host_id, owner: "me", status: "online" });

function renderWithPush() {
  let pushStatus: (s: ArcaStatus) => void = () => {};
  vi.mocked(getArcaStatus).mockResolvedValue(null);
  vi.mocked(onArcaStatusChanged).mockImplementation((cb) => {
    pushStatus = cb;
    return () => {};
  });
  const qc = makeQueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  renderHook(() => useArcaAutoConnect(), { wrapper: wrapperWith(qc) });
  return { push: (s: ArcaStatus) => act(() => pushStatus(s)), invalidate };
}

const starting: ArcaStatus = { state: "starting", command: null };
const online: ArcaStatus = { state: "online", command: null };

describe("useArcaAutoConnect", () => {
  it("records the newly-online host as the Arca instance after a fresh connect", async () => {
    vi.useFakeTimers();
    vi.mocked(fetchHosts)
      .mockResolvedValueOnce([box("mac")]) // baseline when starting
      .mockResolvedValue([box("mac"), box("arca-box")]);
    const { push, invalidate } = renderWithPush();
    await act(() => vi.runAllTimersAsync());

    push(starting);
    await act(() => vi.runAllTimersAsync());
    push(online);
    await act(() => vi.runAllTimersAsync());

    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["hosts"] });
    expect(writeArcaHostId).toHaveBeenCalledWith("arca-box");
  });

  it("only refreshes hosts when the daemon was already running", async () => {
    vi.useFakeTimers();
    vi.mocked(fetchHosts).mockResolvedValue([box("mac")]);
    const { push, invalidate } = renderWithPush();
    await act(() => vi.runAllTimersAsync());

    push(starting);
    await act(() => vi.runAllTimersAsync());
    push({ ...online, alreadyRunning: true });
    await act(() => vi.runAllTimersAsync());

    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["hosts"] });
    expect(writeArcaHostId).not.toHaveBeenCalled();
  });

  it("skips discovery when online arrives before the baseline host list", async () => {
    vi.useFakeTimers();
    let resolveBaseline: (hosts: Host[]) => void = () => {};
    vi.mocked(fetchHosts)
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveBaseline = resolve;
          }),
      )
      .mockResolvedValue([box("mac")]);
    const { push } = renderWithPush();
    await act(() => vi.runAllTimersAsync());

    push(starting);
    push(online);
    resolveBaseline([]);
    await act(() => vi.runAllTimersAsync());

    // With no baseline the Mac would look new; it must not be tagged as Arca.
    expect(writeArcaHostId).not.toHaveBeenCalled();
  });

  it("does nothing on failure", async () => {
    vi.useFakeTimers();
    const { push, invalidate } = renderWithPush();
    await act(() => vi.runAllTimersAsync());

    push(starting);
    await act(() => vi.runAllTimersAsync());
    push({ state: "failed", command: null, errorKind: "timeout", error: "x" });
    await act(() => vi.runAllTimersAsync());

    expect(invalidate).not.toHaveBeenCalled();
    expect(writeArcaHostId).not.toHaveBeenCalled();
  });
});
