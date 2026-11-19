import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import type * as UseTerminalsModule from "@/hooks/useTerminals";
import { isCommandListed, usePaletteCommandsStore } from "@/store/paletteCommands";
import { usePullRequestListCounts } from "@/store/pullRequestListCounts";
import { registeredPaletteCommands } from "@/test/paletteCommands";

import type { WorkspacePlaceViewProps } from "./placeViews/WorkspacePlaceView";
import { WorkspaceTabsPanel, type WorkspaceTabsPanelProps } from "./WorkspaceTabsPanel";
import {
  openWorkspaceFile,
  resetWorkspaceTabsSessionsForTests,
  workspaceTabsSession,
} from "./workspaceTabsSessions";

// The tabs' content has its own suites; these tests cover which panel owns the keyboard, and
// what the panel hands each place view.
const placeViews = vi.hoisted(() => ({
  latest: new Map<string, WorkspacePlaceViewProps>(),
  bySession: new Map<string, WorkspacePlaceViewProps>(),
}));
vi.mock("./placeViews/WorkspacePlaceView", () => ({
  WorkspacePlaceView: (props: WorkspacePlaceViewProps) => {
    const place = props.place;
    placeViews.latest.set(`${place.kind}:${"path" in place ? (place.path ?? "") : ""}`, props);
    placeViews.bySession.set(props.conversationId, props);
    return <div data-testid="place-view-stub" />;
  },
}));
// The Search tab's results have their own suite; this one checks what choosing one opens.
const contentSearch = vi.hoisted(() => ({
  onFileSelect: undefined as
    ((path: string, options?: { line?: number; column?: number }) => void) | undefined,
}));
vi.mock("../ContentSearchPanel", () => ({
  ContentSearchPanel: (props: {
    onFileSelect: typeof contentSearch.onFileSelect;
    inputRef?: React.Ref<HTMLInputElement>;
  }) => {
    contentSearch.onFileSelect = props.onFileSelect;
    return <input aria-label="Search text in workspace" ref={props.inputRef} />;
  },
}));
vi.mock("@/components/BrowserPane/BrowserPane", () => ({
  BrowserPane: () => <div data-testid="browser-pane-stub" />,
}));
vi.mock("@/hooks/useTerminals", async (importOriginal) => ({
  ...(await importOriginal<typeof UseTerminalsModule>()),
  useTerminals: vi.fn(() => ({ terminals: [], isLoading: false, error: null })),
}));

// A shell tab shows upstream's terminal view, whose connection has its own suite.
vi.mock("@/components/blocks/TerminalView", () => ({
  TerminalView: ({ terminalId }: { terminalId: string }) => (
    <div data-testid="terminal-view-stub" data-terminal={terminalId} />
  ),
}));

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

type PanelOverrides = Partial<WorkspaceTabsPanelProps> & { conversationId: string };

function panel(overrides: PanelOverrides) {
  return (
    <WorkspaceTabsPanel
      key={overrides.conversationId}
      width={360}
      handleProps={{ tabIndex: 0 }}
      showFilesPanel
      showGithubTab={false}
      showBrowserTab
      changedCount={0}
      changedFiles={[]}
      subagentsWorking={0}
      agentCount={0}
      rootSessionId={overrides.conversationId}
      terminalKeys={[]}
      openTerminalTab={vi.fn()}
      confirmCloseShell={vi.fn(async () => true)}
      endShell={vi.fn(async () => true)}
      maximized={false}
      onToggleMaximized={vi.fn()}
      permissionLevel={3}
      filesPanelSort="alpha"
      onSortChange={vi.fn()}
      filesPanelShowHidden={false}
      onShowHiddenChange={vi.fn()}
      {...overrides}
    />
  );
}

function renderPanels(panels: readonly PanelOverrides[]) {
  const tree = (current: readonly PanelOverrides[]) => (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <TooltipProvider delayDuration={0}>{current.map(panel)}</TooltipProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
  const view = render(tree(panels));
  return { rerender: (next: readonly PanelOverrides[]) => view.rerender(tree(next)) };
}

/** The sessions whose workspace rows ⌘K offers, by the id each row is namespaced under. */
function sessionsWithWorkspaceRows(): string[] {
  const ids = registeredPaletteCommands()
    .map((command) => /^workspace:([^:]+):/.exec(command.id)?.[1])
    .filter((id): id is string => id !== undefined);
  return [...new Set(ids)].sort();
}

function listedPaletteCommands() {
  return registeredPaletteCommands().filter((command) => isCommandListed(command.availability));
}

function browserTabCount(sessionId: string): number {
  return workspaceTabsSession(sessionId)
    .store.tabs()
    .filter((tab) => "place" in tab && tab.place.kind === "web").length;
}

/** Cmd+Alt+B on a Mac, the new-browser chord. */
function pressNewBrowser() {
  fireEvent.keyDown(window, { code: "KeyB", key: "b", metaKey: true, altKey: true });
}

/** Cmd+Alt+W, Close tab's chord where no desktop shell routes ⌘W. */
function pressCloseTab() {
  fireEvent.keyDown(window, { code: "KeyW", key: "w", metaKey: true, altKey: true });
}

beforeEach(() => {
  vi.spyOn(navigator, "platform", "get").mockReturnValue("MacIntel");
  placeViews.latest.clear();
  placeViews.bySession.clear();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  resetWorkspaceTabsSessionsForTests();
  usePaletteCommandsStore.setState({ groups: {} });
  usePullRequestListCounts.setState({ counts: {} });
  queryClient.clear();
});

describe("WorkspaceTabsPanel keyboard target", () => {
  it("offers the session view's rows and browser chord with no props, as before", async () => {
    renderPanels([{ conversationId: "conv_a" }]);
    await waitFor(() => expect(sessionsWithWorkspaceRows()).not.toEqual([]));
    expect(sessionsWithWorkspaceRows()).toEqual(["conv_a"]);
    const before = browserTabCount("conv_a");

    act(pressNewBrowser);

    await waitFor(() => expect(browserTabCount("conv_a")).toBe(before + 1));
  });

  it("lets only the keyboard target among contained panels answer ⌘K and the chords", async () => {
    const { rerender } = renderPanels([
      { conversationId: "conv_a", contained: true, keyboardTarget: true },
      { conversationId: "conv_b", contained: true, keyboardTarget: false },
    ]);
    await waitFor(() => expect(sessionsWithWorkspaceRows()).not.toEqual([]));
    expect(sessionsWithWorkspaceRows()).toEqual(["conv_a"]);
    const [a, b] = [browserTabCount("conv_a"), browserTabCount("conv_b")];

    act(pressNewBrowser);
    await waitFor(() => expect(browserTabCount("conv_a")).toBe(a + 1));
    expect(browserTabCount("conv_b")).toBe(b);

    // Selecting the other cell moves every row and chord with it.
    rerender([
      { conversationId: "conv_a", contained: true, keyboardTarget: false },
      { conversationId: "conv_b", contained: true, keyboardTarget: true },
    ]);
    expect(sessionsWithWorkspaceRows()).toEqual(["conv_b"]);

    act(pressNewBrowser);
    await waitFor(() => expect(browserTabCount("conv_b")).toBe(b + 1));
    expect(browserTabCount("conv_a")).toBe(a + 1);

    // Close tab closes the target's selected browser tab only.
    act(pressCloseTab);
    await waitFor(() => expect(browserTabCount("conv_b")).toBe(b));
    expect(browserTabCount("conv_a")).toBe(a + 1);
  });

  it("hands keyboard ownership to the target's place view only, while both show Files", async () => {
    renderPanels([
      { conversationId: "conv_a", contained: true, keyboardTarget: true },
      { conversationId: "conv_b", contained: true, keyboardTarget: false },
    ]);
    await act(async () => {
      for (const id of ["conv_a", "conv_b"]) {
        // oxlint-disable-next-line no-await-in-loop
        await workspaceTabsSession(id).store.open({ place: { kind: "files" } }, { opener: "user" });
      }
    });
    await waitFor(() => expect(placeViews.bySession.size).toBe(2));
    expect(placeViews.bySession.get("conv_a")).toMatchObject({
      active: true,
      keyboardTarget: true,
    });
    expect(placeViews.bySession.get("conv_b")).toMatchObject({
      active: true,
      keyboardTarget: false,
    });
  });

  it("offers one browser row in a contained target, its own chord's, and none elsewhere", async () => {
    renderPanels([
      { conversationId: "conv_a", contained: true, keyboardTarget: true },
      { conversationId: "conv_b", contained: true, keyboardTarget: false },
    ]);
    await waitFor(() => expect(sessionsWithWorkspaceRows()).not.toEqual([]));

    const browserRows = listedPaletteCommands().filter(
      (command) => command.label === "Open a new browser tab",
    );
    expect(browserRows).toHaveLength(1);
    expect(browserRows[0].nativeShortcut).toBeDefined();
  });
});

describe("the panel's placement", () => {
  const workspace = () => screen.getByRole("complementary", { name: "Workspace", hidden: true });

  it("docks in the rail by default, with its width, resize handle and full screen", () => {
    renderPanels([
      { conversationId: "conv_a", handleProps: { tabIndex: 0, "data-testid": "resize" } as never },
    ]);
    expect(workspace().className).toMatch(/(^| )hidden( |$)/);
    expect(workspace().style.width).toBe("360px");
    expect(screen.getByTestId("resize")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Full screen", hidden: true })).toBeInTheDocument();
  });

  it("fills a drawer at any width, with no resize handle or full screen", () => {
    renderPanels([
      {
        conversationId: "conv_a",
        placement: "drawer",
        handleProps: { tabIndex: 0, "data-testid": "resize" } as never,
      },
    ]);
    // Shown below the desktop breakpoint too, and as wide as the drawer.
    expect(workspace().className).not.toMatch(/(^| )hidden( |$)/);
    expect(workspace().className).toMatch(/(^| )flex( |$)/);
    expect(workspace().style.width).toBe("");
    expect(screen.queryByTestId("resize")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Full screen", hidden: true }),
    ).not.toBeInTheDocument();
  });
});

describe("the Tasks tab's offer", () => {
  it.each([
    [true, "lists Open the Tasks panel among the workspace's commands"],
    [false, "leaves it out when the session has no workflows"],
  ])("%s: %s", async (showTasksTab) => {
    renderPanels([{ conversationId: "s-tasks", showTasksTab }]);
    await waitFor(() => expect(sessionsWithWorkspaceRows()).not.toEqual([]));
    const labels = listedPaletteCommands().map((command) => command.label);
    expect(labels.includes("Open the Tasks panel")).toBe(showTasksTab);
  });
});

describe("the Pull requests tab's count pill", () => {
  it("shows how many pull requests its list shows, as that changes, and none on one PR's tab", async () => {
    renderPanels([{ conversationId: "s-pulls", showGithubTab: true }]);
    const session = workspaceTabsSession("s-pulls");
    await act(async () => {
      await session.store.open({ place: { kind: "pulls" } }, { opener: "user" });
      await session.store.open(
        { place: { kind: "pull", url: "https://github.com/acme/app/pull/1", view: "summary" } },
        { opener: "user" },
      );
    });
    const pullsTab = () => screen.getByRole("tab", { name: /^Pull requests/ });
    const pullTab = () => screen.getByRole("tab", { name: /#1/ });
    // Until the list shows its rows, there's nothing to count.
    expect(pullsTab()).toHaveTextContent(/^Pull requests$/);
    const pullTitle = pullTab().textContent;

    act(() => usePullRequestListCounts.getState().publish("s-pulls", 4));
    expect(pullsTab()).toHaveTextContent(/^Pull requests4$/);
    act(() => usePullRequestListCounts.getState().publish("s-pulls", 3));
    expect(pullsTab()).toHaveTextContent(/^Pull requests3$/);
    expect(pullTab().textContent).toBe(pullTitle);

    // Another session's list doesn't count here, and an empty list shows no pill.
    act(() => usePullRequestListCounts.getState().publish("s-other", 9));
    expect(pullsTab()).toHaveTextContent(/^Pull requests3$/);
    act(() => usePullRequestListCounts.getState().publish("s-pulls", 0));
    expect(pullsTab()).toHaveTextContent(/^Pull requests$/);
  });
});

describe("a pull request chosen in the Pull requests tab", () => {
  it("becomes the tab's place: its title, its saved tabs and a step Back returns from", async () => {
    renderPanels([{ conversationId: "s-chosen-pr", showGithubTab: true }]);
    const session = workspaceTabsSession("s-chosen-pr");
    await act(async () => {
      await session.store.open({ place: { kind: "pulls" } }, { opener: "user" });
    });
    const tabId = session.store.selectedTabId()!;
    const url = "https://github.com/acme/app/pull/1";
    await act(async () => {
      placeViews.latest
        .get("pulls:")!
        .onNavigate({ kind: "pull", url, view: "summary" }, { replace: false });
    });
    expect(session.store.findTab(tabId)?.place).toEqual({ kind: "pull", url, view: "summary" });
    expect(screen.getByRole("tab", { name: /#1/ })).toBeInTheDocument();
    expect(window.localStorage.getItem("omnigent:workspace-tabs")).toContain(url);
    await act(async () => {
      await session.store.go("back");
    });
    expect(session.store.findTab(tabId)?.place).toEqual({ kind: "pulls" });
    expect(screen.getByRole("tab", { name: /^Pull requests/ })).toBeInTheDocument();
  });
});

describe("a changed file's own Diffs tab", () => {
  const changed = (path: string) => ({
    path,
    name: path.split("/").at(-1)!,
    status: "modified" as const,
    bytes: null,
    modified_at: null,
    lines_added: 1,
    lines_removed: 0,
  });
  // Listed out of order: the tab counts its place as the review orders the files.
  const changedFiles = [changed("b/main.go"), changed("c.go"), changed("a.go")];

  it("names the file with its place among the changes, also after a reload", async () => {
    renderPanels([{ conversationId: "s-file-tab", changedCount: 3, changedFiles }]);
    const session = workspaceTabsSession("s-file-tab");
    await act(async () => {
      await session.store.open({ place: { kind: "changes" } }, { opener: "user" });
      await session.store.open(
        { place: { kind: "changes", path: "b/main.go", fileTab: true } },
        { opener: "user", keepSelection: true },
      );
    });
    const expectTitles = () => {
      expect(screen.getByRole("tab", { name: /^Diffs/ })).toHaveTextContent(/^Diffs3$/);
      expect(screen.getByRole("tab", { name: /^main\.go/ })).toHaveTextContent(/^main\.go2\/3$/);
    };
    expectTitles();

    cleanup();
    resetWorkspaceTabsSessionsForTests();
    renderPanels([{ conversationId: "s-file-tab", changedCount: 3, changedFiles }]);
    expectTitles();
  });
});

describe("WorkspaceTabsPanel diff links", () => {
  it("opens a diff link's cited lines in the review, replacing an earlier citation", async () => {
    const sessionId = "s-diff-link";
    renderPanels([{ conversationId: sessionId }]);
    const session = workspaceTabsSession(sessionId);
    await act(async () => {
      await openWorkspaceFile(sessionId, "server/long.py", { line: 3, view: "diff" }, "user");
    });
    await act(async () => {
      await openWorkspaceFile(
        sessionId,
        "server/long.py",
        { line: 150, endLine: 151, column: 9, view: "diff" },
        "user",
      );
    });
    await waitFor(() =>
      expect(placeViews.latest.get("changes:server/long.py")?.position).toEqual({
        line: 150,
        endLine: 151,
        column: 9,
      }),
    );
    expect(placeViews.latest.get("files:server/long.py")).toBeUndefined();
    expect(
      session.store.tabs().filter((tab) => "place" in tab && tab.place.kind === "changes"),
    ).toHaveLength(1);
  });
});

describe("WorkspaceTabsPanel search results", () => {
  it("opens a chosen match's file at its line", async () => {
    renderPanels([{ conversationId: "s-search-line", showContentSearch: true }]);
    const { store } = workspaceTabsSession("s-search-line");
    await act(async () => {
      await store.open({ place: { kind: "search" } }, { opener: "user" });
    });
    await waitFor(() => expect(contentSearch.onFileSelect).toBeDefined());
    await act(async () => {
      contentSearch.onFileSelect?.("src/app.py", { line: 351, column: 3 });
    });
    await waitFor(() =>
      expect(
        store
          .tabs()
          .flatMap((tab) => ("place" in tab && tab.place.kind === "files" ? [tab.place] : [])),
      ).toContainEqual({ kind: "files", path: "src/app.py", line: { start: 351, column: 3 } }),
    );
  });
});

describe("pinning a tab from its menu", () => {
  it("keeps keyboard focus on the tab as it is pinned and unpinned", async () => {
    renderPanels([{ conversationId: "s-pin-keeps-focus" }]);
    const { store } = workspaceTabsSession("s-pin-keeps-focus");
    await act(async () => {
      await store.open({ place: { kind: "changes" } }, { opener: "user" });
      await store.open({ place: { kind: "files" } }, { opener: "user" });
    });
    const changesTab = () =>
      document.querySelector<HTMLElement>(
        `[data-workspace-tab-id="${store.tabs().find((tab) => "place" in tab && tab.place.kind === "changes")!.id}"]`,
      )!;
    const chooseFromItsMenu = async (label: RegExp) => {
      changesTab().focus();
      fireEvent.contextMenu(changesTab().closest("[data-tab-strip-entry]")!);
      fireEvent.click(await screen.findByRole("menuitem", { name: label }));
      // The tab moved to the other group; focus follows it, not the page.
      await waitFor(() => expect(document.activeElement).toBe(changesTab()));
    };
    await chooseFromItsMenu(/^Pin tab/);
    await chooseFromItsMenu(/^Unpin tab/);
  });
});

describe("a shell's tab", () => {
  it("shows the session's live shell in upstream's terminal view", async () => {
    const { useTerminals } = await import("@/hooks/useTerminals");
    const terminal = {
      id: "term_1",
      name: "term_1:s1",
      terminalName: "zsh",
      sessionKey: "s1",
      running: true,
    };
    vi.mocked(useTerminals).mockReturnValue({
      terminals: [terminal],
      isLoading: false,
      error: null,
      listCompleteAt: 1,
    } as unknown as ReturnType<typeof useTerminals>);
    renderPanels([{ conversationId: "s-shell", terminalKeys: ["terminal:term_1"] }]);
    const { store } = workspaceTabsSession("s-shell");
    await act(async () => {
      await store.open({ place: { kind: "terminal", key: "terminal:term_1" } }, { opener: "user" });
    });
    expect(await screen.findByTestId("terminal-view-stub")).toHaveAttribute(
      "data-terminal",
      "term_1",
    );
  });
});
