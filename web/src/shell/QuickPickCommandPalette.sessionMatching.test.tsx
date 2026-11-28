// The QuickPick palette matches loaded sessions as the user types, and finds the session
// linked to a pasted `#123` or pull request link.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ContextType } from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SidebarDataContext } from "@/hooks/useSidebarData";

import { PULL_REQUEST_NOT_FOUND, QuickPickCommandPalette } from "./QuickPickCommandPalette";

const navigate = vi.fn();
vi.mock("@/lib/routing", () => ({
  useNavigate: () => navigate,
  useLocation: () => ({ pathname: "/", search: "", hash: "", state: null, key: "default" }),
}));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ session_search_scope: true }),
}));
const useConversations = vi.fn();
const useSessionSearch = vi.fn((..._args: unknown[]): unknown => ({ isFetching: false }));
vi.mock("@/hooks/useConversations", () => ({
  useConversations: (...args: unknown[]) => useConversations(...args),
  useSessionSearch: (...args: unknown[]) => useSessionSearch(...args),
  useProjects: () => ({ data: [] }),
}));

beforeEach(() => {
  navigate.mockClear();
});
afterEach(() => {
  cleanup();
  localStorage.clear();
  useSessionSearch.mockImplementation(() => ({ isFetching: false }));
});

/** Only the sidebar's loaded and pinned rows, the parts the palette reads. */
function sidebarDataWith(loadedRows: Record<string, unknown>[]) {
  return { loadedRows, pinned: { data: { conversations: [] } } } as unknown as ContextType<
    typeof SidebarDataContext
  >;
}

function renderPalette(sidebarRows?: Record<string, unknown>[]) {
  const sidebarData = sidebarRows ? sidebarDataWith(sidebarRows) : null;
  const palette = (
    <QuickPickCommandPalette
      open
      onOpenChange={vi.fn()}
      onToggleLeftSidebar={vi.fn()}
      onToggleRightSidebar={vi.fn()}
    />
  );
  render(
    <QueryClientProvider client={new QueryClient()}>
      {sidebarData ? (
        <SidebarDataContext.Provider value={sidebarData}>{palette}</SidebarDataContext.Provider>
      ) : (
        palette
      )}
    </QueryClientProvider>,
  );
  return { input: screen.getByTestId("command-palette-input") };
}

describe("QuickPickCommandPalette session matching", () => {
  const sessionNames = () =>
    within(screen.getByRole("group", { name: "Sessions" }))
      .getAllByRole("option")
      .map((option) => option.textContent ?? "");
  const listLoaded = (rows: Record<string, unknown>[]) =>
    useConversations.mockReturnValue({ data: { pages: [{ data: rows }] }, isFetching: false });
  const linked = (number: number) => ({
    url: `https://github.com/acme/app/pull/${number}`,
    provider: "github",
    repository: "acme/app",
    number,
  });

  it("lists a loaded session as soon as the query matches it, marking each matched letter", () => {
    listLoaded([
      { id: "c1", title: "Fix the parser", agent_name: null, updated_at: 1 },
      { id: "c2", title: "Deploy API", agent_name: null, updated_at: 2 },
    ]);
    const { input } = renderPalette();
    fireEvent.change(input, { target: { value: "fxprs" } });
    // Before the debounce lets the server search: the loaded session already shows.
    expect(sessionNames()).toEqual([expect.stringContaining("Fix the parser")]);
    const marks = screen.getByRole("option", { name: /Fix the parser/ }).querySelectorAll("mark");
    expect([...marks].map((mark) => mark.textContent).join("")).toBe("Fxprs");
  });

  it("puts loaded matches first, then the server's other title matches, each once", async () => {
    listLoaded([{ id: "c1", title: "Kestrel notes", agent_name: null }]);
    useSessionSearch.mockImplementation((_query: unknown, scope: unknown) =>
      scope === "content"
        ? { isFetching: false }
        : {
            isFetching: false,
            data: {
              pages: [
                {
                  data: [
                    { id: "k9", title: "Kestrel plan", agent_name: null },
                    { id: "c1", title: "Kestrel notes", agent_name: null },
                  ],
                },
              ],
            },
          },
    );
    const { input } = renderPalette();
    fireEvent.change(input, { target: { value: "kestrel" } });
    await waitFor(() => expect(sessionNames()).toHaveLength(2));
    expect(sessionNames()[0]).toContain("Kestrel notes");
    expect(sessionNames()[1]).toContain("Kestrel plan");
  });

  it("keeps a loaded session archived since it loaded, marked archived", () => {
    listLoaded([{ id: "c1", title: "Kestrel notes", agent_name: null, archived: true }]);
    const { input } = renderPalette();
    fireEvent.change(input, { target: { value: "kestrel" } });
    expect(sessionNames()).toEqual([expect.stringContaining("Archived")]);
  });

  it.each(["#42", "https://github.com/acme/app/pull/42/files"])(
    "finds the session linked to %s and opens it",
    (reference) => {
      listLoaded([
        { id: "c1", title: "Parser fix", agent_name: null, pull_request: linked(42) },
        { id: "c2", title: "Other", agent_name: null, pull_request: linked(7) },
      ]);
      const { input } = renderPalette();
      fireEvent.change(input, { target: { value: reference } });
      expect(sessionNames()).toEqual([expect.stringContaining("Parser fix")]);
      fireEvent.keyDown(input, { key: "Enter" });
      expect(navigate).toHaveBeenCalledWith("/c/c1");
    },
  );

  it("says when no loaded session links the pull request", () => {
    listLoaded([{ id: "c1", title: "Parser fix", agent_name: null, pull_request: linked(42) }]);
    const { input } = renderPalette();
    fireEvent.change(input, { target: { value: "#43" } });
    expect(screen.queryByRole("group", { name: "Sessions" })).toBeNull();
    expect(screen.getByText(PULL_REQUEST_NOT_FOUND)).toBeTruthy();
  });

  it("finds a session the sidebar loaded outside the list's window, such as a pinned one", () => {
    listLoaded([{ id: "c1", title: "Recent work", agent_name: null }]);
    const pinned = {
      id: "p1",
      title: "Old pinned fix",
      agent_name: null,
      pull_request: linked(99),
    };
    const { input } = renderPalette([pinned, { id: "c1", title: "Recent work", agent_name: null }]);
    fireEvent.change(input, { target: { value: "#99" } });
    expect(sessionNames()).toEqual([expect.stringContaining("Old pinned fix")]);
    fireEvent.change(input, { target: { value: "recent" } });
    // Listed by both, shown once.
    expect(sessionNames()).toEqual([expect.stringContaining("Recent work")]);
  });
});
