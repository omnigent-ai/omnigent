import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, expect, it, vi } from "vitest";

const identity = vi.hoisted(() => ({
  isAdmin: false,
  listeners: new Set<() => void>(),
}));

vi.mock("@/lib/identity", () => ({
  getCurrentIsAdmin: () => identity.isAdmin,
  resolveIdentity: () => Promise.resolve(null),
  subscribeIdentity: (listener: () => void) => {
    identity.listeners.add(listener);
    return () => {
      identity.listeners.delete(listener);
    };
  },
}));

import { useIsAdmin } from "./useIsAdmin";

let client: QueryClient;

beforeEach(() => {
  identity.isAdmin = false;
  identity.listeners.clear();
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

it("follows an admin flag that settles after the boot probe failed", async () => {
  const { result } = renderHook(useIsAdmin, { wrapper });
  await act(async () => {});
  expect(result.current).toBe(false);

  identity.isAdmin = true;
  act(() => {
    for (const listener of identity.listeners) listener();
  });
  await waitFor(() => expect(result.current).toBe(true));
});

it("stops listening on unmount", () => {
  const { unmount } = renderHook(useIsAdmin, { wrapper });
  expect(identity.listeners.size).toBe(1);

  unmount();
  expect(identity.listeners.size).toBe(0);
});
