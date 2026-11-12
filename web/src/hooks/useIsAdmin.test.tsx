import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { getCurrentIsAdmin, resolveIdentity } from "@/lib/identity";
import { useIsAdmin } from "./useIsAdmin";

vi.mock("@/lib/identity", () => ({
  getCurrentIsAdmin: vi.fn(),
  resolveIdentity: vi.fn(),
}));

beforeEach(() => vi.resetAllMocks());
afterEach(cleanup);

it.each([true, false])(
  "resolves a pending identity to admin=%s without waiting for cache expiry",
  async (isAdmin) => {
    let finishIdentity!: (id: string) => void;
    const pendingIdentity = new Promise<string>((resolve) => {
      finishIdentity = resolve;
    });
    vi.mocked(getCurrentIsAdmin).mockReturnValue(false);
    vi.mocked(resolveIdentity).mockReturnValue(pendingIdentity);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useIsAdmin(), {
      wrapper: ({ children }) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      ),
    });

    expect(result.current).toBe(false);
    expect(resolveIdentity).toHaveBeenCalledOnce();
    vi.mocked(getCurrentIsAdmin).mockReturnValue(isAdmin);
    await act(async () => {
      finishIdentity("viewer");
      await pendingIdentity;
    });
    expect(client.getQueryData(["identity-is-admin"])).toBe(isAdmin);
    expect(client.getQueryState(["identity-is-admin"])?.dataUpdatedAt).toBeGreaterThan(0);
  },
);
