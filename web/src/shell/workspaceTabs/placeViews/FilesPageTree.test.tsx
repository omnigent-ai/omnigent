import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  IgnoredFilesSearchError,
  RunnerOfflineError,
  type WorkspaceFile,
  type WorkspaceListing,
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

import { toast } from "sonner";

import { FilesPageTree } from "./FilesPageTree";

vi.mock("sonner", () => ({ toast: { info: vi.fn() } }));

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
    status: "success",
    isPending: false,
    isError: false,
    isPlaceholderData: false,
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
  const tree = (ignored: boolean) => (
    <TooltipProvider>
      <FilesPageTree
        conversationId="conv_1"
        active={active}
        commandsActive={active}
        activePath="src/a.ts"
        sort="alpha"
        showHidden={showHidden}
        onShowHidden={onShowHidden}
        showIgnored={ignored}
        onShowIgnored={onShowIgnored}
        onFileSelect={onFileSelect}
      />
    </TooltipProvider>
  );
  const view = render(tree(showIgnored));
  return {
    onFileSelect,
    onShowHidden,
    onShowIgnored,
    /** Switch ignored files, with `next` as the listing that answers then. */
    setShowIgnored: (ignored: boolean, next: Partial<FilesResult>) => {
      vi.mocked(useWorkspaceAllFiles).mockReturnValue(next as FilesResult);
      view.rerender(tree(ignored));
    },
  };
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

  it("says a listing of only hidden entries stopped short, since files may lie past it", () => {
    const entries: WorkspaceListing = Object.assign([entry(".a"), entry(".b")], {
      cutOff: true as const,
    });
    renderTree(listed(entries));
    expect(screen.getByRole("status")).toHaveTextContent(
      "The first 2 entries here are hidden, and the list stops there.",
    );
    expect(screen.getByRole("button", { name: "Show hidden files" })).toBeInTheDocument();
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
          commandsActive
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

  it("says why a reveal found nothing in a folder whose every entry Git ignores", () => {
    vi.mocked(toast.info).mockClear();
    const { onShowIgnored } = renderTree(listed([], true), false, { showIgnored: false });
    fireEvent.click(screen.getByRole("button", { name: "Reveal Active File" }));
    expect(toast.info).toHaveBeenCalledExactlyOnceWith(
      "a.ts isn’t in the Files tree. Git may ignore it, and ignored files are hidden.",
      { action: { label: "Show git-ignored files", onClick: onShowIgnored } },
    );
  });

  it("says why a reveal found nothing, and offers ignored files while they're off", () => {
    vi.mocked(toast.info).mockClear();
    const { onShowIgnored } = renderTree(listed([entry("README.md")]), false, {
      showIgnored: false,
    });
    act(() => captured.tree?.onActiveRevealMissed?.("artifact.log"));
    expect(toast.info).toHaveBeenCalledExactlyOnceWith(
      "artifact.log isn’t in the Files tree. Git may ignore it, and ignored files are hidden.",
      { action: { label: "Show git-ignored files", onClick: onShowIgnored } },
    );
    cleanup();

    vi.mocked(toast.info).mockClear();
    renderTree(listed([entry("README.md")]));
    act(() => captured.tree?.onActiveRevealMissed?.("gone.ts"));
    expect(toast.info).toHaveBeenCalledExactlyOnceWith(
      "gone.ts isn’t in the Files tree. It may have moved or been deleted.",
      {},
    );
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
      expect(searches().at(-1)).toEqual([
        "conv_1",
        "button",
        "",
        "",
        { enabled: true, hideIgnored: false },
        "src",
      ]);
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
      // ⌘K's Clear Files Tree Filter empties the fields and brings back the whole tree.
      const clear = Object.values(usePaletteCommandsStore.getState().groups)
        .flatMap((group) => group.commands)
        .find((command) => command.label === "Clear Files Tree Filter");
      act(() => void clear!.run());
      expect(nameField()).toHaveValue("");
      expect(screen.getByTestId("files-tree")).toBeInTheDocument();
      expect(paletteLabels()).not.toContain("Clear Files Tree Filter");
    });

    it("searches without what Git ignores while ignored files are off, as the tree lists", () => {
      vi.useFakeTimers();
      renderTree(listed([entry("README.md")], true), false, { showIgnored: false });
      fireEvent.click(filterButton());
      typeName("artifact");
      expect(searches().at(-1)?.[4]).toEqual({ enabled: true, hideIgnored: true });
    });

    it("searches without what Git ignores while the listing hasn't said whether it can", () => {
      vi.useFakeTimers();
      renderTree(
        { status: "pending", isPending: true, isError: false, data: undefined } as never,
        false,
        {
          showIgnored: false,
        },
      );
      fireEvent.click(filterButton());
      typeName("artifact");
      expect(searches().at(-1)?.[4]).toEqual({ enabled: true, hideIgnored: true });
    });

    it("drops matches kept from a search with ignored files once they're switched off", () => {
      vi.useFakeTimers();
      const withIgnored = { files: [entry("build/out.o")], truncated: false, hasMore: false };
      vi.mocked(useWorkspaceFileSearch).mockReturnValue({ data: withIgnored } as never);
      const { setShowIgnored } = renderTree(listed([entry("README.md")]));
      fireEvent.click(filterButton());
      typeName("out");
      expect(screen.getByRole("button", { name: "build/out.o" })).toBeInTheDocument();

      // The new search hasn't answered: React Query still hands back the old matches.
      vi.mocked(useWorkspaceFileSearch).mockReturnValue({
        data: withIgnored,
        isPlaceholderData: true,
      } as never);
      // The new listing hasn't answered either, so the old unfiltered one is kept meanwhile.
      setShowIgnored(false, {
        ...listed([entry("README.md"), entry("build")]),
        isPlaceholderData: true,
      } as Partial<FilesResult>);
      expect(screen.queryByRole("button", { name: "build/out.o" })).toBeNull();
      expect(searches().at(-1)?.[4]).toEqual({ enabled: true, hideIgnored: true });
    });

    it("searches everything on a host whose listing can't leave ignored files out", () => {
      vi.useFakeTimers();
      renderTree(listed([entry("README.md")]), false, { showIgnored: false });
      fireEvent.click(filterButton());
      typeName("artifact");
      expect(searches().at(-1)?.[4]).toEqual({ enabled: true, hideIgnored: false });
    });

    it("says it can't search without ignored files right now, and offers a retry or them", () => {
      vi.useFakeTimers();
      const refetch = vi.fn();
      vi.mocked(useWorkspaceFileSearch).mockReturnValue({
        data: undefined,
        isError: true,
        isFetching: false,
        error: new IgnoredFilesSearchError(),
        refetch,
      } as never);
      const { onShowIgnored } = renderTree(listed([entry("README.md")], true), false, {
        showIgnored: false,
      });
      fireEvent.click(filterButton());
      typeName("artifact");
      expect(
        screen.getByText("The files can’t be searched without what Git ignores right now."),
      ).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
      expect(refetch).toHaveBeenCalledTimes(1);
      fireEvent.click(screen.getByRole("button", { name: "Show git-ignored files" }));
      expect(onShowIgnored).toHaveBeenCalledTimes(1);
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

    it("keeps a name typed while a reveal waits behind a listing that failed", () => {
      vi.useFakeTimers();
      renderTree({
        isPending: false,
        isError: true,
        error: new Error("permission denied"),
      } as never);
      // No tree mounts to take the reveal up, so it stays pending.
      fireEvent.click(screen.getByRole("button", { name: "Reveal Active File" }));
      fireEvent.click(filterButton());
      typeName("button");
      expect(nameField()).toHaveValue("button");
      expect(searches().at(-1)).toEqual([
        "conv_1",
        "button",
        "",
        "",
        { enabled: true, hideIgnored: false },
        "",
      ]);
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

    it.each([
      [
        "the host can't search",
        { data: { files: [], truncated: false, hasMore: false, unavailable: true } },
      ],
      [
        "the workspace can't be reached, so the search never starts",
        { isPending: true, fetchStatus: "idle" },
      ],
    ])(
      "says the files can't be searched when %s, rather than that nothing matched",
      (_case, search) => {
        vi.useFakeTimers();
        vi.mocked(useWorkspaceFileSearch).mockReturnValue(search as never);
        renderTree(listed([entry("src")]));
        fireEvent.click(filterButton());
        typeName("zzz");
        expect(screen.getByTestId("tree-filter-strip").nextElementSibling).toHaveTextContent(
          "The files can’t be searched right now. Send a message to reconnect the session’s machine.",
        );
        expect(screen.queryByText("No files match “zzz”.")).toBeNull();
        expect(screen.queryByText("Searching…")).toBeNull();
      },
    );

    it("says visible matches may lie past a limit filled with hidden ones", () => {
      vi.useFakeTimers();
      vi.mocked(useWorkspaceFileSearch).mockReturnValue({
        data: {
          files: [entry(".cache/app.log")],
          truncated: false,
          hasMore: true,
          unavailable: false,
        },
      } as never);
      renderTree(listed([entry("src")]));
      fireEvent.click(filterButton());
      typeName("app");
      expect(screen.getByText(/^The first matches are all hidden files/)).toBeInTheDocument();
      expect(screen.queryByText("No files match “app”.")).toBeNull();
    });

    it("says a search that stopped early may have missed matches", () => {
      vi.useFakeTimers();
      vi.mocked(useWorkspaceFileSearch).mockReturnValue({
        data: { files: [], truncated: true, hasMore: false, unavailable: false },
      } as never);
      renderTree(listed([entry("src")]));
      fireEvent.click(filterButton());
      typeName("zzz");
      expect(screen.getByText(/^No matches in the files searched so far\./)).toBeInTheDocument();
      expect(screen.queryByText("No files match “zzz”.")).toBeNull();
    });

    it("keeps the active tab's Clear Files Tree Filter while other tabs of the session change", () => {
      vi.useFakeTimers();
      vi.mocked(useWorkspaceAllFiles).mockReturnValue(listed([entry("src")]));
      const tab = (active: boolean) => (
        <FilesPageTree
          conversationId="conv_1"
          active={active}
          commandsActive={active}
          activePath="src/a.ts"
          sort="alpha"
          showHidden={false}
          showIgnored
          onFileSelect={vi.fn()}
        />
      );
      const tabs = (firstActive: boolean, firstMounted = true) => (
        <TooltipProvider>
          {firstMounted ? <div key="first">{tab(firstActive)}</div> : null}
          <div key="second">{tab(!firstActive)}</div>
        </TooltipProvider>
      );
      const view = render(tabs(true));
      // Both tabs filter their trees.
      for (const button of screen.getAllByRole("button", { name: "Filter Files Tree by Name" })) {
        fireEvent.click(button);
      }
      for (const field of screen.getAllByRole("textbox", { name: "Name contains" })) {
        fireEvent.change(field, { target: { value: "app" } });
      }
      act(() => vi.advanceTimersByTime(TREE_FILTER_DEBOUNCE_MS));
      expect(paletteLabels().filter((label) => label === "Clear Files Tree Filter")).toHaveLength(
        1,
      );

      // The second tab becomes active, then the first, now inactive, closes.
      view.rerender(tabs(false));
      expect(paletteLabels()).toContain("Clear Files Tree Filter");
      view.rerender(tabs(false, false));
      expect(paletteLabels()).toContain("Clear Files Tree Filter");
    });

    it("offers Clear Files Tree Filter only for the pane that owns the keyboard", () => {
      vi.useFakeTimers();
      vi.mocked(useWorkspaceAllFiles).mockReturnValue(listed([entry("src")]));
      const pane = (conversationId: string, keyboardTarget: boolean) => (
        <div data-testid={conversationId}>
          <FilesPageTree
            conversationId={conversationId}
            active
            commandsActive={keyboardTarget}
            activePath="src/a.ts"
            sort="alpha"
            showHidden={false}
            showIgnored
            onFileSelect={vi.fn()}
          />
        </div>
      );
      // Two sessions' panes on screen; only the one without the keyboard filters its tree.
      const panes = (firstOwnsKeyboard: boolean) => (
        <TooltipProvider>
          {pane("conv_first", firstOwnsKeyboard)}
          {pane("conv_second", !firstOwnsKeyboard)}
        </TooltipProvider>
      );
      const view = render(panes(true));
      const second = within(screen.getByTestId("conv_second"));
      fireEvent.click(second.getByRole("button", { name: "Filter Files Tree by Name" }));
      fireEvent.change(second.getByRole("textbox", { name: "Name contains" }), {
        target: { value: "app" },
      });
      act(() => vi.advanceTimersByTime(TREE_FILTER_DEBOUNCE_MS));
      expect(paletteLabels()).not.toContain("Clear Files Tree Filter");

      // The filtered pane takes the keyboard: its command clears its own session's filter.
      view.rerender(panes(false));
      const clear = Object.values(usePaletteCommandsStore.getState().groups)
        .flatMap((group) => group.commands)
        .filter((command) => command.label === "Clear Files Tree Filter");
      expect(clear).toHaveLength(1);
      act(() => void clear[0]!.run());
      expect(second.getByRole("textbox", { name: "Name contains" })).toHaveValue("");
      expect(paletteLabels()).not.toContain("Clear Files Tree Filter");
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
