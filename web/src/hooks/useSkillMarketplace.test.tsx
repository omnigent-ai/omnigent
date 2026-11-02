import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useInstallSkills } from "./useSkillMarketplace";
import { useSkills } from "./useSkills";

const fetchMock = vi.fn();
const target = { hostId: "host", harness: "claude-native", path: "/repo" };
const review = { name: "review", description: "Review changes" };
const triage = { name: "triage", description: "Triage issues" };

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("marketplace mutations", () => {
  it("refetch a composer's still-fresh skill discovery after an install", async () => {
    let catalog = [review];
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input instanceof Request ? input.url : input);
      if (url.startsWith("/v1/skills")) {
        return Promise.resolve(new Response(JSON.stringify({ skills: catalog })));
      }
      catalog = [review, triage];
      return Promise.resolve(new Response(JSON.stringify({ data: [] }), { status: 201 }));
    });
    // One client across "navigation", as the app keeps it: discovery stays fresh for 30s.
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );

    const composer = renderHook(() => useSkills({ target }), { wrapper });
    await waitFor(() => expect(composer.result.current.skills).toEqual([review]));
    composer.unmount();

    const settings = renderHook(() => useInstallSkills(), { wrapper });
    await act(() =>
      settings.result.current.mutateAsync({ source: "acme/skills", names: ["triage"] }),
    );
    settings.unmount();

    const returned = renderHook(() => useSkills({ target }), { wrapper });
    await waitFor(() => expect(returned.result.current.skills).toEqual([review, triage]));
  });
});
