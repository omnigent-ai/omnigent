import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionRunnerOnline: () => true,
}));

import { authenticatedFetch } from "@/lib/identity";
import { useManagedArtifacts } from "./useManagedArtifacts";

const authenticatedFetchMock = vi.mocked(authenticatedFetch);

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  authenticatedFetchMock.mockReset();
});

describe("useManagedArtifacts", () => {
  it("lists session artifacts through the dedicated managed endpoint", async () => {
    authenticatedFetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          object: "list",
          data: [
            {
              path: "artifacts/revenue/index.html",
              name: "index.html",
              type: "file",
              bytes: 200,
              modified_at: 2,
            },
          ],
          has_more: false,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    const { result } = renderHook(() => useManagedArtifacts("conv_preview"), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(authenticatedFetchMock).toHaveBeenCalledWith("/v1/sessions/conv_preview/artifacts");
    expect(result.current.data?.[0]?.path).toBe("artifacts/revenue/index.html");
  });
});
