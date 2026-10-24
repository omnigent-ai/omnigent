import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { getCurrentIsAdmin, resolveIdentity } from "@/lib/identity";
import { useIsAdmin } from "./useIsAdmin";

vi.mock("@/lib/identity", () => ({ getCurrentIsAdmin: vi.fn(), resolveIdentity: vi.fn() }));
afterEach(cleanup);

it("updates admin navigation when identity resolves after the initial settings render", async () => {
  let finishIdentity!: (id: string) => void;
  vi.mocked(getCurrentIsAdmin).mockReturnValue(false);
  vi.mocked(resolveIdentity).mockReturnValue(
    new Promise((resolve) => {
      finishIdentity = resolve;
    }),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const { result } = renderHook(useIsAdmin, {
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
  expect(result.current).toBe(false);
  await act(async () => {
    vi.mocked(getCurrentIsAdmin).mockReturnValue(true);
    finishIdentity("local");
  });
  await waitFor(() => expect(result.current).toBe(true));
});
