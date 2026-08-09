import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/identity", () => ({
  authenticatedFetch: vi.fn(),
}));

const { authenticatedFetch } = await import("@/lib/identity");
const mockFetch = vi.mocked(authenticatedFetch);

import { useHostLocalSessions } from "./useHostLocalSessions";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  mockFetch.mockReset();
});

afterEach(() => {
  cleanup();
});

describe("useHostLocalSessions", () => {
  it("stays idle and does not fetch when hostId is null or enabled is false", async () => {
    const { result: noHost } = renderHook(() => useHostLocalSessions(null, "claude", true), {
      wrapper,
    });
    const { result: disabled } = renderHook(() => useHostLocalSessions("host_a", "claude", false), {
      wrapper,
    });
    await Promise.resolve();

    expect(noHost.current.fetchStatus).toBe("idle");
    expect(disabled.current.fetchStatus).toBe("idle");
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("resolves data to the fetched list when hostId is set and enabled is true", async () => {
    mockFetch.mockResolvedValue(
      jsonResponse({
        object: "list",
        data: [
          {
            source: "claude",
            external_session_id: "abc",
            workspace: "/repo",
            title: "inspect TODO.md",
            item_count: 4,
            preview: [{ role: "user", text: "inspect TODO.md" }],
          },
        ],
      }),
    );

    const { result } = renderHook(() => useHostLocalSessions("host_a", "claude", true), {
      wrapper,
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data).toEqual([
      {
        source: "claude",
        external_session_id: "abc",
        workspace: "/repo",
        title: "inspect TODO.md",
        item_count: 4,
        preview: [{ role: "user", text: "inspect TODO.md" }],
      },
    ]);
  });
});
