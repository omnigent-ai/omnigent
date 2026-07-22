import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/identity", () => ({
  authenticatedFetch: vi.fn(),
}));

import { authenticatedFetch } from "@/lib/identity";
import { useArtifactPreview } from "./useArtifactPreview";

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

describe("useArtifactPreview", () => {
  it("brokers a credentialless preview URL through authenticatedFetch", async () => {
    authenticatedFetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          url: "http://preview.localhost:6767/p/grant/artifacts/revenue/index.html",
          expires_at: 1234,
        }),
        { status: 201, headers: { "Content-Type": "application/json" } },
      ),
    );

    const { result } = renderHook(
      () => useArtifactPreview("conv_preview", "artifacts/revenue/index.html"),
      { wrapper },
    );

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(authenticatedFetchMock).toHaveBeenCalledWith(
      "/v1/sessions/conv_preview/artifact-previews",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ entry_path: "artifacts/revenue/index.html" }),
      },
    );
    expect(result.current.data?.url).toContain("preview.localhost");
  });
});
