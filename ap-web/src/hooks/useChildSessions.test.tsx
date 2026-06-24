import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useChildSessions } from "./useChildSessions";

const fetchMock = vi.fn();

function row(id: string) {
  return {
    id,
    title: id,
    tool: "agent",
    session_name: id,
    current_task_status: null,
    busy: false,
    last_message_preview: null,
    pending_elicitations_count: 0,
  };
}

function response(ids: string[]): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => ({ object: "list", data: ids.map(row) }),
  } as unknown as Response;
}

function routeFetch(routes: Record<string, Response>) {
  fetchMock.mockImplementation((url: string) => {
    const route = routes[url];
    if (!route) return Promise.reject(new Error(`unrouted fetch in test: ${url}`));
    return Promise.resolve(route);
  });
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
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

describe("useChildSessions", () => {
  it("fetches only direct children by default", async () => {
    routeFetch({
      "/v1/sessions/root/child_sessions": response(["coord_a", "coord_b"]),
    });

    const { result } = renderHook(() => useChildSessions("root"), { wrapper });

    await waitFor(() =>
      expect(result.current.children.map((child) => child.id)).toEqual(["coord_a", "coord_b"]),
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("includes bounded descendants when requested", async () => {
    routeFetch({
      "/v1/sessions/root/child_sessions": response(["coord_a", "coord_b"]),
      "/v1/sessions/coord_a/child_sessions": response(["a1", "a2"]),
      "/v1/sessions/coord_b/child_sessions": response(["b1", "b2"]),
      "/v1/sessions/a1/child_sessions": response([]),
      "/v1/sessions/a2/child_sessions": response([]),
      "/v1/sessions/b1/child_sessions": response([]),
      "/v1/sessions/b2/child_sessions": response([]),
    });

    const { result } = renderHook(() => useChildSessions("root", null, true), { wrapper });

    await waitFor(() =>
      expect(result.current.children.map((child) => child.id)).toEqual([
        "coord_a",
        "coord_b",
        "a1",
        "a2",
        "b1",
        "b2",
      ]),
    );
  });
});
