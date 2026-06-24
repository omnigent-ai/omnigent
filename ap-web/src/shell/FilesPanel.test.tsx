import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { TooltipProvider } from "@/components/ui/tooltip";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  type WorkspaceChangedFile,
  type WorkspaceFile,
  useWorkspaceAllFiles,
  useWorkspaceChangedFiles,
  useWorkspaceDirectory,
  useWorkspaceEnvironment,
  useWorkspaceFileSearch,
} from "@/hooks/useWorkspaceChangedFiles";
import i18n from "@/i18n";
import { FilesPanel } from "./FilesPanel";
import { FilesPanelDrawer } from "./FilesPanelDrawer";
import { FolderTree } from "./FolderTree";

const t = i18n.getFixedT(null, "nav");

vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  useWorkspaceAllFiles: vi.fn(),
  useWorkspaceChangedFiles: vi.fn(),
  useWorkspaceDirectory: vi.fn(),
  useWorkspaceEnvironment: vi.fn(),
  useWorkspaceFileSearch: vi.fn(),
  // Real export consumed by FlatFileList's `instanceof` check; the full
  // module mock would otherwise drop it (undefined → instanceof throws).
  RunnerOfflineError: class RunnerOfflineError extends Error {},
}));

const useAllFilesMock = vi.mocked(useWorkspaceAllFiles);
const useChangedFilesMock = vi.mocked(useWorkspaceChangedFiles);
const useDirectoryMock = vi.mocked(useWorkspaceDirectory);
const useEnvironmentMock = vi.mocked(useWorkspaceEnvironment);
const useSearchMock = vi.mocked(useWorkspaceFileSearch);

function file(path: string, bytes = 10): WorkspaceFile {
  return {
    bytes,
    modified_at: null,
    name: path.split("/").at(-1) ?? path,
    path,
    type: "file",
  };
}

function changedFile(
  path: string,
  status: WorkspaceChangedFile["status"] = "modified",
): WorkspaceChangedFile {
  return {
    bytes: 10,
    modified_at: null,
    name: path.split("/").at(-1) ?? path,
    path,
    status,
  };
}

function allFilesResult(files: WorkspaceFile[]) {
  return {
    data: { available: true, data: files },
    error: null,
    isError: false,
    isLoading: false,
  } as unknown as ReturnType<typeof useWorkspaceAllFiles>;
}

function changedFilesResult(files: WorkspaceChangedFile[] = []) {
  return {
    data: { available: true, data: files },
    error: null,
    isError: false,
    isLoading: false,
  } as unknown as ReturnType<typeof useWorkspaceChangedFiles>;
}

function directoryResult(files: WorkspaceFile[] = []) {
  return {
    data: files,
    error: null,
    isError: false,
    isLoading: false,
  } as unknown as ReturnType<typeof useWorkspaceDirectory>;
}

function environmentResult(root: string | null = null) {
  return {
    data: { available: true, root },
    isLoading: false,
    isError: false,
    error: null,
  } as unknown as ReturnType<typeof useWorkspaceEnvironment>;
}

function searchResult(files: WorkspaceFile[] | undefined = undefined, isFetching = false) {
  return {
    data: files,
    isFetching,
    isLoading: false,
    isError: false,
    error: null,
  } as unknown as ReturnType<typeof useWorkspaceFileSearch>;
}

function renderPanel({
  conversationId,
  flatView = false,
  showHidden = false,
  files,
  changedFiles = [],
  onClose,
  workingDir = null,
  treeSearchResults = [],
  isSearching = false,
}: {
  conversationId: string;
  flatView?: boolean;
  showHidden?: boolean;
  files: WorkspaceFile[];
  changedFiles?: WorkspaceChangedFile[];
  onClose?: () => void;
  workingDir?: string | null;
  treeSearchResults?: WorkspaceFile[] | undefined;
  isSearching?: boolean;
}) {
  useAllFilesMock.mockReturnValue(allFilesResult(files));
  useChangedFilesMock.mockReturnValue(changedFilesResult(changedFiles));
  useDirectoryMock.mockReturnValue(directoryResult());
  useEnvironmentMock.mockReturnValue(environmentResult(workingDir));
  useSearchMock.mockReturnValue(searchResult(treeSearchResults, isSearching));

  return render(
    <MemoryRouter initialEntries={[`/c/${conversationId}`]}>
      <Routes>
        <Route
          path="/c/:conversationId"
          element={
            <FilesPanel
              sort="recent"
              onSortChange={vi.fn()}
              flatView={flatView}
              onFileSelect={vi.fn()}
              onFlatViewChange={vi.fn()}
              showHidden={showHidden}
              onShowHiddenChange={vi.fn()}
              onClose={onClose}
            />
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  useAllFilesMock.mockReset();
  useChangedFilesMock.mockReset();
  useDirectoryMock.mockReset();
  useEnvironmentMock.mockReset();
  useSearchMock.mockReset();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("FilesPanel working folder directory", () => {
  it("shows the directory basename below the Working folder label", () => {
    renderPanel({
      conversationId: "conv_wdir_posix",
      files: [],
      workingDir: "/home/user/my-project",
    });
    expect(screen.getByText("my-project")).toBeInTheDocument();
  });

  it("does not use the native title tooltip because the custom tooltip shows the full path", () => {
    renderPanel({
      conversationId: "conv_wdir_title",
      files: [],
      workingDir: "/home/user/my-project",
    });
    const el = screen.getByText("my-project");
    expect(el).not.toHaveAttribute("title");
  });

  it("handles Windows-style paths correctly", () => {
    renderPanel({
      conversationId: "conv_wdir_win",
      files: [],
      workingDir: "C:\\Users\\foo\\my-project",
    });
    expect(screen.getByText("my-project")).toBeInTheDocument();
  });

  it("does not render a directory label when workingDir is null", () => {
    renderPanel({ conversationId: "conv_wdir_null", files: [] });
    // "Working folder" label is present but no directory name span
    expect(screen.getByText(t("workingFolder"))).toBeInTheDocument();
    // There should be no element with a title that looks like a path
    expect(screen.queryByTitle("/")).toBeNull();
  });
});

describe("FilesPanel working folder header role", () => {
  // The inline right-rail panel passes `frameless` to fill the rail height
  // without the card chrome. That must NOT downgrade the "Working folder"
  // header to a plain label: it stays a collapsible button (accessible name
  // + aria-expanded) so the rail header is focusable and toggleable, and so
  // the e2e suite can target it by role. Only the drawer (onClose), which
  // has its own X close button, uses the static label header.
  it("renders the header as a collapsible button in the standalone card", () => {
    renderPanel({ conversationId: "conv_header_card", files: [] });
    const header = screen.getByRole("button", { name: /working folder/i });
    expect(header).toHaveAttribute("aria-expanded", "true");
  });

  it("renders the header as a collapsible button in frameless (inline rail) mode", () => {
    useAllFilesMock.mockReturnValue(allFilesResult([]));
    useChangedFilesMock.mockReturnValue(changedFilesResult([]));
    useDirectoryMock.mockReturnValue(directoryResult());
    useEnvironmentMock.mockReturnValue(environmentResult("/home/user/workspace"));
    useSearchMock.mockReturnValue(searchResult());

    render(
      <MemoryRouter initialEntries={["/c/conv_header_frameless"]}>
        <Routes>
          <Route
            path="/c/:conversationId"
            element={
              <FilesPanel
                sort="recent"
                onSortChange={vi.fn()}
                frameless
                flatView={false}
                onFileSelect={vi.fn()}
                onFlatViewChange={vi.fn()}
                showHidden={false}
                onShowHiddenChange={vi.fn()}
              />
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    const header = screen.getByRole("button", { name: /working folder/i });
    expect(header).toHaveAttribute("aria-expanded", "true");
  });

  it("renders a static label header (no toggle button) in the drawer", () => {
    renderPanel({ conversationId: "conv_header_drawer", files: [], onClose: vi.fn() });
    // The drawer has its own X close button, so the title is a plain label,
    // not a collapse toggle.
    expect(screen.queryByRole("button", { name: /working folder/i })).toBeNull();
    expect(screen.getByText("Working folder")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close files" })).toBeInTheDocument();
  });
});

describe("FilesPanel scope switch (Changed | All) visibility", () => {
  it("does not enable the root filesystem listing while showing Changed files", () => {
    renderPanel({
      conversationId: "conv_changed_only",
      flatView: true,
      files: [file("src/App.tsx")],
      changedFiles: [changedFile("src/App.tsx")],
    });

    expect(useChangedFilesMock).toHaveBeenCalledWith("conv_changed_only", {
      enabled: true,
    });
    expect(useAllFilesMock).toHaveBeenCalledWith("conv_changed_only", {
      enabled: false,
    });
    expect(useSearchMock).toHaveBeenCalledWith("conv_changed_only", "", "", "", {
      enabled: false,
    });
  });

  it("enables the root filesystem listing while showing All files", () => {
    renderPanel({
      conversationId: "conv_all_files",
      flatView: false,
      files: [file("src/App.tsx")],
      changedFiles: [changedFile("src/App.tsx")],
    });

    expect(useAllFilesMock).toHaveBeenCalledWith("conv_all_files", {
      enabled: true,
    });
    expect(useSearchMock).toHaveBeenCalledWith("conv_all_files", "", "", "", {
      enabled: false,
    });
  });

  it("shows the scope switch in frameless (inline right-rail) mode", () => {
    // The single Files rail tab owns its scope via this switch, so it must be
    // present in frameless mode (where the old separate rail tabs used to live).
    useAllFilesMock.mockReturnValue(allFilesResult([file("src/App.tsx")]));
    useChangedFilesMock.mockReturnValue(changedFilesResult([changedFile("src/App.tsx")]));
    useDirectoryMock.mockReturnValue(directoryResult());
    useEnvironmentMock.mockReturnValue(environmentResult());
    useSearchMock.mockReturnValue(searchResult());

    render(
      <MemoryRouter initialEntries={["/c/conv_frameless"]}>
        <Routes>
          <Route
            path="/c/:conversationId"
            element={
              <FilesPanel
                sort="recent"
                onSortChange={vi.fn()}
                frameless
                flatView={false}
                onFileSelect={vi.fn()}
                onFlatViewChange={vi.fn()}
                showHidden={false}
                onShowHiddenChange={vi.fn()}
              />
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    // Both segments present; All is selected (flatView=false), Changed is not.
    expect(screen.getByRole("radio", { name: /^changed$/i })).toHaveAttribute(
      "aria-checked",
      "false",
    );
    expect(screen.getByRole("radio", { name: /^all$/i })).toHaveAttribute("aria-checked", "true");
  });

  it("shows the scope switch in full-screen drawer mode (onClose)", () => {
    renderPanel({
      conversationId: "conv_drawer_tabs",
      files: [file("src/App.tsx")],
      onClose: vi.fn(),
    });

    expect(screen.getByRole("radio", { name: /^changed$/i })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /^all$/i })).toBeInTheDocument();
  });

  it("calls onFlatViewChange(true) when the Changed segment is clicked", () => {
    const onFlatViewChange = vi.fn();
    useAllFilesMock.mockReturnValue(allFilesResult([file("src/App.tsx")]));
    useChangedFilesMock.mockReturnValue(changedFilesResult([changedFile("src/App.tsx")]));
    useDirectoryMock.mockReturnValue(directoryResult());
    useEnvironmentMock.mockReturnValue(environmentResult());
    useSearchMock.mockReturnValue(searchResult());

    render(
      <MemoryRouter initialEntries={["/c/conv_toggle"]}>
        <Routes>
          <Route
            path="/c/:conversationId"
            element={
              <FilesPanel
                sort="recent"
                onSortChange={vi.fn()}
                frameless
                flatView={false}
                onFileSelect={vi.fn()}
                onFlatViewChange={onFlatViewChange}
                showHidden={false}
                onShowHiddenChange={vi.fn()}
              />
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("radio", { name: /^changed$/i }));
    // Selecting Changed switches to the changed-files-only flat list.
    expect(onFlatViewChange).toHaveBeenCalledWith(true);
  });
});

describe("FilesPanel changed files search", () => {
  it("shows the search field only for the Changed view", () => {
    const files = [file("src/App.tsx")];

    const { rerender } = renderPanel({ conversationId: "conv_search_visible", files });

    expect(screen.queryByRole("searchbox", { name: t("searchChangedFiles") })).toBeNull();

    rerender(
      <MemoryRouter initialEntries={["/c/conv_search_visible"]}>
        <Routes>
          <Route
            path="/c/:conversationId"
            element={
              <FilesPanel
                sort="recent"
                onSortChange={vi.fn()}
                flatView={true}
                onFileSelect={vi.fn()}
                onFlatViewChange={vi.fn()}
                showHidden={false}
                onShowHiddenChange={vi.fn()}
              />
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByRole("searchbox", { name: t("searchChangedFiles") })).toBeInTheDocument();
  });

  it("filters already-loaded changed files case-insensitively", () => {
    renderPanel({
      conversationId: "conv_search_filter",
      flatView: true,
      files: [file("src/components/Button.tsx"), file("docs/Guide.md")],
      changedFiles: [changedFile("src/components/Button.tsx"), changedFile("docs/Guide.md")],
    });

    fireEvent.change(screen.getByRole("searchbox", { name: t("searchChangedFiles") }), {
      target: { value: "BUTTON" },
    });

    expect(screen.getByText((text) => text.includes("Button.tsx"))).toBeInTheDocument();
    expect(screen.queryByText("docs/Guide.md")).toBeNull();

    fireEvent.change(screen.getByRole("searchbox", { name: t("searchChangedFiles") }), {
      target: { value: "" },
    });

    expect(screen.getByText((text) => text.includes("Button.tsx"))).toBeInTheDocument();
    expect(screen.getByText("docs/Guide.md")).toBeInTheDocument();
  });

  it("clears the search query when switching from Changed to Explore view", () => {
    const { rerender } = renderPanel({
      conversationId: "conv_search_clear",
      flatView: true,
      files: [file("src/App.tsx")],
      changedFiles: [changedFile("src/App.tsx")],
    });

    fireEvent.change(screen.getByRole("searchbox", { name: t("searchChangedFiles") }), {
      target: { value: "App" },
    });
    // Confirm the query is active before switching tabs
    expect(screen.getByRole("searchbox", { name: t("searchChangedFiles") })).toHaveValue("App");

    // Switch to Explore (tree) view
    rerender(
      <MemoryRouter initialEntries={["/c/conv_search_clear"]}>
        <Routes>
          <Route
            path="/c/:conversationId"
            element={
              <FilesPanel
                sort="recent"
                onSortChange={vi.fn()}
                flatView={false}
                onFileSelect={vi.fn()}
                onFlatViewChange={vi.fn()}
                showHidden={false}
                onShowHiddenChange={vi.fn()}
              />
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    // Switch back to Changed view
    rerender(
      <MemoryRouter initialEntries={["/c/conv_search_clear"]}>
        <Routes>
          <Route
            path="/c/:conversationId"
            element={
              <FilesPanel
                sort="recent"
                onSortChange={vi.fn()}
                flatView={true}
                onFileSelect={vi.fn()}
                onFlatViewChange={vi.fn()}
                showHidden={false}
                onShowHiddenChange={vi.fn()}
              />
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    // useEffect resets changedSearch when flatView becomes false, so the box should be empty on return
    expect(screen.getByRole("searchbox", { name: t("searchChangedFiles") })).toHaveValue("");
  });

  it("matches changed files by full path, not just filename", () => {
    renderPanel({
      conversationId: "conv_search_path",
      flatView: true,
      files: [file("src/components/Button.tsx"), file("docs/Guide.md")],
      changedFiles: [changedFile("src/components/Button.tsx"), changedFile("docs/Guide.md")],
    });

    fireEvent.change(screen.getByRole("searchbox", { name: t("searchChangedFiles") }), {
      target: { value: "src/components" },
    });

    // Directory prefix "src/components" matches src/components/Button.tsx via f.path
    expect(screen.getByText((text) => text.includes("Button.tsx"))).toBeInTheDocument();
    // docs/Guide.md does not share the path prefix
    expect(screen.queryByText("docs/Guide.md")).toBeNull();
  });

  it("renders a Close button in full-screen drawer mode", () => {
    // Passing `onClose` switches the panel into its full-screen layout
    // — the drawer's chrome.
    const onClose = vi.fn();
    renderPanel({
      conversationId: "conv_fullscreen",
      files: [file("src/App.tsx")],
      onClose,
    });

    const closeButton = screen.getByRole("button", { name: t("closeFiles") });
    expect(closeButton).toBeInTheDocument();

    fireEvent.click(closeButton);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("preserves inline folder expansion state when opening the drawer", () => {
    const files = [file("docs/Guide.md"), file("src/App.tsx")];
    useAllFilesMock.mockReturnValue(allFilesResult(files));
    useChangedFilesMock.mockReturnValue(changedFilesResult());
    useDirectoryMock.mockReturnValue(directoryResult());
    useEnvironmentMock.mockReturnValue(environmentResult());
    useSearchMock.mockReturnValue(searchResult());

    function Harness() {
      const [drawerOpen, setDrawerOpen] = useState(false);
      const [showHidden, setShowHidden] = useState(false);
      return (
        <MemoryRouter initialEntries={["/c/conv_drawer_preserves_tree"]}>
          <Routes>
            <Route
              path="/c/:conversationId"
              element={
                <>
                  <FilesPanelDrawer
                    sort="recent"
                    onSortChange={vi.fn()}
                    open={drawerOpen}
                    onClose={() => setDrawerOpen(false)}
                    onFileSelect={vi.fn()}
                    flatView={false}
                    onFlatViewChange={vi.fn()}
                    showHidden={showHidden}
                    onShowHiddenChange={setShowHidden}
                  />
                  {!drawerOpen && (
                    <>
                      <button type="button" onClick={() => setDrawerOpen(true)}>
                        open drawer
                      </button>
                      <FilesPanel
                        sort="recent"
                        onSortChange={vi.fn()}
                        flatView={false}
                        onFileSelect={vi.fn()}
                        onFlatViewChange={vi.fn()}
                        showHidden={showHidden}
                        onShowHiddenChange={setShowHidden}
                      />
                    </>
                  )}
                </>
              }
            />
          </Routes>
        </MemoryRouter>
      );
    }

    render(<Harness />);

    const srcFolder = screen.getByRole("button", { name: /src\//i });
    expect(srcFolder).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("App.tsx")).toBeInTheDocument();

    fireEvent.click(srcFolder);
    expect(srcFolder).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("App.tsx")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "open drawer" }));

    const drawerSrcFolder = screen.getByRole("button", { name: /src\//i });
    expect(drawerSrcFolder).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("App.tsx")).toBeNull();
  });

  it("preserves eye-icon (show-hidden) state when opening the drawer", () => {
    useAllFilesMock.mockReturnValue(allFilesResult([file("src/App.tsx")]));
    useChangedFilesMock.mockReturnValue(changedFilesResult());
    useDirectoryMock.mockReturnValue(directoryResult());
    useEnvironmentMock.mockReturnValue(environmentResult());
    useSearchMock.mockReturnValue(searchResult());

    function Harness() {
      const [drawerOpen, setDrawerOpen] = useState(false);
      const [showHidden, setShowHidden] = useState(false);
      return (
        <MemoryRouter initialEntries={["/c/conv_drawer_preserves_eye"]}>
          <Routes>
            <Route
              path="/c/:conversationId"
              element={
                <>
                  <FilesPanelDrawer
                    sort="recent"
                    onSortChange={vi.fn()}
                    open={drawerOpen}
                    onClose={() => setDrawerOpen(false)}
                    onFileSelect={vi.fn()}
                    flatView={false}
                    onFlatViewChange={vi.fn()}
                    showHidden={showHidden}
                    onShowHiddenChange={setShowHidden}
                  />
                  {!drawerOpen && (
                    <>
                      <button type="button" onClick={() => setDrawerOpen(true)}>
                        open drawer
                      </button>
                      <FilesPanel
                        sort="recent"
                        onSortChange={vi.fn()}
                        flatView={false}
                        onFileSelect={vi.fn()}
                        onFlatViewChange={vi.fn()}
                        showHidden={showHidden}
                        onShowHiddenChange={setShowHidden}
                      />
                    </>
                  )}
                </>
              }
            />
          </Routes>
        </MemoryRouter>
      );
    }

    render(<Harness />);

    // Toggle "show hidden" on in the inline panel
    expect(screen.getByRole("button", { name: "Show hidden files" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show hidden files" }));

    // Open the drawer
    fireEvent.click(screen.getByRole("button", { name: "open drawer" }));

    // Drawer should reflect the same show-hidden state (toggle now reads "Hide hidden files")
    expect(screen.getByRole("button", { name: "Hide hidden files" })).toBeInTheDocument();
  });

  it("keeps hidden search results hidden until the hidden-file toggle is enabled", () => {
    useAllFilesMock.mockReturnValue(allFilesResult([file(".env"), file("src/App.tsx")]));
    useChangedFilesMock.mockReturnValue(
      changedFilesResult([changedFile(".env"), changedFile("src/App.tsx")]),
    );
    useDirectoryMock.mockReturnValue(directoryResult());
    useEnvironmentMock.mockReturnValue(environmentResult());
    useSearchMock.mockReturnValue(searchResult());

    function Harness() {
      const [showHidden, setShowHidden] = useState(false);
      return (
        <MemoryRouter initialEntries={["/c/conv_search_hidden"]}>
          <Routes>
            <Route
              path="/c/:conversationId"
              element={
                <FilesPanel
                  sort="recent"
                  onSortChange={vi.fn()}
                  flatView={true}
                  onFileSelect={vi.fn()}
                  onFlatViewChange={vi.fn()}
                  showHidden={showHidden}
                  onShowHiddenChange={setShowHidden}
                />
              }
            />
          </Routes>
        </MemoryRouter>
      );
    }
    render(<Harness />);

    fireEvent.change(screen.getByRole("searchbox", { name: t("searchChangedFiles") }), {
      target: { value: ".env" },
    });

    expect(screen.queryByText(".env")).toBeNull();
    expect(screen.getByText('No changed files match ".env"')).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Show hidden files" }));

    expect(screen.getByText(".env")).toBeInTheDocument();
  });
});

describe("FilesPanel tree (Explore) search", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows the search box for the Explore view but not the Changed view", () => {
    // Tree view (flatView=false) should have the tree search box
    const { rerender } = renderPanel({
      conversationId: "conv_tree_search_visible",
      files: [file("src/App.tsx")],
    });

    expect(screen.getByRole("searchbox", { name: t("searchAllFiles") })).toBeInTheDocument();
    // The changed-files search must not appear in tree mode
    expect(screen.queryByRole("searchbox", { name: t("searchChangedFiles") })).toBeNull();

    // Switch to Changed view
    rerender(
      <MemoryRouter initialEntries={["/c/conv_tree_search_visible"]}>
        <Routes>
          <Route
            path="/c/:conversationId"
            element={
              <FilesPanel
                sort="recent"
                onSortChange={vi.fn()}
                flatView={true}
                onFileSelect={vi.fn()}
                onFlatViewChange={vi.fn()}
                showHidden={false}
                onShowHiddenChange={vi.fn()}
              />
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    // Changed view has its own search box; tree search must not be present
    expect(screen.getByRole("searchbox", { name: t("searchChangedFiles") })).toBeInTheDocument();
    expect(screen.queryByRole("searchbox", { name: t("searchAllFiles") })).toBeNull();
  });

  it("shows search results as a flat list after the debounce fires", () => {
    vi.useFakeTimers();

    renderPanel({
      conversationId: "conv_tree_search_results",
      files: [file("src/App.tsx")],
      treeSearchResults: [file("abc/test.md"), file("src/main.py")],
    });

    fireEvent.change(screen.getByRole("searchbox", { name: t("searchAllFiles") }), {
      target: { value: "test" },
    });

    // Before the debounce fires the tree is still shown, not search results
    expect(screen.queryByText("abc/test.md")).toBeNull();

    // Advance past the 300 ms debounce
    act(() => {
      vi.advanceTimersByTime(300);
    });

    // Both search result paths must be visible; the tree's top-level node
    // ("src") would still be in the DOM as a folder button so we only assert
    // on the flat result paths that prove search mode is active
    expect(screen.getByText((t) => t.includes("abc/test.md"))).toBeInTheDocument();
    expect(screen.getByText((t) => t.includes("src/main.py"))).toBeInTheDocument();
  });

  it("returns to the tree view when the search query is cleared", () => {
    vi.useFakeTimers();

    renderPanel({
      conversationId: "conv_tree_search_clear_query",
      files: [file("src/App.tsx")],
      treeSearchResults: [file("abc/test.md")],
    });

    const searchBox = screen.getByRole("searchbox", { name: t("searchAllFiles") });

    fireEvent.change(searchBox, { target: { value: "test" } });
    act(() => {
      vi.advanceTimersByTime(300);
    });
    // Search mode is active — the flat result is visible
    expect(screen.getByText((t) => t.includes("abc/test.md"))).toBeInTheDocument();

    // Clear the query
    fireEvent.change(searchBox, { target: { value: "" } });
    act(() => {
      vi.advanceTimersByTime(300);
    });

    // Tree is back — the top-level src/ folder button is visible again
    expect(screen.getByRole("button", { name: /src\//i })).toBeInTheDocument();
    // Flat search result is gone
    expect(screen.queryByText("abc/test.md")).toBeNull();
  });

  it("shows 'Searching…' while the search request is in flight", () => {
    // Test FolderTree's loading state directly — this is the state FilesPanel
    // passes down when useWorkspaceFileSearch returns isFetching=true with no
    // prior data (the placeholder-data policy returns undefined until the
    // first response lands).  Testing FolderTree directly avoids the 300 ms
    // debounce and focuses on the component that owns the "Searching…" text.
    render(
      <TooltipProvider>
        <FolderTree
          files={[]}
          isLoading={false}
          isError={false}
          error={null}
          onFileSelect={vi.fn()}
          conversationId="conv_tree_search_loading"
          showHidden={false}
          changedFiles={[]}
          searchQuery="test"
          searchResults={undefined}
          isSearching={true}
        />
      </TooltipProvider>,
    );

    // isSearching=true + searchResults=undefined → in-flight indicator
    expect(screen.getByText("Searching…")).toBeInTheDocument();
  });

  it("aligns content at the same indentation for sibling folders and files (VS Code style)", () => {
    // Regression test: the expand caret used to push folder content right
    // of file content, breaking the visible hierarchy. In the minimal VS Code
    // layout, a folder's chevron and a sibling file's icon share the same
    // leftmost content column, so both rows carry identical left indentation.
    useDirectoryMock.mockReturnValue(directoryResult());
    render(
      <TooltipProvider>
        <FolderTree
          files={[file("src/App.tsx"), file("README.md")]}
          isLoading={false}
          isError={false}
          error={null}
          onFileSelect={vi.fn()}
          conversationId="conv_tree_align"
          showHidden={false}
          changedFiles={[]}
        />
      </TooltipProvider>,
    );

    // src/ (folder) and README.md (file) are both top-level → same depth, so
    // the chevron and the file icon start at the same x (BASE_PAD = 8px).
    const folderButton = screen.getByRole("button", { name: /src\//i });
    const fileRow = screen.getByText("README.md").closest("div");
    if (!fileRow) throw new Error("file row container not found");
    expect(folderButton.style.paddingLeft).toBe(fileRow.style.paddingLeft);
    expect(folderButton.style.paddingLeft).toBe("8px");

    // Minimal layout: folders show ONLY a chevron (no folder icon) before the
    // name. The folder row should contain exactly one svg (the chevron).
    expect(folderButton.querySelectorAll("svg")).toHaveLength(1);

    // A nested file (App.tsx, depth 1) is indented one INDENT_STEP further and
    // draws a vertical indent-guide line marking its ancestor level.
    const nestedRow = screen.getByText("App.tsx").closest("div");
    if (!nestedRow) throw new Error("nested file row not found");
    expect(nestedRow.style.paddingLeft).toBe("24px");
    const guides = nestedRow.querySelectorAll(":scope > span[aria-hidden].absolute");
    expect(guides).toHaveLength(1);
  });

  it("shows an empty-state message when the search returns no results", () => {
    vi.useFakeTimers();

    renderPanel({
      conversationId: "conv_tree_search_empty",
      files: [file("src/App.tsx")],
      treeSearchResults: [],
      isSearching: false,
    });

    fireEvent.change(screen.getByRole("searchbox", { name: t("searchAllFiles") }), {
      target: { value: "zzznotfound" },
    });
    act(() => {
      vi.advanceTimersByTime(300);
    });

    expect(screen.getByText('No files match "zzznotfound"')).toBeInTheDocument();
  });

  it("clears the tree search query when switching to the Changed tab", () => {
    vi.useFakeTimers();

    const { rerender } = renderPanel({
      conversationId: "conv_tree_search_tab_clear",
      files: [file("src/App.tsx")],
      treeSearchResults: [file("abc/test.md")],
    });

    const searchBox = screen.getByRole("searchbox", { name: t("searchAllFiles") });
    fireEvent.change(searchBox, { target: { value: "test" } });
    expect(searchBox).toHaveValue("test");

    // Switch to Changed (flat) view — this triggers the useEffect that calls
    // setTreeSearch("") so returning to tree view starts with a blank query.
    rerender(
      <MemoryRouter initialEntries={["/c/conv_tree_search_tab_clear"]}>
        <Routes>
          <Route
            path="/c/:conversationId"
            element={
              <FilesPanel
                sort="recent"
                onSortChange={vi.fn()}
                flatView={true}
                onFileSelect={vi.fn()}
                onFlatViewChange={vi.fn()}
                showHidden={false}
                onShowHiddenChange={vi.fn()}
              />
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    // Switch back to Explore view
    rerender(
      <MemoryRouter initialEntries={["/c/conv_tree_search_tab_clear"]}>
        <Routes>
          <Route
            path="/c/:conversationId"
            element={
              <FilesPanel
                sort="recent"
                onSortChange={vi.fn()}
                flatView={false}
                onFileSelect={vi.fn()}
                onFlatViewChange={vi.fn()}
                showHidden={false}
                onShowHiddenChange={vi.fn()}
              />
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    // The search box must be empty — treeSearch was cleared by the useEffect
    // when flatView became true.  Using rerender() (not render()) keeps the
    // same component instance so state mutations are observable across renders.
    expect(screen.getByRole("searchbox", { name: t("searchAllFiles") })).toHaveValue("");
  });

  it("hides dotfile search results when showHidden is false", () => {
    vi.useFakeTimers();

    renderPanel({
      conversationId: "conv_tree_search_hidden",
      files: [],
      treeSearchResults: [file(".env"), file("src/main.py")],
      isSearching: false,
    });

    fireEvent.change(screen.getByRole("searchbox", { name: t("searchAllFiles") }), {
      target: { value: "env" },
    });
    act(() => {
      vi.advanceTimersByTime(300);
    });

    // .env is a dotfile — must be hidden when showHidden=false
    expect(screen.queryByText((t) => t.includes(".env"))).toBeNull();
    // Non-hidden results are still visible
    expect(screen.getByText((t) => t.includes("src/main.py"))).toBeInTheDocument();
  });

  it("shows a search error message when the search request fails", () => {
    // Test FolderTree's error state directly — FilesPanel passes isSearchError
    // and searchError down when treeSearchQuery.isError is true.  Using a
    // direct render bypasses the debounce and focuses on the error branch.
    render(
      <TooltipProvider>
        <FolderTree
          files={[]}
          isLoading={false}
          isError={false}
          error={null}
          onFileSelect={vi.fn()}
          conversationId="conv_tree_search_error"
          showHidden={false}
          changedFiles={[]}
          searchQuery="test"
          searchResults={undefined}
          isSearching={false}
          isSearchError={true}
          searchError={new Error("503 Service Unavailable")}
        />
      </TooltipProvider>,
    );
    // isSearchError=true → destructive error message, not "no matches"
    expect(screen.getByText(/Search failed:.*503/)).toBeInTheDocument();
    expect(screen.queryByText(/No files match/)).toBeNull();
  });

  it("passes the debounced query to useWorkspaceFileSearch after 300ms", () => {
    vi.useFakeTimers();

    renderPanel({
      conversationId: "conv_tree_search_wiring",
      files: [file("src/App.tsx")],
      treeSearchResults: [],
    });

    fireEvent.change(screen.getByRole("searchbox", { name: t("searchAllFiles") }), {
      target: { value: "wired" },
    });

    // Before the debounce fires, debouncedTreeSearch is still "" — the hook
    // must not yet have been called with "wired".
    expect(useSearchMock.mock.calls.some(([, q]) => q === "wired")).toBe(false);

    // Advance past the 300ms debounce threshold
    act(() => {
      vi.advanceTimersByTime(300);
    });

    // debouncedTreeSearch has now updated to "wired" — FilesPanel re-renders
    // and calls useWorkspaceFileSearch with the debounced value.  This
    // confirms the hook is wired to debouncedTreeSearch, not the raw input.
    expect(
      useSearchMock.mock.calls.some(
        ([convId, q]) => convId === "conv_tree_search_wiring" && q === "wired",
      ),
    ).toBe(true);
  });

  it("reveals the include/exclude glob inputs when the filters toggle is clicked", () => {
    renderPanel({
      conversationId: "conv_tree_filters_toggle",
      files: [file("src/App.tsx")],
      treeSearchResults: [],
    });

    // Hidden by default — the toggle starts collapsed.
    expect(screen.queryByRole("textbox", { name: t("filesToInclude") })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Show search filters" }));

    // Both glob inputs become visible after the toggle is opened.
    expect(screen.getByRole("textbox", { name: t("filesToInclude") })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: t("filesToExclude") })).toBeInTheDocument();
  });

  it("passes the include glob to useWorkspaceFileSearch alongside the query", () => {
    vi.useFakeTimers();

    renderPanel({
      conversationId: "conv_tree_include",
      files: [file("src/App.tsx")],
      treeSearchResults: [file("src/main.py")],
    });

    fireEvent.change(screen.getByRole("searchbox", { name: t("searchAllFiles") }), {
      target: { value: "main" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Show search filters" }));
    fireEvent.change(screen.getByRole("textbox", { name: t("filesToInclude") }), {
      target: { value: "*.py" },
    });
    act(() => {
      vi.advanceTimersByTime(300);
    });

    // Both the text query (2nd arg) and the include glob (3rd arg) reach the
    // hook; a missing include wiring would leave the 3rd arg "".
    expect(
      useSearchMock.mock.calls.some(
        ([convId, q, include]) =>
          convId === "conv_tree_include" && q === "main" && include === "*.py",
      ),
    ).toBe(true);
  });

  it("passes the exclude glob to useWorkspaceFileSearch alongside the query", () => {
    vi.useFakeTimers();

    renderPanel({
      conversationId: "conv_tree_exclude",
      files: [file("src/App.tsx")],
      treeSearchResults: [],
    });

    fireEvent.change(screen.getByRole("searchbox", { name: t("searchAllFiles") }), {
      target: { value: "main" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Show search filters" }));
    fireEvent.change(screen.getByRole("textbox", { name: t("filesToExclude") }), {
      target: { value: "**/node_modules" },
    });
    act(() => {
      vi.advanceTimersByTime(300);
    });

    // Both the text query (2nd arg) and the exclude glob (4th arg) reach the
    // hook; a missing exclude wiring would leave the 4th arg "".
    expect(
      useSearchMock.mock.calls.some(
        ([convId, q, , exclude]) =>
          convId === "conv_tree_exclude" && q === "main" && exclude === "**/node_modules",
      ),
    ).toBe(true);
  });

  it("clears the include/exclude filters when switching to the Changed tab", () => {
    const { rerender } = renderPanel({
      conversationId: "conv_tree_filters_tab_clear",
      files: [file("src/App.tsx")],
      treeSearchResults: [],
    });

    fireEvent.click(screen.getByRole("button", { name: "Show search filters" }));
    fireEvent.change(screen.getByRole("textbox", { name: t("filesToInclude") }), {
      target: { value: "*.ts" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: t("filesToExclude") }), {
      target: { value: "**/node_modules" },
    });
    expect(screen.getByRole("textbox", { name: t("filesToInclude") })).toHaveValue("*.ts");
    expect(screen.getByRole("textbox", { name: t("filesToExclude") })).toHaveValue(
      "**/node_modules",
    );

    // Switch to Changed (flat) view — the useEffect resets the glob filters.
    rerender(
      <MemoryRouter initialEntries={["/c/conv_tree_filters_tab_clear"]}>
        <Routes>
          <Route
            path="/c/:conversationId"
            element={
              <FilesPanel
                sort="recent"
                onSortChange={vi.fn()}
                flatView={true}
                onFileSelect={vi.fn()}
                onFlatViewChange={vi.fn()}
                showHidden={false}
                onShowHiddenChange={vi.fn()}
              />
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    // Switch back to Explore — the toggle stays open (UI preference persists)
    // but both filter inputs must be empty again (the values were cleared).
    rerender(
      <MemoryRouter initialEntries={["/c/conv_tree_filters_tab_clear"]}>
        <Routes>
          <Route
            path="/c/:conversationId"
            element={
              <FilesPanel
                sort="recent"
                onSortChange={vi.fn()}
                flatView={false}
                onFileSelect={vi.fn()}
                onFlatViewChange={vi.fn()}
                showHidden={false}
                onShowHiddenChange={vi.fn()}
              />
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByRole("textbox", { name: t("filesToInclude") })).toHaveValue("");
    expect(screen.getByRole("textbox", { name: t("filesToExclude") })).toHaveValue("");
  });
});
