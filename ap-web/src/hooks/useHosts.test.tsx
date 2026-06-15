import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useHosts } from "./useHosts";

const fetchMock = vi.fn();

function mockResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Error",
    json: async () => body,
  } as unknown as Response;
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("useHosts", () => {
  it("does not fetch while disabled", async () => {
    renderHook(() => useHosts({ enabled: false }), { wrapper });
    await Promise.resolve();

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("fetches hosts from /v1/hosts", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        hosts: [
          {
            host_id: "host_1",
            name: "Laptop",
            owner: "alice",
            status: "online",
            sandbox_provider: null,
          },
        ],
      }),
    );

    const { result } = renderHook(() => useHosts(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(fetchMock.mock.calls[0][0]).toBe("/v1/hosts");
    expect(result.current.data).toEqual([
      {
        host_id: "host_1",
        name: "Laptop",
        owner: "alice",
        status: "online",
        sandbox_provider: null,
      },
    ]);
  });

  it("hides server-managed sandbox hosts from the host list", async () => {
    // Every host picker (NewChatDialog, ForkSessionDialog,
    // ResumeWithDirectoryDialog) consumes this hook, so filtering here
    // is what keeps sandbox-backed hosts out of all of them. A host
    // with a non-null sandbox_provider is server-managed (created from
    // a managed sandbox); one without the field at all comes from an
    // older server and must be kept.
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        hosts: [
          {
            host_id: "host_sandbox",
            name: "sandbox-abc123",
            owner: "alice",
            status: "online",
            sandbox_provider: "modal",
          },
          {
            host_id: "host_laptop",
            name: "Laptop",
            owner: "alice",
            status: "online",
            sandbox_provider: null,
          },
          {
            host_id: "host_legacy",
            name: "Old server host",
            owner: "alice",
            status: "offline",
          },
        ],
      }),
    );

    const { result } = renderHook(() => useHosts(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // The modal-backed host is dropped; the explicit-null and
    // field-absent hosts both survive. If host_sandbox appears, the
    // sandbox filter regressed; if host_legacy disappears, the filter
    // broke old-server compatibility.
    expect(result.current.data?.map((h) => h.host_id)).toEqual(["host_laptop", "host_legacy"]);
  });

  it("surfaces an error when the request fails", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ detail: "nope" }, 503));

    const { result } = renderHook(() => useHosts(), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(result.current.error).toBeInstanceOf(Error);
    expect((result.current.error as Error).message).toContain("503");
  });
});
