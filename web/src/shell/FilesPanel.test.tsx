import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { TooltipProvider } from "@/components/ui/tooltip";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  type WorkspaceChangedFile,
  type WorkspaceFile,
  PathUnreachableError,
  useWorkspaceAllFiles,
  useWorkspaceChangedFiles,
  useWorkspaceDirectory,
  useWorkspaceEnvironment,
  useWorkspaceFileSearch,
} from "@/hooks/useWorkspaceChangedFiles";
import type * as WorkspaceChangedFilesModule from "@/hooks/useWorkspaceChangedFiles";
import type * as WorkspacePickerModule from "./WorkspacePicker";

const { copyTextMock } = vi.hoisted(() => ({ copyTextMock: vi.fn(() => Promise.resolve()) }));
vi.mock("@/lib/clipboard", () => ({ copyText: copyTextMock }));

import { useSession } from "@/hooks/useSession";
import { FilesPanel } from "./FilesPanel";
import { FilesPanelDrawer } from "./FilesPanelDrawer";
import { FolderTree } from "./FolderTree";
import { SCROLL_RESTORE_BUDGET_MS } from "./useScrollRestore";

vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => ({
  // Keep the module's PURE path helpers real. relativizeToWorkspace decides
  // the wire form (and therefore the authorization level) of every request the
  // panel makes, so stubbing it would test a fiction.
  ...(await importOriginal<typeof WorkspaceChangedFilesModule>()),
  useWorkspaceAllFiles: vi.fn(),
  useWorkspaceChangedFiles: vi.fn(),
  useWorkspaceDirectory: vi.fn(),
  useWorkspaceEnvironment: vi.fn(),
  useWorkspaceFileSearch: vi.fn(),
  // Real exports consumed by `instanceof` checks (FlatFileList's offline
  // hint, FilesPanel's unreachable-location message); the full module mock
  // would otherwise drop them (undefined → instanceof throws).
  RunnerOfflineError: class RunnerOfflineError extends Error {},
  PathUnreachableError: class extends Error {
    reachableRoots: string[] = [];
  },
}));

// Stub only the browser component -- its own suite covers its behaviour, and
// driving its host-filesystem fetches here would test the wrong thing. The
// real path helpers stay, since other code under test imports them.
vi.mock("./WorkspacePicker", async (importOriginal) => ({
  ...(await importOriginal<typeof WorkspacePickerModule>()),
  WorkspacePicker: ({ onNavigate }: { onNavigate?: (p: string) => void }) => (
    <button type="button" data-testid="stub-picker-navigate" onClick={() => onNavigate?.("/etc")}>
      pick /etc
    </button>
  ),
}));

// The panel reads the session's host to point the directory browser at it.
vi.mock("@/hooks/useSession", () => ({
  useSession: vi.fn(() => ({ session: { hostId: "host_test" } })),
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
  linesAdded: number | null = null,
  linesRemoved: number | null = null,
): WorkspaceChangedFile {
  return {
    bytes: 10,
    modified_at: null,
    name: path.split("/").at(-1) ?? path,
    path,
    status,
    lines_added: linesAdded,
    lines_removed: linesRemoved,
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

function environmentResult(
  root: string | null = null,
  reachable: {
    unconfined: boolean;
    roots: { path: string; access: string; origin: string }[];
  } | null = null,
) {
  return {
    data: { available: true, root, home: "/home/user", reachable },
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
  reachable = null,
  onFileSelect = vi.fn(),
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
  reachable?: {
    unconfined: boolean;
    roots: { path: string; access: string; origin: string }[];
  } | null;
  onFileSelect?: (path: string) => void;
}) {
  useAllFilesMock.mockReturnValue(allFilesResult(files));
  useChangedFilesMock.mockReturnValue(changedFilesResult(changedFiles));
  useDirectoryMock.mockReturnValue(directoryResult());
  useEnvironmentMock.mockReturnValue(environmentResult(workingDir, reachable));
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
              onFileSelect={onFileSelect}
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
    expect(screen.getByText("Working folder")).toBeInTheDocument();
    // There should be no element with a title that looks like a path
    expect(screen.queryByTitle("/")).toBeNull();
  });
});

describe("FilesPanel working folder header role", () => {
  // The "Working folder" header is a static label in every mode — it is not a
  // collapse toggle. Collapsing was removed: the panel's content is the whole
  // point of the panel, so there is nothing to collapse to. The content is
  // always visible and the header never carries aria-expanded.
  it("renders the header as a static label (no toggle button) in the standalone card", () => {
    renderPanel({ conversationId: "conv_header_card", files: [] });
    expect(screen.queryByRole("button", { name: /working folder/i })).toBeNull();
    expect(screen.getByText("Working folder")).toBeInTheDocument();
    // Content is always shown — the tree search box is part of it.
    expect(screen.getByRole("searchbox", { name: "Search all files" })).toBeInTheDocument();
  });

  it("renders the header as a static label (no toggle button) in frameless (inline rail) mode", () => {
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
                showHidden={false}
                onShowHiddenChange={vi.fn()}
              />
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.queryByRole("button", { name: /working folder/i })).toBeNull();
    expect(screen.getByText("Working folder")).toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: "Search all files" })).toBeInTheDocument();
  });

  it("renders a static label header with a Close button in the drawer", () => {
    renderPanel({ conversationId: "conv_header_drawer", files: [], onClose: vi.fn() });
    // The drawer adds an X close button; the title is a plain label everywhere.
    expect(screen.queryByRole("button", { name: /working folder/i })).toBeNull();
    expect(screen.getByText("Working folder")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close files" })).toBeInTheDocument();
  });
});

describe("FilesPanel hidden-files toggle icon", () => {
  // The eye reflects the current state, not the pending action: a plain eye
  // means hidden files are visible, a slashed eye means they are filtered out.
  it("shows a plain eye while hidden files are visible", () => {
    renderPanel({ conversationId: "conv_eye_on", files: [], showHidden: true });
    const toggle = screen.getByRole("button", { name: "Hide hidden files" });
    expect(toggle.querySelector(".lucide-eye")).not.toBeNull();
    expect(toggle.querySelector(".lucide-eye-off")).toBeNull();
  });

  it("shows a slashed eye while hidden files are filtered out", () => {
    renderPanel({ conversationId: "conv_eye_off", files: [], showHidden: false });
    const toggle = screen.getByRole("button", { name: "Show hidden files" });
    expect(toggle.querySelector(".lucide-eye-off")).not.toBeNull();
  });
});

describe("FilesPanel scope (fixed by the caller's Files/Changes tab)", () => {
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
    expect(useAllFilesMock).toHaveBeenCalledWith("conv_changed_only", { enabled: false }, "");
    expect(useSearchMock).toHaveBeenCalledWith(
      "conv_changed_only",
      "",
      "",
      "",
      { enabled: false },
      "",
    );
  });

  it("enables the root filesystem listing while showing All files", () => {
    renderPanel({
      conversationId: "conv_all_files",
      flatView: false,
      files: [file("src/App.tsx")],
      changedFiles: [changedFile("src/App.tsx")],
    });

    expect(useAllFilesMock).toHaveBeenCalledWith("conv_all_files", { enabled: true }, "");
    expect(useSearchMock).toHaveBeenCalledWith(
      "conv_all_files",
      "",
      "",
      "",
      { enabled: false },
      "",
    );
  });

  it("does not render an in-panel scope switch (scope is the rail tab now)", () => {
    // The Changed|All segmented control was replaced by two peer rail tabs;
    // the panel itself no longer offers a scope toggle.
    renderPanel({
      conversationId: "conv_no_switch",
      flatView: false,
      files: [file("src/App.tsx")],
    });

    expect(screen.queryByRole("radiogroup", { name: "File scope" })).toBeNull();
    expect(screen.queryByRole("radio", { name: /^changed$/i })).toBeNull();
    expect(screen.queryByRole("radio", { name: /^all$/i })).toBeNull();
  });
});

describe("FilesPanel changed files search", () => {
  it("shows the search field only for the Changed view", () => {
    const files = [file("src/App.tsx")];

    const { rerender } = renderPanel({ conversationId: "conv_search_visible", files });

    expect(screen.queryByRole("searchbox", { name: "Search changed files" })).toBeNull();

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
                showHidden={false}
                onShowHiddenChange={vi.fn()}
              />
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByRole("searchbox", { name: "Search changed files" })).toBeInTheDocument();
  });

  it("filters already-loaded changed files case-insensitively", () => {
    renderPanel({
      conversationId: "conv_search_filter",
      flatView: true,
      files: [file("src/components/Button.tsx"), file("docs/Guide.md")],
      changedFiles: [changedFile("src/components/Button.tsx"), changedFile("docs/Guide.md")],
    });

    fireEvent.change(screen.getByRole("searchbox", { name: "Search changed files" }), {
      target: { value: "BUTTON" },
    });

    expect(screen.getByText((text) => text.includes("Button.tsx"))).toBeInTheDocument();
    expect(screen.queryByText("Guide.md")).toBeNull();

    fireEvent.change(screen.getByRole("searchbox", { name: "Search changed files" }), {
      target: { value: "" },
    });

    expect(screen.getByText((text) => text.includes("Button.tsx"))).toBeInTheDocument();
    expect(screen.getByText("Guide.md")).toBeInTheDocument();
  });

  it("clears the search query when switching from Changed to Explore view", () => {
    const { rerender } = renderPanel({
      conversationId: "conv_search_clear",
      flatView: true,
      files: [file("src/App.tsx")],
      changedFiles: [changedFile("src/App.tsx")],
    });

    fireEvent.change(screen.getByRole("searchbox", { name: "Search changed files" }), {
      target: { value: "App" },
    });
    // Confirm the query is active before switching tabs
    expect(screen.getByRole("searchbox", { name: "Search changed files" })).toHaveValue("App");

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
                showHidden={false}
                onShowHiddenChange={vi.fn()}
              />
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    // useEffect resets changedSearch when flatView becomes false, so the box should be empty on return
    expect(screen.getByRole("searchbox", { name: "Search changed files" })).toHaveValue("");
  });

  it("matches changed files by full path, not just filename", () => {
    renderPanel({
      conversationId: "conv_search_path",
      flatView: true,
      files: [file("src/components/Button.tsx"), file("docs/Guide.md")],
      changedFiles: [changedFile("src/components/Button.tsx"), changedFile("docs/Guide.md")],
    });

    fireEvent.change(screen.getByRole("searchbox", { name: "Search changed files" }), {
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

    const closeButton = screen.getByRole("button", { name: "Close files" });
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

    fireEvent.change(screen.getByRole("searchbox", { name: "Search changed files" }), {
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

    expect(screen.getByRole("searchbox", { name: "Search all files" })).toBeInTheDocument();
    // The changed-files search must not appear in tree mode
    expect(screen.queryByRole("searchbox", { name: "Search changed files" })).toBeNull();

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
                showHidden={false}
                onShowHiddenChange={vi.fn()}
              />
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    // Changed view has its own search box; tree search must not be present
    expect(screen.getByRole("searchbox", { name: "Search changed files" })).toBeInTheDocument();
    expect(screen.queryByRole("searchbox", { name: "Search all files" })).toBeNull();
  });

  it("shows search results as a flat list after the debounce fires", () => {
    vi.useFakeTimers();

    renderPanel({
      conversationId: "conv_tree_search_results",
      files: [file("src/App.tsx")],
      treeSearchResults: [file("abc/test.md"), file("src/main.py")],
    });

    fireEvent.change(screen.getByRole("searchbox", { name: "Search all files" }), {
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

    const searchBox = screen.getByRole("searchbox", { name: "Search all files" });

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
          sort="alpha"
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
          sort="alpha"
        />
      </TooltipProvider>,
    );

    // src/ (folder) and README.md (file) are both top-level → same depth, so
    // the chevron and the file icon start at the same x (BASE_PAD = 8px).
    // Both row types are a wrapper div carrying the indent, with the clickable
    // element nested inside — folders included, so the copy button can be a
    // sibling of the toggle rather than a button inside a button.
    const folderButton = screen.getByRole("button", { name: /src\//i });
    const folderRow = folderButton.closest("div");
    const fileRow = screen.getByText("README.md").closest("div");
    if (!folderRow || !fileRow) throw new Error("row container not found");
    expect(folderRow.style.paddingLeft).toBe(fileRow.style.paddingLeft);
    expect(folderRow.style.paddingLeft).toBe("8px");

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

    fireEvent.change(screen.getByRole("searchbox", { name: "Search all files" }), {
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

    const searchBox = screen.getByRole("searchbox", { name: "Search all files" });
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
    expect(screen.getByRole("searchbox", { name: "Search all files" })).toHaveValue("");
  });

  it("hides dotfile search results when showHidden is false", () => {
    vi.useFakeTimers();

    renderPanel({
      conversationId: "conv_tree_search_hidden",
      files: [],
      treeSearchResults: [file(".env"), file("src/main.py")],
      isSearching: false,
    });

    fireEvent.change(screen.getByRole("searchbox", { name: "Search all files" }), {
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
          sort="alpha"
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

    fireEvent.change(screen.getByRole("searchbox", { name: "Search all files" }), {
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
    expect(screen.queryByRole("textbox", { name: "files to include" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Show search filters" }));

    // Both glob inputs become visible after the toggle is opened.
    expect(screen.getByRole("textbox", { name: "files to include" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "files to exclude" })).toBeInTheDocument();
  });

  it("passes the include glob to useWorkspaceFileSearch alongside the query", () => {
    vi.useFakeTimers();

    renderPanel({
      conversationId: "conv_tree_include",
      files: [file("src/App.tsx")],
      treeSearchResults: [file("src/main.py")],
    });

    fireEvent.change(screen.getByRole("searchbox", { name: "Search all files" }), {
      target: { value: "main" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Show search filters" }));
    fireEvent.change(screen.getByRole("textbox", { name: "files to include" }), {
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

    fireEvent.change(screen.getByRole("searchbox", { name: "Search all files" }), {
      target: { value: "main" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Show search filters" }));
    fireEvent.change(screen.getByRole("textbox", { name: "files to exclude" }), {
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
    fireEvent.change(screen.getByRole("textbox", { name: "files to include" }), {
      target: { value: "*.ts" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "files to exclude" }), {
      target: { value: "**/node_modules" },
    });
    expect(screen.getByRole("textbox", { name: "files to include" })).toHaveValue("*.ts");
    expect(screen.getByRole("textbox", { name: "files to exclude" })).toHaveValue(
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
                showHidden={false}
                onShowHiddenChange={vi.fn()}
              />
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByRole("textbox", { name: "files to include" })).toHaveValue("");
    expect(screen.getByRole("textbox", { name: "files to exclude" })).toHaveValue("");
  });
});

describe("FilesPanel sort control", () => {
  it("renders the sort selector in the All (tree) view", () => {
    // Sort applies to the All tree too, not just the Changed list (trigger is
    // labeled "Sort: <active>").
    renderPanel({
      conversationId: "conv_all_sort",
      files: [file("a.txt")],
      flatView: false,
    });
    expect(screen.getByRole("button", { name: /^Sort:/ })).toBeInTheDocument();
  });
});

describe("FilesPanel scroll position persistence", () => {
  function renderAndGetScrollSection(conversationId: string, files: WorkspaceFile[]) {
    const result = renderPanel({ conversationId, files });
    const section = result.container.querySelector("section");
    if (!section) throw new Error("scroll section not found");
    return { result, section };
  }

  it("restores the saved scroll position when returning to a conversation", () => {
    const files = Array.from({ length: 50 }, (_, i) => file(`file-${i}.ts`));

    // Scroll in conversation A, then leave it.
    const a = renderAndGetScrollSection("conv_scroll_a", files);
    a.section.scrollTop = 120;
    fireEvent.scroll(a.section);
    a.result.unmount();

    // Conversation B starts at the top, unaffected by A's position.
    const b = renderAndGetScrollSection("conv_scroll_b", files);
    expect(b.section.scrollTop).toBe(0);
    b.result.unmount();

    // Returning to A restores its saved position.
    const back = renderAndGetScrollSection("conv_scroll_a", files);
    expect(back.section.scrollTop).toBe(120);
  });

  it("does not let the loading clamp overwrite the saved position", async () => {
    const files = Array.from({ length: 50 }, (_, i) => file(`file-${i}.ts`));
    const conversationId = "conv_scroll_clamp";

    // Scroll in the conversation, then leave it.
    const first = renderAndGetScrollSection(conversationId, files);
    first.section.scrollTop = 120;
    fireEvent.scroll(first.section);
    first.result.unmount();

    // Revisit while the queries are still disabled (environment pending):
    // data is undefined — not "loading" — and the short placeholder content
    // clamps scrollTop to 0, which fires a scroll event.
    const pending = {
      data: undefined,
      error: null,
      isError: false,
      isLoading: false,
    };
    useAllFilesMock.mockReturnValue(pending as unknown as ReturnType<typeof useWorkspaceAllFiles>);
    useChangedFilesMock.mockReturnValue(
      pending as unknown as ReturnType<typeof useWorkspaceChangedFiles>,
    );
    useDirectoryMock.mockReturnValue(directoryResult());
    useEnvironmentMock.mockReturnValue(environmentResult(null));
    useSearchMock.mockReturnValue(searchResult());
    // Fresh JSX per render — reusing the same element would let React bail
    // out of the re-render without re-reading the updated hook mocks.
    const panel = () => (
      <MemoryRouter initialEntries={[`/c/${conversationId}`]}>
        <Routes>
          <Route
            path="/c/:conversationId"
            element={
              <FilesPanel
                sort="recent"
                onSortChange={vi.fn()}
                flatView={false}
                onFileSelect={vi.fn()}
                showHidden={false}
                onShowHiddenChange={vi.fn()}
              />
            }
          />
        </Routes>
      </MemoryRouter>
    );
    const view = render(panel());
    const section = view.container.querySelector("section");
    if (!section) throw new Error("scroll section not found");
    section.scrollTop = 0;
    fireEvent.scroll(section);

    // Files arrive: the saved position survives the clamp and is restored.
    useAllFilesMock.mockReturnValue(allFilesResult(files));
    useChangedFilesMock.mockReturnValue(changedFilesResult([]));
    view.rerender(panel());
    expect(section.scrollTop).toBe(120);

    // Let the restore's animation-frame loop settle (jsdom has no layout, so
    // the target is never "reachable" — the loop runs until its time budget
    // expires), then user scrolls are saved again.
    const expired = performance.now() + SCROLL_RESTORE_BUDGET_MS + 1;
    vi.spyOn(performance, "now").mockReturnValue(expired);
    await act(
      () =>
        new Promise((resolve) => {
          requestAnimationFrame(() => resolve(undefined));
        }),
    );
    vi.mocked(performance.now).mockRestore();
    section.scrollTop = 40;
    fireEvent.scroll(section);
    view.unmount();
    const back = renderAndGetScrollSection(conversationId, files);
    expect(back.section.scrollTop).toBe(40);
  });
});

describe("FolderTree expanded state across conversation switches", () => {
  function renderTree(conversationId: string, files: WorkspaceFile[]) {
    useDirectoryMock.mockReturnValue(directoryResult());
    const tree = (id: string) => (
      <TooltipProvider>
        <FolderTree
          files={files}
          isLoading={false}
          isError={false}
          error={null}
          onFileSelect={vi.fn()}
          conversationId={id}
          showHidden={false}
          changedFiles={[]}
          sort="alpha"
        />
      </TooltipProvider>
    );
    const view = render(tree(conversationId));
    return { view, tree };
  }

  it("re-syncs expanded folders when switching conversations without remounting", () => {
    const files = [file("src/App.tsx"), file("README.md")];
    const { view, tree } = renderTree("conv_tree_resync_a", files);

    // Collapse src/ in conversation A (expanded by default).
    expect(screen.getByText("App.tsx")).toBeDefined();
    fireEvent.click(screen.getByRole("button", { name: /src\// }));
    expect(screen.queryByText("App.tsx")).toBeNull();

    // Switch to conversation B in place: defaults apply, src/ is expanded.
    view.rerender(tree("conv_tree_resync_b"));
    expect(screen.getByText("App.tsx")).toBeDefined();

    // Switch back to A in place: its collapsed state is restored.
    view.rerender(tree("conv_tree_resync_a"));
    expect(screen.queryByText("App.tsx")).toBeNull();
  });
});

describe("FilesPanel browse location", () => {
  const CONFINED = {
    unconfined: false,
    roots: [{ path: "/home/user/proj", access: "write", origin: "cwd" }],
  };
  const UNCONFINED = {
    unconfined: true,
    roots: [{ path: "/home/user/proj", access: "write", origin: "cwd" }],
  };

  it("stays a plain label when the session has nowhere else to go", () => {
    // A confined agent with no declared grants can only ever see its
    // workspace, so the navigation affordance must not appear at all --
    // the panel looks exactly as it did before this control existed.
    renderPanel({
      conversationId: "conv_confined",
      files: [],
      workingDir: "/home/user/proj",
      reachable: CONFINED,
    });

    expect(screen.queryByTestId("browse-location-path")).toBeNull();
    expect(screen.getByText("proj")).toBeInTheDocument();
  });

  it("shows the full path, clickable, when the session is unconfined", () => {
    renderPanel({
      conversationId: "conv_unconfined",
      files: [],
      workingDir: "/home/user/proj",
      reachable: UNCONFINED,
    });

    // The whole path, not just the basename -- the point of the control is to
    // tell you where you are before you decide to go elsewhere.
    const trigger = screen.getByTestId("browse-location-path");
    expect(trigger).toHaveTextContent("/home/user/proj");
  });

  it("re-roots both the tree and search when a directory is picked", () => {
    // The bug this feature fixes is a tree showing one directory while search
    // reports on another, so both queries must move together.
    renderPanel({
      conversationId: "conv_reroot",
      files: [],
      workingDir: "/home/user/proj",
      reachable: UNCONFINED,
    });

    fireEvent.click(screen.getByTestId("browse-location-path"));
    fireEvent.click(screen.getByTestId("stub-picker-navigate"));

    expect(useAllFilesMock).toHaveBeenLastCalledWith("conv_reroot", expect.anything(), "/etc");
    expect(useSearchMock).toHaveBeenLastCalledWith(
      "conv_reroot",
      "",
      "",
      "",
      expect.anything(),
      "/etc",
    );
  });

  it("surfaces a refused location instead of an empty tree", () => {
    // An empty tree reads as "this directory is empty", which is a different
    // fact from "you may not look here".
    const err = new PathUnreachableError("Path '/etc' is outside this session's reach", [
      "/home/user/proj",
    ]);
    useAllFilesMock.mockReturnValue({
      data: undefined,
      error: err,
      isError: true,
      isLoading: false,
    } as unknown as ReturnType<typeof useWorkspaceAllFiles>);
    useChangedFilesMock.mockReturnValue(changedFilesResult([]));
    useDirectoryMock.mockReturnValue(directoryResult());
    useEnvironmentMock.mockReturnValue(environmentResult("/home/user/proj", CONFINED));
    useSearchMock.mockReturnValue(searchResult(undefined, false));

    render(
      <MemoryRouter initialEntries={["/c/conv_refused"]}>
        <Routes>
          <Route
            path="/c/:conversationId"
            element={
              <FilesPanel
                sort="recent"
                onSortChange={vi.fn()}
                flatView={false}
                onFileSelect={vi.fn()}
                showHidden={false}
                onShowHiddenChange={vi.fn()}
              />
            }
          />
        </Routes>
      </MemoryRouter>,
    );

    // Confined sessions render the plain label, so the message rides with the
    // panel rather than the (absent) location bar.
    expect(screen.getByText(/outside this session's reach/)).toBeInTheDocument();
  });
});

describe("FilesPanel browse permission", () => {
  const UNCONFINED_REACH = {
    unconfined: true,
    roots: [{ path: "/home/user/proj", access: "write", origin: "cwd" }],
  };

  afterEach(() => {
    // Restore the suite default (owner) — `vi.clearAllMocks` clears calls but
    // keeps an implementation set via mockReturnValue, which would leak.
    vi.mocked(useSession).mockReturnValue({
      session: { hostId: "host_test" },
    } as unknown as ReturnType<typeof useSession>);
  });

  it("hides the control from a collaborator who is not the session owner", () => {
    // `reachable` describes what the ENVIRONMENT can reach and is identical
    // for every viewer, so it cannot decide this on its own. Everything past
    // the workspace is the owner's own machine: a collaborator's browse is
    // refused 403, and the picker itself reads the owner-scoped host
    // filesystem endpoint — so the control would open onto an error.
    vi.mocked(useSession).mockReturnValue({
      session: { hostId: "host_test", permissionLevel: 2 },
    } as unknown as ReturnType<typeof useSession>);

    renderPanel({
      conversationId: "conv_collaborator",
      files: [],
      workingDir: "/home/user/proj",
      reachable: UNCONFINED_REACH,
    });

    expect(screen.queryByTestId("browse-location-path")).toBeNull();
    // Still the plain label — the collaborator keeps the workspace view.
    expect(screen.getByText("proj")).toBeInTheDocument();
  });

  it("keeps the control for the session owner", () => {
    vi.mocked(useSession).mockReturnValue({
      session: { hostId: "host_test", permissionLevel: 4 },
    } as unknown as ReturnType<typeof useSession>);

    renderPanel({
      conversationId: "conv_owner",
      files: [],
      workingDir: "/home/user/proj",
      reachable: UNCONFINED_REACH,
    });

    expect(screen.getByTestId("browse-location-path")).toBeInTheDocument();
  });

  it("keeps the control when the level is unknown (single-user)", () => {
    // A single-user local server reports no level at all. Treating that as
    // "not the owner" would remove browsing from the ONLY user.
    vi.mocked(useSession).mockReturnValue({
      session: { hostId: "host_test", permissionLevel: null },
    } as unknown as ReturnType<typeof useSession>);

    renderPanel({
      conversationId: "conv_single_user",
      files: [],
      workingDir: "/home/user/proj",
      reachable: UNCONFINED_REACH,
    });

    expect(screen.getByTestId("browse-location-path")).toBeInTheDocument();
  });
});

describe("FilesPanel header copy path", () => {
  const UNCONFINED_REACH = {
    unconfined: true,
    roots: [{ path: "/home/user/proj", access: "write", origin: "cwd" }],
  };

  it("copies the working folder's absolute path from the header", () => {
    renderPanel({
      conversationId: "conv_copy_header",
      files: [],
      workingDir: "/home/user/proj",
    });

    fireEvent.click(screen.getByRole("button", { name: "Copy folder path: proj" }));

    // The header copies the ABSOLUTE path -- pasting a basename would be
    // useless, and the header is the one place the full path is on screen.
    expect(copyTextMock).toHaveBeenCalledWith("/home/user/proj");
  });

  it("copies the browsed directory after navigating away from the workspace", () => {
    // The header tracks wherever the panel is pointed, so the copy must
    // follow it rather than pinning to the session's workspace.
    renderPanel({
      conversationId: "conv_copy_browsed",
      files: [],
      workingDir: "/home/user/proj",
      reachable: UNCONFINED_REACH,
    });

    fireEvent.click(screen.getByTestId("browse-location-path"));
    fireEvent.click(screen.getByTestId("stub-picker-navigate"));

    fireEvent.click(screen.getByRole("button", { name: "Copy folder path: etc" }));

    expect(copyTextMock).toHaveBeenCalledWith("/etc");
  });
});

describe("FilesPanel double-click navigation", () => {
  it("re-roots onto a double-clicked folder and asks the server RELATIVELY", () => {
    // The wire form is the point. A subfolder of the workspace must be
    // requested relative, because the server authorizes an absolute location
    // at OWNER level -- sending "/home/user/proj/src" would 403 every
    // collaborator browsing a folder they can already list.
    renderPanel({
      conversationId: "conv_dblclick",
      flatView: false,
      files: [file("src/App.tsx")],
      workingDir: "/home/user/proj",
    });

    useAllFilesMock.mockClear();
    fireEvent.doubleClick(screen.getByRole("button", { name: "src/" }));

    expect(useAllFilesMock).toHaveBeenCalledWith("conv_dblclick", { enabled: true }, "src");
    // The header still names the absolute directory the user is standing in.
    expect(screen.getByText("src")).toBeInTheDocument();
  });

  it("re-attaches the browsed folder when opening a file the tree named", () => {
    // Tree paths are relative to where the tree is rooted, but the viewer
    // resolves against the workspace root -- so after navigating into a
    // folder, handing it the bare name would open the wrong file (or none).
    const onFileSelect = vi.fn();
    renderPanel({
      conversationId: "conv_open_after_nav",
      flatView: false,
      files: [file("src/App.tsx")],
      workingDir: "/home/user/proj",
      onFileSelect,
    });

    // Re-rooting swaps the listing for one relative to the NEW root, which is
    // why the bare name reaches the panel in the first place.
    useAllFilesMock.mockImplementation((_id: unknown, _opts: unknown, location?: string) =>
      location === "src"
        ? allFilesResult([file("App.tsx")])
        : allFilesResult([file("src/App.tsx")]),
    );

    fireEvent.doubleClick(screen.getByRole("button", { name: "src/" }));
    fireEvent.click(screen.getByText("App.tsx"));

    expect(onFileSelect).toHaveBeenCalledWith("src/App.tsx");
  });
});
