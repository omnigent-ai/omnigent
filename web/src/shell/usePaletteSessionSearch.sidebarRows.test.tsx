// Typing in a session search that matches loaded sessions (Go to File's Sessions tab) also
// matches the sidebar's loaded rows: pinned, folder and list rows that the search's own recent
// list can miss. Mounted under the real SidebarDataProvider, so this holds however the hook
// reads those rows.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import { SidebarDataProvider, useSidebarData } from "@/hooks/useSidebarData";
import type * as CapabilitiesModule from "@/lib/CapabilitiesContext";
import { PINNED_LABEL_KEY } from "@/lib/sessionListCache";
import { sessionRowsPage } from "@/lib/sidebarData";
import { usePaletteSessionSearch } from "./usePaletteSessionSearch";

vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => null }));
vi.mock("@/hooks/useSessionUpdatesConnected", () => ({
  useSessionUpdatesConnected: () => true,
}));
vi.mock("@/lib/CapabilitiesContext", async (importOriginal) => ({
  ...(await importOriginal<typeof CapabilitiesModule>()),
  useServerInfo: () => ({ session_search_scope: true }),
}));

const row = (id: string, title: string, labels: Record<string, string> = {}): Conversation => ({
  id,
  permission_level: 4,
  object: "conversation",
  title,
  labels,
  created_at: 1,
  updated_at: 1,
});
const fetchMock = vi.fn();
let client: QueryClient;
beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  fetchMock.mockReset().mockImplementation((url: string) => {
    const parsed = new URL(url, "http://localhost");
    const params = parsed.searchParams;
    const visibility = params.get("visibility");
    let rows: Conversation[] = [];
    // The sidebar's own pins and list; the search's recent list (visibility=all) holds neither.
    if (params.has("search_query")) rows = [];
    else if (params.get("pinned") === "true")
      rows =
        visibility === "mine"
          ? [row("pin-kestrel", "Kestrel pinned plan", { [PINNED_LABEL_KEY]: "1" })]
          : [];
    else if (visibility === "mine") rows = [row("list-kestrel", "Kestrel sidebar notes")];
    else if (visibility === "all") rows = [row("recent", "Recent session")];
    const body: unknown = parsed.pathname.endsWith("/projects") ? [] : sessionRowsPage(rows);
    return Promise.resolve({ ok: true, status: 200, json: async () => body });
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  cleanup();
  client.clear();
  vi.unstubAllGlobals();
});

function renderSearch(sessionsOnly = false) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <SidebarDataProvider>{children}</SidebarDataProvider>
    </QueryClientProvider>
  );
  return renderHook(
    ({ open }: { open: boolean }) => ({
      search: usePaletteSessionSearch({
        open,
        sessionsOnly,
        matchLoaded: true,
        includeArchived: false,
      }),
      sidebar: useSidebarData(),
    }),
    { initialProps: { open: false }, wrapper },
  );
}

const ids = (sessions: { id: string }[]) => sessions.map((session) => session.id).sort();

/** Open the search once the sidebar has loaded, and wait for its own recent list, which
    replaces the sidebar rows it shows until then, before typing `query`. */
async function openAndType(view: ReturnType<typeof renderSearch>, query: string) {
  const { result, rerender } = view;
  await waitFor(() =>
    expect(ids(result.current.sidebar.loadedRows)).toEqual(["list-kestrel", "pin-kestrel"]),
  );
  rerender({ open: true });
  await waitFor(() => expect(ids(result.current.search.visibleSessions)).toEqual(["recent"]));
  act(() => result.current.search.setQuery(query));
}

it("matches the sidebar's loaded rows, opened after they load, as the user types", async () => {
  const view = renderSearch();
  await openAndType(view, "kestrel");
  const { result } = view;

  await waitFor(() =>
    expect(ids(result.current.search.visibleSessions)).toEqual(["list-kestrel", "pin-kestrel"]),
  );
});

it("adds a sidebar row that loads while the search is open", async () => {
  const view = renderSearch();
  await openAndType(view, "kestrel");
  const { result } = view;
  await waitFor(() =>
    expect(ids(result.current.search.visibleSessions)).toEqual(["list-kestrel", "pin-kestrel"]),
  );

  await act(async () =>
    result.current.sidebar.registerFolder("project", [row("folder-kestrel", "Kestrel folder row")]),
  );

  await waitFor(() =>
    expect(ids(result.current.search.visibleSessions)).toEqual([
      "folder-kestrel",
      "list-kestrel",
      "pin-kestrel",
    ]),
  );
});

it.each([false, true])(
  "fills the recent rows from the sidebar until the search's own list loads, sessions-only %s",
  async (sessionsOnly) => {
    let releaseRecent: () => void = () => {};
    const recentLoaded = new Promise<void>((resolve) => {
      releaseRecent = resolve;
    });
    const respond = fetchMock.getMockImplementation() as (url: string) => Promise<unknown>;
    fetchMock.mockImplementation(async (url: string) => {
      const params = new URL(url, "http://localhost").searchParams;
      const recentList =
        params.get("visibility") === "all" && !params.has("search_query") && !params.has("pinned");
      if (recentList) await recentLoaded;
      return respond(url);
    });
    const view = renderSearch(sessionsOnly);
    const { result, rerender } = view;
    await waitFor(() =>
      expect(ids(result.current.sidebar.loadedRows)).toEqual(["list-kestrel", "pin-kestrel"]),
    );
    rerender({ open: true });
    await waitFor(() =>
      expect(ids(result.current.search.visibleSessions)).toEqual(
        sessionsOnly ? [] : ["list-kestrel", "pin-kestrel"],
      ),
    );

    await act(async () => releaseRecent());
    await waitFor(() => expect(ids(result.current.search.visibleSessions)).toEqual(["recent"]));
  },
);
