import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import type { Conversation } from "@/hooks/useConversations";
import type * as SessionsApi from "@/lib/sessionsApi";
import { requestFileFinder, useFileFinderStore } from "@/store/fileFinder";
import { requestSessionSearch, useSessionSearchStore } from "@/store/sessionSearch";
import { usePaletteCommandsStore } from "@/store/paletteCommands";

import type { PaletteSession } from "../usePaletteSessionSearch";

function conversation(id: string, title: string, snippet?: string): Conversation {
  return {
    id,
    title,
    created_at: 1_000,
    updated_at: 2_000,
    workspace: "/work/app",
    archived: false,
    search_snippet: snippet ?? null,
  } as unknown as Conversation;
}

function paletteSession(row: Conversation): PaletteSession {
  return {
    id: row.id,
    label: row.title ?? row.id,
    agent: "claude",
    archived: false,
    snippet: row.search_snippet ?? null,
    parent: null,
    project: null,
    conversation: row,
  };
}

const NAMED = conversation("conv_named", "Upload retries");
const MESSAGED = conversation("conv_messaged", "Storage cleanup", "…the upload kept failing…");
const RECENT = conversation("conv_recent", "Recent work");
// The message matches each later page brings, one a page.
const LATER_MESSAGED = [
  conversation("conv_page2", "Bucket audit", "…an upload stalled…"),
  conversation("conv_page3", "Cache warmup", "…retry the upload…"),
];

// ⌘K's session search, as the finder calls it: what it was asked for, and its answer.
const searches = vi.hoisted(() => ({
  options: [] as unknown[],
  remembered: [] as string[],
  // A failed group or page, as the search reports it; none by default.
  failed: null as null | { more: boolean },
  retries: 0,
  // The query the server last answered, when the debounce lags the typed one; null keeps up.
  answered: null as string | null,
  // How many pages of message matches there are, and how often another was asked for.
  contentPages: 1,
  contentPagesFetched: 0,
  // Whether the first message page's matches were all listed by name, so it adds no rows.
  firstContentPageListed: false,
}));
vi.mock("../usePaletteSessionSearch", () => ({
  usePaletteSessionSearch: (options: { open: boolean }) => {
    searches.options.push(options);
    const [query, setQuery] = useState("");
    const [contentPagesLoaded, setContentPagesLoaded] = useState(1);
    const typed = options.open && query.trim() !== "";
    const debouncedQuery = searches.answered ?? query;
    const showingServerMatches = typed && debouncedQuery === query;
    const paging = {
      hasNextPage: false,
      isFetchNextPageError: false,
      isFetchingNextPage: false,
      fetchNextPage: () => undefined,
    };
    return {
      query,
      setQuery,
      debouncedQuery,
      visibleSessions: typed
        ? query.toLowerCase().includes("upload")
          ? [paletteSession(NAMED)]
          : []
        : [paletteSession(RECENT)],
      contentSessions:
        showingServerMatches && query.toLowerCase().includes("upload")
          ? [
              ...(searches.firstContentPageListed ? [] : [MESSAGED]),
              ...LATER_MESSAGED.slice(0, contentPagesLoaded - 1),
            ].map(paletteSession)
          : [],
      showingServerMatches,
      titleSearch: paging,
      contentSearch: {
        ...paging,
        hasNextPage: contentPagesLoaded < searches.contentPages,
        fetchNextPage: () => {
          searches.contentPagesFetched += 1;
          setContentPagesLoaded((count) => count + 1);
        },
      },
      searchFetching: false,
      loadError: searches.failed !== null,
      loadMoreError: searches.failed?.more === true,
      retrying: false,
      retryLoad: () => {
        searches.retries += 1;
      },
      rememberSearchRow: (id: string) => searches.remembered.push(id),
    };
  },
}));

const fetchSessionItemsPage = vi.fn();
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApi>()),
  fetchSessionItemsPage: (...args: unknown[]) => fetchSessionItemsPage(...args),
}));
vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  useWorkspaceServeable: () => true,
  useWorkspaceFileSearch: () => ({
    data: undefined,
    isSuccess: false,
    isError: false,
    isFetching: false,
    isPlaceholderData: false,
  }),
}));

const { GoToFileHost } = await import("./GoToFileHost");

function message(role: "user" | "assistant", text: string) {
  return {
    type: "message",
    role,
    content: [{ type: role === "user" ? "input_text" : "output_text", text }],
  };
}

function Location() {
  return <div data-testid="location">{useLocation().pathname}</div>;
}

function renderHost(sessionId: string | null = "conv_files") {
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <MemoryRouter initialEntries={["/"]}>
        <TooltipProvider>
          <GoToFileHost
            sessionId={sessionId}
            openFile={vi.fn()}
            showTextSearch={vi.fn()}
            textSearch
          />
          <Routes>
            <Route path="*" element={<Location />} />
          </Routes>
        </TooltipProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function openSessions(query?: string) {
  act(() => requestSessionSearch(query));
  return screen.getByRole("dialog");
}

beforeEach(() => {
  searches.options.length = 0;
  searches.remembered.length = 0;
  searches.failed = null;
  searches.retries = 0;
  searches.answered = null;
  searches.contentPages = 1;
  searches.firstContentPageListed = false;
  searches.contentPagesFetched = 0;
  fetchSessionItemsPage.mockReset();
  fetchSessionItemsPage.mockImplementation(async (sessionId: string) => ({
    hasMore: false,
    items:
      sessionId === MESSAGED.id
        ? [
            message("user", "Why does the build fail?"),
            message("assistant", "The upload kept failing on large files."),
            message("user", "Thanks"),
          ]
        : [message("user", "Hello"), message("assistant", "Hi there")],
  }));
});

afterEach(() => {
  cleanup();
  useSessionSearchStore.setState({ request: null });
});

describe("the finder's Sessions tab", () => {
  it("searches with ⌘K's own session search, only while it shows", () => {
    renderHost();
    expect(searches.options.every((options) => !(options as { open: boolean }).open)).toBe(true);
    openSessions("upload");
    expect(searches.options.at(-1)).toEqual({
      open: true,
      sessionsOnly: false,
      matchLoaded: true,
      includeArchived: false,
    });
  });

  it.each([
    [
      "a failed message search",
      { more: false },
      "Couldn't search sessions. Check your connection and try again.",
    ],
    ["a failed next page", { more: true }, "Couldn't load more sessions."],
  ])("shows %s beside the matches it left, with Retry", async (_, failed, notice) => {
    searches.failed = failed;
    renderHost();
    const dialog = openSessions("upload");

    expect(within(dialog).getAllByRole("option")).toHaveLength(2);
    expect(within(dialog).getByText(notice)).toHaveAttribute("role", "status");
    await userEvent.click(within(dialog).getByRole("button", { name: "Retry" }));
    expect(searches.retries).toBe(1);
  });

  it.each([
    ["the recent sessions", { more: false }, "Couldn't load sessions."],
    ["more recent sessions", { more: true }, "Couldn't load more sessions."],
  ])("shows a failure to load %s before a query, with Retry", async (_, failed, notice) => {
    searches.failed = failed;
    renderHost();
    const dialog = openSessions();

    expect(within(dialog).getByRole("option")).toHaveTextContent("Recent work");
    expect(within(dialog).getByText(notice)).toHaveAttribute("role", "status");
    await userEvent.click(within(dialog).getByRole("button", { name: "Retry" }));
    expect(searches.retries).toBe(1);
  });

  it("lists name matches above message matches, starting on the query it was given", () => {
    renderHost();
    const dialog = openSessions("upload");
    expect(within(dialog).getByRole("combobox", { name: "Search sessions" })).toHaveValue("upload");
    const options = within(dialog).getAllByRole("option");
    expect(options.map((option) => option.textContent)).toEqual([
      expect.stringContaining("Upload retries"),
      expect.stringContaining("Storage cleanup"),
    ]);
    expect(within(dialog).getByText("In messages")).toBeInTheDocument();
  });

  it("offers no message page for a query typed since the server answered", async () => {
    searches.contentPages = 2;
    renderHost();
    const dialog = openSessions("upload");
    expect(within(dialog).getByRole("option", { name: /Show more/ })).toBeInTheDocument();

    // "uploads" is typed while the server's answer is still for "upload".
    searches.answered = "upload";
    await userEvent.type(within(dialog).getByRole("combobox", { name: "Search sessions" }), "s");
    expect(within(dialog).queryByRole("option", { name: /Show more/ })).toBeNull();
    expect(searches.contentPagesFetched).toBe(0);
  });

  it("keeps the keyboard on the last row through Show more, on a middle page and the last", async () => {
    searches.contentPages = 3;
    renderHost();
    const dialog = openSessions("upload");
    const input = within(dialog).getByRole("combobox", { name: "Search sessions" });
    const active = () =>
      document.getElementById(input.getAttribute("aria-activedescendant") ?? "")?.textContent;
    const press = (key: string) => fireEvent.keyDown(input, { key });
    // Upload retries, Storage cleanup, Show more.
    press("ArrowDown");
    press("ArrowDown");
    expect(active()).toContain("Show more");
    press("Enter");
    await within(dialog).findByRole("option", { name: /Bucket audit/ });
    expect(active()).toContain("Storage cleanup");
    press("ArrowDown");
    expect(active()).toContain("Bucket audit");

    // The last page takes Show more away, and the keyboard stays where it was.
    press("ArrowDown");
    press("Enter");
    await within(dialog).findByRole("option", { name: /Cache warmup/ });
    expect(within(dialog).queryByRole("option", { name: /Show more/ })).toBeNull();
    expect(active()).toContain("Bucket audit");
    press("ArrowDown");
    expect(active()).toContain("Cache warmup");
    expect(searches.contentPagesFetched).toBe(2);
  });

  it("keeps Show more for a message page all listed by name, then goes to the first row it brings", async () => {
    searches.contentPages = 2;
    searches.firstContentPageListed = true;
    renderHost();
    const dialog = openSessions("upload");
    const input = within(dialog).getByRole("combobox", { name: "Search sessions" });
    const active = () =>
      document.getElementById(input.getAttribute("aria-activedescendant") ?? "")?.textContent;
    // Upload retries, then In messages with only its Show more.
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(active()).toContain("Show more");
    fireEvent.keyDown(input, { key: "Enter" });
    await within(dialog).findByRole("option", { name: /Bucket audit/ });
    expect(active()).toContain("Bucket audit");
  });

  it("lists recent sessions before a query", () => {
    renderHost();
    const dialog = openSessions();
    expect(within(dialog).getByText("Recent sessions")).toBeInTheDocument();
    expect(within(dialog).getByRole("option", { name: /Recent work/ })).toBeInTheDocument();
  });

  it("previews the messages around the newest match, with the match marked", async () => {
    const user = userEvent.setup();
    renderHost();
    const dialog = openSessions("upload");
    await user.keyboard("{ArrowDown}");
    const preview = await within(dialog).findByTestId("session-search-preview");
    await waitFor(() =>
      expect(preview.querySelector("[data-matched]")).toHaveTextContent(
        "The upload kept failing on large files.",
      ),
    );
    expect(preview).toHaveTextContent("Storage cleanup");
    expect(fetchSessionItemsPage).toHaveBeenCalledWith(MESSAGED.id, expect.anything());
  });

  it("says when a session's messages can't be loaded, and points to opening it", async () => {
    fetchSessionItemsPage.mockRejectedValue(new Error("offline"));
    renderHost();
    const dialog = openSessions("upload");
    expect(
      await within(dialog).findByText("Couldn't load this conversation. Open it to read it."),
    ).toBeInTheDocument();
  });

  it("opens the chosen session and keeps its row for the sidebar", async () => {
    const user = userEvent.setup();
    renderHost();
    openSessions("upload");
    await user.keyboard("{ArrowDown}{Enter}");
    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent("/c/conv_messaged"),
    );
    expect(searches.remembered).toEqual(["conv_messaged"]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("keeps the typed text when switching between Files, Text and Sessions", async () => {
    const user = userEvent.setup();
    renderHost();
    const dialog = openSessions("upload");
    const tabs = within(dialog).getByRole("tablist", { name: "Find files, text or sessions" });
    expect(
      within(tabs)
        .getAllByRole("tab")
        .map((tab) => tab.textContent),
    ).toEqual([expect.stringContaining("Files"), expect.stringContaining("Text"), "Sessions"]);
    await user.click(within(tabs).getByRole("tab", { name: /Text/ }));
    expect(within(dialog).getByRole("combobox", { name: "Search text in files" })).toHaveValue(
      "upload",
    );
    await user.click(within(tabs).getByRole("tab", { name: "Sessions" }));
    expect(within(dialog).getByRole("combobox", { name: "Search sessions" })).toHaveValue("upload");
  });

  it("takes a session search's text while Files shows, even when both requests share an id", () => {
    renderHost();
    // File and session requests count separately, so the same id can come from both.
    let fileRequestId: number | undefined;
    const stop = useFileFinderStore.subscribe((state) => {
      fileRequestId ??= state.request?.id;
    });
    act(() => {
      requestFileFinder("conv_files");
    });
    stop();
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("combobox", { name: "Go to a file" })).toHaveValue("");
    expect(fileRequestId).toBeDefined();
    act(() => useSessionSearchStore.setState({ request: { id: fileRequestId!, query: "upload" } }));
    expect(
      within(screen.getByRole("dialog")).getByRole("combobox", { name: "Search sessions" }),
    ).toHaveValue("upload");
  });

  it("offers Sessions alone, with no tabs, when no session is open", () => {
    renderHost(null);
    const dialog = openSessions("upload");
    expect(within(dialog).queryByRole("tablist")).toBeNull();
    expect(within(dialog).getByRole("combobox", { name: "Search sessions" })).toBeInTheDocument();
  });

  it("offers Search in Sessions… in ⌘K while it's hosted, and opens the tab from it", () => {
    renderHost(null);
    const command = Object.values(usePaletteCommandsStore.getState().groups)
      .flatMap((group) => group.commands)
      .find((candidate) => candidate.id === "search-sessions");
    expect(command?.label).toBe("Search in Sessions…");
    act(() => command?.run());
    expect(screen.getByRole("combobox", { name: "Search sessions" })).toBeInTheDocument();
  });

  it("doesn't reopen for a request an earlier host already took", () => {
    renderHost();
    openSessions("upload");
    cleanup();
    renderHost();
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
