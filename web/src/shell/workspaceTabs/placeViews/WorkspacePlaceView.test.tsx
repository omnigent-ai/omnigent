import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState, type ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { isMacPlatform } from "@/lib/hotkeys";
import type { PullRequestPageBody } from "@/pullRequestPage/PullRequestPageBody";
import type * as ReviewLayout from "@/components/diff-review/reviewLayout";
import type { ChangesReview } from "@/shell/ChangesReview";
import type { PullRequestPanel } from "@/shell/PullRequestPanel";
import { stopFilePosition } from "@/shell/filePositionState";
import {
  REVEAL_ACTIVE_FILE_PALETTE_LABEL,
  forgetActiveFileRevealsForTests,
  useActiveFileRevealRequests,
} from "@/shell/fileTree/revealActiveFile";
import { showTreeRoot } from "@/shell/fileTree/treeRoot";
import { rememberBrowseLocation } from "@/shell/filesPane/browseLocations";
import type * as WorkspaceChangedFilesModule from "@/hooks/useWorkspaceChangedFiles";
import { usePaletteCommandsStore } from "@/store/paletteCommands";
import { registeredPaletteCommands } from "@/test/paletteCommands";

import type { FilesPageTree } from "./FilesPageTree";
import type { FilesPageViewer } from "./FilesPageViewer";
import { WorkspacePlaceView, type WorkspacePlaceViewProps } from "./WorkspacePlaceView";

const captured = vi.hoisted(() => ({
  tree: null as ComponentProps<typeof FilesPageTree> | null,
  viewer: null as ComponentProps<typeof FilesPageViewer> | null,
  list: null as ComponentProps<typeof PullRequestPanel> | null,
  page: null as ComponentProps<typeof PullRequestPageBody> | null,
  review: null as ComponentProps<typeof ChangesReview> | null,
}));

// Unmeasured counts as wide; a test sets a width to see the narrow page's popover tree.
const pageWidth = vi.hoisted(() => ({ value: null as number | null }));
vi.mock("@/components/diff-review/reviewLayout", async (importOriginal) => ({
  ...(await importOriginal<typeof ReviewLayout>()),
  useElementWidth: () => pageWidth.value,
}));

vi.mock("./FilesPageTree", () => ({
  FilesPageTree: (props: ComponentProps<typeof FilesPageTree>) => {
    captured.tree = props;
    return <div data-testid="file-tree" />;
  },
}));
vi.mock("./FilesPageViewer", () => ({
  FilesPageViewer: (props: ComponentProps<typeof FilesPageViewer>) => {
    captured.viewer = props;
    return (
      <div data-testid="file-viewer">
        {props.toolbarLeading}
        {props.path}
      </div>
    );
  },
}));
vi.mock("@/shell/PullRequestPanel", () => ({
  PullRequestPanel: (props: ComponentProps<typeof PullRequestPanel>) => {
    captured.list = props;
    return <div data-testid="pull-request-list" />;
  },
}));
vi.mock("@/pullRequestPage/PullRequestPageBody", () => ({
  PullRequestPageBody: (props: ComponentProps<typeof PullRequestPageBody>) => {
    captured.page = props;
    return <div data-testid="pull-request-page" />;
  },
}));
vi.mock("@/shell/ChangesReview", () => ({
  ChangesReview: (props: ComponentProps<typeof ChangesReview>) => {
    captured.review = props;
    return <div data-testid="changes-review" />;
  },
}));
vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => ({
  ...(await importOriginal<typeof WorkspaceChangedFilesModule>()),
  useWorkspaceEnvironment: () => ({ data: { root: "/workspace" } }),
}));

const PR = "https://github.com/example-org/widgets/pull/12";

function renderView(
  place: WorkspacePlaceViewProps["place"],
  {
    filesTab = true,
    active = true,
    onCommentOnFile,
    position,
    registerLeaveGuard,
    permissionLevel,
    keyboardTarget,
    files = {},
  }: Partial<
    Pick<
      WorkspacePlaceViewProps,
      | "filesTab"
      | "active"
      | "onCommentOnFile"
      | "position"
      | "registerLeaveGuard"
      | "keyboardTarget"
    > & {
      permissionLevel: number | null;
      files: Partial<WorkspacePlaceViewProps["files"]>;
    }
  > = {},
) {
  type FileSettings = WorkspacePlaceViewProps["files"];
  const fileSettings = {
    onSortChange: vi.fn<FileSettings["onSortChange"]>(),
    onShowHiddenChange: vi.fn<FileSettings["onShowHiddenChange"]>(),
  };
  const onNavigate = vi.fn();
  const onOpen = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // The settings follow each change, as the sidebar's preferences do.
  function View() {
    const [sort, setSort] = useState<FileSettings["sort"]>(files.sort ?? "recent");
    const [showHidden, setShowHidden] = useState(files.showHidden ?? false);
    return (
      <WorkspacePlaceView
        conversationId="conv_1"
        place={place}
        filesTab={filesTab}
        active={active}
        onNavigate={onNavigate}
        onOpen={onOpen}
        onCommentOnFile={onCommentOnFile}
        files={{
          permissionLevel,
          ...files,
          sort,
          showHidden,
          onSortChange: (next) => {
            fileSettings.onSortChange(next);
            setSort(next);
          },
          onShowHiddenChange: (next) => {
            fileSettings.onShowHiddenChange(next);
            setShowHidden(next);
          },
        }}
        position={position}
        registerLeaveGuard={registerLeaveGuard}
        keyboardTarget={keyboardTarget}
      />
    );
  }
  render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <View />
      </TooltipProvider>
    </QueryClientProvider>,
  );
  return { onNavigate, onOpen, fileSettings };
}

const toggleCommands = () =>
  registeredPaletteCommands().filter((command) => command.id === "hotkey:files.toggleTree");

const pressToggleChord = (target: Element | Window = window) =>
  fireEvent.keyDown(target, {
    key: "E",
    code: "KeyE",
    metaKey: isMacPlatform(),
    ctrlKey: !isMacPlatform(),
    shiftKey: true,
  });

beforeEach(() => {
  pageWidth.value = null;
  window.localStorage.clear();
  for (const key of Object.keys(captured) as (keyof typeof captured)[]) captured[key] = null;
  usePaletteCommandsStore.setState({ groups: {} });
});
afterEach(cleanup);

describe("Files tab", () => {
  it("shows the tree beside an Open a file state until a file is chosen", () => {
    renderView({ kind: "files" });
    expect(screen.getByTestId("file-tree")).toBeInTheDocument();
    expect(screen.getByText("Open a file")).toBeInTheDocument();
    expect(captured.viewer).toBeNull();
    expect(captured.tree?.activePath).toBeUndefined();
  });

  it("opens tree clicks in this tab and ⌘-clicks in a new one", () => {
    const { onOpen } = renderView({ kind: "files" });
    captured.tree!.onFileSelect("src/a.ts");
    captured.tree!.onFileSelect("src/b.ts", { newTab: true });
    expect(onOpen.mock.calls).toEqual([
      [{ place: { kind: "files", path: "src/a.ts" } }, { opener: "user", origin: "files-tree" }],
      [{ place: { kind: "files", path: "src/b.ts" } }, { opener: "user", disposition: "new" }],
    ]);
  });

  it("gives a file tab the same tree, whose picks step that tab", () => {
    const { onNavigate, onOpen } = renderView({ kind: "files", path: "b.ts" }, { filesTab: false });
    expect(captured.tree?.activePath).toBe("b.ts");
    captured.tree!.onFileSelect("src/a.ts");
    expect(onNavigate).toHaveBeenCalledWith(
      { kind: "files", path: "src/a.ts" },
      { replace: false },
    );
    captured.tree!.onFileSelect("src/c.ts", { newTab: true });
    expect(onOpen).toHaveBeenCalledWith(
      { place: { kind: "files", path: "src/c.ts" } },
      { opener: "user", disposition: "new" },
    );
    // A breadcrumb pick steps the same tab, through the navigation its unsaved-edit guard holds.
    onNavigate.mockClear();
    captured.viewer!.onOpenFile!("src/d.ts");
    expect(onNavigate.mock.calls).toEqual([
      [{ kind: "files", path: "src/d.ts" }, { replace: false }],
    ]);
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it("shows the file at its cited lines and opens its changes there for Show changes", () => {
    const { onOpen } = renderView({ kind: "files", path: "a.ts", line: { start: 3, end: 5 } });
    expect(captured.viewer).toMatchObject({
      path: "a.ts",
      position: { line: 3, endLine: 5 },
      active: true,
    });
    captured.viewer!.onShowChanges!();
    expect(onOpen).toHaveBeenCalledWith(
      { place: { kind: "changes", path: "a.ts", line: { start: 3, end: 5 } } },
      { opener: "user" },
    );
  });

  it("doesn't replay a citation the reader moved on from when they ask for Show changes", () => {
    const { onOpen } = renderView({ kind: "files", path: "a.ts", line: { start: 3, end: 5 } });
    stopFilePosition(captured.viewer!.position!);
    captured.viewer!.onShowChanges!();
    expect(onOpen).toHaveBeenCalledWith(
      { place: { kind: "changes", path: "a.ts" } },
      { opener: "user" },
    );
  });

  it("prefers the tab's own citation and passes its leave guard and linked comment to the viewer", () => {
    const citation = { line: 9 };
    const registerLeaveGuard = vi.fn(() => () => {});
    renderView(
      { kind: "files", path: "a.ts", line: { start: 3 }, comment: "c1" },
      { position: citation, registerLeaveGuard },
    );
    expect(captured.viewer!.position).toBe(citation);
    expect(captured.viewer!.registerLeaveGuard).toBe(registerLeaveGuard);
    expect(captured.viewer!.commentId).toBe("c1");
  });

  it("gives the file's viewer the session's permission level, so a viewer can't edit", () => {
    renderView({ kind: "files", path: "a.ts" }, { permissionLevel: 1 });
    expect(captured.viewer!.permissionLevel).toBe(1);
  });

  it("closes the Files tab's file back to its tree, and a file tab's to the Files tab", () => {
    const files = renderView({ kind: "files", path: "a.ts" });
    captured.viewer!.onClose();
    expect(files.onNavigate).toHaveBeenCalledWith({ kind: "files" }, { replace: false });
    cleanup();
    const fileTab = renderView({ kind: "files", path: "b.ts" }, { filesTab: false });
    captured.viewer!.onClose();
    expect(fileTab.onOpen).toHaveBeenCalledWith({ place: { kind: "files" } }, { opener: "user" });
  });

  it("hides and shows the tree from the viewer's toolbar, and remembers it on this device", async () => {
    renderView({ kind: "files", path: "a.ts" });
    const toggle = within(screen.getByTestId("file-viewer")).getByRole("button", {
      name: "Hide Files Tree",
    });
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(toggle);
    expect(screen.queryByTestId("file-tree")).toBeNull();
    cleanup();
    renderView({ kind: "files", path: "a.ts" });
    expect(screen.queryByTestId("file-tree")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Show Files Tree" }));
    expect(screen.getByTestId("file-tree")).toBeInTheDocument();
  });

  it("keeps the toggle when no file is open", async () => {
    renderView({ kind: "files" });
    await userEvent.click(screen.getByRole("button", { name: "Hide Files Tree" }));
    expect(screen.queryByTestId("file-tree")).toBeNull();
    expect(screen.getByText("Open a file")).toBeInTheDocument();
  });

  it("keeps Reveal Active File in ⌘K while the tree is hidden, and shows the tree for it", async () => {
    renderView({ kind: "files", path: "a.ts" });
    await userEvent.click(screen.getByRole("button", { name: "Hide Files Tree" }));
    const reveal = registeredPaletteCommands().find(
      (command) => command.label === REVEAL_ACTIVE_FILE_PALETTE_LABEL,
    );
    expect(reveal?.availability).toEqual({ available: true });
    act(() => reveal!.run());
    expect(screen.getByTestId("file-tree")).toBeInTheDocument();
    const taken = vi.fn();
    const Tree = () => {
      useActiveFileRevealRequests("conv_1", taken);
      return null;
    };
    render(<Tree />);
    expect(taken).toHaveBeenCalledTimes(1);
    forgetActiveFileRevealsForTests();
  });

  it("shows a hidden tree when a breadcrumb roots it at a folder", async () => {
    renderView({ kind: "files", path: "src/a.ts" });
    await userEvent.click(screen.getByRole("button", { name: "Hide Files Tree" }));
    act(() => showTreeRoot("conv_1", "/workspace/src"));
    expect(screen.getByTestId("file-tree")).toBeInTheDocument();
    rememberBrowseLocation("conv_1", null);
  });

  it("hides and shows the tree from ⌘K and ⇧⌘E, with the toolbar's wording", () => {
    renderView({ kind: "files", path: "a.ts" });
    expect(toggleCommands()).toHaveLength(1);
    expect(toggleCommands()[0]).toMatchObject({ label: "Hide Files Tree", movesFocus: true });
    act(() => toggleCommands()[0]!.run());
    expect(screen.queryByTestId("file-tree")).toBeNull();
    expect(toggleCommands()[0]).toMatchObject({ label: "Show Files Tree" });

    // The chord works from an editor holding the keyboard too.
    const editor = document.createElement("textarea");
    document.body.append(editor);
    editor.focus();
    pressToggleChord(editor);
    editor.remove();
    expect(screen.getByTestId("file-tree")).toBeInTheDocument();
  });

  it("offers the tree toggle only from the Files place on screen", () => {
    renderView({ kind: "files", path: "a.ts" }, { active: false });
    renderView({ kind: "files", path: "b.ts" });
    expect(toggleCommands().filter((command) => command.availability?.available)).toHaveLength(1);
    pressToggleChord();
    expect(screen.getAllByRole("button", { name: "Show Files Tree" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Hide Files Tree" })).toHaveLength(1);
  });

  it("keeps the tree's options in the tree's own header, not the toolbar", () => {
    const { fileSettings } = renderView({ kind: "files", path: "a.ts" });
    expect(
      within(screen.getByTestId("file-viewer")).queryByRole("button", {
        name: /^Tree options/,
      }),
    ).toBeNull();
    act(() => captured.tree!.onSortChange("size"));
    expect(fileSettings.onSortChange).toHaveBeenCalledWith("size");
    expect(captured.tree!.sort).toBe("size");
  });

  it("shows hidden files when an all-hidden workspace's tree asks", () => {
    const { fileSettings } = renderView({ kind: "files", path: "a.ts" });
    act(() => captured.tree!.onShowHiddenChange(true));
    expect(fileSettings.onShowHiddenChange).toHaveBeenCalledTimes(1);
    expect(fileSettings.onShowHiddenChange).toHaveBeenCalledWith(true);
    expect(captured.tree!.showHidden).toBe(true);
  });

  it("leaves the options to ⌘K while the tree is hidden", async () => {
    renderView({ kind: "files", path: "a.ts" });
    await userEvent.click(screen.getByRole("button", { name: "Hide Files Tree" }));
    expect(registeredPaletteCommands().some((row) => row.id === "files-tree-hidden")).toBe(true);
  });

  it("leaves the options to ⌘K on a narrow page, whose tree opens as a popover", async () => {
    pageWidth.value = 400;
    renderView({ kind: "files", path: "a.ts" });
    await userEvent.click(screen.getByRole("button", { name: "Show Files Tree" }));
    expect(screen.getByRole("dialog", { name: "Workspace files" })).toBeInTheDocument();
    expect(registeredPaletteCommands().some((row) => row.id === "files-tree-hidden")).toBe(true);
  });

  it("sorts the tree and shows hidden files from ⌘K, for the keyboard target only", () => {
    renderView({ kind: "files", path: "a.ts" }, { keyboardTarget: false });
    const { fileSettings } = renderView(
      { kind: "files", path: "b.ts" },
      { files: { showHidden: true } },
    );
    const listed = registeredPaletteCommands().filter(
      (row) => row.id.startsWith("files-tree-") && row.availability?.available !== false,
    );
    // The current order isn't offered again.
    expect(listed.map((row) => row.label)).toEqual([
      "Sort Files Tree by Filename",
      "Sort Files Tree by Size",
      "Sort Files Tree by Type",
      "Hide Hidden Files",
      "Show Git-Ignored Files",
    ]);
    act(() => listed.find((row) => row.label === "Sort Files Tree by Type")!.run());
    expect(fileSettings.onSortChange).toHaveBeenCalledWith("type");
    act(() => listed.find((row) => row.label === "Hide Hidden Files")!.run());
    expect(fileSettings.onShowHiddenChange).toHaveBeenCalledWith(false);
    // The rows follow: the new order isn't offered, and hidden files can be shown again.
    expect(
      registeredPaletteCommands()
        .filter((row) => row.id.startsWith("files-tree-") && row.availability?.available !== false)
        .map((row) => row.label),
    ).toEqual([
      "Sort Files Tree by Filename",
      "Sort Files Tree by Last edited",
      "Sort Files Tree by Size",
      "Show Hidden Files",
      "Show Git-Ignored Files",
    ]);
  });

  it("shows git-ignored files from the tree and ⌘K, off by default and kept per device", () => {
    renderView({ kind: "files", path: "a.ts" });
    expect(captured.tree!.showIgnored).toBe(false);
    act(() => captured.tree!.onShowIgnoredChange(true));
    expect(captured.tree!.showIgnored).toBe(true);
    expect(window.localStorage.getItem("omnigent:files-tree-show-ignored")).toBe("true");

    const hide = registeredPaletteCommands().find((row) => row.id === "files-tree-ignored")!;
    expect(hide.label).toBe("Hide Git-Ignored Files");
    act(() => hide.run());
    expect(captured.tree!.showIgnored).toBe(false);
    expect(window.localStorage.getItem("omnigent:files-tree-show-ignored")).not.toBe("true");
  });

  it("gives the tree's ⌘K commands to the keyboard target only, while both trees stay on screen", () => {
    renderView({ kind: "files", path: "a.ts" }, { keyboardTarget: false });
    expect(captured.tree).toMatchObject({ active: true, commandsActive: false });
    renderView({ kind: "files", path: "b.ts" }, { keyboardTarget: true });
    expect(captured.tree).toMatchObject({ active: true, commandsActive: true });
  });

  it("leaves ⌘K and ⇧⌘E to the keyboard target when two Files places are on screen", () => {
    renderView({ kind: "files", path: "a.ts" }, { keyboardTarget: false });
    renderView({ kind: "files", path: "b.ts" }, { keyboardTarget: true });
    expect(toggleCommands().filter((command) => command.availability?.available)).toHaveLength(1);
    pressToggleChord();
    expect(screen.getAllByRole("button", { name: "Show Files Tree" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Hide Files Tree" })).toHaveLength(1);
  });
});

describe("pull request tab", () => {
  it("shows the session's pull requests in upstream's panel", () => {
    renderView({ kind: "pulls" }, { active: false });
    expect(screen.getByTestId("pull-request-list")).toBeInTheDocument();
    expect(captured.list).toMatchObject({ conversationId: "conv_1", active: false });
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("steps the tab to a pull request chosen in the list, so Back returns to the list", () => {
    const { onNavigate } = renderView({ kind: "pulls" });
    captured.list!.onOpenPullRequest!(PR);
    expect(onNavigate).toHaveBeenCalledWith(
      { kind: "pull", url: PR, view: "summary" },
      { replace: false },
    );
    captured.list!.onOpenPullRequest!(PR, "changes");
    expect(onNavigate).toHaveBeenLastCalledWith(
      { kind: "pull", url: PR, view: "changes" },
      { replace: false },
    );
  });

  it("shows one pull request's section, and records a picked section as a step", () => {
    const { onNavigate } = renderView({ kind: "pull", url: PR, view: "comments" });
    expect(captured.page).toMatchObject({
      conversationId: "conv_1",
      prUrl: PR,
      section: "comments",
      active: true,
    });
    captured.page!.onSectionChange("changes");
    expect(onNavigate).toHaveBeenCalledWith(
      { kind: "pull", url: PR, view: "changes" },
      { replace: false },
    );
    onNavigate.mockClear();
    captured.page!.onSectionChange("comments");
    expect(onNavigate).not.toHaveBeenCalled();
  });

  it("opens or selects the session's pull requests tab from a pull request's page", () => {
    const { onNavigate, onOpen } = renderView({ kind: "pull", url: PR, view: "summary" });
    captured.page!.onShowAllPullRequests();
    expect(onOpen).toHaveBeenCalledWith(
      { place: { kind: "pulls" } },
      { opener: "user", disposition: "focus" },
    );
    expect(onNavigate).not.toHaveBeenCalled();
  });

  it("says what a link points at that the tab can't show, and can settle on what's shown", async () => {
    const { onNavigate } = renderView({
      kind: "pull",
      url: PR,
      view: "changes",
      path: "src/a.ts",
      line: { start: 4 },
    });
    expect(screen.getByRole("status")).toHaveTextContent(
      "This link points at src/a.ts line 4, which this tab can't open yet.",
    );
    await userEvent.click(screen.getByRole("button", { name: "Show what's here" }));
    expect(onNavigate).toHaveBeenCalledWith(
      { kind: "pull", url: PR, view: "changes" },
      { replace: true },
    );
  });
});

describe("Changes tab", () => {
  it("shows the review at the place's file, and views a file in a Files tab", async () => {
    const { onOpen } = renderView({ kind: "changes", path: "src/app.ts" });
    expect(await screen.findByTestId("changes-review")).toBeInTheDocument();
    expect(captured.review).toMatchObject({
      conversationId: "conv_1",
      revealPath: "src/app.ts",
      active: true,
      sort: "recent",
    });
    captured.review!.onViewFile("src/app.ts");
    expect(onOpen).toHaveBeenCalledWith(
      { place: { kind: "files", path: "src/app.ts" } },
      { opener: "user" },
    );
  });

  it("leaves a linked range for the session's own comparison, keeping the file", async () => {
    const { onNavigate } = renderView({
      kind: "changes",
      path: "src/app.ts",
      range: { base: "v1.2" },
    });
    await screen.findByTestId("changes-review");
    expect(captured.review?.requestedRange).toEqual({ base: "v1.2", head: "Working Copy" });
    captured.review!.onShowDefaultComparison!();
    expect(onNavigate).toHaveBeenCalledWith(
      { kind: "changes", path: "src/app.ts" },
      { replace: false },
    );
  });

  it("hands the review the visit's comparison, a linked range's too, and records what it shows", async () => {
    const { onNavigate } = renderView({ kind: "changes", range: { base: "v1.2" } });
    await screen.findByTestId("changes-review");
    expect(captured.review?.comparison).toEqual({ kind: "typed", base: "v1.2", head: "" });
    cleanup();

    const saved = { kind: "saved", intent: "branch-point" } as const;
    const view = renderView({ kind: "changes", path: "a.go", comparison: saved });
    await screen.findByTestId("changes-review");
    expect(captured.review?.comparison).toEqual(saved);
    const typed = { kind: "typed", base: "main", head: "" } as const;
    captured.review!.onComparisonShown!(typed, { step: true, root: null });
    expect(view.onNavigate).toHaveBeenCalledWith(
      { kind: "changes", path: "a.go", comparison: typed },
      { replace: false },
    );
    // A worktree's review records its folder with the comparison.
    captured.review!.onComparisonShown!(typed, { step: true, root: "/repo/.worktrees/wt" });
    expect(view.onNavigate).toHaveBeenLastCalledWith(
      { kind: "changes", path: "a.go", comparison: typed, comparisonRoot: "/repo/.worktrees/wt" },
      { replace: false },
    );
    expect(onNavigate).not.toHaveBeenCalled();
    cleanup();

    // A visit recorded in a worktree hands the review that folder, and a workspace visit none.
    renderView({ kind: "changes", comparison: typed, comparisonRoot: "/repo/.worktrees/wt" });
    await screen.findByTestId("changes-review");
    expect(captured.review?.comparisonRoot).toBe("/repo/.worktrees/wt");
    cleanup();
    renderView({ kind: "changes", comparison: typed });
    await screen.findByTestId("changes-review");
    expect(captured.review?.comparisonRoot).toBeNull();
  });

  it("opens a file from the review's tree in a Diffs tab of its own, keeping the range", async () => {
    const { onOpen } = renderView({ kind: "changes", range: { base: "v1.2" } });
    await screen.findByTestId("changes-review");
    captured.review!.onOpenFileInNewTab!("src/app.ts", { background: true });
    expect(onOpen).toHaveBeenCalledWith(
      { place: { kind: "changes", path: "src/app.ts", fileTab: true, range: { base: "v1.2" } } },
      { opener: "user", disposition: "focus", reuse: "same-place", keepSelection: true },
    );
  });

  it("hands a comment's selection on to whoever opens the file for it", async () => {
    const onCommentOnFile = vi.fn();
    renderView({ kind: "changes" }, { onCommentOnFile });
    await screen.findByTestId("changes-review");
    expect(captured.review?.onCommentOnFile).toBe(onCommentOnFile);
  });
});
