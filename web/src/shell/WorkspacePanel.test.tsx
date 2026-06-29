import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChangedSort } from "./FlatFileList";
import type { RightRailTab } from "./railTabs";
import { WorkspacePanel } from "./WorkspacePanel";

// The rail's content children are exercised by their own suites; stub them so
// these tests focus on WorkspacePanel's own logic (the open-file tab strip and
// the content branch that swaps FileViewer ↔ FilesPanel). Each stub renders a
// testid (plus, for FileViewer, the path it was asked to show) so we can prove
// which child mounted without dragging in Monaco / hook stacks.
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

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
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
    showSchedulesTab?: boolean;
    showCanvasTab?: boolean;
  } = {},
) {
  const openFileViewer = vi.fn();
  const onCloseFile = vi.fn();
  const onRightRailTabChange = vi.fn();
  render(
    <WorkspacePanel
      conversationId="conv_ws"
      width={360}
      handleProps={{ tabIndex: 0 }}
      rightRailTab={overrides.rightRailTab ?? "files"}
      onRightRailTabChange={onRightRailTabChange}
      showFilesPanel
      showSchedulesTab={overrides.showSchedulesTab ?? false}
      showCanvasTab={overrides.showCanvasTab ?? false}
      changedCount={0}
      showShellsTab={false}
      terminalsLength={0}
      subagentsWorking={0}
      agentCount={1}
      isClaudeNative={false}
      todosCompleted={0}
      todosTotal={0}
      rootSessionId={null}
      selectedFilePath={overrides.selectedFilePath ?? null}
      openFiles={overrides.openFiles ?? []}
      openFileViewer={openFileViewer}
      onCloseFile={onCloseFile}
      onShowScopeView={vi.fn()}
      onCommentsOpenChange={vi.fn()}
      openTerminalsPanel={vi.fn()}
      permissionLevel={null}
      filesPanelSort={"recent" as ChangedSort}
      onSortChange={vi.fn()}
      filesPanelFlatView={false}
      onFlatViewChange={vi.fn()}
      filesPanelShowHidden={false}
      onShowHiddenChange={vi.fn()}
    />,
  );
  return { openFileViewer, onCloseFile, onRightRailTabChange };
}

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
