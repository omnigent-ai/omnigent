import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useFileContent", () => ({ fetchFileContent: vi.fn() }));
vi.mock("@/hooks/useHosts", () => ({ useHosts: vi.fn() }));

import { fetchFileContent } from "@/hooks/useFileContent";
import { useHosts } from "@/hooks/useHosts";
import { useHostSshTarget, useHostSshTargetDemand } from "./useHostSshTarget";

const fetchMock = vi.mocked(fetchFileContent);
const LABELS = "/etc/metric_proxy/labels/default";
const HOST_KEY = ["hostSshTarget", "host_1"];

let client: QueryClient;

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function stubHost(host: { status: string; sandbox_provider?: string | null } | null) {
  vi.mocked(useHosts).mockReturnValue({
    data: host ? [{ host_id: "host_1", name: "dev box", ...host }] : undefined,
  } as ReturnType<typeof useHosts>);
}

function metadataFile(content: string) {
  return {
    object: "session.environment.filesystem.file_content",
    encoding: "utf-8",
    truncated: false,
    content,
  } as Awaited<ReturnType<typeof fetchFileContent>>;
}

/** Metadata that names no machine type: the read finishes with no SSH name. */
function serveUnnamedMachine() {
  fetchMock.mockResolvedValue(metadataFile("region example\n"));
}

/** Two sessions on one host showing its SSH name; the first wants it while `wanted`. */
function renderTwoSessions(wanted = false) {
  return renderHook(
    ({ wanted: wantedNow }: { wanted: boolean }) => {
      useHostSshTargetDemand("host_1", "conv_1", wantedNow);
      return {
        first: useHostSshTarget("host_1", "conv_1"),
        second: useHostSshTarget("host_1", "conv_2"),
      };
    },
    { wrapper, initialProps: { wanted } },
  );
}

const labelReads = () => fetchMock.mock.calls.filter(([, path]) => path === LABELS);

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  fetchMock.mockReset();
  serveUnnamedMachine();
  stubHost({ status: "online", sandbox_provider: null });
});

afterEach(() => vi.useRealTimers());

describe("useHostSshTarget", () => {
  it("reads nothing from the host while surfaces only show its SSH name", async () => {
    renderTwoSessions();
    await act(async () => {});
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("reads the host's metadata once when wanted, for every session on it", async () => {
    const { rerender } = renderTwoSessions();
    rerender({ wanted: true });
    await waitFor(() => expect(client.getQueryState(HOST_KEY)?.status).toBe("success"));
    rerender({ wanted: false });
    rerender({ wanted: true });
    await act(async () => {});
    expect(fetchMock.mock.calls.map(([conversationId, path]) => [conversationId, path])).toEqual([
      ["conv_1", LABELS],
    ]);
  });

  it("shows a name read for the host in every session's surface", async () => {
    const { result } = renderTwoSessions();
    act(() => void client.setQueryData(HOST_KEY, "dev-box.ssh"));
    await waitFor(() => expect(result.current.first).toBe("dev-box.ssh"));
    expect(result.current.second).toBe("dev-box.ssh");
  });

  it("reads once the host is known when it was wanted before the host list loaded", async () => {
    stubHost(null);
    const { rerender } = renderTwoSessions(true);
    await act(async () => {});
    expect(fetchMock).not.toHaveBeenCalled();
    stubHost({ status: "online", sandbox_provider: null });
    rerender({ wanted: true });
    await waitFor(() => expect(labelReads()).toHaveLength(1));
  });

  it("reads it on showing when asked to, as a surface that offers it at once does", async () => {
    renderHook(() => useHostSshTarget("host_1", "conv_1", { resolve: true }), { wrapper });
    await waitFor(() => expect(labelReads()).toHaveLength(1));
  });

  it("reads again when wanted after a while, so a host that changed shows its new name", async () => {
    const { rerender } = renderTwoSessions(true);
    await waitFor(() => expect(client.getQueryState(HOST_KEY)?.status).toBe("success"));
    rerender({ wanted: false });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(Date.now() + 5 * 60_000 + 1);
    rerender({ wanted: true });
    await waitFor(() => expect(labelReads()).toHaveLength(2));
  });

  it("has no SSH name, and reads again when next wanted, when the metadata can't be read", async () => {
    fetchMock.mockRejectedValue(new Error("Not found"));
    const { result, rerender } = renderTwoSessions(true);
    await waitFor(() => expect(client.getQueryState(HOST_KEY)?.status).toBe("error"));
    expect(result.current.first).toBeNull();
    rerender({ wanted: false });
    rerender({ wanted: true });
    await waitFor(() => expect(labelReads()).toHaveLength(2));
  });

  it("reads a failed host again through the session whose menu asks next", async () => {
    fetchMock.mockRejectedValue(new Error("This session can't read the file."));
    const { rerender } = renderHook(
      ({ session, wanted }: { session: string; wanted: boolean }) =>
        useHostSshTargetDemand("host_1", session, wanted),
      { wrapper, initialProps: { session: "conv_1", wanted: true } },
    );
    await waitFor(() => expect(client.getQueryState(HOST_KEY)?.status).toBe("error"));
    serveUnnamedMachine();
    // A menu in another session on the host closes, then opens.
    rerender({ session: "conv_2", wanted: false });
    rerender({ session: "conv_2", wanted: true });
    await waitFor(() => expect(client.getQueryState(HOST_KEY)?.status).toBe("success"));
    expect(labelReads().map(([conversationId]) => conversationId)).toEqual(["conv_1", "conv_2"]);
  });

  it("reads nothing for an offline host or one its provider names", async () => {
    stubHost({ status: "offline", sandbox_provider: null });
    renderTwoSessions(true).unmount();
    stubHost({ status: "online", sandbox_provider: "example-provider" });
    renderTwoSessions(true);
    await act(async () => {});
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
