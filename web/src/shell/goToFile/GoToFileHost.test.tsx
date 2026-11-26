import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

import { TooltipProvider } from "@/components/ui/tooltip";
import type { WorkspaceFileSearchResult } from "@/hooks/useWorkspaceChangedFiles";
import type * as ContentSearchModule from "@/lib/contentSearch";
import { recordRecentWorkspaceFile } from "@/lib/recentWorkspaceFiles";
import { registeredPaletteCommands } from "@/test/paletteCommands";
import { requestFileFinder, requestTextSearch, useFileFinderStore } from "@/store/fileFinder";

// The workspace answers each query from the files below.
const workspace = vi.hoisted(() => ({
  paths: [] as string[],
  serveable: true as boolean | undefined,
}));
vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  useWorkspaceServeable: () => workspace.serveable,
  useWorkspaceFileSearch: (_sessionId: string, query: string) => {
    const typed = query.trim().toLowerCase();
    const data: WorkspaceFileSearchResult | undefined = typed
      ? {
          files: workspace.paths
            .filter((path) => path.toLowerCase().includes(typed))
            .map((path) => ({
              path,
              name: path.split("/").at(-1)!,
              type: "file" as const,
              bytes: 0,
              modified_at: null,
            })),
          truncated: false,
          hasMore: false,
          unavailable: false,
        }
      : undefined;
    return {
      data,
      isSuccess: !!data,
      isError: false,
      isFetching: false,
      isPlaceholderData: false,
    };
  },
}));

// The preview beside the list shows each file's path as its text.
vi.mock("@/hooks/useFileContent", () => ({
  useFileContent: (_sessionId: string, path: string) => ({
    data: { content: `contents of ${path}`, encoding: "utf-8" },
    isError: false,
  }),
}));
vi.mock("../codeMirror/CodeMirrorFileViewer", () => ({
  CodeMirrorFileViewer: ({ content }: { content: string }) => <pre>{content}</pre>,
}));

// The workspace's text search finds "needle" on two lines of src/widget.ts.
const textSearches = vi.hoisted(() => ({
  calls: [] as unknown[],
  held: null as Promise<void> | null,
  // A search's own hold, by its query, so searches can answer out of order.
  heldByQuery: {} as Record<string, Promise<void>>,
}));
vi.mock("@/lib/contentSearch", async (importOriginal) => ({
  ...(await importOriginal<typeof ContentSearchModule>()),
  runContentSearch: async (_sessionId: string, input: { query: string }) => {
    textSearches.calls.push(input);
    await (textSearches.heldByQuery[input.query] ?? textSearches.held);
    if (input.query === "gizmo") {
      const gizmo = { path: "src/gizmo.ts", line: 12, column: 1, text: "gizmo();" };
      return {
        matches: [{ ...gizmo, match_start: 0, match_end: 5 }],
        truncated: false,
        warning: null,
      };
    }
    return {
      matches: input.query.includes("needle")
        ? [
            {
              path: "src/widget.ts",
              line: 4,
              column: 9,
              text: "  const needle = 1;",
              match_start: 8,
              match_end: 14,
            },
            {
              path: "src/widget.ts",
              line: 9,
              column: 1,
              text: "needle();",
              match_start: 0,
              match_end: 6,
            },
          ]
        : [],
      truncated: false,
      warning: null,
    };
  },
}));

const { GoToFileHost } = await import("./GoToFileHost");
const { contentSearchHandOffKey, contentSearchInputKey, contentSearchResultKey } =
  await import("../contentSearchCache");

const SESSION = "conv_files";

function renderHost(sessionId: string | null = SESSION, { textSearch = true } = {}) {
  const openFile = vi.fn();
  const showTextSearch = vi.fn();
  const queryClient = new QueryClient();
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <TooltipProvider>
          <GoToFileHost
            sessionId={sessionId}
            openFile={openFile}
            showTextSearch={showTextSearch}
            textSearch={textSearch}
          />
        </TooltipProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { openFile, showTextSearch, queryClient };
}

const input = () => screen.queryByTestId("go-to-file-input");
/** ⌃⌘P on the Mac, where the tests' platform reports one. */
const pressGoToFile = () =>
  fireEvent.keyDown(window, { key: "p", code: "KeyP", metaKey: true, ctrlKey: true });
/** ⌃⌘O, Search Text's chord. */
const pressSearchText = () =>
  fireEvent.keyDown(window, { key: "o", code: "KeyO", metaKey: true, ctrlKey: true });

beforeEach(() => {
  Object.defineProperty(navigator, "platform", { value: "MacIntel", configurable: true });
  workspace.paths = ["src/widget.ts", "docs/guide.md", "src/window.ts"];
  workspace.serveable = true;
  window.localStorage.clear();
  textSearches.calls = [];
  textSearches.held = null;
  textSearches.heldByQuery = {};
});

afterEach(() => {
  cleanup();
  useFileFinderStore.setState({
    availableSessionId: null,
    textSearchAvailable: false,
    request: null,
    pane: null,
  });
});

describe("GoToFileHost", () => {
  it("offers Go to File and Search Text in ⌘K with their chords, for the hosted session", () => {
    renderHost();
    expect(useFileFinderStore.getState().availableSessionId).toBe(SESSION);
    const rows = registeredPaletteCommands().filter((command) =>
      command.id.startsWith("go-to-file:"),
    );
    expect(rows.map((row) => [row.label, row.shortcut])).toEqual([
      ["Go to File in Workspace…", "palette.files"],
      ["Search Text in Workspace…", "palette.text"],
    ]);
  });

  it("opens from its chord and closes from it again", async () => {
    renderHost();
    act(() => pressGoToFile());
    expect(await screen.findByTestId("go-to-file-input")).toHaveFocus();
    act(() => pressGoToFile());
    await waitFor(() => expect(input()).toBeNull());
  });

  it("opens from its ⌘K row, finds a file by name and opens it", async () => {
    const { openFile } = renderHost();
    const row = registeredPaletteCommands().find((command) =>
      command.label.startsWith("Go to File"),
    )!;
    act(() => row.run());
    const field = await screen.findByTestId("go-to-file-input");
    await userEvent.type(field, "widg");
    const option = await screen.findByRole("option", { name: /widget\.ts/ });
    expect(screen.queryByRole("option", { name: /window\.ts/ })).toBeNull();
    // The active match shows beside the list.
    expect(screen.getByTestId("go-to-file-preview")).toHaveTextContent("contents of src/widget.ts");
    await userEvent.click(option);
    await waitFor(() => expect(openFile).toHaveBeenCalledWith("src/widget.ts"));
    expect(input()).toBeNull();
  });

  it("starts from a request's text, such as a key typed into the Files page's field", async () => {
    renderHost();
    act(() => void requestFileFinder(SESSION, { query: "guide" }));
    expect(await screen.findByTestId("go-to-file-input")).toHaveValue("guide");
    expect(await screen.findByRole("option", { name: /guide\.md/ })).toBeInTheDocument();
  });

  it("takes a later request's text while open, even an empty one, and keeps it on a mode switch", async () => {
    renderHost();
    act(() => void requestFileFinder(SESSION, { query: "guide" }));
    expect(await screen.findByTestId("go-to-file-input")).toHaveValue("guide");
    // Search Text in Workspace… from a selection, with Go to File already open.
    act(() => void requestTextSearch(SESSION, { query: "needle" }));
    await waitFor(() => expect(input()).toHaveValue("needle"));
    expect(screen.getByRole("tab", { name: /Text/ })).toHaveAttribute("aria-selected", "true");
    act(() => void requestFileFinder(SESSION, { query: "" }));
    await waitFor(() => expect(input()).toHaveValue(""));
    // A chord carries no text, so switching modes keeps what was typed.
    await userEvent.type(input()!, "widget");
    act(() => void requestTextSearch(SESSION, { toggle: true }));
    await waitFor(() =>
      expect(screen.getByRole("tab", { name: /Text/ })).toHaveAttribute("aria-selected", "true"),
    );
    expect(input()).toHaveValue("widget");
  });

  it("lists the session's recently opened files before a query", async () => {
    recordRecentWorkspaceFile(SESSION, "docs/guide.md");
    recordRecentWorkspaceFile("conv_other", "elsewhere.ts");
    renderHost();
    act(() => void requestFileFinder(SESSION));
    expect(await screen.findByRole("option", { name: /guide\.md/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /elsewhere\.ts/ })).toBeNull();
  });

  it("says the workspace can't be searched rather than that nothing matched", async () => {
    workspace.serveable = false;
    renderHost();
    act(() => void requestFileFinder(SESSION, { query: "widget" }));
    expect(await screen.findByText(/aren't available right now/)).toBeInTheDocument();
  });

  it("opens on Text for a text search, finds lines and opens one at its line", async () => {
    const { openFile } = renderHost();
    act(() => void requestTextSearch(SESSION));
    const field = await screen.findByRole("combobox", { name: "Search text in files" });
    expect(screen.getByRole("tab", { name: /Text/ })).toHaveAttribute("aria-selected", "true");
    await userEvent.type(field, "needle");
    // Each line shows under its file, without its indentation, at its line number.
    const line = await screen.findByRole("option", { name: /const needle = 1;/ });
    expect(screen.getByRole("group", { name: "src/widget.ts" })).toContainElement(line);
    expect(line).toHaveTextContent("4");
    // The preview names the line it shows.
    expect(screen.getByTestId("go-to-file-preview")).toHaveTextContent("Line 4");
    await userEvent.keyboard("{Enter}");
    await waitFor(() =>
      expect(openFile).toHaveBeenCalledWith("src/widget.ts", { line: 4, column: 9 }),
    );
  });

  it("switches between Files and Text from their chords, keeping what was typed", async () => {
    renderHost();
    act(() => pressGoToFile());
    const field = await screen.findByTestId("go-to-file-input");
    await userEvent.type(field, "needle");
    act(() => pressSearchText());
    expect(await screen.findByRole("combobox", { name: "Search text in files" })).toHaveValue(
      "needle",
    );
    act(() => pressGoToFile());
    expect(await screen.findByRole("combobox", { name: "Go to a file" })).toHaveValue("needle");
    act(() => pressGoToFile());
    await waitFor(() => expect(input()).toBeNull());
  });

  it("searches with the options switched on, from their buttons or Alt and a letter", async () => {
    renderHost();
    act(() => void requestTextSearch(SESSION));
    const field = await screen.findByRole("combobox", { name: "Search text in files" });
    await userEvent.type(field, "needle");
    await screen.findByRole("option", { name: /const needle/ });
    fireEvent.keyDown(field, { key: "ç", code: "KeyC", altKey: true });
    expect(screen.getByRole("button", { name: "Match case" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await userEvent.click(screen.getByRole("button", { name: "Files to include or exclude" }));
    const include = screen.getByRole("textbox", { name: "Files to include" });
    await userEvent.type(include, "src/**");
    // In a filter field Option and a letter type a character, so no option flips.
    expect(fireEvent.keyDown(include, { key: "ç", code: "KeyC", altKey: true })).toBe(true);
    expect(screen.getByRole("button", { name: "Match case" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await waitFor(() =>
      expect(textSearches.calls.at(-1)).toMatchObject({
        query: "needle",
        caseSensitive: true,
        include: "src/**",
      }),
    );
  });

  it("hands the search and its lines to the Search page from Open in Search", async () => {
    const { showTextSearch, queryClient } = renderHost();
    act(() => void requestTextSearch(SESSION));
    const field = await screen.findByRole("combobox", { name: "Search text in files" });
    await userEvent.type(field, "needle");
    await screen.findByRole("option", { name: /const needle/ });
    await userEvent.click(screen.getByRole("button", { name: /Open in Search/ }));
    await waitFor(() => expect(showTextSearch).toHaveBeenCalledTimes(1));
    expect(input()).toBeNull();
    expect(queryClient.getQueryData(contentSearchInputKey(SESSION))).toMatchObject({
      query: "needle",
    });
    expect(queryClient.getQueryData(contentSearchResultKey(SESSION))).toMatchObject({
      query: "needle",
      matches: [expect.objectContaining({ line: 4 }), expect.objectContaining({ line: 9 })],
    });
    // The page it shows takes the keyboard.
    expect(useFileFinderStore.getState().textSearchFocus).toMatchObject({ sessionId: SESSION });
  });

  it("keeps the newer search's lines when an older one answers after it, and hands those over", async () => {
    let answerOlder = () => {};
    let answerNewer = () => {};
    textSearches.heldByQuery = {
      needle: new Promise<void>((resolve) => {
        answerOlder = resolve;
      }),
      gizmo: new Promise<void>((resolve) => {
        answerNewer = resolve;
      }),
    };
    const { showTextSearch, queryClient } = renderHost();
    act(() => void requestTextSearch(SESSION));
    const field = await screen.findByRole("combobox", { name: "Search text in files" });
    const asked = (query: string) =>
      waitFor(() => expect(textSearches.calls).toContainEqual(expect.objectContaining({ query })));
    await userEvent.type(field, "needle");
    await asked("needle");
    await userEvent.clear(field);
    await userEvent.type(field, "gizmo");
    await asked("gizmo");

    await act(async () => answerNewer());
    expect(await screen.findByRole("option", { name: /gizmo\(\)/ })).toBeInTheDocument();
    await act(async () => answerOlder());
    // The next keystroke renders the list again; the older answer still doesn't show.
    fireEvent.keyDown(field, { key: "ArrowDown" });
    expect(screen.queryByRole("option", { name: /const needle/ })).toBeNull();
    expect(screen.getByRole("option", { name: /gizmo\(\)/ })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /Open in Search/ }));
    await waitFor(() => expect(showTextSearch).toHaveBeenCalledTimes(1));
    expect(queryClient.getQueryData(contentSearchResultKey(SESSION))).toMatchObject({
      query: "gizmo",
      matches: [expect.objectContaining({ path: "src/gizmo.ts", line: 12 })],
    });
  });

  it("hands a search that hasn't answered yet to the Search page to run there", async () => {
    textSearches.held = new Promise(() => {});
    const { showTextSearch, queryClient } = renderHost();
    act(() => void requestTextSearch(SESSION));
    const field = await screen.findByRole("combobox", { name: "Search text in files" });
    await userEvent.type(field, "needle");
    await waitFor(() => expect(textSearches.calls).not.toHaveLength(0));
    await userEvent.click(screen.getByRole("button", { name: /Open in Search/ }));
    await waitFor(() => expect(showTextSearch).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(queryClient.getQueryData(contentSearchHandOffKey(SESSION))).toMatchObject({
        input: { query: "needle" },
        run: true,
      }),
    );
    expect(queryClient.getQueryData(contentSearchResultKey(SESSION))).toBeUndefined();
  });

  it("offers no Search Text where the session's files can't be searched for text", () => {
    const { showTextSearch } = renderHost(SESSION, { textSearch: false });
    expect(registeredPaletteCommands().map((command) => command.label)).not.toContain(
      "Search Text in Workspace…",
    );
    expect(requestTextSearch(SESSION)).toBe(false);
    act(() => pressSearchText());
    expect(input()).toBeNull();
    expect(showTextSearch).not.toHaveBeenCalled();
    // Go to File still opens, without a Text tab.
    act(() => void requestFileFinder(SESSION));
    expect(screen.queryByRole("tab", { name: /Text/ })).toBeNull();
  });

  it("hosts nothing without a session, so every entry point says it's unavailable", () => {
    renderHost(null);
    expect(useFileFinderStore.getState().availableSessionId).toBeNull();
    expect(requestFileFinder(SESSION)).toBe(false);
    expect(
      registeredPaletteCommands().some((command) => command.id.startsWith("go-to-file:")),
    ).toBe(false);
    act(() => pressGoToFile());
    expect(input()).toBeNull();
  });
});
