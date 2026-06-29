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

  it("retains sandbox hosts when includeSandbox is set", async () => {
    // The chat-header HostBadge needs sandbox-backed hosts so it can
    // label them ("Databricks Sandbox"); the default filtered call must
    // stay sandbox-free for the pickers.
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        hosts: [
          {
            host_id: "host_sandbox",
            name: "managed-abc123",
            owner: "alice",
            status: "online",
            sandbox_provider: "lakebox",
          },
          {
            host_id: "host_laptop",
            name: "Laptop",
            owner: "alice",
            status: "online",
            sandbox_provider: null,
          },
        ],
      }),
    );

    const { result } = renderHook(() => useHosts({ includeSandbox: true }), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data?.map((h) => h.host_id)).toEqual(["host_sandbox", "host_laptop"]);
  });

  it("keeps the filtered and unfiltered lists in separate cache entries", async () => {
    // The filtered (picker) and unfiltered (badge) calls share an
    // endpoint but use distinct query keys. If they ever collapsed back
    // to a bare ["hosts"] key, the second queryFn's result would clobber
    // the first in cache and both consumers would see the same list.
    // Mounting both in ONE QueryClient and asserting they diverge guards
    // that regression. mockResolvedValue (persistent) feeds both fetches.
    fetchMock.mockResolvedValue(
      mockResponse({
        hosts: [
          {
            host_id: "host_sandbox",
            name: "managed-abc123",
            owner: "alice",
            status: "online",
            sandbox_provider: "lakebox",
          },
          {
            host_id: "host_laptop",
            name: "Laptop",
            owner: "alice",
            status: "online",
            sandbox_provider: null,
          },
        ],
      }),
    );

    const { result } = renderHook(
      () => ({
        filtered: useHosts(),
        all: useHosts({ includeSandbox: true }),
      }),
      { wrapper },
    );
    await waitFor(() => {
      expect(result.current.filtered.isSuccess).toBe(true);
      expect(result.current.all.isSuccess).toBe(true);
    });

    expect(result.current.filtered.data?.map((h) => h.host_id)).toEqual(["host_laptop"]);
    expect(result.current.all.data?.map((h) => h.host_id)).toEqual(["host_sandbox", "host_laptop"]);
  });

  it("surfaces an error when the request fails", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ detail: "nope" }, 503));

    const { result } = renderHook(() => useHosts(), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(result.current.error).toBeInstanceOf(Error);
    expect((result.current.error as Error).message).toContain("503");
  });
});
