import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { FlagIcon } from "lucide-react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { readQuickPickHistory } from "@/components/ui/quick-pick";
import {
  type PaletteCommand,
  unavailableBecause,
  usePaletteCommandsStore,
} from "@/store/paletteCommands";

import { QuickPickCommandPalette } from "./QuickPickCommandPalette";

// Behavior specific to the QuickPick palette; CommandPalette.quickPick.test.tsx runs
// every shared CommandPalette case against it.

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
  useConversations.mockReturnValue({
    data: {
      pages: [{ data: [{ id: "c1", title: "Fix the parser", agent_name: null, archived: false }] }],
    },
    isFetching: false,
  });
});
afterEach(() => {
  cleanup();
  usePaletteCommandsStore.setState({ groups: {} });
  localStorage.clear();
  useSessionSearch.mockImplementation(() => ({ isFetching: false }));
});

function renderPalette() {
  const onOpenChange = vi.fn();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <QuickPickCommandPalette
        open
        onOpenChange={onOpenChange}
        onToggleLeftSidebar={vi.fn()}
        onToggleRightSidebar={vi.fn()}
      />
    </QueryClientProvider>,
  );
  return { onOpenChange, input: screen.getByTestId("command-palette-input") };
}

describe("QuickPickCommandPalette", () => {
  it("lines session labels up with action labels through a blank icon slot", () => {
    renderPalette();
    const session = screen.getByRole("option", { name: /Fix the parser/ });
    const action = screen.getByRole("option", { name: /Go to Settings/ });
    // Both rows start with the same icon slot; the session's is empty.
    const sessionSlot = session.firstElementChild;
    const actionSlot = action.firstElementChild;
    expect(sessionSlot?.className).toBe(actionSlot?.className);
    expect(sessionSlot?.querySelector("svg")).toBeNull();
    expect(actionSlot?.querySelector("svg")).toBeTruthy();
  });

  it("finds actions by their initials and underlines the letters they hit", () => {
    const { input } = renderPalette();
    fireEvent.change(input, { target: { value: "gts" } });
    const actions = within(screen.getByRole("group", { name: "Actions" })).getAllByRole("option");
    expect(actions[0].textContent).toContain("Go to Settings");
    const marks = [...actions[0].querySelectorAll("mark")];
    expect(marks.map((mark) => mark.textContent)).toEqual(["G", "t", "S"]);
    expect(marks.every((mark) => mark.dataset.matchHighlight === "underline")).toBe(true);
  });

  it("moves between groups with Alt+Arrow and runs the action with Enter", () => {
    const { input, onOpenChange } = renderPalette();
    fireEvent.keyDown(input, { key: "ArrowDown", altKey: true });
    const active = document.getElementById(input.getAttribute("aria-activedescendant") ?? "");
    expect(active?.closest('[role="group"]')?.getAttribute("aria-labelledby")).toBeTruthy();
    expect(within(screen.getByRole("group", { name: "Actions" })).getAllByRole("option")[0]).toBe(
      active,
    );
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(navigate).toHaveBeenCalledWith("/");
  });
});

interface SearchRow {
  id: string;
  title: string;
  agent_name: null;
  search_snippet?: string;
}

/** Each scope's server search: its pages so far, one more per Show more, up to `total`. */
/**
 * Each scope's server search: its pages so far, one more per Show more, up to `total`. With
 * `deferred`, a page stays loading until the returned `release` answers it.
 */
function pagedSessionSearch(
  scopes: Record<string, { total: number; page: (n: number) => SearchRow[] }>,
  { deferred = false }: { deferred?: boolean } = {},
) {
  const pending = { release: () => {} };
  useSessionSearch.mockImplementation((_query: unknown, scope: unknown) => {
    const [pages, setPages] = useState(1);
    const [loading, setLoading] = useState(false);
    const group = scopes[scope as string];
    if (!group) return { isFetching: false };
    const nextPage = () => setPages((count) => count + 1);
    return {
      isFetching: false,
      data: {
        pages: Array.from({ length: pages }, (_, index) => ({ data: group.page(index + 1) })),
      },
      hasNextPage: pages < group.total,
      isFetchingNextPage: loading,
      isFetchNextPageError: false,
      fetchNextPage: () => {
        if (!deferred) return nextPage();
        setLoading(true);
        pending.release = () => {
          setLoading(false);
          nextPage();
        };
      },
    };
  });
  return { release: () => act(() => pending.release()) };
}

const messageMatch = (n: number): SearchRow => ({
  id: `m${n}`,
  title: `Message match ${n}`,
  agent_name: null,
  search_snippet: "…the kestrel nests…",
});

describe("QuickPickCommandPalette's Show more", () => {
  const activeLabel = (input: HTMLElement) =>
    document.getElementById(input.getAttribute("aria-activedescendant") ?? "")?.textContent;
  const press = (input: HTMLElement, key: string) => fireEvent.keyDown(input, { key });

  it("keeps the keyboard on the last row through a middle page and the last one", async () => {
    pagedSessionSearch({ content: { total: 3, page: (n) => [messageMatch(n)] } });
    const { input } = renderPalette();
    fireEvent.change(input, { target: { value: "kestrel" } });
    await screen.findByRole("option", { name: /Show more/ });
    expect(activeLabel(input)).toContain("Message match 1");

    press(input, "ArrowDown");
    expect(activeLabel(input)).toContain("Show more");
    press(input, "Enter");
    await screen.findByRole("option", { name: /Message match 2/ });
    expect(activeLabel(input)).toContain("Message match 1");
    press(input, "ArrowDown");
    expect(activeLabel(input)).toContain("Message match 2");

    // The last page takes Show more away, and the keyboard stays where it was.
    press(input, "ArrowDown");
    press(input, "Enter");
    await screen.findByRole("option", { name: /Message match 3/ });
    expect(screen.queryByRole("option", { name: /Show more/ })).toBeNull();
    expect(activeLabel(input)).toContain("Message match 2");
    press(input, "ArrowDown");
    expect(activeLabel(input)).toContain("Message match 3");
  });
  /** Searches "kestrel" and takes Show more twice, which parks the keyboard on match 2. */
  async function parkOnSecondPage(input: HTMLElement) {
    fireEvent.change(input, { target: { value: "kestrel" } });
    await screen.findByRole("option", { name: /Show more/ });
    press(input, "ArrowDown");
    press(input, "Enter");
    await screen.findByRole("option", { name: /Message match 2/ });
    press(input, "ArrowDown");
    press(input, "ArrowDown");
    press(input, "Enter");
    await screen.findByRole("option", { name: /Message match 3/ });
    expect(activeLabel(input)).toContain("Message match 2");
  }

  it("keeps a row the user moved to while the page loads, once it arrives", async () => {
    const search = pagedSessionSearch(
      {
        title: { total: 1, page: () => [{ id: "t1", title: "Kestrel plan", agent_name: null }] },
        content: { total: 2, page: (n) => [messageMatch(n)] },
      },
      { deferred: true },
    );
    const { input } = renderPalette();
    fireEvent.change(input, { target: { value: "kestrel" } });
    await screen.findByRole("option", { name: /Show more/ });
    press(input, "ArrowDown");
    press(input, "ArrowDown");
    press(input, "Enter");
    await screen.findByRole("option", { name: /Loading/ });
    expect(activeLabel(input)).toContain("Message match 1");

    // The user moves up while the page is still loading; its rows don't take that back.
    press(input, "ArrowUp");
    expect(activeLabel(input)).toContain("Kestrel plan");
    search.release();
    await screen.findByRole("option", { name: /Message match 2/ });
    expect(activeLabel(input)).toContain("Kestrel plan");
  });

  it("goes to the first row of all once the parked row is gone, not the group's first", async () => {
    let firstMatchDeleted = false;
    pagedSessionSearch({
      title: { total: 1, page: () => [{ id: "t1", title: "Kestrel plan", agent_name: null }] },
      content: {
        total: 2,
        page: (n) => (n === 1 ? (firstMatchDeleted ? [] : [messageMatch(1)]) : [messageMatch(2)]),
      },
    });
    const { input } = renderPalette();
    fireEvent.change(input, { target: { value: "kestrel" } });
    await screen.findByRole("option", { name: /Show more/ });
    press(input, "ArrowDown");
    press(input, "ArrowDown");
    expect(activeLabel(input)).toContain("Show more");
    // The parked row's session goes away while the next page loads.
    firstMatchDeleted = true;
    press(input, "Enter");
    await screen.findByRole("option", { name: /Message match 2/ });
    expect(screen.queryByRole("option", { name: /Message match 1/ })).toBeNull();
    expect(activeLabel(input)).toContain("Kestrel plan");
  });

  it("starts a query typed again from its top row, so Enter opens that row", async () => {
    pagedSessionSearch({ content: { total: 3, page: (n) => [messageMatch(n)] } });
    const { input } = renderPalette();
    await parkOnSecondPage(input);
    fireEvent.change(input, { target: { value: "kestrels" } });
    fireEvent.change(input, { target: { value: "kestrel" } });
    await waitFor(() => expect(activeLabel(input)).toContain("Message match 1"));
    press(input, "Enter");
    expect(navigate).toHaveBeenLastCalledWith("/c/m1");
  });

  it("starts from the top row after a close and reopen", async () => {
    pagedSessionSearch({ content: { total: 3, page: (n) => [messageMatch(n)] } });
    const props = {
      onOpenChange: vi.fn(),
      onToggleLeftSidebar: vi.fn(),
      onToggleRightSidebar: vi.fn(),
    };
    const client = new QueryClient();
    const view = render(
      <QueryClientProvider client={client}>
        <QuickPickCommandPalette open {...props} />
      </QueryClientProvider>,
    );
    await parkOnSecondPage(screen.getByTestId("command-palette-input"));
    view.rerender(
      <QueryClientProvider client={client}>
        <QuickPickCommandPalette open={false} {...props} />
      </QueryClientProvider>,
    );
    view.rerender(
      <QueryClientProvider client={client}>
        <QuickPickCommandPalette open {...props} />
      </QueryClientProvider>,
    );
    const input = screen.getByTestId("command-palette-input");
    fireEvent.change(input, { target: { value: "kestrel" } });
    await screen.findByRole("option", { name: /Message match 1/ });
    await waitFor(() => expect(activeLabel(input)).toContain("Message match 1"));
    press(input, "Enter");
    expect(navigate).toHaveBeenLastCalledWith("/c/m1");
  });
});

describe("QuickPickCommandPalette ordering", () => {
  const groupNames = () =>
    screen
      .getAllByRole("group")
      .map(
        (group) =>
          document.getElementById(group.getAttribute("aria-labelledby") ?? "")?.textContent,
      );

  it("lists a command picked before under Recent, below the sessions", () => {
    const first = renderPalette();
    fireEvent.change(first.input, { target: { value: "go to settings" } });
    fireEvent.keyDown(first.input, { key: "Enter" });
    expect(navigate).toHaveBeenCalledWith("/settings");
    cleanup();

    renderPalette();
    expect(groupNames().slice(0, 2)).toEqual(["Sessions", "Recent"]);
    const recent = screen.getByRole("group", { name: "Recent" });
    expect(
      within(recent)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual([expect.stringContaining("Go to Settings")]);
  });

  it("keeps session and Show more picks out of the commands' history", async () => {
    const commandHistory = () => readQuickPickHistory("command-palette:commands");
    const first = renderPalette();
    fireEvent.keyDown(first.input, { key: "Enter" });
    expect(navigate).toHaveBeenCalledWith("/c/c1");
    expect(commandHistory()).toEqual([]);
    cleanup();

    useSessionSearch.mockImplementation((_query: unknown, scope: unknown) =>
      scope === "content"
        ? { isFetching: false }
        : {
            isFetching: false,
            data: {
              pages: [{ data: [{ id: "k1", title: "Kestrel plan", agent_name: null }] }],
            },
            hasNextPage: true,
            fetchNextPage: vi.fn(),
          },
    );
    const { input } = renderPalette();
    fireEvent.change(input, { target: { value: "kestrel" } });
    const showMore = await screen.findByRole("option", { name: /Show more/ });
    fireEvent.click(showMore);
    expect(commandHistory()).toEqual([]);
  });

  it("steps through its rows with Ctrl-N and Ctrl-P", () => {
    const { input } = renderPalette();
    const active = () =>
      document.getElementById(input.getAttribute("aria-activedescendant") ?? "")?.textContent;
    const first = active();
    expect(fireEvent.keyDown(input, { key: "n", code: "KeyN", ctrlKey: true })).toBe(false);
    expect(active()).not.toBe(first);
    fireEvent.keyDown(input, { key: "p", code: "KeyP", ctrlKey: true });
    expect(active()).toBe(first);
  });

  it("puts a command the query names ahead of the sessions that match it", () => {
    useConversations.mockReturnValue({
      data: {
        pages: [
          { data: [{ id: "c2", title: "Settings sync", agent_name: null, archived: false }] },
        ],
      },
      isFetching: false,
    });
    const { input } = renderPalette();
    fireEvent.change(input, { target: { value: "settings" } });
    const options = screen.getAllByRole("option").map((option) => option.textContent ?? "");
    expect(options[0]).toContain("Go to Settings");
    expect(options.some((option) => option.includes("Settings sync"))).toBe(true);
  });
});

describe("QuickPickCommandPalette feature commands", () => {
  // No sessions, so the command the query names is the first row.
  beforeEach(() => useConversations.mockReturnValue({ data: { pages: [] }, isFetching: false }));
  const offer = (commands: PaletteCommand[]) =>
    act(() =>
      usePaletteCommandsStore.getState().register("tabs", { heading: "Workspace", commands }),
    );
  const command = (id: string, overrides: Partial<PaletteCommand> = {}): PaletteCommand => ({
    id,
    label: id,
    icon: FlagIcon,
    keywords: [],
    run: vi.fn(),
    ...overrides,
  });
  const accept = (input: HTMLElement, query: string) => {
    fireEvent.change(input, { target: { value: query } });
    fireEvent.keyDown(input, { key: "Enter" });
  };

  it("lists a feature's commands under its heading and runs one", () => {
    const reopen = command("Reopen closed tab");
    offer([reopen]);
    const { input, onOpenChange } = renderPalette();
    expect(
      within(screen.getByRole("group", { name: "Workspace" })).getByRole("option", {
        name: /Reopen closed tab/,
      }),
    ).toBeTruthy();
    accept(input, "reopen closed");
    expect(reopen.run).toHaveBeenCalledOnce();
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("keeps matching sessions above a command named after the user's data", () => {
    useConversations.mockReturnValue({
      data: {
        pages: [
          { data: [{ id: "c3", title: "Parser rewrite", agent_name: null, archived: false }] },
        ],
      },
      isFetching: false,
    });
    offer([command("Open Parser rewrite pull request", { leadsSources: false })]);
    const { input } = renderPalette();
    fireEvent.change(input, { target: { value: "parser rewrite" } });
    const options = screen.getAllByRole("option").map((option) => option.textContent ?? "");
    expect(options[0]).toContain("Parser rewrite");
    expect(options[0]).not.toContain("pull request");
    expect(options.some((option) => option.includes("Open Parser rewrite pull request"))).toBe(
      true,
    );
  });

  it("doesn't run a command that moves focus when it's withdrawn while the palette closes", async () => {
    const focusFilter = command("Filter files", { movesFocus: true });
    offer([focusFilter]);
    function Palette() {
      const [open, setOpen] = useState(true);
      return (
        <QuickPickCommandPalette
          open={open}
          onOpenChange={setOpen}
          onToggleLeftSidebar={vi.fn()}
          onToggleRightSidebar={vi.fn()}
        />
      );
    }
    render(
      <QueryClientProvider client={new QueryClient()}>
        <Palette />
      </QueryClientProvider>,
    );
    accept(screen.getByTestId("command-palette-input"), "filter files");
    // The feature unmounts after the accept, before the palette has finished closing.
    usePaletteCommandsStore.setState({ groups: {} });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await act(async () => {
      await new Promise((resolve) => {
        setTimeout(resolve, 20);
      });
    });
    expect(focusFilter.run).not.toHaveBeenCalled();
  });

  it.each(["withdrawn", "greyed out"] as const)(
    "keeps the palette open when the accepted row's command was %s since",
    (change) => {
      const later = command("Later");
      offer([later]);
      const { input, onOpenChange } = renderPalette();
      fireEvent.change(input, { target: { value: "later" } });
      // Changed between the render and the accept, as a feature unmounting or updating would.
      usePaletteCommandsStore.setState({
        groups:
          change === "withdrawn"
            ? {}
            : {
                tabs: {
                  heading: "Workspace",
                  commands: [{ ...later, availability: unavailableBecause("Not now.") }],
                },
              },
      });
      fireEvent.keyDown(input, { key: "Enter" });
      expect(later.run).not.toHaveBeenCalled();
      expect(onOpenChange).not.toHaveBeenCalled();
    },
  );
});
