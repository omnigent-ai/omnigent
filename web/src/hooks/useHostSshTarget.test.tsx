import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useFileContent", () => ({ fetchFileContent: vi.fn() }));
vi.mock("@/hooks/useHosts", () => ({ useHosts: vi.fn() }));

import { fetchFileContent } from "@/hooks/useFileContent";
import { useHosts } from "@/hooks/useHosts";
import { useHostSshTarget, useResolveHostSshTarget } from "./useHostSshTarget";

const fetchMock = vi.mocked(fetchFileContent);
const HOST_KEY = ["hostSshTarget", "host_1"];

let client: QueryClient;

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function stubHost(host: { status: string; sandbox_provider?: string | null }) {
  vi.mocked(useHosts).mockReturnValue({
    data: [{ host_id: "host_1", name: "dev box", ...host }],
  } as ReturnType<typeof useHosts>);
}

/** Two sessions on one host showing its SSH name, each with a menu that can ask for it. */
function renderTwoSessions() {
  return renderHook(
    () => ({
      first: useHostSshTarget("host_1", "conv_1"),
      second: useHostSshTarget("host_1", "conv_2"),
      resolveFirst: useResolveHostSshTarget("host_1", "conv_1"),
      resolveSecond: useResolveHostSshTarget("host_1", "conv_2"),
    }),
    { wrapper },
  );
}

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  fetchMock.mockReset();
  // Metadata that names no machine type: the read finishes with no SSH name.
  fetchMock.mockResolvedValue({
    object: "session.environment.filesystem.file_content",
    encoding: "utf-8",
    truncated: false,
    content: "region example\n",
  } as Awaited<ReturnType<typeof fetchFileContent>>);
  stubHost({ status: "online", sandbox_provider: null });
});

describe("useHostSshTarget", () => {
  it("reads nothing from the host while a surface only shows its SSH name", async () => {
    renderTwoSessions();
    await act(async () => {});
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("reads the host's metadata once, when a menu asks, for every session on it", async () => {
    const { result } = renderTwoSessions();
    act(() => result.current.resolveFirst());
    await waitFor(() => expect(client.getQueryState(HOST_KEY)?.status).toBe("success"));
    act(() => result.current.resolveSecond());
    await act(async () => {});
    expect(fetchMock.mock.calls).toEqual([["conv_1", "/etc/metric_proxy/labels/default"]]);
  });

  it("shows a name read for the host in every session's surface", async () => {
    const { result } = renderTwoSessions();
    act(() => void client.setQueryData(HOST_KEY, "dev-box.ssh"));
    await waitFor(() => expect(result.current.first).toBe("dev-box.ssh"));
    expect(result.current.second).toBe("dev-box.ssh");
  });

  it("reads it on showing when asked to, as a surface that offers it at once does", async () => {
    renderHook(() => useHostSshTarget("host_1", "conv_1", { resolve: true }), { wrapper });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  });

  it("has no SSH name, and reads again on the next ask, when the metadata can't be read", async () => {
    fetchMock.mockRejectedValue(new Error("Not found"));
    const { result } = renderTwoSessions();
    act(() => result.current.resolveFirst());
    await waitFor(() => expect(client.getQueryState(HOST_KEY)?.status).toBe("error"));
    expect(result.current.first).toBeNull();
    act(() => result.current.resolveFirst());
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });

  it("reads nothing for an offline host or one its provider names", async () => {
    stubHost({ status: "offline", sandbox_provider: null });
    const offline = renderTwoSessions();
    act(() => offline.result.current.resolveFirst());
    offline.unmount();
    stubHost({ status: "online", sandbox_provider: "example-provider" });
    const provided = renderTwoSessions();
    act(() => provided.result.current.resolveFirst());
    await act(async () => {});
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
