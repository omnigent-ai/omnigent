import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { usePaletteSessionSearch } from "./usePaletteSessionSearch";

const useConversations = vi.fn();
const useSessionSearch = vi.fn();
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ session_search_scope: true }),
}));
vi.mock("@/hooks/useConversations", () => ({
  useConversations: (...args: unknown[]) => useConversations(...args),
  useSessionSearch: (...args: unknown[]) => useSessionSearch(...args),
}));

function conv(id: string, title: string) {
  return { id, title, agent_name: null, archived: false, search_snippet: null };
}

function renderSearch(
  props: { open: boolean; sessionsOnly: boolean },
  queryClient = new QueryClient(),
) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return renderHook((current: typeof props) => usePaletteSessionSearch(current), {
    initialProps: props,
    wrapper,
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  useConversations.mockReset();
  useSessionSearch.mockReset();
  useConversations.mockReturnValue({ data: { pages: [{ data: [] }] }, isFetching: false });
  useSessionSearch.mockReturnValue({ isFetching: false });
});
afterEach(() => vi.useRealTimers());

describe("usePaletteSessionSearch", () => {
  it("debounces the query into a search and clears it when the palette closes", () => {
    const { result, rerender } = renderSearch({ open: true, sessionsOnly: false });
    act(() => result.current.setQuery("deploy"));
    expect(result.current.searching).toBe(false);
    act(() => {
      vi.advanceTimersByTime(300);
    });
    expect(result.current.debouncedQuery).toBe("deploy");
    expect(result.current.searching).toBe(true);
    expect(useSessionSearch).toHaveBeenLastCalledWith("deploy", "content", {
      enabled: true,
      limit: 8,
    });

    rerender({ open: false, sessionsOnly: false });
    expect(result.current.query).toBe("");
    expect(result.current.debouncedQuery).toBe("");
  });

  it("never searches the server when listing sessions only", () => {
    const { result } = renderSearch({ open: true, sessionsOnly: true });
    act(() => result.current.setQuery("deploy"));
    act(() => {
      vi.advanceTimersByTime(300);
    });
    expect(result.current.searching).toBe(false);
    expect(useSessionSearch).toHaveBeenLastCalledWith("deploy", "content", {
      enabled: false,
      limit: 8,
    });
  });

  it("offers older sessions at the paging bound and raises it on request", () => {
    const fetchNextPage = vi.fn();
    const pages = Array.from({ length: 10 }, (_, index) => ({
      data: [conv(`session-${index}`, `Session ${index}`)],
    }));
    useConversations.mockReturnValue({
      data: { pages },
      isFetching: false,
      hasNextPage: true,
      fetchNextPage,
    });
    const { result } = renderSearch({ open: true, sessionsOnly: true });
    expect(result.current.olderSessionsAvailable).toBe(true);
    expect(fetchNextPage).not.toHaveBeenCalled();

    act(() => result.current.searchOlderSessions());
    expect(result.current.olderSessionsAvailable).toBe(false);
    expect(fetchNextPage).toHaveBeenCalledTimes(1);
  });

  it("remembers only rows that came from a search", () => {
    useConversations.mockReturnValue({
      data: { pages: [{ data: [conv("recent", "Recent session")] }] },
      isFetching: false,
    });
    useSessionSearch.mockImplementation((_query: string, scope: string) => ({
      isFetching: false,
      data: scope === "title" ? { pages: [{ data: [conv("found", "Found session")] }] } : undefined,
    }));
    const queryClient = new QueryClient();
    const { result } = renderSearch({ open: true, sessionsOnly: false }, queryClient);

    act(() => {
      result.current.rememberSearchRow("found");
      result.current.rememberSearchRow("recent");
    });
    expect(queryClient.getQueryData(["conversation-backfill", "found"])).toMatchObject({
      id: "found",
    });
    expect(queryClient.getQueryData(["conversation-backfill", "recent"])).toBeUndefined();
  });
});
