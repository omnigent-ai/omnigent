import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type * as FileTreeModule from "@/components/file-tree/vendored";
import type { FileTreeBatchOperation, FileTreeModel } from "@/components/file-tree/vendored";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as IdentityModule from "@/lib/identity";
import { authenticatedFetch } from "@/lib/identity";
import type * as WorkspaceApiModule from "@/lib/workspaceApi";
import { TooltipProvider } from "@/components/ui/tooltip";
import { workspaceDirectoryQuery } from "@/hooks/useWorkspaceChangedFiles";
import { FileBreadcrumbs } from "./FileBreadcrumbs";
import { WorkspaceFolderTree } from "./WorkspaceFolderTree";

interface FakeRow {
  kind: "file" | "directory";
  path: string;
  isExpanded?: boolean;
}

// A stand-in for the file tree model: tests set the rows it shows and see
// what the component asked of it. Its edits also go to a real model, which
// rejects an add of a path it already has.
const tree = vi.hoisted(() => {
  const state = {
    options: undefined as
      | undefined
      | {
          paths: string[];
          initialExpandedPaths?: string[];
          initialSelectedPaths?: string[];
          onSelectionChange: (paths: string[]) => void;
        },
    rows: [] as FakeRow[],
    real: null as FileTreeModel | null,
    listeners: new Set<() => void>(),
    model: {} as Record<string, unknown>,
  };
  state.model = {
    subscribe: (listener: () => void) => {
      state.listeners.add(listener);
      return () => state.listeners.delete(listener);
    },
    getVisibleCount: () => state.rows.length,
    getVisibleRows: (start: number, end: number) => state.rows.slice(start, end),
    batch: vi.fn((operations: unknown[]) =>
      state.real?.batch(operations as FileTreeBatchOperation[]),
    ),
    scrollToPath: vi.fn(),
    focusPath: vi.fn((path: string) => state.real?.focusPath(path)),
    getSelectedPaths: () => state.real?.getSelectedPaths() ?? [],
    getFocusedPath: () => state.real?.getFocusedPath() ?? null,
    getItem: (path: string) => state.real?.getItem(path) ?? null,
  };
  return state;
});

vi.mock("@/components/file-tree/vendored", async () => {
  const actual = await vi.importActual<typeof FileTreeModule>("@/components/file-tree/vendored");
  const RealTree = actual.FileTreeModel;
  return {
    ...actual,
    useFileTree: (options: NonNullable<typeof tree.options>) => {
      tree.options = options;
      tree.real ??= new RealTree({
        paths: options.paths,
        flattenEmptyDirectories: true,
        initialSelectedPaths: options.initialSelectedPaths,
      });
      return { model: tree.model };
    },
    FileTree: (props: { "aria-label"?: string }) => (
      <div data-testid="file-tree" aria-label={props["aria-label"]} />
    ),
  };
});

// Listings come over REST here: the workspace API has no route for these sessions.
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  authenticatedFetch: vi.fn(),
}));
vi.mock("@/lib/workspaceApi", async (importOriginal) => ({
  ...(await importOriginal<typeof WorkspaceApiModule>()),
  listDirectoryViaWorkspaceApi: vi.fn(async () => null),
}));

type Listings = Record<
  string,
  { name: string; path: string; type: "file" | "directory"; leaf_directory_path?: string }[] | Error
>;

/** Answers folder listings by the folder named in the request URL. */
function serveListings(listings: Listings) {
  vi.mocked(authenticatedFetch).mockImplementation(async (input: RequestInfo | URL) => {
    const url = String(input);
    const folder = decodeURIComponent(
      new URL(url, "http://localhost").pathname.split("/filesystem")[1]?.replace(/^\//, "") ?? "",
    );
    const listing = listings[folder];
    if (listing === undefined || listing instanceof Error) {
      return new Response("unavailable", { status: 503, statusText: "Service Unavailable" });
    }
    const data = listing.map((entry) => ({
      id: entry.path,
      bytes: null,
      modified_at: null,
      ...entry,
    }));
    return new Response(JSON.stringify({ object: "list", data, has_more: false }));
  });
}

function testQueryClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

/** A cached entry, with its path relative to the folder the tree browses. */
function cachedEntry(path: string, type: "file" | "directory", leafDirectoryPath?: string) {
  return {
    id: path,
    name: path.split("/").at(-1) ?? path,
    path,
    type,
    bytes: null,
    modified_at: null,
    ...(leafDirectoryPath ? { leaf_directory_path: leafDirectoryPath } : {}),
  };
}

/** Caches a listing long enough ago that opening the tree refetches it. */
function cacheStaleListing(
  queryClient: QueryClient,
  location: string,
  subfolder: string,
  entries: ReturnType<typeof cachedEntry>[],
) {
  const { queryKey } = workspaceDirectoryQuery("conv_1", subfolder, location);
  queryClient.setQueryData(queryKey, entries, { updatedAt: Date.now() - 60_000 });
}

function renderTree(folder: string, revealPath: string, queryClient = testQueryClient()) {
  const onOpenFile = vi.fn();
  render(
    <QueryClientProvider client={queryClient}>
      <WorkspaceFolderTree
        conversationId="conv_1"
        folder={folder}
        revealPath={revealPath}
        label={`Files in ${folder}`}
        dark={false}
        onOpenFile={onOpenFile}
      />
    </QueryClientProvider>,
  );
  return { onOpenFile };
}

const SRC_LISTINGS: Listings = {
  src: [
    { name: "shell", path: "src/shell", type: "directory" },
    { name: "lib", path: "src/lib", type: "directory" },
    { name: "app.ts", path: "src/app.ts", type: "file" },
  ],
  "src/shell": [{ name: "viewer.ts", path: "src/shell/viewer.ts", type: "file" }],
  "src/lib": [{ name: "util.ts", path: "src/lib/util.ts", type: "file" }],
};

beforeEach(() => {
  tree.options = undefined;
  tree.real = null;
  tree.rows = [];
  tree.listeners.clear();
  vi.mocked(tree.model.batch as () => void).mockClear();
  vi.mocked(tree.model.scrollToPath as () => void).mockClear();
  vi.mocked(tree.model.focusPath as () => void).mockClear();
  vi.mocked(authenticatedFetch).mockReset();
});
afterEach(cleanup);

describe("WorkspaceFolderTree", () => {
  it("lists the folder with the viewed file's folders expanded and the file selected", async () => {
    serveListings(SRC_LISTINGS);
    renderTree("src", "shell/viewer.ts");

    expect(await screen.findByTestId("file-tree")).toHaveAttribute("aria-label", "Files in src");
    expect(tree.options?.paths).toEqual(["shell/", "lib/", "app.ts", "shell/viewer.ts"]);
    expect(tree.options?.initialExpandedPaths).toEqual(["shell/"]);
    expect(tree.options?.initialSelectedPaths).toEqual(["shell/viewer.ts"]);
    await waitFor(() => expect(tree.model.focusPath).toHaveBeenCalledWith("shell/viewer.ts"));
  });

  it("selects and focuses a revealed folder without expanding it", async () => {
    serveListings(SRC_LISTINGS);
    const { onOpenFile } = renderTree("src", "shell");

    await screen.findByTestId("file-tree");
    expect(tree.options?.initialExpandedPaths).toEqual([]);
    expect(tree.options?.initialSelectedPaths).toEqual(["shell/"]);
    await waitFor(() => expect(tree.model.focusPath).toHaveBeenCalledWith("shell/"));
    expect(vi.mocked(authenticatedFetch)).toHaveBeenCalledTimes(1);
    expect(onOpenFile).not.toHaveBeenCalled();
  });

  it("selects a single-folder chain's row when its top folder is revealed", async () => {
    const queryClient = testQueryClient();
    cacheStaleListing(queryClient, "", "", [
      cachedEntry("pkg", "directory", "pkg/inner/deep"),
      cachedEntry("README.md", "file"),
    ]);
    serveListings({
      "": [
        { name: "pkg", path: "pkg", type: "directory" },
        { name: "README.md", path: "README.md", type: "file" },
      ],
    });

    renderTree("", "pkg", queryClient);

    await screen.findByTestId("file-tree");
    expect(tree.options?.initialSelectedPaths).toEqual(["pkg/inner/deep/"]);
    await waitFor(() => expect(tree.model.focusPath).toHaveBeenCalledWith("pkg/inner/deep/"));
  });

  it("keeps a revealed folder marked when a refresh folds it into a chain row", async () => {
    const queryClient = testQueryClient();
    cacheStaleListing(queryClient, "", "", [
      cachedEntry("aaa", "directory"),
      cachedEntry("pkg", "directory"),
      cachedEntry("other", "directory"),
    ]);
    serveListings({
      "": [
        { name: "aaa", path: "aaa", type: "directory" },
        { name: "pkg", path: "pkg", type: "directory", leaf_directory_path: "pkg/inner" },
        { name: "other", path: "other", type: "directory" },
      ],
    });

    renderTree("", "pkg", queryClient);

    await screen.findByTestId("file-tree");
    await waitFor(() => expect(tree.real?.getFocusedPath()).toBe("pkg/"));
    expect(tree.options?.initialSelectedPaths).toEqual(["pkg/"]);
    await waitFor(() => expect(tree.real?.getSelectedPaths()).toEqual(["pkg/inner/"]));
    const visible = tree.real?.getVisibleRows(0, 10) ?? [];
    expect(visible.filter((row) => row.isSelected).map((row) => row.path)).toEqual(["pkg/inner/"]);
    await waitFor(() => expect(tree.model.focusPath).toHaveBeenLastCalledWith("pkg/inner/"));
  });

  it("leaves a selection the reader moved when a refresh folds the revealed folder", async () => {
    const queryClient = testQueryClient();
    cacheStaleListing(queryClient, "", "", [
      cachedEntry("pkg", "directory"),
      cachedEntry("other", "directory"),
    ]);
    let release = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    serveListings({
      "": [
        { name: "pkg", path: "pkg", type: "directory", leaf_directory_path: "pkg/inner" },
        { name: "other", path: "other", type: "directory" },
      ],
    });
    const serve = vi.mocked(authenticatedFetch).getMockImplementation();
    vi.mocked(authenticatedFetch).mockImplementation(async (...args) => {
      await gate;
      return serve!(...args);
    });

    renderTree("", "pkg", queryClient);
    await screen.findByTestId("file-tree");
    // The reader arrows to another row before the refresh lands.
    fireEvent.keyDown(screen.getByTestId("file-tree"), { key: "ArrowDown" });
    act(() => {
      tree.real?.getItem("pkg/")?.deselect();
      tree.real?.getItem("other/")?.select();
      tree.real?.focusPath("other/");
    });
    vi.mocked(tree.model.focusPath as () => void).mockClear();
    release();

    await waitFor(() => expect(tree.real?.getItem("pkg/inner/")).not.toBeNull());
    expect(tree.real?.getSelectedPaths()).toEqual(["other/"]);
    expect(tree.model.focusPath).not.toHaveBeenCalledWith("pkg/inner/");
  });

  it("opens a chosen file, but not a folder or the file already open", async () => {
    serveListings(SRC_LISTINGS);
    const { onOpenFile } = renderTree("src", "shell/viewer.ts");
    await screen.findByTestId("file-tree");

    tree.options?.onSelectionChange(["lib/"]);
    tree.options?.onSelectionChange(["shell/viewer.ts"]);
    expect(onOpenFile).not.toHaveBeenCalled();

    tree.options?.onSelectionChange(["app.ts"]);
    expect(onOpenFile).toHaveBeenCalledWith("app.ts");
  });

  it("loads a folder's contents the first time it expands", async () => {
    serveListings(SRC_LISTINGS);
    renderTree("src", "shell/viewer.ts");
    await screen.findByTestId("file-tree");
    const listedFolders = () => vi.mocked(authenticatedFetch).mock.calls.length;
    const before = listedFolders();

    tree.rows = [{ kind: "directory", path: "lib/", isExpanded: true }];
    act(() => tree.listeners.forEach((listener) => listener()));

    await waitFor(() =>
      expect(tree.model.batch).toHaveBeenCalledWith([{ type: "add", path: "lib/util.ts" }]),
    );
    act(() => tree.listeners.forEach((listener) => listener()));
    expect(listedFolders()).toBe(before + 1);
  });

  it("offers a retry when an expanded folder fails to load", async () => {
    serveListings({ ...SRC_LISTINGS, "src/lib": new Error("offline") });
    renderTree("src", "shell/viewer.ts");
    await screen.findByTestId("file-tree");

    tree.rows = [{ kind: "directory", path: "lib/", isExpanded: true }];
    act(() => tree.listeners.forEach((listener) => listener()));

    expect(await screen.findByText("Couldn’t load lib.")).toBeInTheDocument();
    serveListings(SRC_LISTINGS);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() =>
      expect(tree.model.batch).toHaveBeenCalledWith([{ type: "add", path: "lib/util.ts" }]),
    );
    expect(screen.queryByText("Couldn’t load lib.")).toBeNull();
  });

  it("explains a folder that couldn't load and retries it", async () => {
    serveListings({ "src/shell": SRC_LISTINGS["src/shell"] });
    renderTree("src", "shell/viewer.ts");

    expect(await screen.findByText("Couldn’t load this folder.")).toBeInTheDocument();
    serveListings(SRC_LISTINGS);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByTestId("file-tree")).toBeInTheDocument();
  });

  it("says when a folder is empty", async () => {
    serveListings({ "": [] });
    renderTree("", "");

    expect(await screen.findByText("This folder is empty.")).toBeInTheDocument();
    expect(vi.mocked(authenticatedFetch).mock.calls[0]?.[0]).toMatch(/\/filesystem\/?\?/);
  });

  it("applies a refreshed listing to the open tree, adding and removing entries", async () => {
    // The popover reopens on a listing cached long enough ago to be refetched.
    const queryClient = testQueryClient();
    cacheStaleListing(queryClient, "src", "", [
      cachedEntry("shell", "directory"),
      cachedEntry("lib", "directory"),
      cachedEntry("app.ts", "file"),
    ]);
    cacheStaleListing(queryClient, "src", "shell", [cachedEntry("shell/viewer.ts", "file")]);
    serveListings({
      ...SRC_LISTINGS,
      src: [
        { name: "shell", path: "src/shell", type: "directory" },
        { name: "app.ts", path: "src/app.ts", type: "file" },
        { name: "main.ts", path: "src/main.ts", type: "file" },
      ],
    });

    renderTree("src", "shell/viewer.ts", queryClient);

    // The cached listing shows at once, then the refetch reaches the same tree.
    expect(await screen.findByTestId("file-tree")).toBeInTheDocument();
    expect(tree.options?.paths).toEqual(["shell/", "lib/", "app.ts", "shell/viewer.ts"]);
    await waitFor(() =>
      expect(tree.model.batch).toHaveBeenCalledWith([
        { type: "remove", path: "lib/", recursive: true },
        { type: "add", path: "main.ts" },
      ]),
    );
  });

  it("keeps the shown tree when a refresh of its listing fails", async () => {
    const queryClient = testQueryClient();
    cacheStaleListing(queryClient, "src", "", [
      cachedEntry("shell", "directory"),
      cachedEntry("app.ts", "file"),
    ]);
    cacheStaleListing(queryClient, "src", "shell", [cachedEntry("shell/viewer.ts", "file")]);
    // Every refetch answers 503.
    serveListings({});

    renderTree("src", "shell/viewer.ts", queryClient);

    expect(await screen.findByTestId("file-tree")).toBeInTheDocument();
    await waitFor(() => expect(authenticatedFetch).toHaveBeenCalled());
    await waitFor(() =>
      expect(
        queryClient.getQueryState(workspaceDirectoryQuery("conv_1", "", "src").queryKey)?.status,
      ).toBe("error"),
    );
    expect(screen.getByTestId("file-tree")).toBeInTheDocument();
    expect(screen.queryByText("Couldn’t load this folder.")).toBeNull();
  });
});

describe("WorkspaceFolderTree refreshing a single-folder chain", () => {
  // `pkg` held only `inner` when the root was cached, so its listing named the chain.
  function cacheRootWithChain(queryClient: QueryClient) {
    cacheStaleListing(queryClient, "", "", [
      cachedEntry("pkg", "directory", "pkg/inner"),
      cachedEntry("README.md", "file"),
    ]);
  }

  it("keeps the chain when its hint goes away, and lists the folder to find what it gained", async () => {
    const queryClient = testQueryClient();
    cacheRootWithChain(queryClient);
    serveListings({
      "": [
        { name: "pkg", path: "pkg", type: "directory" },
        { name: "README.md", path: "README.md", type: "file" },
      ],
      pkg: [
        { name: "inner", path: "pkg/inner", type: "directory" },
        { name: "b.ts", path: "pkg/b.ts", type: "file" },
      ],
    });

    renderTree("", "README.md", queryClient);

    await waitFor(() =>
      expect(tree.model.batch).toHaveBeenCalledWith([{ type: "add", path: "pkg/b.ts" }]),
    );
    const removals = vi
      .mocked(tree.model.batch as (operations: { type: string }[]) => void)
      .mock.calls.flatMap(([operations]) => operations)
      .filter((operation) => operation.type === "remove");
    expect(removals).toEqual([]);
    expect(tree.real?.getItem("pkg/inner/")).not.toBeNull();
  });

  it("removes a deleted chain in one change the tree accepts", async () => {
    const queryClient = testQueryClient();
    cacheRootWithChain(queryClient);
    serveListings({ "": [{ name: "README.md", path: "README.md", type: "file" }] });

    renderTree("", "README.md", queryClient);

    await waitFor(() =>
      expect(tree.model.batch).toHaveBeenCalledWith([
        { type: "remove", path: "pkg/", recursive: true },
      ]),
    );
    expect(tree.model.batch).toHaveBeenCalledTimes(1);
    expect(tree.real?.getItem("pkg/")).toBeNull();
    expect(screen.getByTestId("file-tree")).toBeInTheDocument();
  });
});

describe("WorkspaceFolderTree in the breadcrumb popover", () => {
  it("keeps keyboard focus in the popover when the folder fails to load, and retries", async () => {
    serveListings({});
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={testQueryClient()}>
        <TooltipProvider>
          <FileBreadcrumbs
            conversationId="conv_1"
            path="src/shell/viewer.ts"
            workspaceName="repo"
            onOpenFile={vi.fn()}
          />
        </TooltipProvider>
      </QueryClientProvider>,
    );

    await user.click(screen.getByRole("button", { name: "shell" }));
    const popover = await screen.findByRole("dialog", { name: "Browse src" });
    await within(popover).findByText("Couldn’t load this folder.");
    expect(popover.contains(document.activeElement)).toBe(true);

    await user.tab();
    expect(within(popover).getByRole("button", { name: "Close" })).toHaveFocus();
    await user.tab();
    const retry = within(popover).getByRole("button", { name: "Retry" });
    expect(retry).toHaveFocus();

    serveListings(SRC_LISTINGS);
    await user.keyboard("{Enter}");
    expect(await within(popover).findByTestId("file-tree")).toBeInTheDocument();
    expect(popover).toBeInTheDocument();
    expect(tree.options?.initialSelectedPaths).toEqual(["shell/"]);
  });

  it("lists a folder segment's siblings with that folder selected, and opens a sibling file", async () => {
    serveListings(SRC_LISTINGS);
    const onOpenFile = vi.fn();
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={testQueryClient()}>
        <TooltipProvider>
          <FileBreadcrumbs
            conversationId="conv_1"
            path="src/shell/viewer.ts"
            workspaceName="repo"
            onOpenFile={onOpenFile}
          />
        </TooltipProvider>
      </QueryClientProvider>,
    );

    await user.click(screen.getByRole("button", { name: "shell" }));

    const popover = await screen.findByRole("dialog", { name: "Browse src" });
    expect(await within(popover).findByTestId("file-tree")).toHaveAttribute(
      "aria-label",
      "Files in src",
    );
    expect(tree.options?.paths).toEqual(["shell/", "lib/", "app.ts"]);
    expect(tree.options?.initialSelectedPaths).toEqual(["shell/"]);
    act(() => tree.options?.onSelectionChange(["app.ts"]));
    expect(onOpenFile).toHaveBeenCalledWith("src/app.ts");
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
