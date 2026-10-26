import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { RefObject } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import * as sessionsApi from "@/lib/sessionsApi";
import { useChatStore } from "@/store/chatStore";
import { ALT_KEY, MOD_KEY } from "@/components/KeyboardShortcut";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useSessionAgent } from "@/hooks/useAgents";
import type { SessionLiveness } from "@/hooks/useSessionLiveness";
import type * as UseTerminalsModule from "@/hooks/useTerminals";
import { useCreateTerminal, useTerminals } from "@/hooks/useTerminals";
import { writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import type { ChangedSort } from "./FlatFileList";
import type { RightRailTab } from "./railTabs";
import {
  type DefaultWorkspaceTab,
  writeDefaultWorkspaceTab,
  writePinnedWorkspaceTabs,
} from "@/lib/workspaceTabPreferences";
import { WorkspacePanel } from "./WorkspacePanel";

// The rail's content children are exercised by their own suites; stub them so
// these tests focus on WorkspacePanel's own logic (the open-file/shell tab
// strips and the content branch that swaps FileViewer ↔ FilesPanel ↔ shell).
// Each stub renders a testid (plus, for FileViewer, the path it was asked to
// show) so we can prove which child mounted without dragging in Monaco / hook
// stacks / xterm.
vi.mock("./FileViewer", () => ({
  FileViewer: ({
    path,
    open,
    mediaActive,
  }: {
    path: string;
    open: boolean;
    mediaActive: boolean;
  }) => (
    <div data-testid="file-viewer-stub" data-open={open} data-media-active={mediaActive}>
      {path}
    </div>
  ),
}));
vi.mock("./FilesPanel", () => ({
  // Echo the fixed scope so tests can prove the Files tab renders the tree
  // (flatView=false) and the Changes tab the changed-only list (flatView=true).
  FilesPanel: ({ flatView, conversationId }: { flatView: boolean; conversationId: string }) => (
    <div
      data-testid="files-panel-stub"
      data-session={conversationId}
      data-flat-view={String(flatView)}
    />
  ),
}));
vi.mock("./SubagentsPanel", () => ({
  SubagentsPanel: () => <div data-testid="subagents-stub" />,
}));
vi.mock("./ContentSearchPanel", () => ({
  ContentSearchPanel: ({ inputRef }: { inputRef: RefObject<HTMLInputElement | null> }) => (
    <input data-testid="content-search-stub" ref={inputRef} />
  ),
}));
vi.mock("@/components/BrowserPane/BrowserPane", () => ({
  BrowserPane: ({ conversationId, active }: { conversationId: string; active?: boolean }) => (
    <div data-testid="browser-pane-stub" data-active={String(active)}>
      {conversationId}
    </div>
  ),
}));
// The rail terminal view mounts a real xterm/WebSocket; stub it to a marker
// echoing the terminal id it was asked to attach so we can prove the right
// shell tab surfaced.
vi.mock("@/components/blocks/TerminalView", () => ({
  TerminalView: ({ terminalId }: { terminalId: string }) => (
    <div data-testid="terminal-view-stub">{terminalId}</div>
  ),
}));
vi.mock("@/hooks/useTerminals", async (importOriginal) => ({
  ...(await importOriginal<typeof UseTerminalsModule>()),
  useTerminals: vi.fn(() => ({ terminals: [], isLoading: false, error: null })),
  useCreateTerminal: vi.fn(() => ({ mutate: vi.fn(), isPending: false, isError: false })),
}));
// The "+" menu reads the agent's declared terminals to gate the Shell entry.
vi.mock("@/hooks/useAgents", () => ({
  useSessionAgent: vi.fn(() => ({ data: undefined })),
}));
vi.mock("sonner", () => ({ toast: { error: vi.fn() } }));
// A side chat's pane is covered by its own suite; the stub just names its child.
vi.mock("@/components/chat/SideChatPane", () => ({
  SideChatPane: ({ childId }: { childId: string }) => (
    <div data-testid="side-chat-pane-stub">{childId}</div>
  ),
}));
const isMobileMock = vi.hoisted(() => vi.fn(() => false));
vi.mock("@/hooks/useIsMobileViewport", () => ({ useIsMobileViewport: () => isMobileMock() }));

const useTerminalsMock = vi.mocked(useTerminals);
const useCreateTerminalMock = vi.mocked(useCreateTerminal);
const useSessionAgentMock = vi.mocked(useSessionAgent);

beforeEach(() => {
  vi.spyOn(navigator, "platform", "get").mockReturnValue("Win32");
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.clearAllMocks();
  vi.restoreAllMocks();
  isMobileMock.mockReturnValue(false);
  Reflect.deleteProperty(window, "omnigentDesktop");
  useTerminalsMock.mockReturnValue({ terminals: [], isLoading: false, error: null });
  useCreateTerminalMock.mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
    isError: false,
  } as unknown as ReturnType<typeof useCreateTerminal>);
  useSessionAgentMock.mockReturnValue({ data: undefined } as unknown as ReturnType<
    typeof useSessionAgent
  >);
});

/**
 * Render WorkspacePanel with a complete prop set, overridable per test. Returns
 * the spied callbacks the tests assert against (openFileViewer / onCloseFile /
 * onRightRailTabChange) alongside the render result.
 */
function renderWorkspace(
  initialOverrides: {
    conversationId?: string;
    contained?: boolean;
    rightRailTab?: RightRailTab | null;
    openPanels?: DefaultWorkspaceTab[];
    selectedFilePath?: string | null;
    openFiles?: string[];
    changedCount?: number;
    showGithubTab?: boolean;
    showContentSearch?: boolean;
    showFilesPanel?: boolean;
    showBrowserTab?: boolean;
    openTerminals?: string[];
    selectedTerminalKey?: string | null;
    maximized?: boolean;
    liveness?: SessionLiveness;
    pending?: boolean;
    open?: boolean;
    animateVisibility?: boolean;
    resizing?: boolean;
    inert?: boolean;
    mobileSideChatsOpen?: boolean;
  } = {},
) {
  let overrides = initialOverrides;
  const openFileViewer = vi.fn();
  const onCloseFile = vi.fn();
  const onRightRailTabChange = vi.fn();
  const onClosePanel = vi.fn();
  const onReveal = vi.fn();
  const openTerminalTab = vi.fn();
  const onCloseTerminal = vi.fn();
  const onToggleMaximized = vi.fn();
  const onMobileSideChatsOpenChange = vi.fn();
  const tree = () => (
    <TooltipProvider delayDuration={0}>
      <WorkspacePanel
        conversationId={overrides.conversationId ?? "conv_ws"}
        contained={overrides.contained}
        width={360}
        open={overrides.open}
        animateVisibility={overrides.animateVisibility}
        resizing={overrides.resizing}
        inert={overrides.inert}
        handleProps={{
          tabIndex: 0,
          role: "separator",
          "aria-label": "Resize panel",
          onMouseDown: vi.fn(),
          onKeyDown: vi.fn(),
        }}
        rightRailTab={overrides.rightRailTab === undefined ? "files" : overrides.rightRailTab}
        onRightRailTabChange={onRightRailTabChange}
        openPanels={overrides.openPanels ?? []}
        onClosePanel={onClosePanel}
        showFilesPanel={overrides.showFilesPanel ?? true}
        showContentSearch={overrides.showContentSearch}
        showGithubTab={overrides.showGithubTab ?? false}
        showBrowserTab={overrides.showBrowserTab ?? false}
        onReveal={onReveal}
        changedCount={overrides.changedCount ?? 0}
        subagentsWorking={0}
        agentCount={1}
        rootSessionId={null}
        selectedFilePath={overrides.selectedFilePath ?? null}
        openFiles={overrides.openFiles ?? []}
        openFileViewer={openFileViewer}
        onCloseFile={onCloseFile}
        onShowScopeView={vi.fn()}
        onCommentsOpenChange={vi.fn()}
        openTerminalTab={openTerminalTab}
        openTerminals={overrides.openTerminals ?? []}
        selectedTerminalKey={overrides.selectedTerminalKey ?? null}
        onCloseTerminal={onCloseTerminal}
        maximized={overrides.maximized ?? false}
        onToggleMaximized={onToggleMaximized}
        permissionLevel={null}
        filesPanelSort={"recent" as ChangedSort}
        onSortChange={vi.fn()}
        filesPanelShowHidden={false}
        onShowHiddenChange={vi.fn()}
        liveness={overrides.liveness}
        pending={overrides.pending}
        mobileSideChatsOpen={overrides.mobileSideChatsOpen}
        onMobileSideChatsOpenChange={onMobileSideChatsOpenChange}
      />
    </TooltipProvider>
  );
  const view = render(tree());
  return {
    rerender: (next: typeof overrides) => {
      overrides = { ...overrides, ...next };
      view.rerender(tree());
    },
    openFileViewer,
    onCloseFile,
    onRightRailTabChange,
    onClosePanel,
    onReveal,
    openTerminalTab,
    onCloseTerminal,
    onToggleMaximized,
    onMobileSideChatsOpenChange,
    view,
  };
}

describe("WorkspacePanel surface presentation", () => {
  it("offers unpinned pages in + without requiring shell or browser support", () => {
    writePinnedWorkspaceTabs(["files", "subagents"]);
    const { onRightRailTabChange } = renderWorkspace({ showGithubTab: true });

    expect(screen.queryByRole("tab", { name: "Changes" })).toBeNull();
    expect(screen.getByRole("tab", { name: "Agents 1" })).toHaveAttribute("aria-keyshortcuts", "2");
    fireEvent.pointerDown(screen.getByRole("button", { name: "Open new" }), { button: 0 });
    expect(screen.getByRole("menuitem", { name: "Pull Requests" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Files" })).toBeNull();
    fireEvent.click(screen.getByRole("menuitem", { name: "Changes" }));
    expect(onRightRailTabChange).toHaveBeenCalledWith("changes");
  });

  it("renders opened unpinned pages as closable tabs alongside files", () => {
    writePinnedWorkspaceTabs(["files", "subagents"]);
    const { onClosePanel } = renderWorkspace({
      rightRailTab: "changes",
      openPanels: ["changes"],
      openFiles: ["README.md"],
    });

    expect(screen.getByRole("tab", { name: "Changes" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("files-panel-stub")).toHaveAttribute("data-flat-view", "true");
    fireEvent.click(screen.getByRole("button", { name: "Close Changes" }));
    expect(onClosePanel).toHaveBeenCalledWith("changes");
    fireEvent.pointerDown(screen.getByRole("button", { name: "Open new" }), { button: 0 });
    expect(screen.getByRole("menuitem", { name: "Changes" })).toBeInTheDocument();
  });

  it("keeps + available with no pinned pages and hides unavailable pages", () => {
    writePinnedWorkspaceTabs([]);
    renderWorkspace({ rightRailTab: null, showFilesPanel: false });

    expect(screen.queryByRole("tab")).toBeNull();
    expect(screen.queryByRole("button", { name: "Select panel" })).toBeNull();
    expect(screen.queryByTestId("files-panel-stub")).toBeNull();
    expect(screen.getByText("Open a Workspace tab from the + menu.")).toBeInTheDocument();
    fireEvent.pointerDown(screen.getByRole("button", { name: "Open new" }), { button: 0 });
    expect(screen.getByRole("menuitem", { name: "Agents" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Files" })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "Pull Requests" })).toBeNull();
  });

  it.each(["tab", "conversation"])(
    "cancels a deferred search focus after changing %s",
    (changed) => {
      const { rerender } = renderWorkspace({
        showContentSearch: true,
        rightRailTab: "content-search",
        open: true,
        inert: true,
      });
      const chord = { code: "KeyF", ctrlKey: true, shiftKey: true };
      fireEvent.keyDown(window, chord);
      rerender(changed === "tab" ? { rightRailTab: "files" } : { conversationId: "other" });
      rerender({ rightRailTab: "content-search", inert: false });
      expect(screen.getByTestId("content-search-stub")).not.toHaveFocus();
      fireEvent.keyDown(window, chord);
      expect(screen.getByTestId("content-search-stub")).toHaveFocus();
    },
  );

  it.each([
    { showContentSearch: false },
    { showContentSearch: true, showFilesPanel: false },
    { showContentSearch: true, pending: true },
  ])("leaves the content-search shortcut alone when unavailable: %j", (overrides) => {
    const { onRightRailTabChange, onReveal } = renderWorkspace(overrides);
    expect(fireEvent.keyDown(window, { code: "KeyF", ctrlKey: true, shiftKey: true })).toBe(true);
    expect(onRightRailTabChange).not.toHaveBeenCalled();
    expect(onReveal).not.toHaveBeenCalled();
  });

  it("ignores content-search shortcuts on mobile and unrelated chords on desktop", () => {
    isMobileMock.mockReturnValue(true);
    const mobile = renderWorkspace({ showContentSearch: true });
    fireEvent.keyDown(window, { code: "KeyF", ctrlKey: true, shiftKey: true });
    expect(mobile.onReveal).not.toHaveBeenCalled();
    mobile.view.unmount();
    isMobileMock.mockReturnValue(false);
    const desktop = renderWorkspace({ showContentSearch: true });
    for (const modifiers of [
      { shiftKey: false },
      { altKey: true },
      { metaKey: true },
      { repeat: true },
      { isComposing: true },
    ]) {
      expect(
        fireEvent.keyDown(window, {
          code: "KeyF",
          ctrlKey: true,
          shiftKey: true,
          ...modifiers,
        }),
      ).toBe(true);
    }
    expect(desktop.onReveal).not.toHaveBeenCalled();
    expect(fireEvent.keyDown(window, { code: "KeyF", ctrlKey: true, shiftKey: true })).toBe(false);
    expect(desktop.onReveal).toHaveBeenCalledOnce();
    expect(desktop.onRightRailTabChange).toHaveBeenCalledWith("content-search");
  });

  it("gates content search and exposes it in the tab strip and panel picker", async () => {
    const disabled = renderWorkspace();
    expect(screen.queryByRole("tab", { name: "Content search" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("content-search-stub")).not.toBeInTheDocument();
    disabled.view.unmount();
    const enabled = renderWorkspace({ showContentSearch: true, showGithubTab: true });
    expect(screen.getByTestId("content-search-stub")).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Content search" }), {
      button: 0,
      ctrlKey: false,
    });
    expect(enabled.onRightRailTabChange).toHaveBeenCalledWith("content-search");
    fireEvent.pointerDown(screen.getByRole("button", { name: "Select panel" }), {
      button: 0,
      ctrlKey: false,
    });
    const searchMenuItem = await screen.findByRole("menuitem", { name: "Content search" });
    enabled.onRightRailTabChange.mockClear();
    fireEvent.click(searchMenuItem);
    expect(enabled.onRightRailTabChange).toHaveBeenCalledWith("content-search");
    const searchTab = screen.getByRole("tab", { name: "Content search" });
    expect(searchTab).toHaveAttribute("aria-keyshortcuts", expect.stringContaining(" 5"));
    enabled.onRightRailTabChange.mockClear();
    fireEvent.keyDown(searchTab, { key: "5" });
    expect(enabled.onRightRailTabChange).toHaveBeenLastCalledWith("content-search");

    writePinnedWorkspaceTabs([]);
    enabled.rerender({ rightRailTab: "content-search" });
    expect(screen.getAllByRole("tab")).toHaveLength(1);
    expect(searchTab).toHaveAttribute("aria-keyshortcuts", expect.stringContaining(" 1"));
    expect(screen.getByTestId("content-search-stub").parentElement).toHaveClass("flex");
  });

  it("sits flush to the window edge with a left divider, no floating card frame", () => {
    renderWorkspace();

    const panel = screen.getByRole("complementary", { name: "Workspace" });
    expect(panel).toHaveClass("md:border-l", "md:border-border");
    expect(panel).not.toHaveClass("md:m-2", "md:rounded-lg", "md:shadow-lg");
    expect(panel).toHaveClass("workspace-panel-motion", "md:overflow-hidden");
    expect(panel).toHaveAttribute("data-state", "open");
    expect(panel).not.toHaveAttribute("data-animate-visibility");
  });

  it("marks explicit visibility motion separately from resizing", () => {
    renderWorkspace({ open: false, animateVisibility: true, resizing: true, inert: true });

    const panel = document.querySelector('aside[aria-label="Workspace"]');
    expect(panel).not.toBeNull();
    expect(panel).toHaveAttribute("data-state", "closed");
    expect(panel).toHaveAttribute("data-animate-visibility", "true");
    expect(panel).toHaveAttribute("data-resizing", "true");
    expect(panel).toHaveAttribute("aria-hidden", "true");
  });

  it("presents the fixed pane tabs as compact icon controls with accessible labels", () => {
    renderWorkspace();

    const filesTab = screen.getByRole("tab", { name: "Files" });
    const changesTab = screen.getByRole("tab", { name: "Changes" });
    const agentsTab = screen.getByRole("tab", { name: "Agents 1" });
    // Icon-only tabs carry their accessible name (queried above) plus a hover
    // tooltip, never a native title attribute. Exact size/padding classes are
    // intentionally not asserted — brittle styling detail, not behaviour.
    expect(filesTab).not.toHaveAttribute("title");
    expect(changesTab).not.toHaveAttribute("title");
    expect(agentsTab).not.toHaveAttribute("title");
  });

  it.each([
    ["files", ["Files", "Changes", "Pull Requests", "Agents 1"]],
    ["changes", ["Changes", "Files", "Pull Requests", "Agents 1"]],
    ["github", ["Pull Requests", "Files", "Changes", "Agents 1"]],
    ["subagents", ["Agents 1", "Files", "Changes", "Pull Requests"]],
  ] as const)("places the %s default first without reordering the remaining tabs", (tab, order) => {
    writeDefaultWorkspaceTab(tab);
    renderWorkspace({ showGithubTab: true, showBrowserTab: true, rightRailTab: "files" });

    expect(screen.getAllByRole("tab").map((item) => item.getAttribute("aria-label"))).toEqual(
      order,
    );
    expect(screen.getByRole("tab", { name: "Files" })).toHaveAttribute("aria-selected", "true");
  });

  it("moves keyboard focus through the displayed order", async () => {
    writeDefaultWorkspaceTab("changes");
    const { onRightRailTabChange } = renderWorkspace({
      showGithubTab: true,
      rightRailTab: "changes",
    });

    fireEvent.keyDown(screen.getByRole("tab", { name: "Changes" }), { key: "ArrowRight" });

    await waitFor(() => expect(screen.getByRole("tab", { name: "Files" })).toHaveFocus());
    expect(onRightRailTabChange).toHaveBeenCalledWith("files");
  });

  it("selects permanent tabs by their visible number while the tab strip is focused", () => {
    writeDefaultWorkspaceTab("changes");
    const { onRightRailTabChange } = renderWorkspace({
      showGithubTab: true,
      showBrowserTab: true,
      rightRailTab: "changes",
    });
    const changes = screen.getByRole("tab", { name: "Changes" });

    expect(changes).toHaveAttribute("aria-keyshortcuts", "1");
    expect(screen.getByRole("tab", { name: "Files" })).toHaveAttribute("aria-keyshortcuts", "2");
    expect(screen.getByRole("tab", { name: "Agents 1" })).toHaveAttribute("aria-keyshortcuts", "4");

    fireEvent.keyDown(changes, { key: "2" });
    fireEvent.keyDown(changes, { key: "5" });
    fireEvent.keyDown(changes, { key: "4" });

    const windowKeyDown = vi.fn();
    window.addEventListener("keydown", windowKeyDown);
    try {
      fireEvent.keyDown(changes, { key: "2", code: "Digit2", ctrlKey: true, altKey: true });
      expect(windowKeyDown).toHaveBeenCalledOnce();
      expect(windowKeyDown.mock.calls[0][0].defaultPrevented).toBe(false);
    } finally {
      window.removeEventListener("keydown", windowKeyDown);
    }

    expect(onRightRailTabChange).toHaveBeenNthCalledWith(1, "files");
    expect(onRightRailTabChange).toHaveBeenNthCalledWith(2, "subagents");
    expect(onRightRailTabChange).toHaveBeenCalledTimes(2);
  });

  it("compresses numeric positions around unavailable permanent tabs", () => {
    writeDefaultWorkspaceTab("github");
    const { onRightRailTabChange } = renderWorkspace({ showGithubTab: false });
    const files = screen.getByRole("tab", { name: "Files" });

    expect(files).toHaveAttribute("aria-keyshortcuts", "1");
    expect(screen.getByRole("tab", { name: "Changes" })).toHaveAttribute("aria-keyshortcuts", "2");
    expect(screen.getByRole("tab", { name: "Agents 1" })).toHaveAttribute("aria-keyshortcuts", "3");

    fireEvent.keyDown(files, { key: "3" });
    expect(onRightRailTabChange).toHaveBeenCalledWith("subagents");
  });

  it("keeps the remaining order when the default tab is unavailable", () => {
    writeDefaultWorkspaceTab("github");
    renderWorkspace({ showGithubTab: false });

    expect(screen.getAllByRole("tab").map((tab) => tab.getAttribute("aria-label"))).toEqual([
      "Files",
      "Changes",
      "Agents 1",
    ]);
  });

  it("shows inert workspace chrome while a temporary session is pending", () => {
    renderWorkspace({ pending: true });

    for (const name of ["Files", "Changes", "Pull Requests", "Agents"]) {
      expect(screen.getByRole("tab", { name: new RegExp(name) })).toBeDisabled();
    }
    expect(screen.getByText("Starting workspace…")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open new" })).toBeNull();
    expect(screen.queryByTestId("files-panel-stub")).toBeNull();
    expect(screen.queryByTestId("file-viewer-stub")).toBeNull();
    expect(screen.queryByTestId("subagents-stub")).toBeNull();
    expect(useCreateTerminalMock).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Full screen" })).toBeDisabled();
    expect(screen.getByRole("separator", { name: "Resize panel" })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(screen.getByRole("separator", { name: "Resize panel" })).toHaveAttribute(
      "tabindex",
      "-1",
    );
  });

  it("has no static Shells nav tab — shells open only as closable soft tabs", () => {
    renderWorkspace({ openTerminals: [] });
    expect(screen.queryByRole("tab", { name: /shells/i })).toBeNull();
  });

  it("badges the Changes tab (not Files) with the changed-file count", () => {
    renderWorkspace({ changedCount: 3 });

    // The changed-file count moved from the old single Files tab to Changes;
    // Files carries no count.
    expect(screen.getByRole("tab", { name: "Changes 3 changed" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Files" })).toBeInTheDocument();
  });

  it.each([
    { tabName: "Files", tooltip: "Files", shortcut: "1" },
    { tabName: "Changes", tooltip: "Changes", shortcut: "2" },
    { tabName: "Agents 1", tooltip: "Agents", shortcut: "3" },
  ])(
    "explains the $tabName pane icon and its number with a hover tooltip",
    async ({ tabName, tooltip, shortcut }) => {
      renderWorkspace();

      const tab = screen.getByRole("tab", { name: tabName });
      fireEvent.pointerMove(tab.parentElement!, { pointerType: "mouse" });
      const tip = await screen.findByRole("tooltip");
      expect(tip).toHaveTextContent(tooltip);
      expect(
        Array.from(tip.querySelectorAll('[data-slot="kbd"]')).map((key) => key.textContent),
      ).toEqual([MOD_KEY, ALT_KEY, "]", shortcut]);
      expect(tip.textContent?.match(/\+/g)).toHaveLength(1);
    },
  );
});

describe("WorkspacePanel open-file tabs", () => {
  it("renders a tab per open file labeled by basename, next to the fixed Files tab", () => {
    renderWorkspace({ openFiles: ["src/App.tsx", "docs/README.md"] });

    // The fixed Files tab and one file tab per open file (by basename, not the
    // full path). A failure means the strip didn't iterate openFiles or used
    // the full path instead of the basename.
    expect(screen.getByRole("tab", { name: /files/i })).toBeInTheDocument();
    expect(screen.getByText("App.tsx")).toBeInTheDocument();
    expect(screen.getByText("README.md")).toBeInTheDocument();
  });

  it("renders no file tabs when none are open", () => {
    renderWorkspace({ openFiles: [] });

    // No open files → no per-tab close buttons. A failure means the strip
    // rendered for an empty list.
    expect(screen.queryByRole("button", { name: /^Close / })).toBeNull();
  });

  it("marks the active file tab and leaves the Files tab inactive", () => {
    renderWorkspace({
      openFiles: ["src/App.tsx", "docs/README.md"],
      selectedFilePath: "docs/README.md",
    });

    // The active file's tab carries aria-current; the other does not. Located
    // via the uniquely-labeled close button since the basename text also
    // appears in the FileViewer stub.
    const readmeTab = screen
      .getByRole("button", { name: "Close README.md" })
      .closest("[role='button']");
    const appTab = screen.getByRole("button", { name: "Close App.tsx" }).closest("[role='button']");
    expect(readmeTab).toHaveAttribute("aria-current", "true");
    expect(appTab).toHaveAttribute("aria-current", "false");

    // With a file active the radix value is a sentinel, so the fixed Files tab
    // must read inactive — otherwise both "Files" and the file tab would look
    // selected at once (the bug the sentinel prevents).
    expect(screen.getByRole("tab", { name: /files/i })).toHaveAttribute("data-state", "inactive");
  });

  it("shows the Files tab as active when no file is selected", () => {
    renderWorkspace({ rightRailTab: "files", selectedFilePath: null });

    // No file selected on the Files tab → the fixed Files trigger is the active
    // selection. A failure means the sentinel leaked into the no-file case.
    expect(screen.getByRole("tab", { name: /files/i })).toHaveAttribute("data-state", "active");
  });

  it("activates a file via openFileViewer when its tab body is clicked", () => {
    const { openFileViewer } = renderWorkspace({
      openFiles: ["src/App.tsx", "docs/README.md"],
    });

    fireEvent.click(screen.getByText("README.md"));

    // Clicking the tab body opens that file. A failure means the row's onClick
    // isn't wired to openFileViewer with the tab's full path.
    expect(openFileViewer).toHaveBeenCalledWith("docs/README.md");
  });

  it("closes a file via onCloseFile (and does not also open it) when the x is clicked", () => {
    const { openFileViewer, onCloseFile } = renderWorkspace({
      openFiles: ["src/App.tsx", "docs/README.md"],
    });

    fireEvent.click(screen.getByRole("button", { name: "Close App.tsx" }));

    // The x closes exactly that file and must not also activate it
    // (stopPropagation), or closing would race with a selection.
    expect(onCloseFile).toHaveBeenCalledWith("src/App.tsx");
    expect(openFileViewer).not.toHaveBeenCalled();
  });
});

describe("WorkspacePanel content area", () => {
  it("keeps the file editor open while disabling media in the collapsed rail", () => {
    renderWorkspace({ open: false, selectedFilePath: "notes.md" });
    expect(screen.getByTestId("file-viewer-stub")).toHaveAttribute("data-open", "true");
    expect(screen.getByTestId("file-viewer-stub")).toHaveAttribute("data-media-active", "false");
  });

  it("renders the FileViewer for the active path (not the scope panel)", () => {
    renderWorkspace({
      openFiles: ["src/App.tsx"],
      selectedFilePath: "src/App.tsx",
    });

    // A selected file shows its viewer in the content slot; the scope panel
    // must not also mount. The stub echoes the path it received.
    expect(screen.getByTestId("file-viewer-stub")).toHaveTextContent("src/App.tsx");
    expect(screen.queryByTestId("files-panel-stub")).toBeNull();
  });

  it("renders the FilesPanel tree scope when no file is active on the Files tab", () => {
    renderWorkspace({ rightRailTab: "files", selectedFilePath: null });

    // No active file → the scope view owns the content slot and the viewer is
    // unmounted. The Files tab pins the panel to the full folder tree.
    const panel = screen.getByTestId("files-panel-stub");
    expect(panel).toHaveAttribute("data-flat-view", "false");
    expect(screen.queryByTestId("file-viewer-stub")).toBeNull();
  });

  it("renders the FilesPanel changed-only scope on the Changes tab", () => {
    renderWorkspace({ rightRailTab: "changes", selectedFilePath: null });

    // The Changes tab pins the same panel to the changed-files-only flat list.
    expect(screen.getByTestId("files-panel-stub")).toHaveAttribute("data-flat-view", "true");
    expect(screen.queryByTestId("file-viewer-stub")).toBeNull();
  });
});

describe("WorkspacePanel shell tabs", () => {
  const term = {
    id: "terminal_zsh_s1",
    name: "zsh",
    session: "u-g9qopr",
    running: true,
  };
  const termKey = "terminal:terminal_zsh_s1";

  it("renders a tab per open shell labeled by name · session", () => {
    useTerminalsMock.mockReturnValue({ terminals: [term], isLoading: false, error: null });
    renderWorkspace({ openTerminals: [termKey] });

    // The label resolves through the terminals cache (name · session), not the
    // raw tab key. A failure means the strip didn't render or didn't resolve
    // the label.
    expect(screen.getByText("zsh · u-g9qopr")).toBeInTheDocument();
  });

  it("sizes shell tab pills to the same 24px height as file tabs", () => {
    useTerminalsMock.mockReturnValue({ terminals: [term], isLoading: false, error: null });
    renderWorkspace({
      openFiles: ["src/App.tsx"],
      openTerminals: [termKey],
    });

    const fileTab = screen.getByText("App.tsx").closest("[role='button']");
    const shellTab = screen.getByText("zsh · u-g9qopr").closest("[role='button']");
    expect(fileTab).toHaveClass("h-[24px]");
    expect(shellTab).toHaveClass("h-[24px]");
    expect(shellTab).not.toHaveClass("h-[32px]");
  });

  it("surfaces the active shell's xterm in the content slot", async () => {
    useTerminalsMock.mockReturnValue({ terminals: [term], isLoading: false, error: null });
    const { rerender } = renderWorkspace({
      showContentSearch: true,
      rightRailTab: "content-search",
      openTerminals: [termKey],
      selectedTerminalKey: termKey,
    });

    // The selected shell tab owns the single content slot — its terminal id is
    // attached, and neither the files scope view nor a file viewer mounts.
    // findByTestId waits for the lazy TerminalView chunk to resolve through
    // its Suspense boundary.
    expect(await screen.findByTestId("terminal-view-stub")).toHaveTextContent("terminal_zsh_s1");
    expect(screen.queryByTestId("files-panel-stub")).toBeNull();
    expect(screen.queryByTestId("file-viewer-stub")).toBeNull();
    expect(screen.getByTestId("content-search-stub").parentElement).toHaveClass("hidden");
    rerender({ openTerminals: [] });
    expect(screen.getByTestId("content-search-stub").parentElement).toHaveClass("flex");
    fireEvent.keyDown(window, { code: "KeyF", ctrlKey: true, shiftKey: true });
    expect(screen.getByTestId("content-search-stub")).toHaveFocus();
    rerender({ openTerminals: [termKey] });
    expect(await screen.findByTestId("terminal-view-stub")).toHaveTextContent("terminal_zsh_s1");
    expect(screen.getByTestId("content-search-stub").parentElement).toHaveClass("hidden");
  });

  it("activates a shell via openTerminalTab when its tab body is clicked", () => {
    useTerminalsMock.mockReturnValue({ terminals: [term], isLoading: false, error: null });
    const { openTerminalTab } = renderWorkspace({
      openTerminals: [termKey],
    });

    fireEvent.click(screen.getByText("zsh · u-g9qopr"));

    expect(openTerminalTab).toHaveBeenCalledWith(termKey);
  });

  it("closes a shell via onCloseTerminal (and does not also open it) when the x is clicked", () => {
    useTerminalsMock.mockReturnValue({ terminals: [term], isLoading: false, error: null });
    const { openTerminalTab, onCloseTerminal } = renderWorkspace({
      openTerminals: [termKey],
    });

    fireEvent.click(screen.getByRole("button", { name: "Close zsh · u-g9qopr" }));

    expect(onCloseTerminal).toHaveBeenCalledWith(termKey);
    expect(openTerminalTab).not.toHaveBeenCalled();
  });

  it("keeps the static nav tabs inactive while a shell tab is active", () => {
    useTerminalsMock.mockReturnValue({ terminals: [term], isLoading: false, error: null });
    renderWorkspace({
      openTerminals: [termKey],
      selectedTerminalKey: termKey,
    });

    // The sentinel value must deselect every static trigger so a shell tab and
    // e.g. the Files tab never look selected at once.
    expect(screen.getByRole("tab", { name: /files/i })).toHaveAttribute("data-state", "inactive");
  });

  it("keeps an active shell selected when the last background browser closes", async () => {
    writeSessionWorkspaceState("conv_ws", {
      openBrowsers: ["browser-1"],
      selectedBrowserId: "browser-1",
    });
    useTerminalsMock.mockReturnValue({ terminals: [term], isLoading: false, error: null });
    const { onRightRailTabChange } = renderWorkspace({
      showBrowserTab: true,
      rightRailTab: "browser",
      openTerminals: [termKey],
      selectedTerminalKey: termKey,
    });

    fireEvent.click(screen.getByRole("button", { name: "Close Browser 1" }));

    await waitFor(() =>
      expect(screen.queryByRole("tab", { name: "Browser 1" })).not.toBeInTheDocument(),
    );
    expect(onRightRailTabChange).not.toHaveBeenCalled();
    expect(await screen.findByTestId("terminal-view-stub")).toHaveTextContent("terminal_zsh_s1");
  });
});

describe('WorkspacePanel "+" new-tab menu', () => {
  const declaresShell = () =>
    useSessionAgentMock.mockReturnValue({ data: { terminals: ["zsh"] } } as unknown as ReturnType<
      typeof useSessionAgent
    >);

  it("is hidden when the agent has no terminal access", () => {
    // No declared terminals (default mock: data undefined) → nothing to open.
    renderWorkspace({ showBrowserTab: false });
    expect(screen.queryByRole("button", { name: "Open new" })).toBeNull();
  });

  it("shows the Browser and Shell shortcuts as keycaps", async () => {
    declaresShell();
    renderWorkspace({ showBrowserTab: true });

    fireEvent.pointerDown(screen.getByRole("button", { name: "Open new" }), { button: 0 });
    const browser = await screen.findByRole("menuitem", { name: "Browser" });
    const shell = screen.getByRole("menuitem", { name: /shell \(zsh\)/i });
    const keycaps = (item: HTMLElement) =>
      Array.from(item.querySelectorAll("kbd"), (key) => key.textContent);

    expect(keycaps(browser)).toEqual(["Ctrl", "Alt", "B"]);
    expect(keycaps(shell)).toEqual(["Ctrl", "Alt", "T"]);
  });

  it("ignores number shortcuts from the portalled menu", async () => {
    const { onRightRailTabChange } = renderWorkspace({
      showBrowserTab: true,
      openFiles: ["src/App.tsx"],
      selectedFilePath: "src/App.tsx",
    });

    fireEvent.pointerDown(screen.getByRole("button", { name: "Open new" }), { button: 0 });
    const browser = await screen.findByRole("menuitem", { name: "Browser" });
    browser.focus();
    fireEvent.keyDown(browser, { key: "1", code: "Digit1" });

    expect(onRightRailTabChange).not.toHaveBeenCalled();
    expect(screen.getByTestId("file-viewer-stub")).toHaveTextContent("src/App.tsx");
  });

  it("renders exactly one '+' — after the nav tabs with no open tabs, trailing the tabs otherwise", () => {
    // No open tabs → "+" follows the nav tabs, outside the right-pinned group.
    declaresShell();
    renderWorkspace({ openFiles: [] });
    expect(screen.getAllByRole("button", { name: "Open new" })).toHaveLength(1);
    const leadingPlus = screen.getByRole("button", { name: "Open new" });
    expect(leadingPlus.closest(".ml-auto")).toBeNull();
    expect(
      screen.getByRole("tablist").compareDocumentPosition(leadingPlus) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    cleanup();

    // With open tabs "+" leads the right-pinned group, outside the scrolling
    // tabs region so it stays put when the tabs overflow.
    declaresShell();
    renderWorkspace({ openFiles: ["src/App.tsx"] });
    expect(screen.getAllByRole("button", { name: "Open new" })).toHaveLength(1);
    const plus = screen.getByRole("button", { name: "Open new" });
    const trailing = plus.closest(".ml-auto")!;
    expect(trailing.firstElementChild).toContainElement(plus);
    const tabsRegion = trailing.previousElementSibling as HTMLElement;
    expect(tabsRegion).toContainElement(screen.getByRole("button", { name: "Close App.tsx" }));
    expect(tabsRegion).not.toContainElement(plus);
  });

  it.each([false, true])(
    "excludes the scrolling tabs from window dragging without excluding the whole toolbar (maximized=%s)",
    (maximized) => {
      renderWorkspace({ openFiles: ["src/App.tsx", "docs/README.md"], maximized });
      const toolbar = screen.getByRole("toolbar", { name: "Workspace tabs" });
      const viewport = toolbar.querySelector(".overflow-x-auto");
      expect(viewport).not.toBeNull();
      expect(viewport).toHaveClass("no-drag");
      expect(viewport).toContainElement(screen.getByRole("button", { name: "Close App.tsx" }));
      expect(toolbar).not.toHaveClass("no-drag");
    },
  );

  it("offers Shell (gated on declared terminals), creating one and opening it as a tab", async () => {
    // Agent declares a shell; creating it resolves to a terminal whose tab key
    // is handed to openTerminalTab.
    declaresShell();
    const created = { id: "terminal_zsh_s1", name: "zsh", session: "u-1", running: true };
    const mutate = vi.fn((_name: string, opts?: { onSuccess?: (info: unknown) => void }) =>
      opts?.onSuccess?.(created),
    );
    useCreateTerminalMock.mockReturnValue({
      mutate,
      isPending: false,
      isError: false,
    } as unknown as ReturnType<typeof useCreateTerminal>);

    const { openTerminalTab } = renderWorkspace({ showBrowserTab: false });

    fireEvent.pointerDown(screen.getByRole("button", { name: "Open new" }), { button: 0 });
    fireEvent.click(await screen.findByRole("menuitem", { name: /shell/i }));

    // Launches the first declared shell and opens the created terminal's tab.
    expect(mutate).toHaveBeenCalledWith("zsh", expect.any(Object));
    expect(openTerminalTab).toHaveBeenCalledWith("terminal:terminal_zsh_s1");

    // The menu closes on its own after the launch.
    await waitFor(() => expect(screen.queryByRole("menuitem", { name: /shell/i })).toBeNull());
  });

  it("names the current default in the Shell item and launches it on click when several are declared", async () => {
    // Multiple declared terminals → the "Shell" item names the default inline
    // ("Shell (zsh)") and clicking it launches that default; the OTHER types
    // live behind a separate "Other shells" chevron.
    useSessionAgentMock.mockReturnValue({
      data: { terminals: ["zsh", "bash", "fish"] },
    } as unknown as ReturnType<typeof useSessionAgent>);
    const created = { id: "terminal_zsh_s1", name: "zsh", session: "u-2", running: true };
    const mutate = vi.fn((_name: string, opts?: { onSuccess?: (info: unknown) => void }) =>
      opts?.onSuccess?.(created),
    );
    useCreateTerminalMock.mockReturnValue({
      mutate,
      isPending: false,
      isError: false,
    } as unknown as ReturnType<typeof useCreateTerminal>);

    const { openTerminalTab } = renderWorkspace({ showBrowserTab: false });

    fireEvent.pointerDown(screen.getByRole("button", { name: "Open new" }), { button: 0 });
    // The default's type is shown inline in the row's label.
    const shellItem = await screen.findByRole("menuitem", { name: /shell \(zsh\)/i });
    fireEvent.click(shellItem);

    // Launches the default (first-declared) shell without a further pick.
    expect(mutate).toHaveBeenCalledWith("zsh", expect.any(Object));
    expect(openTerminalTab).toHaveBeenCalledWith("terminal:terminal_zsh_s1");

    // The menu closes on its own after the launch.
    await waitFor(() => expect(screen.queryByRole("menuitem", { name: /shell/i })).toBeNull());

    // Focus is not restored to the "+" trigger on close — that focus return is
    // what re-opened its tooltip for a frame (the visible flash after launch).
    expect(screen.getByRole("button", { name: "Open new" })).not.toHaveFocus();
  });

  it("does not launch on the multi-shell row when the session is offline", async () => {
    // The row is disabled on an offline session. Its click handler is a
    // sub-trigger's onClick (which Radix runs before its own disabled check), so
    // the handler guards on shellDisabled itself — a click must not fire create.
    useSessionAgentMock.mockReturnValue({
      data: { terminals: ["zsh", "bash", "fish"] },
    } as unknown as ReturnType<typeof useSessionAgent>);
    const mutate = vi.fn();
    useCreateTerminalMock.mockReturnValue({
      mutate,
      isPending: false,
      isError: false,
    } as unknown as ReturnType<typeof useCreateTerminal>);

    renderWorkspace({ liveness: { kind: "local_stranded" } });

    fireEvent.pointerDown(screen.getByRole("button", { name: "Open new" }), { button: 0 });
    const shellItem = await screen.findByRole("menuitem", { name: /shell \(zsh\)/i });
    expect(shellItem).toHaveTextContent(/offline/i);
    expect(shellItem).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(shellItem);
    expect(mutate).not.toHaveBeenCalled();
  });

  it("lists only the OTHER types in the flyout and remembers a pick as the new default (persisted)", async () => {
    window.localStorage.removeItem("omnigent:preferred-shell");
    useSessionAgentMock.mockReturnValue({
      data: { terminals: ["zsh", "bash", "fish"] },
    } as unknown as ReturnType<typeof useSessionAgent>);
    const mutate = vi.fn();
    useCreateTerminalMock.mockReturnValue({
      mutate,
      isPending: false,
      isError: false,
    } as unknown as ReturnType<typeof useCreateTerminal>);

    renderWorkspace({ showBrowserTab: false });

    // Open the flyout off the "Shell (zsh)" row (ArrowRight reveals the
    // submenu). It lists only the non-default types — zsh is already named in
    // the row itself.
    fireEvent.pointerDown(screen.getByRole("button", { name: "Open new" }), { button: 0 });
    const shellItem = await screen.findByRole("menuitem", { name: /shell \(zsh\)/i });
    shellItem.focus();
    fireEvent.keyDown(shellItem, { key: "ArrowRight" });
    expect(await screen.findByText("Other shells")).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /^Other shells$/i })).toBeNull();
    expect(await screen.findByRole("menuitem", { name: /^bash$/i })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /^fish$/i })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /^zsh$/i })).toBeNull();

    // Pick "bash": launches it and persists as the preferred type (app-global).
    fireEvent.click(screen.getByRole("menuitem", { name: /^bash$/i }));
    expect(mutate).toHaveBeenCalledWith("bash", expect.any(Object));
    expect(window.localStorage.getItem("omnigent:preferred-shell")).toBe("bash");

    // A fresh menu seeds its default from the persisted pick — the row now reads
    // "Shell (bash)" and launches bash on click.
    cleanup();
    mutate.mockClear();
    useSessionAgentMock.mockReturnValue({
      data: { terminals: ["zsh", "bash", "fish"] },
    } as unknown as ReturnType<typeof useSessionAgent>);
    renderWorkspace({ showBrowserTab: false });
    fireEvent.pointerDown(screen.getByRole("button", { name: "Open new" }), { button: 0 });
    fireEvent.click(await screen.findByRole("menuitem", { name: /shell \(bash\)/i }));
    expect(mutate).toHaveBeenCalledWith("bash", expect.any(Object));

    window.localStorage.removeItem("omnigent:preferred-shell");
  });

  it("keeps Shell enabled on a wakeable session — the server reconnects on create", async () => {
    // A runner merely asleep (host up) is reconnected transparently by the
    // server on create, so the item stays clickable and launches as normal.
    declaresShell();
    const mutate = vi.fn();
    useCreateTerminalMock.mockReturnValue({
      mutate,
      isPending: false,
      isError: false,
    } as unknown as ReturnType<typeof useCreateTerminal>);

    renderWorkspace({ liveness: { kind: "runner_asleep" } });

    fireEvent.pointerDown(screen.getByRole("button", { name: "Open new" }), { button: 0 });
    const shell = await screen.findByRole("menuitem", { name: /shell/i });
    expect(shell).not.toHaveAttribute("aria-disabled", "true");
    fireEvent.click(shell);
    expect(mutate).toHaveBeenCalledWith("zsh", expect.any(Object));
  });

  it("keeps unpinned pages accessible while a shell reconnects", async () => {
    declaresShell();
    writePinnedWorkspaceTabs([]);
    const mutate = vi.fn();
    useCreateTerminalMock.mockReturnValue({
      mutate,
      isPending: true,
      isError: false,
    } as unknown as ReturnType<typeof useCreateTerminal>);

    const { onRightRailTabChange } = renderWorkspace({ liveness: { kind: "host_asleep" } });

    const plus = screen.getByRole("button", { name: "Open new" });
    expect(plus).toBeEnabled();
    fireEvent.pointerDown(plus, { button: 0 });
    const shell = await screen.findByRole("menuitem", { name: /reconnecting/i });
    expect(shell).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(shell);
    expect(mutate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("menuitem", { name: "Changes" }));
    expect(onRightRailTabChange).toHaveBeenCalledWith("changes");
  });

  it("disables Shell and labels it Offline when the session can't be reconnected from the web", async () => {
    // host_offline / local_stranded need a CLI reconnect — the browser can't
    // wake them, so the item is disabled and marked Offline.
    declaresShell();
    const mutate = vi.fn();
    useCreateTerminalMock.mockReturnValue({
      mutate,
      isPending: false,
      isError: false,
    } as unknown as ReturnType<typeof useCreateTerminal>);

    renderWorkspace({ liveness: { kind: "local_stranded" } });

    fireEvent.pointerDown(screen.getByRole("button", { name: "Open new" }), { button: 0 });
    const shell = await screen.findByRole("menuitem", { name: /shell/i });
    expect(shell).toHaveTextContent(/offline/i);
    expect(shell).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(shell);
    expect(mutate).not.toHaveBeenCalled();
  });
});

describe("WorkspacePanel maximize", () => {
  it("shows a full-screen toggle pinned to the right and fires onToggleMaximized", () => {
    const { onToggleMaximized } = renderWorkspace();

    const toggle = screen.getByRole("button", { name: "Full screen" });
    expect(toggle).toHaveClass("text-muted-foreground", "hover:text-foreground");
    fireEvent.click(toggle);

    expect(onToggleMaximized).toHaveBeenCalledTimes(1);
  });

  it("swaps to the exit-full-screen affordance and covers the content region when maximized", () => {
    renderWorkspace({ maximized: true });

    // Label + pressed state flip so the icon reads as a minimize/exit control.
    const toggle = screen.getByRole("button", { name: "Exit full screen" });
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    // The rail breaks out of the docked flex sizing to cover the region, but
    // keeps the same flush/bordered styling so only the width changes — the
    // height is unaffected.
    const panel = screen.getByRole("complementary", { name: "Workspace" });
    expect(panel).toHaveClass("md:absolute", "md:inset-0", "md:border-l");
    expect(panel).not.toHaveClass("md:shrink-0", "md:m-2", "md:rounded-lg");
  });
});

describe("WorkspacePanel tab-strip layout (regression)", () => {
  const declaresShell = () =>
    useSessionAgentMock.mockReturnValue({ data: { terminals: ["zsh"] } } as unknown as ReturnType<
      typeof useSessionAgent
    >);

  // The row containing the fixed nav tabs — the tab strip.
  const strip = () => screen.getByRole("tab", { name: /files/i }).closest("div.border-b")!;

  it("keeps exactly one ml-auto in the strip with no open tabs (trailing group pins right)", () => {
    // Two ml-auto siblings split the free space and strand the nav group
    // mid-strip. The single ml-auto lives on the trailing control group; the
    // nav group must NOT also carry one.
    renderWorkspace({ openFiles: [], showBrowserTab: true });

    expect(strip().querySelectorAll(".ml-auto")).toHaveLength(1);
    const picker = screen.getByRole("button", { name: "Select panel" });
    expect(picker.closest(".ml-auto")).not.toBeNull();
    // The nav tablist stays left — no ml-auto.
    expect(screen.getByRole("tablist")).not.toHaveClass("ml-auto");
  });

  it("keeps exactly one ml-auto in the strip with open tabs (trailing group pins right)", () => {
    // The nav tabs stay anchored on the LEFT with open tabs — the open-tabs
    // region renders to their right and the trailing control group keeps the
    // row's single ml-auto. Two ml-auto siblings would split the free space.
    renderWorkspace({ openFiles: ["src/App.tsx"], showBrowserTab: true });

    expect(strip().querySelectorAll(".ml-auto")).toHaveLength(1);
    // The nav tablist stays left — no ml-auto of its own.
    expect(screen.getByRole("tablist")).not.toHaveClass("ml-auto");
    // The trailing control group owns the single ml-auto, pinning it right.
    expect(screen.getByRole("button", { name: "Select panel" }).closest(".ml-auto")).not.toBeNull();
    // The divider is present (separating the nav tabs from the open tabs) and
    // stays container-query gated to the ≥500px anchored case.
    const divider = strip().querySelector(".bg-border-strong");
    expect(divider).not.toBeNull();
    expect(divider).not.toHaveClass("ml-auto");
  });

  it("renders only populated tab strips in the scroll viewport", () => {
    // FileTabsStrip / TerminalTabsStrip must return null when empty — an empty
    // wrapper would still occupy a slot in the scroller's gap and offset the
    // trailing "+". With a file open (and no shells) the scroller should hold
    // exactly one child: the file-tabs strip (the empty terminal strip is null).
    declaresShell();
    renderWorkspace({ openFiles: ["src/App.tsx"] });

    const plus = screen.getByRole("button", { name: "Open new" });
    // Scroller precedes the trailing group holding "+"; it holds only the tabs.
    const tabsRegion = plus.closest(".ml-auto")!.previousElementSibling as HTMLElement;
    expect(tabsRegion.children).toHaveLength(1);
    expect(tabsRegion).toContainElement(screen.getByRole("button", { name: "Close App.tsx" }));
  });

  it("gives the full-screen button no left padding", () => {
    // The maximize button must not carry a pl-* gap — it sits flush against the
    // preceding nav icon like the rest of the strip.
    renderWorkspace({ openFiles: [] });
    const fullScreen = screen.getByRole("button", { name: "Full screen" });
    expect(fullScreen.parentElement).not.toHaveClass("pl-0.5");
  });
});

describe("WorkspacePanel browser tab", () => {
  it("opens a browser tab with Ctrl+Alt+B", () => {
    const { onRightRailTabChange, onReveal } = renderWorkspace({
      showBrowserTab: true,
    });

    fireEvent.keyDown(window, { code: "KeyB", ctrlKey: true, altKey: true });

    expect(screen.getByRole("tab", { name: "Browser 1" })).toBeInTheDocument();
    expect(onRightRailTabChange).toHaveBeenCalledWith("browser");
    expect(onReveal).toHaveBeenCalledOnce();
  });

  it("offers browsers without shell access and creates multiple closable tabs", async () => {
    renderWorkspace({ showBrowserTab: true, rightRailTab: "browser" });
    const openBrowser = async () => {
      fireEvent.pointerDown(screen.getByRole("button", { name: "Open new" }), {
        button: 0,
        ctrlKey: false,
      });
      fireEvent.click(await screen.findByRole("menuitem", { name: "Browser" }));
    };
    await openBrowser();
    const firstView = screen.getByTestId("browser-pane-stub").textContent;
    await openBrowser();
    expect(screen.getAllByRole("tab", { name: /^Browser \d/ })).toHaveLength(2);
    expect(screen.getByTestId("browser-pane-stub").textContent).not.toBe(firstView);
    fireEvent.click(screen.getByRole("tab", { name: "Browser 1" }));
    expect(screen.getByTestId("browser-pane-stub")).toHaveTextContent(firstView!);
    fireEvent.click(screen.getByRole("button", { name: "Close Browser 1" }));
    await waitFor(() =>
      expect(screen.getAllByRole("tab", { name: /^Browser \d/ })).toHaveLength(1),
    );
    fireEvent.click(screen.getByRole("button", { name: "Close Browser 1" }));
    await waitFor(() => expect(screen.queryByTestId("browser-pane-stub")).not.toBeInTheDocument());
    expect(screen.getByTestId("files-panel-stub")).toBeInTheDocument();
  });

  it("offers Browser only from the new-tab menu, without a permanent tab", async () => {
    renderWorkspace({ showBrowserTab: true });
    expect(screen.queryByRole("tab", { name: /^Browser$/i })).toBeNull();
    fireEvent.pointerDown(screen.getByRole("button", { name: "Open new" }), { button: 0 });
    expect(await screen.findByRole("menuitem", { name: "Browser" })).toBeInTheDocument();
  });

  it("omits Browser entirely when showBrowserTab is false", () => {
    renderWorkspace({ showBrowserTab: false });
    expect(screen.queryByRole("tab", { name: /browser/i })).toBeNull();
    expect(screen.queryByRole("button", { name: "Open new" })).toBeNull();
  });

  it("mounts the browser pane when a browser soft tab is selected", () => {
    writeSessionWorkspaceState("conv_ws", {
      openBrowsers: ["browser-1"],
      selectedBrowserId: "browser-1",
    });
    renderWorkspace({ showBrowserTab: true, rightRailTab: "browser" });
    // The content slot swaps to the embedded browser pane (stubbed here).
    expect(screen.getByTestId("browser-pane-stub")).toBeInTheDocument();
    // And the file scope views are not mounted in that branch.
    expect(screen.queryByTestId("files-panel-stub")).toBeNull();
  });

  it("deactivates the browser pane while the persistent rail is closed", () => {
    writeSessionWorkspaceState("conv_ws", {
      openBrowsers: ["browser-1"],
      selectedBrowserId: "browser-1",
    });
    renderWorkspace({ showBrowserTab: true, rightRailTab: "browser", open: false, inert: true });

    expect(screen.getByTestId("browser-pane-stub")).toHaveAttribute("data-active", "false");
  });

  it("shows an error when a native browser close fails", async () => {
    renderWorkspace({ showBrowserTab: true, rightRailTab: "browser" });
    fireEvent.pointerDown(screen.getByRole("button", { name: "Open new" }), {
      button: 0,
      ctrlKey: false,
    });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Browser" }));

    Object.assign(window, {
      omnigentDesktop: { browserClose: vi.fn().mockRejectedValue(new Error("disconnected")) },
    });
    fireEvent.click(screen.getByRole("button", { name: "Close Browser 1" }));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Couldn't close browser tab. Try again."),
    );
  });
});

describe("WorkspacePanel mobile side chats", () => {
  const openTabs = () =>
    writeSessionWorkspaceState("conv_ws", {
      openSideChats: ["conv_side_a", "conv_side_b"],
      selectedSideChatId: "conv_side_b",
    });

  it.each([false, true])(
    "keeps a side chat, draft, and queue when close fails (mobile=%s)",
    async (mobile) => {
      isMobileMock.mockReturnValue(mobile);
      openTabs();
      const followUp = {
        queueId: "q_side",
        text: "Queued follow-up",
        conversationId: "conv_side_b",
      };
      const otherChat = { queueId: "q_other", text: "Other chat", conversationId: "conv_side_a" };
      const sideQueue = () =>
        useChatStore.getState().queuedMessages.filter((m) => m.conversationId === "conv_side_b");
      const queuedAtStop: unknown[] = [];
      const stop = vi.spyOn(sessionsApi, "stopSession").mockImplementationOnce(async () => {
        queuedAtStop.push(sideQueue());
        throw new Error("offline");
      });
      useChatStore.setState({
        sideChatDrafts: { conv_side_b: "Keep this question" },
        queuedMessages: [followUp, otherChat],
      });
      renderWorkspace({ rightRailTab: "sidechat", mobileSideChatsOpen: true });
      const surface = mobile
        ? screen.getByTestId("side-chats-panel-drawer")
        : screen.getByRole("complementary", { name: "Workspace" });
      fireEvent.click(within(surface).getByRole("button", { name: "Close Side chat 2" }));

      await waitFor(() =>
        expect(toast.error).toHaveBeenCalledWith("Couldn't close side chat. Try again."),
      );
      expect(screen.getByTestId("side-chat-pane-stub")).toHaveTextContent("conv_side_b");
      expect(useChatStore.getState().sideChatDrafts.conv_side_b).toBe("Keep this question");
      expect(sideQueue()).toEqual([followUp]);

      const queuedDuringStop = { ...followUp, queueId: "q_late", text: "Queued while closing" };
      stop.mockImplementationOnce(async () => {
        queuedAtStop.push(sideQueue());
        useChatStore.setState((s) => ({ queuedMessages: [...s.queuedMessages, queuedDuringStop] }));
        return { queued: false };
      });
      fireEvent.click(within(surface).getByRole("button", { name: "Close Side chat 2" }));
      await waitFor(() =>
        expect(screen.getByTestId("side-chat-pane-stub")).toHaveTextContent("conv_side_a"),
      );
      expect(stop.mock.calls).toEqual([["conv_side_b"], ["conv_side_b"]]);
      expect(queuedAtStop).toEqual([[], []]);
      expect(useChatStore.getState().sideChatDrafts.conv_side_b).toBeUndefined();
      expect(useChatStore.getState().queuedMessages).toEqual([otherChat]);
      useChatStore.setState({ queuedMessages: [] });
      stop.mockRestore();
    },
  );

  it("shows the selected side chat in the drawer, not the hidden rail", () => {
    isMobileMock.mockReturnValue(true);
    openTabs();
    const { onMobileSideChatsOpenChange } = renderWorkspace({
      rightRailTab: "sidechat",
      mobileSideChatsOpen: true,
    });

    const drawer = screen.getByTestId("side-chats-panel-drawer");
    expect(drawer).toHaveAttribute("data-state", "open");
    expect(screen.getAllByTestId("side-chat-pane-stub").map((el) => el.textContent)).toEqual([
      "conv_side_b",
    ]);
    expect(drawer).toContainElement(screen.getByTestId("side-chat-pane-stub"));

    fireEvent.click(within(drawer).getByRole("tab", { name: "Side chat 1" }));
    expect(screen.getByTestId("side-chat-pane-stub")).toHaveTextContent("conv_side_a");
    fireEvent.click(within(drawer).getByRole("button", { name: "Close" }));
    expect(onMobileSideChatsOpenChange).toHaveBeenCalledWith(false);
  });

  it("renders the drawer inside the app subtree, beside the hidden rail", () => {
    // The embed scopes its stylesheet to its own root, and native shells scope
    // safe-area insets to the app shell, so a drawer portaled to the body
    // would render unstyled there.
    isMobileMock.mockReturnValue(true);
    openTabs();
    const { view } = renderWorkspace({ rightRailTab: "sidechat", mobileSideChatsOpen: true });

    const drawer = screen.getByTestId("side-chats-panel-drawer");
    expect(view.container).toContainElement(drawer);
    expect(
      screen.getByRole("complementary", { name: "Workspace", hidden: true }),
    ).not.toContainElement(drawer);
  });

  it("keeps the side chat mounted while the drawer is closed", () => {
    // Dismissing the drawer must not unmount the pane: a seeded `/side`
    // question still waiting on the child's agent binding has to go out, and
    // unsent composer text has to survive — as behind a collapsed desktop rail.
    isMobileMock.mockReturnValue(true);
    openTabs();
    renderWorkspace({ rightRailTab: "sidechat", mobileSideChatsOpen: false });

    const drawer = screen.getByTestId("side-chats-panel-drawer");
    expect(drawer).toHaveAttribute("data-state", "closed");
    expect(within(drawer).getByTestId("side-chat-pane-stub")).toHaveTextContent("conv_side_b");
  });

  it("keeps the side chat in the rail on desktop", () => {
    openTabs();
    renderWorkspace({ rightRailTab: "sidechat", mobileSideChatsOpen: true });

    expect(screen.queryByTestId("side-chats-panel-drawer")).toBeNull();
    expect(screen.getByTestId("side-chat-pane-stub")).toHaveTextContent("conv_side_b");
  });
});

describe("WorkspacePanel panel picker", () => {
  it("selects a file by full path, distinguishing duplicate basenames", () => {
    const { openFileViewer } = renderWorkspace({
      openFiles: ["src/index.ts", "tests/index.ts"],
      selectedFilePath: "src/index.ts",
    });
    fireEvent.pointerDown(screen.getByRole("button", { name: "Select panel" }), { button: 0 });
    expect(screen.getByRole("menuitem", { name: "src/index.ts" })).toHaveAttribute(
      "aria-current",
      "true",
    );
    fireEvent.click(screen.getByRole("menuitem", { name: "tests/index.ts" }));
    expect(openFileViewer).toHaveBeenCalledWith("tests/index.ts");
  });

  it("lists available permanent panels and switches to them", () => {
    const { onRightRailTabChange } = renderWorkspace({ showGithubTab: true });
    fireEvent.pointerDown(screen.getByRole("button", { name: "Select panel" }), { button: 0 });
    for (const name of ["Changes", "Pull Requests", "Agents"]) {
      expect(screen.getByRole("menuitem", { name })).toBeInTheDocument();
    }
    fireEvent.click(screen.getByRole("menuitem", { name: "Pull Requests" }));
    expect(onRightRailTabChange).toHaveBeenCalledWith("github");
  });

  // A real mouse press also releases; Radix's tooltip ignores focus while it
  // still believes the pointer is down on its trigger.
  function openPickerWithMouse() {
    const trigger = screen.getByRole("button", { name: "Select panel" });
    fireEvent.pointerDown(trigger, { button: 0 });
    fireEvent.pointerUp(trigger, { button: 0 });
    return trigger;
  }

  // Radix hands focus back from a timer queued when the menu unmounts; give it
  // a short, bounded window before asserting that nothing refocused.
  async function settleMenuClose() {
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await new Promise<void>((resolve) => {
      setTimeout(resolve, 50);
    });
  }

  it("keeps the Select panel tooltip closed after a mouse pick", async () => {
    renderWorkspace({ openFiles: ["src/App.tsx", "docs/README.md"] });
    const trigger = openPickerWithMouse();
    const item = await screen.findByRole("menuitem", { name: "docs/README.md" });
    fireEvent.pointerDown(item, { button: 0 });
    fireEvent.click(item);
    await settleMenuClose();
    expect(screen.queryByRole("tooltip")).toBeNull();
    expect(trigger).not.toHaveFocus();
    // Hovering the button afterwards still shows its tooltip.
    fireEvent.pointerMove(trigger.parentElement!, { pointerType: "mouse" });
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Select panel");
  });

  it("keeps the tooltip closed when the pick releases a press that began on the trigger", async () => {
    renderWorkspace({ openFiles: ["src/App.tsx"] });
    const trigger = screen.getByRole("button", { name: "Select panel" });
    fireEvent.pointerDown(trigger, { button: 0 });
    const item = await screen.findByRole("menuitem", { name: "src/App.tsx" });
    // Releasing on an item that was never pressed is how Radix selects on drag.
    fireEvent.pointerUp(item, { button: 0 });
    await settleMenuClose();
    expect(screen.queryByRole("tooltip")).toBeNull();
    expect(trigger).not.toHaveFocus();
  });

  it("keeps the tooltip closed after a mouse click outside the open picker", async () => {
    renderWorkspace({ openFiles: ["src/App.tsx"] });
    const trigger = openPickerWithMouse();
    await screen.findByRole("menu");
    // The outside-press listener attaches a tick after the menu opens.
    await new Promise<void>((resolve) => {
      setTimeout(resolve, 0);
    });
    fireEvent.pointerDown(document.body, { button: 0 });
    await settleMenuClose();
    expect(screen.queryByRole("tooltip")).toBeNull();
    expect(trigger).not.toHaveFocus();
  });

  it("returns focus to the trigger after a keyboard pick, even after a press in the menu", async () => {
    renderWorkspace({ openFiles: ["src/App.tsx"] });
    const trigger = screen.getByRole("button", { name: "Select panel" });
    trigger.focus();
    fireEvent.keyDown(trigger, { key: "Enter" });
    const item = await screen.findByRole("menuitem", { name: "src/App.tsx" });
    // A press inside the open menu switches the picker to pointer input; the
    // Enter that follows must switch it back so focus still returns.
    fireEvent.pointerDown(item, { button: 0 });
    fireEvent.keyDown(item, { key: "Enter" });
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("selects an open terminal", () => {
    const { openTerminalTab } = renderWorkspace({ openTerminals: ["terminal:zsh"] });
    fireEvent.pointerDown(screen.getByRole("button", { name: "Select panel" }), { button: 0 });
    fireEvent.click(screen.getByRole("menuitem", { name: "zsh" }));
    expect(openTerminalTab).toHaveBeenCalledWith("terminal:zsh");
  });

  it("selects browser and side-chat tabs", () => {
    writeSessionWorkspaceState("conv_ws", {
      openBrowsers: ["browser-one"],
      openSideChats: ["pending:one"],
    });
    const { onRightRailTabChange } = renderWorkspace({ showBrowserTab: true });
    fireEvent.pointerDown(screen.getByRole("button", { name: "Select panel" }), { button: 0 });
    fireEvent.click(screen.getByRole("menuitem", { name: "Browser 1" }));
    expect(onRightRailTabChange).toHaveBeenCalledWith("browser");
    fireEvent.pointerDown(screen.getByRole("button", { name: "Select panel" }), { button: 0 });
    fireEvent.click(screen.getByRole("menuitem", { name: "Side chat 1" }));
    expect(onRightRailTabChange).toHaveBeenCalledWith("sidechat");
  });
});

it("keeps a contained workspace inline on narrow viewports and scopes its file browser", () => {
  isMobileMock.mockReturnValue(true);
  renderWorkspace({ contained: true, maximized: true });
  expect(screen.getByRole("complementary", { name: "Workspace" })).toHaveClass(
    "absolute",
    "inset-0",
    "flex",
  );
  expect(screen.getByTestId("files-panel-stub")).toHaveAttribute("data-session", "conv_ws");
});
