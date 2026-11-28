import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";

import { isPinnedTab } from "./pinnedTabs";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { browserViewId } from "@/lib/browserTabs";
import type * as UseTerminalsModule from "@/hooks/useTerminals";
import type * as SessionsApiModule from "@/lib/sessionsApi";
import type { PullRequest, PullRequestAssociation } from "@/hooks/usePullRequests";
import type * as PanesViewGateModule from "../panesViewGate";
import { resetPaneSplitsCache, updatePaneSplit } from "../paneSplitStore";
import { useFileFinderStore } from "@/store/fileFinder";
import type { TerminalGroup } from "@/lib/terminalGroups";
import {
  PINNED_WORKSPACE_TABS_STORAGE_KEY,
  writeDefaultWorkspaceTab,
  writePinnedWorkspaceTabs,
} from "@/lib/workspaceTabPreferences";

import { resetTerminalGroupsCache, updateTerminalGroups } from "../terminalGroupsStore";
import { requestTerminalOpen, resetTerminalOpenRequestsForTests } from "../terminalOpenRequest";
import { resetTerminalPlacement, usePublishMainTerminalKeys } from "../terminalPlacement";
import { MOUNTED_SHELL_TAB_LIMIT } from "./shellTabBudget";
import { LEVEL_EDIT, LEVEL_OWNER, LEVEL_READ } from "@/lib/permissionsApi";
import { terminalsQueryKey } from "@/lib/terminals";
import { readRecentWorkspaceFiles } from "@/lib/recentWorkspaceFiles";
import { LOCAL_RECONNECT_DESCRIPTION, LOCAL_RECONNECT_TITLE } from "@/shell/ReconnectSessionDialog";
import {
  isCommandAvailable,
  isCommandListed,
  usePaletteCommandsStore,
} from "@/store/paletteCommands";
import { usePullRequestListCounts } from "@/store/pullRequestListCounts";
import { registeredPaletteCommands } from "@/test/paletteCommands";

import type { WorkspacePlaceViewProps } from "./placeViews/WorkspacePlaceView";
import { treeFileChosen } from "./placeViews/placeViewActions";
import type { RecentPullRequestFacts } from "./recentPullRequestRow";
import { SHELL_LIST_LIMIT_MS } from "./terminalTabConnection";
import { OWNER_RECONNECT_HINT, VIEWER_RESUME_HINT } from "./TerminalTabStatus";
import type * as UseRecentPullRequestFacts from "./useRecentPullRequestFacts";
import { resetSessionResumes } from "./useTerminalTabConnection";
import { WorkspaceTabsPanel, type WorkspaceTabsPanelProps } from "./WorkspaceTabsPanel";
import {
  openWorkspaceFile,
  openWorkspaceNewTab,
  resetWorkspaceTabsSessionsForTests,
  workspaceTabsSession,
} from "./workspaceTabsSessions";
import { MAX_WORKSPACE_TABS, saveWorkspaceTabsSession } from "./workspaceTabsPersistence";

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
// Pull request facts a test knows already, as if the session had read them; null reads the cache.
const knownPullRequests = vi.hoisted(() => ({
  facts: null as null | RecentPullRequestFacts,
}));
vi.mock("./useRecentPullRequestFacts", async (importOriginal) => {
  const original = await importOriginal<typeof UseRecentPullRequestFacts>();
  return {
    useRecentPullRequestFacts: (conversationId: string, urls: readonly string[]) => {
      const real = original.useRecentPullRequestFacts(conversationId, urls);
      return knownPullRequests.facts ?? real;
    },
  };
});
vi.mock("@/components/BrowserPane/BrowserPane", () => ({
  BrowserPane: () => <div data-testid="browser-pane-stub" />,
}));
vi.mock("@/hooks/useTerminals", async (importOriginal) => ({
  ...(await importOriginal<typeof UseTerminalsModule>()),
  useTerminals: vi.fn(() => ({ terminals: [], isLoading: false, error: null })),
}));

// Resume session asks the server to start the runner; the tests answer for it.
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApiModule>()),
  retrySession: vi.fn(async () => ({})),
}));
// A shell tab shows upstream's terminal view, whose connection has its own suite.
vi.mock("@/components/blocks/TerminalView", () => ({
  TerminalView: ({ terminalId }: { terminalId: string }) => (
    <div data-testid="terminal-view-stub" data-terminal={terminalId} />
  ),
}));

// Whether the session's main area shows the panes view; off unless a case turns it on.
const panesGate = vi.hoisted(() => ({ shown: false }));
vi.mock("../panesViewGate", async (importOriginal) => ({
  ...(await importOriginal<typeof PanesViewGateModule>()),
  usePanesView: () => panesGate.shown,
  usePanesSplitsAllowed: (readOnly: boolean) => panesGate.shown && !readOnly,
}));

const floatingPanes = vi.hoisted(() => ({ on: false, wide: true }));
vi.mock("../useFloatingPanes", () => ({
  useFloatingPanes: () => floatingPanes.on,
  useFloatingPanesShown: () => floatingPanes.on && floatingPanes.wide,
}));

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
// A shell that couldn't end is still running, so its toast stays until the person dismisses it.
const STAYS_UNTIL_DISMISSED = { duration: Number.POSITIVE_INFINITY, closeButton: true };

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
      confirmCloseShells={vi.fn(async () => true)}
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
  // These sessions start on an empty New tab; the Settings pins have their own tests.
  window.localStorage.setItem(PINNED_WORKSPACE_TABS_STORAGE_KEY, "[]");
  vi.spyOn(navigator, "platform", "get").mockReturnValue("MacIntel");
  placeViews.latest.clear();
  placeViews.bySession.clear();
  floatingPanes.on = false;
  floatingPanes.wide = true;
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  resetWorkspaceTabsSessionsForTests();
  usePaletteCommandsStore.setState({ groups: {} });
  usePullRequestListCounts.setState({ counts: {} });
  queryClient.clear();
  resetSessionResumes();
  knownPullRequests.facts = null;
  panesGate.shown = false;
  resetPaneSplitsCache();
  resetTerminalPlacement();
  resetTerminalOpenRequestsForTests();
  resetTerminalGroupsCache();
  window.localStorage.clear();
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

  // A closed rail is aria-hidden, which leaves its landmark out of the accessibility tree.
  const rail = () => document.querySelector<HTMLElement>('aside[aria-label="Workspace"]')!;

  it("names its open state data-rail-state, so a toggle doesn't restyle what's inside", () => {
    const view = renderPanels([{ conversationId: "conv_a", open: true }]);
    expect(rail()).toHaveAttribute("data-rail-state", "open");
    expect(rail()).not.toHaveAttribute("data-state");
    view.rerender([{ conversationId: "conv_a", open: false }]);
    expect(rail()).toHaveAttribute("data-rail-state", "closed");
  });

  it("moves focus out of a collapsing rail before it goes inert, and keeps it out", () => {
    const outside = document.createElement("button");
    document.body.append(outside);
    const view = renderPanels([{ conversationId: "conv_a", open: true, inert: false }]);
    const inside = screen.getByRole("button", { name: "Full screen", hidden: true });
    inside.focus();
    expect(inside).toHaveFocus();
    view.rerender([{ conversationId: "conv_a", open: false, inert: false }]);
    expect(rail()).not.toHaveAttribute("inert");
    expect(inside).not.toHaveFocus();
    // Focus that enters from outside goes back where it came from.
    outside.focus();
    inside.focus();
    expect(outside).toHaveFocus();
    outside.blur();
    outside.remove();
  });

  it("keeps focus out of an open rail while its host says it's about to go inert", () => {
    const view = renderPanels([{ conversationId: "conv_a", open: true, inertPending: true }]);
    const inside = screen.getByRole("button", { name: "Full screen", hidden: true });
    inside.focus();
    expect(inside).not.toHaveFocus();
    view.rerender([{ conversationId: "conv_a", open: true, inertPending: false }]);
    inside.focus();
    expect(inside).toHaveFocus();
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

describe("the gutter beside floating panes", () => {
  const gutter = () => document.querySelector<HTMLElement>("[data-workspace-rail-gutter-handle]");
  const rail = () => document.querySelector<HTMLElement>('aside[aria-label="Workspace"]')!;

  it("drags the docked rail from a band beside it, placed by the rail's width", () => {
    floatingPanes.on = true;
    const onPointerDown = vi.fn();
    renderPanels([
      { conversationId: "conv_a", open: true, handleProps: { tabIndex: 0, onPointerDown } },
    ]);
    expect(rail().nextElementSibling).toBe(gutter());
    expect(gutter()!.style.right).toBe("355px");
    fireEvent.pointerDown(gutter()!);
    expect(onPointerDown).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["without floating panes", { floating: false }],
    ["full screen", { floating: true, maximized: true }],
    ["in a grid cell", { floating: true, contained: true }],
  ])("has no band %s", (_case, { floating, ...overrides }) => {
    floatingPanes.on = floating;
    renderPanels([{ conversationId: "conv_a", open: true, ...overrides }]);
    expect(gutter()).toBeNull();
  });
});

describe("the strip's Collapse right panel button", () => {
  const collapse = () => screen.getByRole("button", { name: "Collapse right panel", hidden: true });

  it("with floating panes, shows the collapse-right icon, stays in a narrow strip and hides the sidebar", () => {
    floatingPanes.on = true;
    const onHide = vi.fn();
    renderPanels([{ conversationId: "conv_a", open: true, onHide }]);
    expect(collapse().querySelector(".lucide-panel-right-close")).not.toBeNull();
    expect(collapse().querySelector(".lucide-x")).toBeNull();
    // The header hides its own toggle while this sidebar is open, so this one never folds away.
    expect(collapse().className).not.toContain("@max-[240px]/rail:hidden");
    fireEvent.click(collapse());
    expect(onHide).toHaveBeenCalledTimes(1);
  });

  it("without floating panes, keeps its X and leaves a narrow strip to the header's toggle", () => {
    renderPanels([{ conversationId: "conv_a", open: true, onHide: vi.fn() }]);
    expect(collapse().querySelector(".lucide-x")).not.toBeNull();
    expect(collapse().querySelector(".lucide-panel-right-close")).toBeNull();
    expect(collapse().className).toContain("@max-[240px]/rail:hidden");
  });
});

describe("the New tab's Search text", () => {
  afterEach(() => {
    useFileFinderStore.setState({ availableSessionId: null, textSearchAvailable: false });
  });

  it("shows only where the session's files can be searched for text, not just found", async () => {
    useFileFinderStore.setState({ availableSessionId: "s-files", textSearchAvailable: false });
    renderPanels([{ conversationId: "s-files" }]);
    act(() => void openWorkspaceNewTab("s-files"));
    await screen.findByTestId("workspace-new-tab-page");
    expect(screen.queryByRole("option", { name: /^Search text/ })).toBeNull();

    act(() => useFileFinderStore.setState({ textSearchAvailable: true }));
    expect(await screen.findByRole("option", { name: /^Search text/ })).toBeInTheDocument();
  });
});

describe("All tabs' place in the strip", () => {
  const button = (name: string) => screen.getByRole("button", { name, hidden: true });
  const precedes = (first: Element, second: Element) =>
    Boolean(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING);
  const firstTab = () => screen.getAllByRole("tab", { hidden: true })[0]!;
  const stripDividers = () =>
    document.querySelectorAll('aside[aria-label="Workspace"] [data-slot="separator"]');

  it("leads the strip as layers before the tabs, with the divider after New tab", () => {
    renderPanels([{ conversationId: "conv_a", open: true }]);
    const allTabs = button("All tabs");
    expect(precedes(allTabs, firstTab())).toBe(true);
    expect(allTabs.querySelector(".lucide-layers")).not.toBeNull();
    expect(stripDividers()).toHaveLength(1);
    expect(precedes(button("New tab"), stripDividers()[0]!)).toBe(true);
    expect(precedes(stripDividers()[0]!, button("Full screen"))).toBe(true);
  });

  it("with floating panes, opens from a chevron beside Full screen, after the divider", () => {
    floatingPanes.on = true;
    renderPanels([{ conversationId: "conv_a", open: true }]);
    const allTabs = button("All tabs");
    expect(precedes(firstTab(), allTabs)).toBe(true);
    expect(precedes(button("New tab"), allTabs)).toBe(true);
    expect(precedes(allTabs, button("Full screen"))).toBe(true);
    expect(allTabs.querySelector(".lucide-chevron-down")).not.toBeNull();
    // One divider, between New tab and All tabs.
    expect(stripDividers()).toHaveLength(1);
    expect(precedes(button("New tab"), stripDividers()[0]!)).toBe(true);
    expect(precedes(stripDividers()[0]!, allTabs)).toBe(true);
  });

  it("keeps the classic place in a grid cell, which floating panes leave as it is", () => {
    floatingPanes.on = true;
    renderPanels([{ conversationId: "conv_a", open: true, contained: true }]);
    const allTabs = button("All tabs");
    expect(precedes(allTabs, firstTab())).toBe(true);
    expect(allTabs.querySelector(".lucide-layers")).not.toBeNull();
    expect(precedes(button("New tab"), stripDividers()[0]!)).toBe(true);
  });

  // The drawer holds the tabs below the desktop breakpoint, past 48rem too, where the panes float.
  it.each([
    ["a narrow window's", false],
    ["a wider window's", true],
  ])("in %s drawer, stays before the tabs as layers with the flag on", async (_, wide) => {
    floatingPanes.on = true;
    floatingPanes.wide = wide;
    renderPanels([{ conversationId: "conv_a", open: true, placement: "drawer" }]);
    const allTabs = button("All tabs");
    expect(precedes(allTabs, firstTab())).toBe(true);
    expect(allTabs.querySelector(".lucide-layers")).not.toBeNull();
    expect(allTabs.querySelector(".lucide-chevron-down")).toBeNull();
    expect(stripDividers()).toHaveLength(1);
    expect(precedes(button("New tab"), stripDividers()[0]!)).toBe(true);
    await userEvent.click(allTabs);
    expect(await screen.findByRole("dialog", { hidden: true })).toHaveAttribute(
      "data-align",
      "start",
    );
  });

  it("with floating panes, lines its list up with the button's end", async () => {
    floatingPanes.on = true;
    renderPanels([{ conversationId: "conv_a", open: true }]);
    await userEvent.click(button("All tabs"));
    const list = await screen.findByRole("dialog", { hidden: true });
    expect(list).toHaveAttribute("data-align", "end");
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

describe("WorkspaceTabsPanel and the Workspace settings", () => {
  it("starts a new session on its pinned pages and follows a changed first page", async () => {
    writePinnedWorkspaceTabs(["files", "subagents", "tasks"]);
    writeDefaultWorkspaceTab("subagents");
    // Tasks isn't offered in this session, so it isn't pinned there.
    renderPanels([{ conversationId: "s-settings", showTasksTab: false }]);
    const { store } = workspaceTabsSession("s-settings");
    const strip = () =>
      store.tabs().map((tab) => ("place" in tab ? [tab.place.kind, tab.pinned === true] : null));
    await waitFor(() =>
      expect(strip()).toEqual([
        ["files", true],
        ["agents", true],
      ]),
    );
    expect(store.selectedTab()?.place.kind).toBe("agents");
    act(() => writeDefaultWorkspaceTab("files"));
    await waitFor(() => expect(store.selectedTab()?.place.kind).toBe("files"));
  });

  it("keeps a file a link opened before the sidebar first showed, with Settings as shipped", async () => {
    window.localStorage.removeItem(PINNED_WORKSPACE_TABS_STORAGE_KEY);
    const sessionId = "s-settings-link";
    await openWorkspaceFile(sessionId, "src/linked.ts", { line: 7, commentId: "c1" }, "link");
    const { store } = workspaceTabsSession(sessionId);
    renderPanels([{ conversationId: sessionId }]);
    await waitFor(() =>
      expect(store.tabs().some((tab) => "place" in tab && tab.place.kind === "agents")).toBe(true),
    );
    expect(store.selectedTab()?.place).toMatchObject({ kind: "files", path: "src/linked.ts" });
  });

  it("adds a pinned page that didn't fit once a tab closes", async () => {
    const sessionId = "s-settings-full";
    const full = Array.from({ length: MAX_WORKSPACE_TABS }, (_, index) => ({
      id: `kept-${index}`,
      place: { kind: "files" as const, path: `src/file-${index}.ts` },
      openedBy: "user" as const,
    }));
    saveWorkspaceTabsSession(sessionId, {
      tabs: full,
      selectedTabId: full[0].id,
      timeline: [{ tabId: full[0].id, place: full[0].place }],
      cursor: 0,
      closed: [],
    });
    writePinnedWorkspaceTabs(["tasks"]);
    writeDefaultWorkspaceTab("tasks");
    renderPanels([{ conversationId: sessionId, showTasksTab: true }]);
    const { store } = workspaceTabsSession(sessionId);
    const hasTasks = () => store.tabs().some((tab) => "place" in tab && tab.place.kind === "tasks");
    expect(store.tabs()).toHaveLength(MAX_WORKSPACE_TABS);
    expect(hasTasks()).toBe(false);
    await act(async () => {
      await store.close(full.at(-1)!.id);
    });
    await waitFor(() => expect(hasTasks()).toBe(true));
    expect(store.tabs()).toHaveLength(MAX_WORKSPACE_TABS);
  });
});

describe("recently opened files, for Go to File", () => {
  it("lists a file opened from the Files tree, and from a Search result", async () => {
    renderPanels([{ conversationId: "s-recent-files", showContentSearch: true }]);
    const { store } = workspaceTabsSession("s-recent-files");
    await act(async () => {
      await store.open({ place: { kind: "files" } }, { opener: "user" });
    });
    await waitFor(() => expect(placeViews.latest.get("files:")).toBeDefined());
    const tree = treeFileChosen("src/tree.ts");
    if (tree.type !== "open") throw new Error("the tree opens its file");
    await act(async () => {
      placeViews.latest.get("files:")?.onOpen(tree.link, tree.options);
    });
    await waitFor(() =>
      expect(readRecentWorkspaceFiles("s-recent-files")).toEqual(["src/tree.ts"]),
    );

    await act(async () => {
      await store.open({ place: { kind: "search" } }, { opener: "user" });
    });
    await waitFor(() => expect(contentSearch.onFileSelect).toBeDefined());
    await act(async () => {
      contentSearch.onFileSelect?.("src/found.py", { line: 7 });
    });
    await waitFor(() =>
      expect(readRecentWorkspaceFiles("s-recent-files")).toEqual(["src/found.py", "src/tree.ts"]),
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

describe("a browser view's tab", () => {
  it("forgets its view once closing the tab closed it, so a link opens a fresh page", async () => {
    const { openBrowserTab, readBrowserTabsState } = await import("@/hooks/useBrowserTabs");
    renderPanels([{ conversationId: "s-web" }]);
    const { store } = workspaceTabsSession("s-web");
    // A chat link opened this view, which the session's browser state lists.
    act(() => {
      openBrowserTab("s-web", "v1");
      store.openBrowserView("v1", { select: true, opener: "user" });
    });
    expect(readBrowserTabsState("s-web").tabs).toEqual(["v1"]);

    act(pressCloseTab);

    await waitFor(() => expect(readBrowserTabsState("s-web").tabs).toEqual([]));
  });

  it("logs a closed view the desktop app couldn't forget, rather than leaving it unhandled", async () => {
    const { fakeMainTabs } = await import("@/components/BrowserPane/agentTools/fakeMainTabs");
    const { loadTabs } = await import("@/components/BrowserPane/agentTools/tabStore");
    const { writeBrowserAgentToolsEnabled } =
      await import("@/components/BrowserPane/agentTools/preferences");
    // The desktop app keeps the session's browser tabs, and closes the view but fails to save.
    const main = fakeMainTabs();
    main.otherWindow("s-web", (record) => ({ ...record, tabs: ["v1"], selected: "v1" }));
    Object.assign(window, {
      omnigentDesktop: {
        browserClose: vi.fn().mockResolvedValue({ ok: true }),
        onBrowserTabsChanged: main.onBrowserTabsChanged,
        browserTabs: async (args: Record<string, unknown>) =>
          args.op === "update"
            ? { ok: false, error: "registry write failed" }
            : main.browserTabs(args),
      },
    });
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      writeBrowserAgentToolsEnabled(true);
      await loadTabs("s-web");
      renderPanels([{ conversationId: "s-web" }]);
      const { store } = workspaceTabsSession("s-web");
      act(() => {
        store.openBrowserView("v1", { select: true, opener: "user" });
      });

      act(pressCloseTab);

      await waitFor(() =>
        expect(warn).toHaveBeenCalledWith(
          expect.stringContaining("Couldn't release the resource of closed workspace tab"),
          expect.anything(),
        ),
      );
      expect(browserTabCount("s-web")).toBe(0);
    } finally {
      Reflect.deleteProperty(window, "omnigentDesktop");
      localStorage.clear();
    }
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
    const listing = (listCompleteAt: number, error: Error | null = null) =>
      vi.mocked(useTerminals).mockReturnValue({
        terminals: listCompleteAt > 0 ? [terminal] : [],
        isLoading: listCompleteAt === 0,
        error,
        listCompleteAt,
      } as unknown as ReturnType<typeof useTerminals>);
    const panels = [{ conversationId: "s-shell", terminalKeys: ["terminal:term_1"] }];
    // Before the session's shells are listed, the tab connects rather than calling it ended.
    listing(0);
    const view = renderPanels(panels);
    const { store } = workspaceTabsSession("s-shell");
    await act(async () => {
      await store.open({ place: { kind: "terminal", key: "terminal:term_1" } }, { opener: "user" });
    });
    expect(await screen.findByTestId("terminal-tab-connecting")).toHaveTextContent("Connecting…");
    // Its strip tab spins while it connects.
    const stripTab = screen.getByRole("tab", { name: /term_1/ });
    expect(within(stripTab).getByRole("status", { name: "Loading" })).toBeInTheDocument();
    expect(screen.queryByText("This shell has ended")).toBeNull();

    listing(1);
    view.rerender(panels);
    expect(await screen.findByTestId("terminal-view-stub")).toHaveAttribute(
      "data-terminal",
      "term_1",
    );

    // A failed refresh of the list leaves the attached shell on screen, without remounting it.
    const attached = screen.getByTestId("terminal-view-stub");
    listing(2, new Error("offline"));
    view.rerender(panels);
    await waitFor(() => expect(store.resourceState(store.selectedTabId()!)).toBe("unknown"));
    expect(screen.getByTestId("terminal-view-stub")).toBe(attached);
    expect(screen.queryByText("This shell has ended")).toBeNull();

    // While a cleared list reloads, the attached shell's strip tab doesn't spin over its terminal.
    vi.mocked(useTerminals).mockReturnValue({
      terminals: [terminal],
      isLoading: true,
      error: null,
      listCompleteAt: 0,
    } as unknown as ReturnType<typeof useTerminals>);
    view.rerender(panels);
    await waitFor(() => expect(store.resourceState(store.selectedTabId()!)).toBe("unknown"));
    expect(screen.getByTestId("terminal-view-stub")).toBe(attached);
    expect(
      within(screen.getByRole("tab", { name: /term_1/ })).queryByRole("status", {
        name: "Loading",
      }),
    ).toBeNull();
  });

  /** Listed shells, each open in its own tab, the last selected; returns each shell's tab. */
  async function openShells(
    conversationId: string,
    ids: readonly string[],
    overrides: Partial<PanelOverrides> = {},
  ) {
    const { useTerminals } = await import("@/hooks/useTerminals");
    vi.mocked(useTerminals).mockReturnValue({
      terminals: ids.map((id) => ({
        id,
        name: `${id}:s`,
        terminalName: "zsh",
        sessionKey: "s",
        running: true,
      })),
      isLoading: false,
      isFetching: false,
      error: null,
      listCompleteAt: 1,
    } as unknown as ReturnType<typeof useTerminals>);
    const panels = [
      { conversationId, terminalKeys: ids.map((id) => `terminal:${id}`), ...overrides },
    ];
    const view = renderPanels(panels);
    const { store } = workspaceTabsSession(conversationId);
    const tabOf = new Map<string, string>();
    await act(async () => {
      for (const id of ids) {
        // oxlint-disable-next-line no-await-in-loop -- each tab opens after the one before
        const opened = await store.open(
          { place: { kind: "terminal", key: `terminal:${id}` } },
          { opener: "user", disposition: "new" },
        );
        if (opened.ok) tabOf.set(id, opened.tabId);
      }
    });
    const select = (id: string) => act(() => void store.select(tabOf.get(id)!));
    return { view, panels, store, select };
  }

  const shownTerminals = () =>
    screen
      .queryAllByTestId("terminal-view-stub")
      .map((stub) => stub.dataset.terminal)
      .sort();
  const stubOf = (id: string) =>
    screen.queryAllByTestId("terminal-view-stub").find((stub) => stub.dataset.terminal === id) ??
    null;

  it("keeps a shown shell tab's terminal attached while another tab shows, and back", async () => {
    const { select } = await openShells("s-two-shells", ["term_a", "term_b"]);
    select("term_a");
    await waitFor(() => expect(shownTerminals()).toEqual(["term_a", "term_b"]));
    const attached = screen.getAllByTestId("terminal-view-stub");
    act(() => void openWorkspaceNewTab("s-two-shells"));
    select("term_a");
    // The same two terminals, never remounted.
    expect(attached.every((stub) => stub.isConnected)).toBe(true);
    expect(shownTerminals()).toEqual(["term_a", "term_b"]);
  });

  it(`keeps the ${MOUNTED_SHELL_TAB_LIMIT} most recently shown shells attached, and reconnects an older one`, async () => {
    const ids = Array.from({ length: MOUNTED_SHELL_TAB_LIMIT + 2 }, (_, index) => `term_${index}`);
    const { select } = await openShells("s-many-shells", ids);
    for (const id of ids) select(id);
    await waitFor(() => expect(shownTerminals()).toHaveLength(MOUNTED_SHELL_TAB_LIMIT));
    // The two shown longest ago let their terminals go.
    expect(stubOf("term_0")).toBeNull();
    expect(stubOf("term_1")).toBeNull();
    const newest = stubOf(ids.at(-1)!);

    select("term_0");
    await waitFor(() => expect(stubOf("term_0")).not.toBeNull());
    expect(shownTerminals()).toHaveLength(MOUNTED_SHELL_TAB_LIMIT);
    // It takes the place of the next oldest; the newest stays as it was.
    expect(stubOf("term_2")).toBeNull();
    expect(stubOf(ids.at(-1)!)).toBe(newest);
  });

  it("counts a selected split's every shell against the budget, though it joins an evicted one", async () => {
    panesGate.shown = true;
    const ids = Array.from({ length: MOUNTED_SHELL_TAB_LIMIT + 1 }, (_, index) => `term_${index}`);
    const { select } = await openShells("s-split-budget", ids, { permissionLevel: LEVEL_OWNER });
    for (const id of ids) select(id);
    await waitFor(() => expect(stubOf("term_0")).toBeNull());
    // The newest shell and the evicted oldest become one sidebar split, which then shows.
    act(() =>
      updateTerminalGroups("s-split-budget", () => [
        {
          id: "g_newest_oldest",
          split: {
            layout: {
              axis: "columns",
              ratio: 0.5,
              children: ["terminal:term_8", "terminal:term_0"],
            },
            focusedKey: "terminal:term_8",
          },
        },
      ]),
    );
    await waitFor(() => expect(stubOf("term_0")).not.toBeNull());
    expect(shownTerminals()).toHaveLength(MOUNTED_SHELL_TAB_LIMIT);
  });

  it("keeps only the selected split's shells attached while the sidebar is closed", async () => {
    panesGate.shown = true;
    const { view, panels, select } = await openShells(
      "s-closed-split",
      ["term_a", "term_b", "term_c"],
      { permissionLevel: LEVEL_OWNER },
    );
    act(() =>
      updateTerminalGroups("s-closed-split", () => [
        {
          id: "g_ab",
          split: {
            layout: {
              axis: "columns",
              ratio: 0.5,
              children: ["terminal:term_a", "terminal:term_b"],
            },
            focusedKey: "terminal:term_a",
          },
        },
      ]),
    );
    select("term_c");
    select("term_a");
    await waitFor(() => expect(shownTerminals()).toEqual(["term_a", "term_b", "term_c"]));
    view.rerender(panels.map((current) => ({ ...current, open: false })));
    await waitFor(() => expect(shownTerminals()).toEqual(["term_a", "term_b"]));
  });

  it("selects a split shell's tab as its pane takes focus, in place of the current step", async () => {
    panesGate.shown = true;
    const { store, select } = await openShells("s-split-focus", ["term_a", "term_b", "term_c"], {
      permissionLevel: LEVEL_OWNER,
    });
    act(() =>
      updateTerminalGroups("s-split-focus", () => [
        {
          id: "g_ab",
          split: {
            layout: {
              axis: "columns",
              ratio: 0.5,
              children: ["terminal:term_a", "terminal:term_b"],
            },
            focusedKey: "terminal:term_a",
          },
        },
      ]),
    );
    select("term_c");
    select("term_a");
    const tabA = store.selectedTabId();
    await waitFor(() => expect(stubOf("term_b")).not.toBeNull());
    fireEvent.focusIn(stubOf("term_b")!);
    const tabB = store.selectedTab();
    expect(tabB?.binding).toEqual({ kind: "terminal", key: "terminal:term_b" });
    // The step a's selection made is b's now, so Back goes where it would have from a.
    expect(store.history.canGoForward()).toBe(false);
    await act(async () => void (await store.go("back")));
    expect(store.selectedTab()?.binding).toEqual({ kind: "terminal", key: "terminal:term_c" });
    expect(store.selectedTabId()).not.toBe(tabA);
    // A shell outside the shown split taking focus selects nothing.
    select("term_a");
    fireEvent.focusIn(stubOf("term_c")!);
    expect(store.selectedTabId()).toBe(tabA);
  });

  it.each<[string, () => readonly TerminalGroup[]]>([
    ["goes", () => []],
    [
      "becomes a/c",
      () => [
        {
          id: "g_ac",
          split: {
            layout: {
              axis: "columns",
              ratio: 0.5,
              children: ["terminal:term_a", "terminal:term_c"],
            },
            focusedKey: "terminal:term_a",
          },
        },
      ],
    ],
  ])(
    "selects nothing for focus from a split that %s before the sidebar rerenders",
    async (_change, nextGroups) => {
      const conversationId = `s-split-stale-${nextGroups().length}`;
      panesGate.shown = true;
      const { store, select } = await openShells(conversationId, ["term_a", "term_b", "term_c"], {
        permissionLevel: LEVEL_OWNER,
      });
      act(() =>
        updateTerminalGroups(conversationId, () => [
          {
            id: "g_ab",
            split: {
              layout: {
                axis: "columns",
                ratio: 0.5,
                children: ["terminal:term_a", "terminal:term_b"],
              },
              focusedKey: "terminal:term_a",
            },
          },
        ]),
      );
      select("term_a");
      const tabA = store.selectedTabId();
      await waitFor(() => expect(stubOf("term_b")).not.toBeNull());
      const paneB = stubOf("term_b")!;
      const visits = store.history.state.visits;
      // The split changes and b's focus lands in one batch, before the panel renders again.
      act(() => {
        updateTerminalGroups(conversationId, nextGroups);
        paneB.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
      });
      expect(store.selectedTabId()).toBe(tabA);
      expect(store.history.state.visits).toEqual(visits);
    },
  );

  describe("a sidebar split's strip entry", () => {
    /** Shells a, b and c with a and b split, a focused and selected. */
    async function splitAB(conversationId: string, overrides: Partial<PanelOverrides> = {}) {
      panesGate.shown = true;
      const opened = await openShells(conversationId, ["term_a", "term_b", "term_c"], {
        permissionLevel: LEVEL_OWNER,
        ...overrides,
      });
      act(() =>
        updateTerminalGroups(conversationId, () => [
          {
            id: "g_ab",
            split: {
              layout: {
                axis: "columns",
                ratio: 0.5,
                children: ["terminal:term_a", "terminal:term_b"],
              },
              focusedKey: "terminal:term_a",
            },
          },
        ]),
      );
      opened.select("term_a");
      return opened;
    }
    const stripNames = () => screen.getAllByRole("tab").map((tab) => tab.textContent);

    it("shows the split once, named for its focused shell, and follows focus", async () => {
      await splitAB("s-group-entry");
      await waitFor(() => expect(stripNames()).toEqual(["term_a:s +1", "term_c:s"]));
      const entry = screen.getByRole("tab", { name: /term_a:s \+1/ });
      expect(entry).toHaveAttribute("aria-selected", "true");
      await waitFor(() => expect(stubOf("term_b")).not.toBeNull());
      fireEvent.focusIn(stubOf("term_b")!);
      await waitFor(() => expect(stripNames()).toEqual(["term_b:s +1", "term_c:s"]));
      expect(screen.getByRole("tab", { name: /term_b:s \+1/ })).toHaveAttribute(
        "aria-selected",
        "true",
      );
    });

    it("keeps each shell's own tab where splits aren't allowed", async () => {
      await splitAB("s-group-editor", { permissionLevel: LEVEL_EDIT });
      await waitFor(() => expect(stripNames()).toEqual(["term_a:s", "term_b:s", "term_c:s"]));
    });

    it("closes the split whole from its ×, asking once, and keeps it on Cancel", async () => {
      const confirmCloseShells = vi.fn(async () => false);
      const endShell = vi.fn(async () => true);
      const { store } = await splitAB("s-group-close", { confirmCloseShells, endShell });
      const close = await screen.findByRole("button", { name: "Close term_a:s +1" });
      await userEvent.click(close);
      await waitFor(() =>
        expect(confirmCloseShells).toHaveBeenCalledExactlyOnceWith([
          "terminal:term_a",
          "terminal:term_b",
        ]),
      );
      expect(endShell).not.toHaveBeenCalled();
      expect(store.tabs()).toHaveLength(3);

      confirmCloseShells.mockResolvedValue(true);
      await userEvent.click(screen.getByRole("button", { name: "Close term_a:s +1" }));
      await waitFor(() => expect(store.tabs()).toHaveLength(1));
      expect(endShell.mock.calls).toEqual([["terminal:term_a"], ["terminal:term_b"]]);
      expect(confirmCloseShells).toHaveBeenCalledTimes(2);
    });

    it("closes the split whole from Close tab's chord and from its menu", async () => {
      const confirmCloseShells = vi.fn(async () => true);
      const confirmCloseShell = vi.fn(async () => true);
      const endShell = vi.fn(async () => true);
      const { store } = await splitAB("s-group-chord", {
        confirmCloseShells,
        confirmCloseShell,
        endShell,
      });
      await screen.findByRole("tab", { name: /term_a:s \+1/ });
      act(pressCloseTab);
      await waitFor(() => expect(store.tabs()).toHaveLength(1));
      expect(confirmCloseShells).toHaveBeenCalledOnce();
      expect(confirmCloseShell).not.toHaveBeenCalled();

      cleanup();
      const second = await splitAB("s-group-menu", { confirmCloseShells, endShell });
      const entry = await screen.findByRole("tab", { name: /term_a:s \+1/ });
      fireEvent.contextMenu(entry.closest("[data-tab-strip-entry]")!);
      expect(screen.queryByRole("menuitem", { name: /^Pin tab/ })).toBeNull();
      fireEvent.click(await screen.findByRole("menuitem", { name: "Close split" }));
      await waitFor(() => expect(second.store.tabs()).toHaveLength(1));
    });

    it("keeps a shell that won't end in a consistent strip, and says which", async () => {
      const failed = vi.spyOn(toast, "error").mockImplementation(() => "");
      const endShell = vi.fn(async (key: string) => key !== "terminal:term_b");
      const { store } = await splitAB("s-group-partial", { endShell });
      await userEvent.click(await screen.findByRole("button", { name: "Close term_a:s +1" }));
      await waitFor(() =>
        expect(failed).toHaveBeenCalledExactlyOnceWith(
          "Couldn't close term_b:s. It's still running.",
          STAYS_UNTIL_DISMISSED,
        ),
      );
      // a's tab went; b's and c's stay, with the selection on one of them and history agreeing.
      const remaining = store.tabs().map((tab) => ("binding" in tab ? tab.binding : undefined));
      expect(remaining).toEqual([
        { kind: "terminal", key: "terminal:term_b" },
        { kind: "terminal", key: "terminal:term_c" },
      ]);
      const selected = store.selectedTabId();
      expect(store.tabs().some((tab) => tab.id === selected)).toBe(true);
      expect(store.history.state.visits[store.history.state.cursor]?.tabId).toBe(selected);
      failed.mockRestore();
    });

    it("names every shell that kept running in one message", async () => {
      const failed = vi.spyOn(toast, "error").mockImplementation(() => "");
      onTestFinished(() => failed.mockRestore());
      const endShell = vi.fn(async () => false);
      const { store } = await splitAB("s-group-all-fail", { endShell });
      await userEvent.click(await screen.findByRole("button", { name: "Close term_a:s +1" }));
      await waitFor(() => expect(endShell).toHaveBeenCalledTimes(2));
      await waitFor(() =>
        expect(failed).toHaveBeenCalledExactlyOnceWith(
          "Couldn't close term_a:s and term_b:s. They're still running.",
          STAYS_UNTIL_DISMISSED,
        ),
      );
      expect(store.tabs()).toHaveLength(3);
    });

    it("ends every shell the split names, though a member has no tab", async () => {
      const endShell = vi.fn(async () => true);
      const { store } = await splitAB("s-group-tabless", { endShell });
      const tabB = store
        .tabs()
        .find(
          (tab) =>
            "binding" in tab &&
            tab.binding?.kind === "terminal" &&
            tab.binding.key === "terminal:term_b",
        )!.id;
      // A restored split whose second shell's tab is gone; closing that tab ends nothing.
      await act(async () => void (await store.close(tabB)));
      await userEvent.click(await screen.findByRole("button", { name: "Close term_a:s +1" }));
      await waitFor(() => expect(endShell).toHaveBeenCalledTimes(2));
      expect(endShell.mock.calls).toEqual([["terminal:term_a"], ["terminal:term_b"]]);
    });

    it("ends nothing when the split changes while its close is asking", async () => {
      let answer!: (confirmed: boolean) => void;
      const confirmCloseShells = vi.fn(
        () =>
          new Promise<boolean>((resolve) => {
            answer = resolve;
          }),
      );
      const endShell = vi.fn(async () => true);
      const { store } = await splitAB("s-group-changed", { confirmCloseShells, endShell });
      await userEvent.click(await screen.findByRole("button", { name: "Close term_a:s +1" }));
      await waitFor(() => expect(confirmCloseShells).toHaveBeenCalledOnce());
      act(() =>
        updateTerminalGroups("s-group-changed", () => [
          {
            id: "g_ab",
            split: {
              layout: {
                axis: "columns",
                ratio: 0.5,
                children: ["terminal:term_a", "terminal:term_c"],
              },
              focusedKey: "terminal:term_a",
            },
          },
        ]),
      );
      await act(async () => answer(true));
      await act(async () => {});
      expect(endShell).not.toHaveBeenCalled();
      expect(store.tabs()).toHaveLength(3);
    });

    it("moves a split's tabs together with ⌥⇧→, and a tab past them whole", async () => {
      const { store } = await splitAB("s-group-move");
      await waitFor(() => expect(stripNames()).toEqual(["term_a:s +1", "term_c:s"]));
      fireEvent.keyDown(screen.getByRole("tab", { name: /term_a:s \+1/ }), {
        key: "ArrowRight",
        altKey: true,
        shiftKey: true,
      });
      expect(stripNames()).toEqual(["term_c:s", "term_a:s +1"]);
      const keys = () =>
        store
          .tabs()
          .map((tab) =>
            "binding" in tab && tab.binding?.kind === "terminal" ? tab.binding.key : "",
          );
      expect(keys()).toEqual(["terminal:term_c", "terminal:term_a", "terminal:term_b"]);
      // c moving right past the split lands after it, not between its tabs.
      fireEvent.keyDown(screen.getByRole("tab", { name: "term_c:s" }), {
        key: "ArrowRight",
        altKey: true,
        shiftKey: true,
      });
      expect(stripNames()).toEqual(["term_a:s +1", "term_c:s"]);
      expect(keys()).toEqual(["terminal:term_a", "terminal:term_b", "terminal:term_c"]);
    });

    it("keeps a split at the strip's end where it is, its tabs in their order", async () => {
      const { store, select } = await splitAB("s-group-edge");
      // b, the split's second tab, has its focus, so the split goes by b's tab.
      fireEvent.focusIn((await waitFor(() => stubOf("term_b")))!);
      select("term_b");
      await waitFor(() => expect(stripNames()).toEqual(["term_b:s +1", "term_c:s"]));
      // ⌘K goes by the strip's entries: the split is the first, though b is the second tab.
      const moveLeft = registeredPaletteCommands().find(
        (command) => command.label === "Move workspace tab left",
      );
      expect(isCommandAvailable(moveLeft?.availability)).toBe(false);
      const keys = () =>
        store
          .tabs()
          .map((tab) =>
            "binding" in tab && tab.binding?.kind === "terminal" ? tab.binding.key : "",
          );
      fireEvent.keyDown(screen.getByRole("tab", { name: /term_b:s \+1/ }), {
        key: "ArrowLeft",
        altKey: true,
        shiftKey: true,
      });
      expect(stripNames()).toEqual(["term_b:s +1", "term_c:s"]);
      expect(keys()).toEqual(["terminal:term_a", "terminal:term_b", "terminal:term_c"]);
    });

    it("moves a tab past a whole entry when the split's tabs aren't side by side", async () => {
      panesGate.shown = true;
      const { store, select } = await openShells(
        "s-group-scattered",
        ["term_a", "term_b", "term_c", "term_d"],
        { permissionLevel: LEVEL_OWNER },
      );
      // a and c split, with b between their tabs: the strip shows the split, b, then d.
      act(() =>
        updateTerminalGroups("s-group-scattered", () => [
          {
            id: "g_ac",
            split: {
              layout: {
                axis: "columns",
                ratio: 0.5,
                children: ["terminal:term_a", "terminal:term_c"],
              },
              focusedKey: "terminal:term_a",
            },
          },
        ]),
      );
      select("term_b");
      await waitFor(() => expect(stripNames()).toEqual(["term_a:s +1", "term_b:s", "term_d:s"]));
      fireEvent.keyDown(screen.getByRole("tab", { name: "term_b:s" }), {
        key: "ArrowRight",
        altKey: true,
        shiftKey: true,
      });
      expect(stripNames()).toEqual(["term_a:s +1", "term_d:s", "term_b:s"]);
      expect(store.tabs()).toHaveLength(4);
    });

    it("offers Move right in a tab's menu by its place among the entries, past a split", async () => {
      panesGate.shown = true;
      const { store } = await openShells(
        "s-group-menu-move",
        ["term_a", "term_b", "term_c", "term_d"],
        {
          permissionLevel: LEVEL_OWNER,
        },
      );
      act(() =>
        updateTerminalGroups("s-group-menu-move", () => [
          {
            id: "g_ab",
            split: {
              layout: {
                axis: "columns",
                ratio: 0.5,
                children: ["terminal:term_a", "terminal:term_b"],
              },
              focusedKey: "terminal:term_a",
            },
          },
        ]),
      );
      await waitFor(() => expect(stripNames()).toEqual(["term_a:s +1", "term_c:s", "term_d:s"]));
      // c is the third tab but the second entry: d still follows it.
      fireEvent.contextMenu(
        screen.getByRole("tab", { name: "term_c:s" }).closest("[data-tab-strip-entry]")!,
      );
      const moveRight = await screen.findByRole("menuitem", { name: /^Move right/ });
      expect(moveRight).not.toHaveAttribute("data-disabled");
      fireEvent.click(moveRight);
      expect(stripNames()).toEqual(["term_a:s +1", "term_d:s", "term_c:s"]);
      expect(store.tabs()).toHaveLength(4);
    });

    it("unpins a pinned shell as it joins a split", async () => {
      const { store } = await openShells("s-group-pin", ["term_a", "term_b", "term_c"], {
        permissionLevel: LEVEL_OWNER,
      });
      const tabOf = (key: string) =>
        store
          .tabs()
          .find(
            (tab) =>
              "binding" in tab && tab.binding?.kind === "terminal" && tab.binding.key === key,
          )!.id;
      act(() => store.setPinned([tabOf("terminal:term_b")], true));
      expect(isPinnedTab(store.findTab(tabOf("terminal:term_b"))!)).toBe(true);
      panesGate.shown = true;
      act(() =>
        updateTerminalGroups("s-group-pin", () => [
          {
            id: "g_bc",
            split: {
              layout: {
                axis: "columns",
                ratio: 0.5,
                children: ["terminal:term_b", "terminal:term_c"],
              },
              focusedKey: "terminal:term_b",
            },
          },
        ]),
      );
      await waitFor(() =>
        expect(isPinnedTab(store.findTab(tabOf("terminal:term_b"))!)).toBe(false),
      );
    });
  });

  it("keeps a shell's tab that won't end, and says so once, naming it", async () => {
    const failed = vi.spyOn(toast, "error").mockImplementation(() => "");
    onTestFinished(() => failed.mockRestore());
    const endShell = vi.fn(async () => false);
    const { store, select } = await openShells("s-shell-close-fails", ["term_a", "term_b"], {
      endShell,
    });
    select("term_a");
    await userEvent.click(await screen.findByRole("button", { name: "Close term_a:s" }));
    await waitFor(() =>
      expect(failed).toHaveBeenCalledExactlyOnceWith(
        "Couldn't close term_a:s. It's still running.",
        STAYS_UNTIL_DISMISSED,
      ),
    );
    expect(endShell).toHaveBeenCalledExactlyOnceWith("terminal:term_a");
    expect(store.tabs()).toHaveLength(2);
  });

  it("keeps only the selected shell attached while the sidebar is closed", async () => {
    const { view, panels, select } = await openShells("s-closed", ["term_a", "term_b"]);
    select("term_a");
    select("term_b");
    await waitFor(() => expect(shownTerminals()).toEqual(["term_a", "term_b"]));
    view.rerender(panels.map((current) => ({ ...current, open: false })));
    await waitFor(() => expect(shownTerminals()).toEqual(["term_b"]));
  });

  it("leaves out a shell the main view shows, and says where it is", async () => {
    renderHook(() => usePublishMainTerminalKeys("s-main-shows", ["terminal:term_b"]));
    await openShells("s-main-shows", ["term_a", "term_b"]);
    expect(await screen.findByText("This shell is open in the Terminal view.")).toBeVisible();
    expect(stubOf("term_b")).toBeNull();
  });

  it("mounts a saved split's shell while no view shows that split", async () => {
    // The panes view is on, but the main view fell back and shows no split.
    panesGate.shown = true;
    updatePaneSplit("s-split-unshown", () => ({
      layout: { axis: "columns", ratio: 0.5, children: ["terminal:agent", "terminal:term_b"] },
      focusedKey: "terminal:term_b",
    }));
    await openShells("s-split-unshown", ["term_a", "term_b"]);
    await waitFor(() => expect(stubOf("term_b")).not.toBeNull());
    expect(screen.queryByText("This shell is open in the Terminal view.")).toBeNull();
  });

  it("opens a shell's tab when a pane asks, as its own open does", async () => {
    const openTerminalTab = vi.fn();
    await openShells("s-open-request", ["term_a", "term_b"], { openTerminalTab });
    act(() => requestTerminalOpen("s-open-request", { key: "terminal:term_a", focus: false }));
    expect(openTerminalTab).toHaveBeenCalledExactlyOnceWith("terminal:term_a", { focus: false });
  });

  it("leaves a pane's open request to the session's own sidebar, not a grid cell's", async () => {
    const openTerminalTab = vi.fn();
    await openShells("s-open-grid", ["term_a", "term_b"], { openTerminalTab, contained: true });
    act(() => requestTerminalOpen("s-open-grid", { key: "terminal:term_a", focus: true }));
    expect(openTerminalTab).not.toHaveBeenCalled();
  });

  /** The session's shell list as the panel reads it. */
  async function listShells(state: {
    error?: Error;
    isFetching?: boolean;
    listCompleteAt?: number;
  }) {
    const { useTerminals } = await import("@/hooks/useTerminals");
    vi.mocked(useTerminals).mockReturnValue({
      terminals: [],
      isLoading: false,
      isFetching: false,
      error: null,
      listCompleteAt: 0,
      ...state,
    } as unknown as ReturnType<typeof useTerminals>);
  }

  it("offers Try Again when the session's shells can't be listed, which asks for them again", async () => {
    await listShells({ error: new Error("offline") });
    const cancel = vi.spyOn(queryClient, "cancelQueries");
    const refetch = vi.spyOn(queryClient, "refetchQueries");
    const panels = [{ conversationId: "s-shell-down", terminalKeys: ["terminal:term_1"] }];
    const view = renderPanels(panels);
    const { store } = workspaceTabsSession("s-shell-down");
    await act(async () => {
      await store.open({ place: { kind: "terminal", key: "terminal:term_1" } }, { opener: "user" });
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't connect to this terminal");
    expect(screen.queryByTestId("terminal-view-stub")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Try Again" }));
    expect(refetch).toHaveBeenCalledWith(
      { queryKey: terminalsQueryKey("s-shell-down") },
      { cancelRefetch: false },
    );
    expect(cancel).not.toHaveBeenCalled();

    // While the list is asked for again, the tab connects; a failed answer shows the failure again.
    await listShells({ error: new Error("offline"), isFetching: true });
    view.rerender(panels);
    expect(await screen.findByTestId("terminal-tab-connecting")).toBeInTheDocument();
    await listShells({ error: new Error("offline") });
    view.rerender(panels);
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't connect to this terminal");
  });

  describe("a shell the loaded list doesn't show", () => {
    beforeEach(async () => {
      await listShells({ listCompleteAt: 1 });
      vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"] });
    });
    afterEach(() => vi.useRealTimers());
    const openUnlisted = async (conversationId: string) => {
      const { store } = workspaceTabsSession(conversationId);
      await act(async () => {
        await store.open(
          { place: { kind: "terminal", key: "terminal:term_9" } },
          { opener: "user" },
        );
      });
      expect(store.resourceState(store.selectedTabId()!)).toBe("unknown");
    };
    const advance = async (ms: number) => {
      await act(async () => {
        vi.advanceTimersByTime(ms);
      });
    };
    const connecting = () => screen.queryByTestId("terminal-tab-connecting") !== null;

    it("stops connecting after the limit", async () => {
      renderPanels([{ conversationId: "s-shell-unlisted", terminalKeys: [] }]);
      await openUnlisted("s-shell-unlisted");
      expect(connecting()).toBe(true);
      await advance(SHELL_LIST_LIMIT_MS + 1_000);
      expect(screen.getByRole("alert")).toHaveTextContent("Couldn't connect to this terminal");
    });

    it("still stops after the limit when another panel replaces the one it opened in", async () => {
      const panels = [{ conversationId: "s-shell-replaced", terminalKeys: [] }];
      const view = renderPanels(panels);
      await openUnlisted("s-shell-replaced");
      await advance(SHELL_LIST_LIMIT_MS - 5_000);
      view.rerender([]);
      view.rerender(panels);
      expect(connecting()).toBe(true);
      await advance(6_000);
      expect(screen.getByRole("alert")).toHaveTextContent("Couldn't connect to this terminal");
    });

    it("asks again on each Try Again, with the limit starting over each time", async () => {
      const refetch = vi.spyOn(queryClient, "refetchQueries");
      renderPanels([{ conversationId: "s-shell-retry", terminalKeys: [] }]);
      await openUnlisted("s-shell-retry");
      const timeOutThenTryAgain = async (attempt: number) => {
        await advance(SHELL_LIST_LIMIT_MS + 1_000);
        fireEvent.click(screen.getByRole("button", { name: "Try Again" }));
        expect(refetch).toHaveBeenCalledTimes(attempt);
        expect(connecting()).toBe(true);
        await advance(SHELL_LIST_LIMIT_MS - 1_000);
        expect(connecting()).toBe(true);
      };
      await timeOutThenTryAgain(1);
      await timeOutThenTryAgain(2);
    });
  });

  it.each([
    [{ kind: "host_offline", isOwner: true } as const, "Host is offline"],
    [{ kind: "runner_asleep" } as const, "Session isn't running"],
  ])("says why a shell can't connect while the session reads %o", async (liveness, title) => {
    const { useTerminals } = await import("@/hooks/useTerminals");
    vi.mocked(useTerminals).mockReturnValue({
      terminals: [],
      isLoading: true,
      error: null,
      listCompleteAt: 0,
    } as unknown as ReturnType<typeof useTerminals>);
    const conversationId = `s-shell-${liveness.kind}`;
    renderPanels([{ conversationId, terminalKeys: ["terminal:term_1"], liveness }]);
    const { store } = workspaceTabsSession(conversationId);
    await act(async () => {
      await store.open({ place: { kind: "terminal", key: "terminal:term_1" } }, { opener: "user" });
    });
    expect(await screen.findByTestId("terminal-tab-status")).toHaveTextContent(title);
    expect(screen.queryByTestId("terminal-tab-connecting")).toBeNull();
  });

  it.each([
    [LEVEL_READ, "a viewer"],
    [LEVEL_EDIT, "an editor"],
    [LEVEL_OWNER, "the owner"],
  ])(
    "gives the relaunch command for a stopped session with no host only to its owner (%i, %s)",
    async (permissionLevel) => {
      await listShells({});
      const conversationId = `s-shell-stranded-${permissionLevel}`;
      renderPanels([
        {
          conversationId,
          terminalKeys: ["terminal:term_1"],
          liveness: { kind: "local_stranded" },
          permissionLevel,
        },
      ]);
      const { store } = workspaceTabsSession(conversationId);
      await act(async () => {
        await store.open(
          { place: { kind: "terminal", key: "terminal:term_1" } },
          { opener: "user" },
        );
      });
      const status = await screen.findByTestId("terminal-tab-status");
      expect(status).toHaveTextContent(LOCAL_RECONNECT_TITLE);
      const owner = permissionLevel === LEVEL_OWNER;
      expect(status).toHaveTextContent(owner ? LOCAL_RECONNECT_DESCRIPTION : OWNER_RECONNECT_HINT);
      expect(status.textContent?.includes(`--resume ${conversationId}`)).toBe(owner);
    },
  );

  it.each([
    [LEVEL_READ, "a viewer"],
    [LEVEL_EDIT, "an editor"],
    [LEVEL_OWNER, "the owner"],
  ])(
    "lets someone who can edit resume a session that isn't running from its shell tab, once while it's on its way (%i, %s)",
    async (permissionLevel) => {
      const { useTerminals } = await import("@/hooks/useTerminals");
      const { retrySession } = await import("@/lib/sessionsApi");
      vi.mocked(retrySession).mockReset();
      vi.mocked(useTerminals).mockReturnValue({
        terminals: [],
        isLoading: true,
        error: null,
        listCompleteAt: 0,
      } as unknown as ReturnType<typeof useTerminals>);
      let answer: () => void = () => {};
      vi.mocked(retrySession).mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            answer = () => resolve({} as never);
          }),
      );
      const conversationId = `s-shell-asleep-${permissionLevel}`;
      renderPanels([
        {
          conversationId,
          terminalKeys: ["terminal:term_1"],
          liveness: { kind: "runner_asleep" },
          permissionLevel,
        },
      ]);
      const { store } = workspaceTabsSession(conversationId);
      await act(async () => {
        await store.open(
          { place: { kind: "terminal", key: "terminal:term_1" } },
          { opener: "user" },
        );
      });
      const status = await screen.findByTestId("terminal-tab-status");
      if (permissionLevel === LEVEL_READ) {
        expect(status).toHaveTextContent(VIEWER_RESUME_HINT);
        expect(screen.queryByRole("button", { name: "Resume session" })).toBeNull();
        expect(retrySession).not.toHaveBeenCalled();
        return;
      }
      const resume = screen.getByRole("button", { name: "Resume session" });
      await userEvent.click(resume);
      await userEvent.click(resume);
      expect(retrySession).toHaveBeenCalledTimes(1);
      expect(retrySession).toHaveBeenCalledWith(conversationId);
      await act(async () => answer());
      await waitFor(() => expect(resume).not.toBeDisabled());
    },
  );
});

describe("closing a group of tabs", () => {
  it("closes every file tab from ⌘K and every page from the All tabs heading", async () => {
    const conversationId = "s-close-groups";
    renderPanels([{ conversationId }]);
    const { store } = workspaceTabsSession(conversationId);
    await act(async () => {
      await store.open({ place: { kind: "files", path: "a.md" } }, { opener: "user" });
      await store.open({ place: { kind: "files", path: "b.md" } }, { opener: "user" });
      await store.open({ place: { kind: "web", url: "https://example.com" } }, { opener: "user" });
      await store.open({ place: { kind: "agents" } }, { opener: "user" });
    });
    const kinds = () => store.tabs().map((tab) => ("place" in tab ? tab.place.kind : "unknown"));
    expect(kinds()).toEqual(expect.arrayContaining(["files", "files", "web", "agents"]));

    const closeFiles = await waitFor(() => {
      const command = listedPaletteCommands().find(
        (candidate) => candidate.id === `workspace:${conversationId}:tab-action:close-file-tabs`,
      );
      expect(command?.label).toBe("Close all file tabs");
      return command!;
    });
    act(() => closeFiles.run());
    await waitFor(() => expect(kinds()).not.toContain("files"));
    expect(kinds()).toEqual(expect.arrayContaining(["web", "agents"]));

    await userEvent.click(screen.getByRole("button", { name: "All tabs" }));
    expect(await screen.findByRole("group", { name: /^Pages 1/ })).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Close all page tabs" }));
    await waitFor(() => expect(kinds()).not.toContain("web"));
    // The list stays open on what's left: the tab on screen, which leads outside any group.
    expect(screen.getByRole("option", { name: /Current/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^Close all/ })).toBeNull();
  });

  it("closes the merged pull requests' tabs, PR files too, but keeps open and pinned ones", async () => {
    const conversationId = "s-close-merged";
    const merged = "https://github.example.com/acme/repo/pull/1";
    const open = "https://github.example.com/acme/repo/pull/2";
    const pinnedMerged = "https://github.example.com/acme/repo/pull/3";
    const pr = (url: string, number: number, state: string) =>
      ({ url, number, title: `Change ${number}`, state, is_draft: false }) as PullRequest;
    knownPullRequests.facts = {
      linked: [],
      details: new Map([
        [merged, pr(merged, 1, "MERGED")],
        [open, pr(open, 2, "OPEN")],
        [pinnedMerged, pr(pinnedMerged, 3, "MERGED")],
      ]),
    };
    renderPanels([{ conversationId }]);
    const { store } = workspaceTabsSession(conversationId);
    await act(async () => {
      // One at a time, so the tabs open in this order.
      await store.open(
        { place: { kind: "pull", url: merged, view: "summary" } },
        { opener: "user" },
      );
      await store.open({ place: { kind: "pull", url: open, view: "summary" } }, { opener: "user" });
      await store.open(
        { place: { kind: "pull", url: pinnedMerged, view: "summary" } },
        { opener: "user" },
      );
      await store.open(
        { place: { kind: "pull", url: merged, view: "changes", path: "a.ts" } },
        { opener: "user", disposition: "new" },
      );
      await store.open({ place: { kind: "agents" } }, { opener: "user" });
    });
    const pinned = store
      .tabs()
      .find((tab) => "place" in tab && "url" in tab.place && tab.place.url === pinnedMerged)!;
    act(() => store.setPinned([pinned.id], true));
    const urls = () =>
      store
        .tabs()
        .flatMap((tab) => ("place" in tab && tab.place.kind === "pull" ? [tab.place.url] : []));

    await userEvent.click(screen.getByRole("button", { name: "All tabs" }));
    const pulls = await screen.findByRole("group", { name: /^Pull requests 4/ });
    expect(within(pulls).getByRole("option", { name: /#2 Change 2/ })).toHaveTextContent("Open");
    await userEvent.click(screen.getByRole("button", { name: "Close merged pull request tabs" }));
    await waitFor(() => expect(urls()).toEqual([pinnedMerged, open]));
    // Nothing merged is left to close but the pinned tab, which stays.
    expect(screen.queryByRole("button", { name: "Close merged pull request tabs" })).toBeNull();
  });

  it("counts a PR as merged when the session saw it merge after its details were read", async () => {
    const conversationId = "s-close-merged-later";
    const url = "https://github.example.com/acme/repo/pull/7";
    knownPullRequests.facts = {
      linked: [
        {
          url,
          host: "github.example.com",
          repository: "acme/repo",
          number: 7,
          title: "Change 7",
          relationship: "created",
          state: "MERGED",
          state_checked_at: Date.UTC(2026, 0, 1, 10, 5) / 1000,
        } as PullRequestAssociation,
      ],
      details: new Map([
        [url, { url, number: 7, title: "Change 7", state: "OPEN", is_draft: false } as PullRequest],
      ]),
      detailsUpdatedAt: new Map([[url, Date.UTC(2026, 0, 1, 10, 0)]]),
    };
    renderPanels([{ conversationId }]);
    const { store } = workspaceTabsSession(conversationId);
    await act(async () => {
      await store.open({ place: { kind: "pull", url, view: "summary" } }, { opener: "user" });
      await store.open({ place: { kind: "agents" } }, { opener: "user" });
    });

    await userEvent.click(screen.getByRole("button", { name: "All tabs" }));
    const pulls = await screen.findByRole("group", { name: /^Pull requests 1/ });
    expect(within(pulls).getByRole("option", { name: /#7 Change 7/ })).toHaveTextContent("Merged");
    await userEvent.click(screen.getByRole("button", { name: "Close merged pull request tabs" }));
    await waitFor(() =>
      expect(store.tabs().some((tab) => "place" in tab && tab.place.kind === "pull")).toBe(false),
    );
  });
});

describe("a browser tab's label", () => {
  afterEach(() => {
    delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
  });

  it("follows its page's live title", async () => {
    let urlChanged:
      ((page: { conversationId: string; url: string; title: string }) => void) | null = null;
    (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = {
      onBrowserUrlChanged: (callback: typeof urlChanged) => {
        urlChanged = callback;
        return () => {};
      },
      browserHasView: async () => ({ exists: false }),
    };
    renderPanels([{ conversationId: "conv_a" }]);
    await waitFor(() => expect(sessionsWithWorkspaceRows()).toEqual(["conv_a"]));
    act(pressNewBrowser);
    await waitFor(() => expect(browserTabCount("conv_a")).toBe(1));
    const tab = workspaceTabsSession("conv_a").store.selectedTab()!;
    const viewKey = tab.binding?.kind === "browser-view" ? tab.binding.viewKey : "";
    await waitFor(() => expect(urlChanged).not.toBeNull());

    act(() =>
      urlChanged!({
        conversationId: browserViewId("conv_a", viewKey),
        url: "https://example.com/docs",
        title: "Example docs",
      }),
    );
    expect(await screen.findByRole("tab", { name: /Example docs/ })).toBeInTheDocument();
  });
});
