import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useSessionAgent } from "./useAgents";

const fetchMock = vi.fn();

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

describe("useSessionAgent", () => {
  it.each([
    { wire: { loads_user_settings: false }, expected: false },
    // Older servers omit the field, which the composer reads as loading user settings.
    { wire: {}, expected: undefined },
  ])("keeps whether the agent loads user settings: %j", async ({ wire, expected }) => {
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      statusText: "OK",
      json: async () => ({ id: "ag_1", object: "agent", name: "sdk", ...wire }),
    } as unknown as Response);

    const { result } = renderHook(() => useSessionAgent("conv_1"), { wrapper });

    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(fetchMock.mock.calls[0][0]).toBe("/v1/sessions/conv_1/agent");
    expect(result.current.data?.loads_user_settings).toBe(expected);
  });
});
