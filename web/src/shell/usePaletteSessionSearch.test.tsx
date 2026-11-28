import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import type * as ConversationsModule from "@/hooks/useConversations";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { usePaletteSessionSearch } from "./usePaletteSessionSearch";

const useConversations = vi.fn();
const useSessionSearch = vi.fn();
const useProjects = vi.fn();
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ session_search_scope: true }),
}));
vi.mock("@/hooks/useConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof ConversationsModule>()),
  useConversations: (...args: unknown[]) => useConversations(...args),
  useSessionSearch: (...args: unknown[]) => useSessionSearch(...args),
  useProjects: (...args: unknown[]) => useProjects(...args),
}));

function conv(id: string, title: string) {
  return { id, title, agent_name: null, archived: false, search_snippet: null };
}

function renderSearch(
  props: {
    open: boolean;
    sessionsOnly: boolean;
    matchLoaded?: boolean;
    includeArchived?: boolean;
  },
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
  useProjects.mockReturnValue({ data: [] });
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
      includeArchived: true,
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
      includeArchived: true,
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

  it.each([false, true])(
    "remembers search rows, and recent rows only from ⌘K's own list (sessionsOnly=%s)",
    (sessionsOnly) => {
      useConversations.mockImplementation((_query: string, includeArchived: boolean) => ({
        data: {
          pages: [{ data: [conv(includeArchived ? "recent" : "sidebar", "Recent session")] }],
        },
        isFetching: false,
      }));
      useSessionSearch.mockImplementation((_query: string, scope: string) => ({
        isFetching: false,
        data:
          scope === "title" ? { pages: [{ data: [conv("found", "Found session")] }] } : undefined,
      }));
      const queryClient = new QueryClient();
      const { result } = renderSearch({ open: true, sessionsOnly }, queryClient);

      act(() => {
        result.current.rememberSearchRow("found");
        result.current.rememberSearchRow("recent");
        result.current.rememberSearchRow("sidebar");
      });
      expect(queryClient.getQueryData(["conversation-backfill", "found"])).toMatchObject({
        id: "found",
      });
      expect(queryClient.getQueryData(["conversation-backfill", "recent"]) !== undefined).toBe(
        !sessionsOnly,
      );
      expect(queryClient.getQueryData(["conversation-backfill", "sidebar"])).toBeUndefined();
    },
  );

  describe("a session picker's archived policy", () => {
    const archivedHit = { ...conv("old", "Kestrel archive"), archived: true };
    const activeHit = conv("new", "Kestrel notes");
    const searchFor = (includeArchived?: boolean) => {
      // Both groups answer with an archived match, as a server that ignores the filter would.
      useSessionSearch.mockImplementation((query: string, scope: string) =>
        query === ""
          ? { isFetching: false }
          : {
              isFetching: false,
              data: {
                pages: [{ data: scope === "title" ? [archivedHit, activeHit] : [archivedHit] }],
              },
            },
      );
      const { result } = renderSearch({
        open: true,
        sessionsOnly: false,
        matchLoaded: true,
        includeArchived,
      });
      act(() => result.current.setQuery("kestrel"));
      act(() => {
        vi.advanceTimersByTime(300);
      });
      return result;
    };
    const ids = (sessions: { id: string }[]) => sessions.map((session) => session.id);

    it("asks both groups for active sessions only and lists none archived, for Archive", () => {
      const result = searchFor(false);
      for (const scope of ["title", "content"]) {
        expect(useSessionSearch).toHaveBeenCalledWith(
          "kestrel",
          scope,
          expect.objectContaining({ enabled: true, includeArchived: false }),
        );
      }
      expect(ids(result.current.visibleSessions)).toEqual(["new"]);
      expect(ids(result.current.contentSessions)).toEqual([]);
    });

    it.each([true, undefined])(
      "keeps the archived matches where archived sessions apply (includeArchived=%s)",
      (includeArchived) => {
        const result = searchFor(includeArchived);
        expect(useSessionSearch).toHaveBeenLastCalledWith(
          "kestrel",
          "content",
          expect.objectContaining({ includeArchived: true }),
        );
        expect(ids(result.current.visibleSessions)).toEqual(["old", "new"]);
      },
    );
  });

  describe("matching loaded sessions", () => {
    const loaded = (...rows: ReturnType<typeof conv>[]) =>
      useConversations.mockReturnValue({ data: { pages: [{ data: rows }] }, isFetching: false });
    const titleHits = (...rows: ReturnType<typeof conv>[]) =>
      useSessionSearch.mockImplementation((query: string, scope: string) =>
        scope === "title" && query !== ""
          ? { isFetching: false, data: { pages: [{ data: rows.map((row) => ({ ...row })) }] } }
          : { isFetching: false },
      );
    const ids = (sessions: { id: string }[]) => sessions.map((session) => session.id);

    it("searches the server for an ordinary link, which names no pull request", () => {
      const { result } = renderSearch({ open: true, sessionsOnly: false, matchLoaded: true });
      act(() => result.current.setQuery("https://example.test/design"));
      act(() => {
        vi.advanceTimersByTime(300);
      });
      expect(result.current.pullRequestLookup).toBe(false);
      expect(result.current.searching).toBe(true);
      expect(useSessionSearch).toHaveBeenLastCalledWith("https://example.test/design", "content", {
        enabled: true,
        limit: 8,
        includeArchived: true,
      });
    });

    it.each(["#42", "https://github.com/acme/app/pull/42/files"])(
      "looks %s up among the loaded sessions, never asking the server",
      (reference) => {
        const { result } = renderSearch({ open: true, sessionsOnly: false, matchLoaded: true });
        act(() => result.current.setQuery(reference));
        act(() => {
          vi.advanceTimersByTime(300);
        });
        expect(result.current.pullRequestLookup).toBe(true);
        for (const call of useSessionSearch.mock.calls) {
          expect(call[2]).toMatchObject({ enabled: false });
        }
      },
    );

    it.each([
      [undefined, ["c2", "c1"]],
      [false, ["c1"]],
    ])(
      "matches archived loaded sessions at once where the search keeps them (includeArchived=%s)",
      (includeArchived, expected) => {
        loaded(conv("c1", "Kestrel plan"), { ...conv("c2", "Kestrel"), archived: true });
        const { result } = renderSearch({
          open: true,
          sessionsOnly: false,
          matchLoaded: true,
          includeArchived,
        });
        act(() => result.current.setQuery("kestrel"));
        expect(ids(result.current.sessions)).toEqual(expected);
      },
    );

    it("lists only the new query's loaded matches until the server answers it", () => {
      loaded(conv("c1", "Kestrel plan"));
      titleHits(conv("s1", "Kestrel notes"));
      const { result } = renderSearch({ open: true, sessionsOnly: false, matchLoaded: true });
      act(() => result.current.setQuery("kestrel"));
      act(() => {
        vi.advanceTimersByTime(300);
      });
      expect(ids(result.current.visibleSessions)).toEqual(["c1", "s1"]);
      // The server's "kestrel" hits don't answer "kestrel plan".
      act(() => result.current.setQuery("kestrel plan"));
      expect(ids(result.current.visibleSessions)).toEqual(["c1"]);
    });

    describe("shows the server's matches only for the query they answer", () => {
      // Each query's server matches, by scope; any other query has none.
      const serverHits = (hits: Record<string, { title?: string[]; content?: string[] }>) =>
        useSessionSearch.mockImplementation((query: string, scope: "title" | "content") => {
          const found = hits[query]?.[scope] ?? [];
          return {
            isFetching: false,
            data: { pages: [{ data: found.map((id) => conv(id, `Server ${id}`)) }] },
          };
        });
      const settle = () =>
        act(() => {
          vi.advanceTimersByTime(300);
        });
      const shown = (result: { current: ReturnType<typeof usePaletteSessionSearch> }) => ({
        sessions: ids(result.current.visibleSessions),
        content: ids(result.current.contentSessions),
        serverMatches: result.current.showingServerMatches,
      });

      beforeEach(() => {
        loaded(conv("kestrel-local", "Kestrel plan"), conv("parser-local", "Parser rewrite"));
        serverHits({
          kestrel: { title: ["kestrel-title"], content: ["kestrel-content"] },
          parser: { title: ["parser-title"], content: ["parser-content"] },
        });
      });

      it("drops the last query's title and content matches until the new one is answered", () => {
        const { result } = renderSearch({ open: true, sessionsOnly: false, matchLoaded: true });
        act(() => result.current.setQuery("kestrel"));
        settle();
        expect(shown(result)).toEqual({
          sessions: ["kestrel-local", "kestrel-title"],
          content: ["kestrel-content"],
          serverMatches: true,
        });
        act(() => result.current.setQuery("parser"));
        expect(shown(result)).toEqual({
          sessions: ["parser-local"],
          content: [],
          serverMatches: false,
        });
        settle();
        expect(shown(result)).toEqual({
          sessions: ["parser-local", "parser-title"],
          content: ["parser-content"],
          serverMatches: true,
        });
      });

      it("lists the recent sessions once the query is cleared, not the last query's matches", () => {
        const { result } = renderSearch({ open: true, sessionsOnly: false, matchLoaded: true });
        act(() => result.current.setQuery("kestrel"));
        settle();
        act(() => result.current.setQuery(""));
        expect(shown(result)).toEqual({
          sessions: ["kestrel-local", "parser-local"],
          content: [],
          serverMatches: false,
        });
        settle();
        expect(shown(result).sessions).toEqual(["kestrel-local", "parser-local"]);
      });

      it("keeps the classic palette's last server matches until the new query is answered", () => {
        const { result } = renderSearch({ open: true, sessionsOnly: false });
        act(() => result.current.setQuery("kestrel"));
        settle();
        act(() => result.current.setQuery("parser"));
        expect(shown(result)).toEqual({
          sessions: ["kestrel-title"],
          content: ["kestrel-content"],
          serverMatches: true,
        });
      });
    });

    it("lists at most a search group's page of loaded matches", () => {
      loaded(...Array.from({ length: 20 }, (_, index) => conv(`c${index}`, `Alpha ${index}`)));
      const { result } = renderSearch({ open: true, sessionsOnly: false, matchLoaded: true });
      act(() => result.current.setQuery("alpha"));
      expect(result.current.visibleSessions).toHaveLength(8);
    });
  });
});
