import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useSessionAgent } from "@/hooks/useAgents";
import type { SessionLiveness } from "@/hooks/useSessionLiveness";
import type * as UseTerminalsModule from "@/hooks/useTerminals";
import { useCreateTerminal, useTerminals } from "@/hooks/useTerminals";
import type { ChangedSort } from "./FlatFileList";
import type { RightRailTab } from "./railTabs";
import { WorkspacePanel } from "./WorkspacePanel";

// The rail's content children are exercised by their own suites; stub them so
// these tests focus on WorkspacePanel's own logic (the open-file/shell tab
// strips and the content branch that swaps FileViewer ↔ FilesPanel ↔ shell).
// Each stub renders a testid (plus, for FileViewer, the path it was asked to
// show) so we can prove which child mounted without dragging in Monaco / hook
// stacks / xterm.
vi.mock("./FileViewer", () => ({
  FileViewer: ({ path }: { path: string }) => <div data-testid="file-viewer-stub">{path}</div>,
}));
vi.mock("./FilesPanel", () => ({
  FilesPanel: () => <div data-testid="files-panel-stub" />,
}));
vi.mock("./InlineTerminalsSection", () => ({
  InlineTerminalsSection: () => <div data-testid="terminals-stub" />,
}));
vi.mock("./SubagentsPanel", () => ({
  SubagentsPanel: () => <div data-testid="subagents-stub" />,
}));
vi.mock("./TodoPanel", () => ({
  TodoPanel: () => <div data-testid="todos-stub" />,
}));
vi.mock("@/components/BrowserPane/BrowserPane", () => ({
  BrowserPane: ({ conversationId }: { conversationId: string }) => (
    <div data-testid="browser-pane-stub">{conversationId}</div>
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

const useTerminalsMock = vi.mocked(useTerminals);
const useCreateTerminalMock = vi.mocked(useCreateTerminal);
const useSessionAgentMock = vi.mocked(useSessionAgent);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
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
  overrides: {
    rightRailTab?: RightRailTab;
    selectedFilePath?: string | null;
    openFiles?: string[];
    showBrowserTab?: boolean;
    showShellsTab?: boolean;
    openTerminals?: string[];
    selectedTerminalKey?: string | null;
    maximized?: boolean;
    liveness?: SessionLiveness;
  } = {},
) {
  const openFileViewer = vi.fn();
  const onCloseFile = vi.fn();
  const onRightRailTabChange = vi.fn();
  const openTerminalTab = vi.fn();
  const onCloseTerminal = vi.fn();
  const onToggleMaximized = vi.fn();
  render(
    <TooltipProvider delayDuration={0}>
      <WorkspacePanel
        conversationId="conv_ws"
        width={360}
        handleProps={{ tabIndex: 0 }}
        rightRailTab={overrides.rightRailTab ?? "files"}
        onRightRailTabChange={onRightRailTabChange}
        showFilesPanel
        showBrowserTab={overrides.showBrowserTab ?? false}
        changedCount={0}
        showShellsTab={overrides.showShellsTab ?? false}
        terminalsLength={0}
        subagentsWorking={0}
        agentCount={1}
        todosSupported={false}
        todosCompleted={0}
        todosTotal={0}
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
        filesPanelFlatView={false}
        onFlatViewChange={vi.fn()}
        filesPanelShowHidden={false}
        onShowHiddenChange={vi.fn()}
        liveness={overrides.liveness}
      />
    </TooltipProvider>,
  );
  return {
    openFileViewer,
    onCloseFile,
    onRightRailTabChange,
    openTerminalTab,
    onCloseTerminal,
    onToggleMaximized,
  };
}

describe("WorkspacePanel surface presentation", () => {
  it("sits flush to the window edge with a left divider, no floating card frame", () => {
    renderWorkspace();

    const panel = screen.getByRole("complementary", { name: "Workspace" });
    expect(panel).toHaveClass("md:border-l", "md:border-border");
    expect(panel).not.toHaveClass("md:m-2", "md:rounded-lg", "md:shadow-lg");
  });

  it("presents the fixed pane tabs as compact icon controls with accessible labels", () => {
    renderWorkspace();

    const filesTab = screen.getByRole("tab", { name: "Files" });
    const agentsTab = screen.getByRole("tab", { name: "Agents 1" });
    expect(filesTab).toHaveClass("size-8", "p-0");
    expect(filesTab).not.toHaveAttribute("title");
    expect(agentsTab).toHaveClass("size-8", "p-0");
    expect(agentsTab).not.toHaveAttribute("title");
  });

  it.each([
    { tabName: "Files", tooltip: "Files" },
    { tabName: "Agents 1", tooltip: "Agents" },
  ])("explains the $tabName pane icon with a hover tooltip", async ({ tabName, tooltip }) => {
    renderWorkspace();

    const tab = screen.getByRole("tab", { name: tabName });
    fireEvent.pointerMove(tab.parentElement!, { pointerType: "mouse" });
    expect(await screen.findByRole("tooltip")).toHaveTextContent(tooltip);
  });
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

  it("renders the FilesPanel scope view when no file is active on the Files tab", () => {
    renderWorkspace({ rightRailTab: "files", selectedFilePath: null });

    // No active file → the scope view (Changed/All list/tree) owns the content
    // slot and the viewer is unmounted.
    expect(screen.getByTestId("files-panel-stub")).toBeInTheDocument();
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
    renderWorkspace({ showShellsTab: true, openTerminals: [termKey] });

    // The label resolves through the terminals cache (name · session), not the
    // raw tab key. A failure means the strip didn't render or didn't resolve
    // the label.
    expect(screen.getByText("zsh · u-g9qopr")).toBeInTheDocument();
  });

  it("surfaces the active shell's xterm in the content slot", () => {
    useTerminalsMock.mockReturnValue({ terminals: [term], isLoading: false, error: null });
    renderWorkspace({
      showShellsTab: true,
      openTerminals: [termKey],
      selectedTerminalKey: termKey,
    });

    // The selected shell tab owns the single content slot — its terminal id is
    // attached, and neither the files scope view nor a file viewer mounts.
    expect(screen.getByTestId("terminal-view-stub")).toHaveTextContent("terminal_zsh_s1");
    expect(screen.queryByTestId("files-panel-stub")).toBeNull();
    expect(screen.queryByTestId("file-viewer-stub")).toBeNull();
  });

  it("activates a shell via openTerminalTab when its tab body is clicked", () => {
    useTerminalsMock.mockReturnValue({ terminals: [term], isLoading: false, error: null });
    const { openTerminalTab } = renderWorkspace({
      showShellsTab: true,
      openTerminals: [termKey],
    });

    fireEvent.click(screen.getByText("zsh · u-g9qopr"));

    expect(openTerminalTab).toHaveBeenCalledWith(termKey);
  });

  it("closes a shell via onCloseTerminal (and does not also open it) when the x is clicked", () => {
    useTerminalsMock.mockReturnValue({ terminals: [term], isLoading: false, error: null });
    const { openTerminalTab, onCloseTerminal } = renderWorkspace({
      showShellsTab: true,
      openTerminals: [termKey],
    });

    fireEvent.click(screen.getByRole("button", { name: "Close zsh · u-g9qopr" }));

    expect(onCloseTerminal).toHaveBeenCalledWith(termKey);
    expect(openTerminalTab).not.toHaveBeenCalled();
  });

  it("keeps the static nav tabs inactive while a shell tab is active", () => {
    useTerminalsMock.mockReturnValue({ terminals: [term], isLoading: false, error: null });
    renderWorkspace({
      showShellsTab: true,
      openTerminals: [termKey],
      selectedTerminalKey: termKey,
    });

    // The sentinel value must deselect every static trigger so a shell tab and
    // e.g. the Files tab never look selected at once.
    expect(screen.getByRole("tab", { name: /files/i })).toHaveAttribute("data-state", "inactive");
  });
});

describe('WorkspacePanel "+" new-tab menu', () => {
  // The "+" gates purely on shell access — the agent's declared terminals.
  // (The embedded browser is one view per conversation, reached via its own
  // pinned tab, so it isn't offered here.)
  const declaresShell = () =>
    useSessionAgentMock.mockReturnValue({ data: { terminals: ["zsh"] } } as unknown as ReturnType<
      typeof useSessionAgent
    >);

  it("is hidden when the agent has no terminal access", () => {
    // No declared terminals (default mock: data undefined) → nothing to open.
    renderWorkspace({ showBrowserTab: true });
    expect(screen.queryByRole("button", { name: "Open new" })).toBeNull();
  });

  it("renders exactly one '+' — after the nav tabs with no open tabs, trailing the tabs otherwise", () => {
    declaresShell();
    // No open tabs → single "+", sitting after the nav tabs.
    renderWorkspace({ openFiles: [] });
    expect(screen.getAllByRole("button", { name: "Open new" })).toHaveLength(1);
    cleanup();

    // With an open file tab → still exactly one "+", now trailing the tabs. It
    // sits OUTSIDE the scrolling tabs region (as its next sibling) so it stays
    // pinned when the tabs overflow instead of scrolling/overlapping.
    declaresShell();
    renderWorkspace({ openFiles: ["src/App.tsx"] });
    const plus = screen.getByRole("button", { name: "Open new" });
    expect(plus).toBeInTheDocument();
    // The scrolling tabs region (holds the file tab's close button) is the
    // "+" wrapper's immediately-preceding sibling — the "+" is not inside it.
    const plusWrapper = plus.parentElement!;
    const tabsRegion = plusWrapper.previousElementSibling as HTMLElement;
    expect(tabsRegion).toContainElement(screen.getByRole("button", { name: "Close App.tsx" }));
    expect(tabsRegion).not.toContainElement(plus);
  });

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
  });

  it("launches the default shell directly when several are declared (type selection is optional)", async () => {
    // Multiple declared terminals → "Shell" nests a submenu to pick a type, but
    // clicking it directly launches the default (declared[0]) without forcing a
    // selection.
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

    // Open the "+" menu and click the "Shell" submenu trigger directly.
    fireEvent.pointerDown(screen.getByRole("button", { name: "Open new" }), { button: 0 });
    fireEvent.click(await screen.findByRole("menuitem", { name: /shell/i }));

    // Launches the default (first-declared) shell without a further pick.
    expect(mutate).toHaveBeenCalledWith("zsh", expect.any(Object));
    expect(openTerminalTab).toHaveBeenCalledWith("terminal:terminal_zsh_s1");
  });

  it("remembers the picked shell type as the new default (persisted + check-marked)", async () => {
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

    // Open the submenu (ArrowRight — a plain click on "Shell" launches the
    // default instead) and pick "bash", which persists as the preferred type.
    fireEvent.pointerDown(screen.getByRole("button", { name: "Open new" }), { button: 0 });
    const shellTrigger = await screen.findByRole("menuitem", { name: /shell/i });
    shellTrigger.focus();
    fireEvent.keyDown(shellTrigger, { key: "ArrowRight" });
    fireEvent.click(await screen.findByRole("menuitem", { name: /^bash$/i }));
    expect(mutate).toHaveBeenCalledWith("bash", expect.any(Object));
    expect(window.localStorage.getItem("omnigent:preferred-shell")).toBe("bash");

    // A fresh menu seeds its default from the persisted pick — clicking "Shell"
    // now launches bash without opening the submenu.
    cleanup();
    mutate.mockClear();
    useSessionAgentMock.mockReturnValue({
      data: { terminals: ["zsh", "bash", "fish"] },
    } as unknown as ReturnType<typeof useSessionAgent>);
    renderWorkspace({ showBrowserTab: false });
    fireEvent.pointerDown(screen.getByRole("button", { name: "Open new" }), { button: 0 });
    fireEvent.click(await screen.findByRole("menuitem", { name: /shell/i }));
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

  it("shows 'Reconnecting…' while a create is in flight on a wakeable session", async () => {
    declaresShell();
    useCreateTerminalMock.mockReturnValue({
      mutate: vi.fn(),
      isPending: true,
      isError: false,
    } as unknown as ReturnType<typeof useCreateTerminal>);

    renderWorkspace({ liveness: { kind: "host_asleep" } });

    fireEvent.pointerDown(screen.getByRole("button", { name: "Open new" }), { button: 0 });
    expect(await screen.findByRole("menuitem", { name: /reconnecting/i })).toBeInTheDocument();
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

    fireEvent.click(screen.getByRole("button", { name: "Full screen" }));

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

  it("keeps exactly one ml-auto in the strip with no open tabs (maximize pins right)", () => {
    // Two ml-auto siblings split the free space and strand the nav group
    // mid-strip. With no open tabs the single ml-auto lives on the maximize
    // button; the nav group must NOT also carry one.
    renderWorkspace({ openFiles: [], showBrowserTab: true });

    expect(strip().querySelectorAll(".ml-auto")).toHaveLength(1);
    // It's the maximize button's wrapper (pins the button right).
    const fullScreen = screen.getByRole("button", { name: "Full screen" });
    expect(fullScreen.parentElement).toHaveClass("ml-auto");
    // The nav tablist stays left — no ml-auto.
    expect(screen.getByRole("tablist")).not.toHaveClass("ml-auto");
  });

  it("keeps exactly one ml-auto in the strip with open tabs (nav tabs stay left, maximize pins right)", () => {
    // The nav tabs stay anchored on the LEFT with open tabs — the open-tabs
    // region renders to their right and the maximize button keeps the row's
    // single ml-auto. Two ml-auto siblings would split the free space.
    renderWorkspace({ openFiles: ["src/App.tsx"], showBrowserTab: true });

    expect(strip().querySelectorAll(".ml-auto")).toHaveLength(1);
    // The nav tablist stays left — no ml-auto of its own.
    expect(screen.getByRole("tablist")).not.toHaveClass("ml-auto");
    // The maximize button owns the single ml-auto, pinning it right.
    expect(screen.getByRole("button", { name: "Full screen" }).parentElement).toHaveClass(
      "ml-auto",
    );
    // The divider is present (separating the nav tabs from the open tabs) and
    // stays container-query gated to the ≥500px anchored case.
    const divider = strip().querySelector(".bg-border-strong");
    expect(divider).not.toBeNull();
    expect(divider).not.toHaveClass("ml-auto");
  });

  it("does not leave a phantom gap: empty tab strips render nothing, so the '+' hugs the last tab", () => {
    // FileTabsStrip / TerminalTabsStrip must return null when empty — an empty
    // wrapper would still occupy a slot in the scroller's gap and offset the
    // trailing "+". With a file open (and no shells) the scroller should hold
    // exactly one child: the file-tabs strip (the empty terminal strip is null).
    declaresShell();
    renderWorkspace({ openFiles: ["src/App.tsx"] });

    const plus = screen.getByRole("button", { name: "Open new" });
    // Scroller is the "+" wrapper's preceding sibling; it holds only the tabs.
    const tabsRegion = plus.parentElement!.previousElementSibling as HTMLElement;
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
  it("renders the Browser tab only when showBrowserTab is set", () => {
    renderWorkspace({ showBrowserTab: true });
    expect(screen.getByRole("tab", { name: /browser/i })).toBeInTheDocument();
  });

  it("omits the Browser tab when showBrowserTab is false", () => {
    renderWorkspace({ showBrowserTab: false });
    expect(screen.queryByRole("tab", { name: /browser/i })).toBeNull();
  });

  it("mounts the browser pane when the browser tab is selected", () => {
    renderWorkspace({ showBrowserTab: true, rightRailTab: "browser" });
    // The content slot swaps to the embedded browser pane (stubbed here).
    expect(screen.getByTestId("browser-pane-stub")).toBeInTheDocument();
    // And the file scope views are not mounted in that branch.
    expect(screen.queryByTestId("files-panel-stub")).toBeNull();
  });
});
