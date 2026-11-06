import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { getCurrentIsAdmin, resolveIdentity } from "@/lib/identity";
import { useIsAdmin } from "./useIsAdmin";

vi.mock("@/lib/identity", () => ({
  getCurrentIsAdmin: vi.fn(),
  resolveIdentity: vi.fn(),
}));

let client: QueryClient;
beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  vi.mocked(getCurrentIsAdmin).mockReset().mockReturnValue(false);
  vi.mocked(resolveIdentity).mockReset().mockResolvedValue("admin");
});
afterEach(() => {
  cleanup();
  client.clear();
});

const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={client}>{children}</QueryClientProvider>
);

it("updates admin access when identity resolves after the first render", async () => {
  let finishIdentity!: (userId: string) => void;
  vi.mocked(resolveIdentity).mockReturnValue(
    new Promise((resolve) => {
      finishIdentity = resolve;
    }),
  );
  const { result } = renderHook(useIsAdmin, { wrapper });
  expect(result.current).toBe(false);

  await act(async () => {
    vi.mocked(getCurrentIsAdmin).mockReturnValue(true);
    finishIdentity("admin");
  });

  await waitFor(() => expect(result.current).toBe(true));
});

it("shows cached admin access on the first render", async () => {
  vi.mocked(getCurrentIsAdmin).mockReturnValue(true);
  const { result } = renderHook(useIsAdmin, { wrapper });

  expect(result.current).toBe(true);
  await act(async () => {});
  expect(result.current).toBe(true);
});
