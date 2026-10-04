import { renderHook, waitFor, cleanup } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import { useSkillContent } from "./useSkillContent";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it("fetches an encoded skill only while enabled, with no browser cache", async () => {
  vi.mocked(authenticatedFetch).mockResolvedValue(
    Response.json({ name: "plugin:a/b", description: "", content: "body", truncated: false }),
  );
  const client = new QueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const { result, rerender, unmount } = renderHook(
    ({ enabled }) => useSkillContent("host id", "claude-native", "plugin:a/b", { enabled }),
    { wrapper, initialProps: { enabled: false } },
  );
  expect(authenticatedFetch).not.toHaveBeenCalled();
  rerender({ enabled: true });
  await waitFor(() => expect(result.current.data?.content).toBe("body"));
  expect(authenticatedFetch).toHaveBeenCalledWith(
    "/v1/hosts/host%20id/harnesses/claude-native/skills/plugin%3Aa%2Fb",
    expect.objectContaining({ cache: "no-store", signal: expect.any(AbortSignal) }),
  );
  unmount();
  await waitFor(() => expect(client.getQueryCache().getAll()).toHaveLength(0));
});
