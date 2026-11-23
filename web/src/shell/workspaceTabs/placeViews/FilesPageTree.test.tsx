import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  RunnerOfflineError,
  type WorkspaceFile,
  useWorkspaceAllFiles,
  useWorkspaceChangedFiles,
  useWorkspaceEnvironment,
  useWorkspaceFileSearch,
} from "@/hooks/useWorkspaceChangedFiles";
import type * as WorkspaceChangedFilesModule from "@/hooks/useWorkspaceChangedFiles";
import { TooltipProvider } from "@/components/ui/tooltip";
import {
  rememberBrowseLocation,
  rememberedBrowseLocation,
} from "@/shell/filesPane/browseLocations";
import type { FilesTree } from "@/shell/fileTree/FilesTree";
import {
  forgetActiveFileRevealsForTests,
  requestActiveFileReveal,
} from "@/shell/fileTree/revealActiveFile";
import {
  forgetTreeNameFilterRequestsForTests,
  requestTreeNameFilter,
} from "@/shell/fileTree/treeNameFilter";
import {
  TREE_FILTER_DEBOUNCE_MS,
  forgetTreeFiltersForTests,
} from "@/shell/filesPane/useTreeFilters";
import { usePaletteCommandsStore } from "@/store/paletteCommands";

import { FilesPageTree } from "./FilesPageTree";

const captured = vi.hoisted(() => ({ tree: null as ComponentProps<typeof FilesTree> | null }));

vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => ({
  ...(await importOriginal<typeof WorkspaceChangedFilesModule>()),
  useWorkspaceAllFiles: vi.fn(),
  useWorkspaceChangedFiles: vi.fn(),
  useWorkspaceEnvironment: vi.fn(),
  useWorkspaceFileSearch: vi.fn(),
}));
vi.mock("@/shell/fileTree/FilesTreeMatches", () => ({
  FilesTreeMatches: (props: {
    matches: { path: string }[];
    onFileSelect: (path: string, options?: { newTab: true }) => void;
  }) => (
    <div data-testid="files-tree-matches">
      {props.matches.map((match) => (
        <button
          key={match.path}
          type="button"
          onClick={(event) =>
            event.metaKey
              ? props.onFileSelect(match.path, { newTab: true })
              : props.onFileSelect(match.path)
          }
        >
          {match.path}
        </button>
      ))}
    </div>
  ),
}));
vi.mock("@/hooks/useSession", () => ({
  useSession: () => ({ session: { permissionLevel: null } }),
}));
vi.mock("@/shell/fileTree/FilesTree", () => ({
  FilesTree: (props: ComponentProps<typeof FilesTree>) => {
    captured.tree = props;
    return <div data-testid="files-tree" />;
  },
}));

function entry(path: string): WorkspaceFile {
  return { path, name: path.split("/").pop()!, type: "file", bytes: 1, modified_at: null };
}

type FilesResult = ReturnType<typeof useWorkspaceAllFiles>;
const listed = (entries: WorkspaceFile[], ignoredHidden = false) =>
  ({
    isPending: false,
    isError: false,
    data: { available: true, data: entries, ...(ignoredHidden ? { ignoredHidden: true } : {}) },
  }) as FilesResult;

function renderTree(
  files: Partial<FilesResult>,
  showHidden = false,
  { active = true, showIgnored = true }: { active?: boolean; showIgnored?: boolean } = {},
) {
  vi.mocked(useWorkspaceAllFiles).mockReturnValue(files as FilesResult);
  const onFileSelect = vi.fn();
  const onShowHidden = vi.fn();
  const onShowIgnored = vi.fn();
  render(
    <TooltipProvider>
      <FilesPageTree
        conversationId="conv_1"
        active={active}
        activePath="src/a.ts"
        sort="alpha"
        showHidden={showHidden}
        onShowHidden={onShowHidden}
        showIgnored={showIgnored}
        onShowIgnored={onShowIgnored}
        onFileSelect={onFileSelect}
      />
    </TooltipProvider>,
  );
  return { onFileSelect, onShowHidden, onShowIgnored };
}

/** The folder each render asked the workspace to list. */
const listedLocations = () => vi.mocked(useWorkspaceAllFiles).mock.calls.map((call) => call[2]);

beforeEach(() => {
  captured.tree = null;
  vi.mocked(useWorkspaceAllFiles).mockClear();
  vi.mocked(useWorkspaceFileSearch).mockReturnValue({ data: undefined } as never);
  vi.mocked(useWorkspaceEnvironment).mockReturnValue({
    data: { available: true, root: "/work/repo" },
    isError: false,
  } as unknown as ReturnType<typeof useWorkspaceEnvironment>);
  vi.mocked(useWorkspaceChangedFiles).mockReturnValue({
    data: { available: true, data: [{ path: "src/a.ts" }] },
  } as unknown as ReturnType<typeof useWorkspaceChangedFiles>);
});
afterEach(() => {
  cleanup();
  rememberBrowseLocation("conv_1", null);
  forgetActiveFileRevealsForTests();
  forgetTreeNameFilterRequestsForTests();
  forgetTreeFiltersForTests();
  vi.useRealTimers();
});

describe("FilesPageTree", () => {
  it("says it's loading until the listing arrives", () => {
    renderTree({ isPending: true });
    expect(screen.getByRole("status")).toHaveTextContent("Loading files…");
  });

  it("asks for a message to reconnect an offline machine, and alerts on any other failure", () => {
    renderTree({ isPending: false, isError: true, error: new RunnerOfflineError() } as never);
    expect(screen.getByRole("status")).toHaveTextContent(
      "The session’s machine is offline. Send a message to reconnect it.",
    );
    cleanup();
    renderTree({ isPending: false, isError: true, error: new Error("permission denied") } as never);
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Couldn’t list the files: permission denied",
    );
  });

  it("explains a session without files, an empty workspace and one that's all hidden", () => {
    renderTree({ isPending: false, isError: false, data: { available: false } } as never);
    expect(screen.getByRole("status")).toHaveTextContent("This session has no workspace files.");
    cleanup();
    renderTree(listed([]));
    expect(screen.getByRole("status")).toHaveTextContent("No files in the workspace.");
    cleanup();
    const { onShowHidden } = renderTree(listed([entry(".env")]));
    expect(screen.getByRole("status")).toHaveTextContent("Every file here is hidden.");
    fireEvent.click(screen.getByRole("button", { name: "Show hidden files" }));
    expect(onShowHidden).toHaveBeenCalledTimes(1);
    cleanup();
    renderTree(listed([entry(".env")]), true);
    expect(screen.getByTestId("files-tree")).toBeInTheDocument();
  });

  it("lists without what Git ignores while ignored files are off, and offers them when nothing's left", () => {
    renderTree(listed([entry("README.md")]), false, { showIgnored: false });
    expect(useWorkspaceAllFiles).toHaveBeenLastCalledWith("conv_1", { hideIgnored: true }, "");
    expect(captured.tree?.showIgnored).toBe(false);
    cleanup();

    const { onShowIgnored } = renderTree(listed([], true), false, { showIgnored: false });
    expect(screen.getByRole("status")).toHaveTextContent(
      "No files here, apart from any that Git ignores.",
    );
    expect(screen.queryByRole("button", { name: "Show hidden files" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show git-ignored files" }));
    expect(onShowIgnored).toHaveBeenCalledTimes(1);
    cleanup();

    // A host that couldn't leave anything out listed everything there is.
    renderTree(listed([]), false, { showIgnored: false });
    expect(screen.getByRole("status")).toHaveTextContent("No files in the workspace.");
    expect(screen.queryByRole("button", { name: /^Show / })).toBeNull();
    cleanup();

    // Only dotfiles are left once Git's are out: either action brings files back.
    const both = renderTree(listed([entry(".git"), entry(".gitignore")], true), false, {
      showIgnored: false,
    });
    expect(screen.getByRole("status")).toHaveTextContent(
      "Every file here is hidden or ignored by Git.",
    );
    fireEvent.click(screen.getByRole("button", { name: "Show hidden files" }));
    fireEvent.click(screen.getByRole("button", { name: "Show git-ignored files" }));
    expect(both.onShowHidden).toHaveBeenCalledTimes(1);
    expect(both.onShowIgnored).toHaveBeenCalledTimes(1);
    cleanup();

    renderTree(listed([entry("README.md")]), false, { showIgnored: true });
    expect(useWorkspaceAllFiles).toHaveBeenLastCalledWith("conv_1", { hideIgnored: false }, "");
    expect(captured.tree?.showIgnored).toBe(true);
  });

  it("shows the listing with its changes, open down to the page's file", () => {
    const { onFileSelect } = renderTree(listed([entry("README.md"), entry("src")]));
    expect(captured.tree).toMatchObject({
      conversationId: "conv_1",
      rootEntries: [entry("README.md"), entry("src")],
      changedFiles: [{ path: "src/a.ts" }],
      activeFile: "src/a.ts",
      sort: "alpha",
      showHidden: false,
      label: "Workspace files",
    });
    captured.tree?.onFileSelect("README.md", { newTab: true });
    expect(onFileSelect).toHaveBeenCalledWith("README.md", { newTab: true });
    expect(listedLocations().at(-1)).toBe("");
  });

  it("shows no changes while the session can't report them", () => {
    vi.mocked(useWorkspaceChangedFiles).mockReturnValue({
      data: { available: false },
    } as unknown as ReturnType<typeof useWorkspaceChangedFiles>);
    renderTree(listed([entry("README.md")]));
    expect(captured.tree?.changedFiles).toEqual([]);
  });

  it("names the workspace folder and offers no way back while it browses the workspace", () => {
    renderTree(listed([entry("README.md")]));
    expect(screen.getByTestId("files-page-tree-header")).toHaveTextContent("repo");
    expect(screen.queryByRole("button", { name: "Back to working folder" })).toBeNull();
  });

  it("browses the remembered folder, with its changes and files relative to it", () => {
    rememberBrowseLocation("conv_1", "/work/repo/src");
    const { onFileSelect } = renderTree(listed([entry("src/a.ts")]));
    expect(listedLocations().at(-1)).toBe("src");
    expect(screen.getByTestId("files-page-tree-header")).toHaveTextContent("src");
    expect(captured.tree).toMatchObject({
      browseLocation: "src",
      changedFiles: [{ path: "a.ts" }],
      activeFile: "a.ts",
    });
    captured.tree?.onFileSelect("b.ts");
    expect(onFileSelect).toHaveBeenCalledWith("src/b.ts", undefined);
    fireEvent.click(screen.getByRole("button", { name: "Back to working folder" }));
    expect(rememberedBrowseLocation("conv_1")).toBeNull();
  });

  it("says a rooted folder is empty rather than the workspace", () => {
    rememberBrowseLocation("conv_1", "/work/repo/empty");
    renderTree(listed([]));
    expect(screen.getByRole("status")).toHaveTextContent("This folder is empty.");
  });

  it("roots the tree at a folder opened from it", () => {
    rememberBrowseLocation("conv_1", "/work/repo/src");
    renderTree(listed([entry("src/lib")]));
    act(() => captured.tree?.onNavigateDir?.("lib"));
    expect(rememberedBrowseLocation("conv_1")).toBe("/work/repo/src/lib");
  });

  it("reveals the viewed file from its header button, and lets the request go once taken", () => {
    renderTree(listed([entry("src")]));
    fireEvent.click(screen.getByRole("button", { name: "Reveal Active File" }));
    expect(captured.tree?.activeReveal).toMatchObject({ file: "src/a.ts" });
    act(() => captured.tree?.onActiveRevealHandled?.(captured.tree.activeReveal!.key));
    expect(captured.tree?.activeReveal).toBeNull();
  });

  it("roots back at the workspace to reveal a file the browsed folder doesn't hold", () => {
    rememberBrowseLocation("conv_1", "/work/repo/docs");
    renderTree(listed([entry("src")]));
    fireEvent.click(screen.getByRole("button", { name: "Reveal Active File" }));
    expect(rememberedBrowseLocation("conv_1")).toBeNull();
    expect(captured.tree).toMatchObject({
      browseLocation: "",
      activeReveal: { file: "src/a.ts" },
    });
  });

  it("takes up a reveal ⌘K asked for before it came on screen, only while on screen", () => {
    requestActiveFileReveal("conv_1", "src/a.ts");
    renderTree(listed([entry("src")]), false, { active: false });
    expect(captured.tree?.activeReveal).toBeNull();
    cleanup();
    renderTree(listed([entry("src")]));
    expect(captured.tree?.activeReveal).toMatchObject({ file: "src/a.ts" });
  });

  it("says why it can't reveal a hidden file", () => {
    vi.mocked(useWorkspaceAllFiles).mockReturnValue(listed([entry("src")]));
    render(
      <TooltipProvider>
        <FilesPageTree
          conversationId="conv_1"
          active
          activePath=".github/ci.yml"
          sort="alpha"
          showHidden={false}
          showIgnored
          onFileSelect={vi.fn()}
        />
      </TooltipProvider>,
    );
    const button = screen.getByRole("button", { name: "Reveal Active File" });
    expect(button).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(button);
    expect(captured.tree?.activeReveal).toBeNull();
  });

  describe("name filter", () => {
    const filterButton = () => screen.getByRole("button", { name: "Filter Files Tree by Name" });
    const nameField = () => screen.getByRole("textbox", { name: "Name contains" });
    const typeName = (name: string) => {
      fireEvent.change(nameField(), { target: { value: name } });
      act(() => vi.advanceTimersByTime(TREE_FILTER_DEBOUNCE_MS));
    };
    const searches = () => vi.mocked(useWorkspaceFileSearch).mock.calls;
    const paletteLabels = () =>
      Object.values(usePaletteCommandsStore.getState().groups)
        .flatMap((group) => group.commands)
        .map((command) => command.label);

    it("searches the browsed folder for a name once typing settles, and shows the matches", () => {
      vi.useFakeTimers();
      rememberBrowseLocation("conv_1", "/work/repo/src");
      vi.mocked(useWorkspaceFileSearch).mockReturnValue({
        data: {
          files: [entry("lib/button.ts"), entry(".hidden/button.ts")],
          truncated: false,
          hasMore: false,
        },
      } as never);
      const { onFileSelect } = renderTree(listed([entry("src/a.ts")]));
      fireEvent.click(filterButton());
      expect(filterButton()).toHaveAttribute("aria-pressed", "true");
      expect(nameField()).toHaveFocus();
      typeName("button");
      expect(searches().at(-1)).toEqual(["conv_1", "button", "", "", { enabled: true }, "src"]);
      expect(screen.getByTestId("tree-filter-strip")).toHaveTextContent(
        "Names containing “button”",
      );
      expect(screen.queryByTestId("files-tree")).toBeNull();
      // Hidden matches stay hidden while hidden files are.
      expect(screen.queryByRole("button", { name: ".hidden/button.ts" })).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: "lib/button.ts" }));
      expect(onFileSelect).toHaveBeenCalledWith("src/lib/button.ts");
      // A match the reader wants in its own tab opens there.
      fireEvent.click(screen.getByRole("button", { name: "lib/button.ts" }), { metaKey: true });
      expect(onFileSelect).toHaveBeenLastCalledWith("src/lib/button.ts", { newTab: true });
      expect(paletteLabels()).toContain("Clear Files Tree Filter");
    });

    it("ends the filter for the header's Reveal, so the whole tree shows the viewed file", () => {
      vi.useFakeTimers();
      vi.mocked(useWorkspaceFileSearch).mockReturnValue({
        data: { files: [entry("src/a.ts")], truncated: false, hasMore: false },
      } as never);
      renderTree(listed([entry("src")]));
      fireEvent.click(filterButton());
      typeName("a.ts");
      expect(screen.getByTestId("files-tree-matches")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Reveal Active File" }));
      expect(screen.queryByTestId("files-tree-matches")).toBeNull();
      expect(nameField()).toHaveValue("");
      expect(captured.tree?.activeReveal).toMatchObject({ file: "src/a.ts" });
    });

    it("ends a name still waiting to apply for a ⌘K reveal", () => {
      vi.useFakeTimers();
      vi.mocked(useWorkspaceFileSearch).mockReturnValue({
        data: { files: [entry("src/a.ts")], truncated: false, hasMore: false },
      } as never);
      renderTree(listed([entry("src")]));
      fireEvent.click(filterButton());
      fireEvent.change(nameField(), { target: { value: "a.ts" } });
      act(() => {
        requestActiveFileReveal("conv_1", "src/a.ts");
      });
      act(() => vi.advanceTimersByTime(TREE_FILTER_DEBOUNCE_MS));
      expect(screen.queryByTestId("files-tree-matches")).toBeNull();
      expect(captured.tree?.activeReveal).toMatchObject({ file: "src/a.ts" });
    });

    it("narrows the search with globs, which do nothing until a name is set", () => {
      vi.useFakeTimers();
      renderTree(listed([entry("src")]));
      fireEvent.click(filterButton());
      fireEvent.click(screen.getByRole("button", { name: "Files to include or exclude" }));
      fireEvent.change(screen.getByRole("textbox", { name: "Files to include" }), {
        target: { value: "*.ts" },
      });
      act(() => vi.advanceTimersByTime(TREE_FILTER_DEBOUNCE_MS));
      expect(screen.getByText("Applies once a name is set.")).toBeInTheDocument();
      expect(screen.getByTestId("files-tree")).toBeInTheDocument();
      typeName("app");
      expect(searches().at(-1)?.slice(1, 4)).toEqual(["app", "*.ts", ""]);
      expect(screen.getByTestId("tree-filter-strip")).toHaveTextContent(
        "Names containing “app” in *.ts",
      );
    });

    it("says what it's doing while it searches, finds nothing, or fails", () => {
      vi.useFakeTimers();
      renderTree(listed([entry("src")]));
      fireEvent.click(filterButton());
      typeName("zzz");
      expect(screen.getByText("Searching…")).toBeInTheDocument();
      cleanup();
      vi.mocked(useWorkspaceFileSearch).mockReturnValue({
        data: { files: [], truncated: false, hasMore: false },
      } as never);
      renderTree(listed([entry("src")]));
      expect(screen.getByText("No files match “zzz”.")).toBeInTheDocument();
      cleanup();
      vi.mocked(useWorkspaceFileSearch).mockReturnValue({
        isError: true,
        error: new Error("timed out"),
      } as never);
      renderTree(listed([entry("src")]));
      expect(screen.getByRole("alert")).toHaveTextContent("Couldn’t search the files: timed out");
    });

    it("keeps the filter across a remount, and clears it from the strip", () => {
      vi.useFakeTimers();
      renderTree(listed([entry("src")]));
      fireEvent.click(filterButton());
      typeName("app");
      cleanup();
      renderTree(listed([entry("src")]));
      expect(nameField()).toHaveValue("app");
      expect(screen.getByTestId("tree-filter-dot")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Clear" }));
      expect(screen.getByTestId("files-tree")).toBeInTheDocument();
      expect(screen.queryByTestId("tree-filter-dot")).toBeNull();
      expect(paletteLabels()).not.toContain("Clear Files Tree Filter");
    });

    it("empties the name on Escape, then closes the filter", () => {
      vi.useFakeTimers();
      renderTree(listed([entry("src")]));
      fireEvent.click(filterButton());
      typeName("app");
      fireEvent.keyDown(nameField(), { key: "Escape" });
      expect(nameField()).toHaveValue("");
      fireEvent.keyDown(nameField(), { key: "Escape" });
      expect(screen.queryByRole("textbox", { name: "Name contains" })).toBeNull();
      expect(filterButton()).toHaveAttribute("aria-pressed", "false");
      // The field that had focus is gone, so focus lands on the filter's button.
      expect(filterButton()).toHaveFocus();
      fireEvent.click(filterButton());
      expect(nameField()).toHaveFocus();
      fireEvent.click(screen.getByRole("button", { name: "Close filter" }));
      expect(filterButton()).toHaveFocus();
    });

    it("shows no matches from the folder it browsed before a root change", () => {
      vi.useFakeTimers();
      const found = { files: [entry("button.ts")], truncated: true, hasMore: true };
      vi.mocked(useWorkspaceFileSearch).mockReturnValue({
        data: found,
        isPlaceholderData: false,
      } as never);
      renderTree(listed([entry("button.ts")]));
      fireEvent.click(filterButton());
      typeName("button");
      expect(screen.getByRole("button", { name: "button.ts" })).toBeInTheDocument();
      expect(screen.getByText(/Showing the first matches/)).toBeInTheDocument();

      // Typing in the same folder keeps its matches in view, without the note.
      vi.mocked(useWorkspaceFileSearch).mockReturnValue({
        data: found,
        isPlaceholderData: true,
      } as never);
      typeName("butto");
      expect(screen.getByRole("button", { name: "button.ts" })).toBeInTheDocument();
      expect(screen.queryByText(/Showing the first matches/)).toBeNull();

      // A new root's placeholder still holds the old folder's matches: show none.
      act(() => rememberBrowseLocation("conv_1", "/work/repo/src"));
      expect(screen.queryByRole("button", { name: "button.ts" })).toBeNull();
      expect(screen.getByText("Searching…")).toBeInTheDocument();
    });

    it("opens and focuses the filter for a ⌘K request, only while on screen", () => {
      requestTreeNameFilter("conv_1");
      renderTree(listed([entry("src")]), false, { active: false });
      expect(screen.queryByRole("textbox", { name: "Name contains" })).toBeNull();
      cleanup();
      renderTree(listed([entry("src")]));
      expect(nameField()).toHaveFocus();
    });
  });
});
