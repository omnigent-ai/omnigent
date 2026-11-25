import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
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

// ⌘K's session search, as the finder calls it: what it was asked for, and its answer.
const searches = vi.hoisted(() => ({ options: [] as unknown[], remembered: [] as string[] }));
vi.mock("../usePaletteSessionSearch", () => ({
  usePaletteSessionSearch: (options: { open: boolean }) => {
    searches.options.push(options);
    const [query, setQuery] = useState("");
    const typed = options.open && query.trim() !== "";
    const paging = {
      hasNextPage: false,
      isFetchNextPageError: false,
      isFetchingNextPage: false,
      fetchNextPage: () => undefined,
    };
    return {
      query,
      setQuery,
      debouncedQuery: query,
      visibleSessions: typed
        ? query.toLowerCase().includes("upload")
          ? [paletteSession(NAMED)]
          : []
        : [paletteSession(RECENT)],
      contentSessions:
        typed && query.toLowerCase().includes("upload") ? [paletteSession(MESSAGED)] : [],
      showingServerMatches: typed,
      titleSearch: paging,
      contentSearch: paging,
      searchFetching: false,
      loadError: false,
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
