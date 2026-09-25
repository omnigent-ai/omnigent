import { QueryClient, QueryClientProvider, focusManager } from "@tanstack/react-query";
import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { useArchivedSessions } from "./useScopeCache";

const archivedKey = ["conversations", "", false, null, "archived"];

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  focusManager.setFocused(undefined);
});

it("fetches on each entry while retaining cached rows and never polling", async () => {
  vi.useFakeTimers();
  const client = new QueryClient({ defaultOptions: { queries: { gcTime: Infinity } } });
  const response = (title: string) =>
    new Response(
      JSON.stringify({
        data: [{ id: "archived-session", title, created_at: 1, updated_at: 1, archived: true }],
        has_more: false,
      }),
    );
  const fetch = vi.fn().mockImplementation(async () => response("Original title"));
  vi.stubGlobal("fetch", fetch);
  const { result, rerender } = renderHook(({ enabled }) => ({ ...useArchivedSessions(enabled) }), {
    initialProps: { enabled: false },
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
  expect(fetch).not.toHaveBeenCalled();
  rerender({ enabled: true });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10);
  });
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(result.current.data?.pages[0].data[0].title).toBe("Original title");
  await act(async () => {
    await vi.advanceTimersByTimeAsync(600_000);
    focusManager.setFocused(false);
    focusManager.setFocused(true);
    await client.invalidateQueries({ queryKey: archivedKey });
    await vi.advanceTimersByTimeAsync(10);
  });
  expect(fetch).toHaveBeenCalledTimes(1);

  rerender({ enabled: false });
  let finish!: (response: Response) => void;
  fetch.mockImplementationOnce(
    () =>
      new Promise<Response>((resolve) => {
        finish = resolve;
      }),
  );
  rerender({ enabled: true });
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10);
  });
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(result.current.data?.pages[0].data[0].title).toBe("Original title");
  expect(result.current.isFetching).toBe(true);
  await act(async () => {
    finish(response("Renamed on another client"));
    await vi.advanceTimersByTimeAsync(10);
  });
  expect(result.current.data?.pages[0].data[0].title).toBe("Renamed on another client");
  await act(async () => {
    await vi.advanceTimersByTimeAsync(600_000);
  });
  expect(fetch).toHaveBeenCalledTimes(2);
  client.clear();
});
