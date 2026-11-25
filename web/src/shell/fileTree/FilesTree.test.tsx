import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  LISTING_ENTRY_LIMIT,
  type WorkspaceFile,
  type WorkspaceListing,
} from "@/hooks/useWorkspaceChangedFiles";
import { isMacPlatform } from "@/lib/hotkeys";
import type * as IdentityModule from "@/lib/identity";
import { authenticatedFetch } from "@/lib/identity";
import type * as WorkspaceApiModule from "@/lib/workspaceApi";

import { useFileActionStore } from "../fileCommands/fileActionStore";
import { pathToCopy } from "../fileCommands/target";
import { FileViewerContext } from "../FileViewerContext";
import { FilesTree, type FilesTreeProps, type FilesTreeReveal } from "./FilesTree";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  authenticatedFetch: vi.fn(),
}));
vi.mock("@/lib/workspaceApi", async (importOriginal) => ({
  ...(await importOriginal<typeof WorkspaceApiModule>()),
  listDirectoryViaWorkspaceApi: vi.fn(async () => null),
}));

/**
 * Answers folder listings by the folder named in the request URL, a page of `limit` entries
 * `after` the given one as the endpoint does, and records which folders were asked.
 */
function serveListings(listings: Record<string, WorkspaceFile[]>): string[] {
  const asked: string[] = [];
  vi.mocked(authenticatedFetch).mockImplementation(async (input: RequestInfo | URL) => {
    const { pathname, searchParams } = new URL(String(input), "http://localhost");
    const folder = decodeURIComponent(pathname.split("/filesystem")[1]?.replace(/^\//, "") ?? "");
    asked.push(folder);
    const listing = listings[folder];
    if (!listing) return new Response("unavailable", { status: 503 });
    const after = searchParams.get("after");
    const start = after === null ? 0 : listing.findIndex((entry) => entry.path === after) + 1;
    const page = listing.slice(start, start + Number(searchParams.get("limit") ?? listing.length));
    const data = page.map((entry) => ({ id: entry.path, ...entry }));
    const hasMore = start + page.length < listing.length;
    return new Response(
      JSON.stringify({ object: "list", data, has_more: hasMore, last_id: page.at(-1)?.path }),
    );
  });
  return asked;
}

const folder = (path: string): WorkspaceFile => ({
  path,
  name: path.split("/").at(-1)!,
  type: "directory",
  bytes: null,
  modified_at: null,
});

const file = (path: string): WorkspaceFile => ({
  path,
  name: path.split("/").at(-1)!,
  type: "file",
  bytes: 10,
  modified_at: null,
});

function renderTree(
  rootEntries: readonly WorkspaceFile[],
  onFileSelect = vi.fn(),
  extra: Partial<FilesTreeProps> = {},
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const props = (
    entries: readonly WorkspaceFile[],
    reveal: FilesTreeReveal | null = null,
  ): FilesTreeProps => ({
    conversationId: "conv_tree",
    browseLocation: "",
    rootEntries: entries,
    changedFiles: [],
    sort: "alpha",
    showHidden: false,
    showIgnored: true,
    refreshToken: 0,
    onFileSelect,
    reveal,
    label: "Workspace files",
    ...extra,
  });
  const view = render(<FilesTree {...props(rootEntries)} />, { wrapper });
  return {
    onFileSelect,
    populate: (entries: readonly WorkspaceFile[]) =>
      view.rerender(<FilesTree {...props(entries)} />),
    /** Ask, with the listings unchanged, to reveal a folder. */
    reveal: (entries: readonly WorkspaceFile[], reveal: FilesTreeReveal) =>
      view.rerender(<FilesTree {...props(entries, reveal)} />),
  };
}

afterEach(cleanup);

/** The viewer context of a session whose workspace is at /repo. */
const REPO_VIEWER = {
  openFile: vi.fn(),
  openGithubTab: vi.fn(),
  isChangedPath: () => false,
  conversationId: "conv_tree",
  workspaceRoot: "/repo",
  workspaceHome: null,
};

describe("FilesTree rooted at a folder", () => {
  it("copies a row's path inside the folder, not the workspace root", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <FileViewerContext.Provider value={REPO_VIEWER}>
          <FilesTree
            conversationId="conv_tree"
            browseLocation="src"
            rootEntries={[file("a.ts")]}
            changedFiles={[]}
            sort="alpha"
            showHidden={false}
            showIgnored
            refreshToken={0}
            onFileSelect={vi.fn()}
            reveal={null}
            label="Workspace files"
          />
        </FileViewerContext.Provider>
      </QueryClientProvider>,
    );

    fireEvent.contextMenu(await screen.findByRole("treeitem", { name: /a\.ts/ }));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Copy path" }));

    // The app's file action host copies what the request's target names.
    const [request] = useFileActionStore.getState().pending;
    expect(request?.command).toBe("copy-path");
    expect(pathToCopy(request!.target)?.text).toBe("/repo/src/a.ts");
  });
});

describe("FilesTree", () => {
  it("opens a file in a folder that was empty when the tree first showed it", async () => {
    const tree = renderTree([]);
    expect(screen.getByText("This folder is empty.")).toBeInTheDocument();

    await act(async () => tree.populate([file("README.md")]));
    const row = await screen.findByRole("treeitem", { name: /README\.md/ });
    fireEvent.click(row);

    expect(tree.onFileSelect.mock.calls.map(([path]) => path)).toEqual(["README.md"]);
  });

  it("marks the files and folders Git ignores as ignored", async () => {
    renderTree([
      { ...folder("build"), ignored: true },
      { ...file("debug.log"), ignored: true },
      file("main.ts"),
    ]);

    const status = async (name: RegExp) =>
      (await screen.findByRole("treeitem", { name })).getAttribute("data-item-git-status");
    expect(await status(/^build/)).toBe("ignored");
    expect(await status(/debug\.log/)).toBe("ignored");
    expect(await status(/main\.ts/)).toBeNull();
  });

  it("opens a file in a new tab on ⌘-click (Ctrl-click off macOS) or a middle click, as browsers do", async () => {
    const tree = renderTree([file("README.md"), file("main.ts")]);
    const command = isMacPlatform() ? { metaKey: true } : { ctrlKey: true };
    fireEvent.click(await screen.findByRole("treeitem", { name: /README\.md/ }), command);
    fireEvent(
      await screen.findByRole("treeitem", { name: /main\.ts/ }),
      new MouseEvent("auxclick", { bubbles: true, cancelable: true, composed: true, button: 1 }),
    );

    expect(tree.onFileSelect.mock.calls).toEqual([
      ["README.md", { newTab: true }],
      ["main.ts", { newTab: true }],
    ]);
  });

  it("opens an Option-click here, like a plain click", async () => {
    const tree = renderTree([file("README.md")]);
    fireEvent.click(await screen.findByRole("treeitem", { name: /README\.md/ }), { altKey: true });

    expect(tree.onFileSelect.mock.calls).toEqual([["README.md"]]);
  });

  it.each([
    ["a file", "artifact.log"],
    ["a file in a folder", "build/out.o"],
  ])("says when its listing leaves out %s it was asked to reveal", async (_case, target) => {
    const onActiveRevealMissed = vi.fn();
    renderTree([file("README.md"), folder("src")], vi.fn(), {
      activeFile: target,
      activeReveal: { key: 1, file: target },
      onActiveRevealMissed,
    });
    await waitFor(() => expect(onActiveRevealMissed).toHaveBeenCalledExactlyOnceWith(target));
  });

  it("reveals a folder asked for after it mounted, and again for a new request for the same folder", async () => {
    const entries = [folder("docs"), file("README.md")];
    const tree = renderTree(entries);
    const docs = await screen.findByRole("treeitem", { name: /docs/ });
    expect(docs).not.toHaveFocus();

    tree.reveal(entries, { path: "docs", key: 1 });
    await waitFor(() => expect(screen.getByRole("treeitem", { name: /docs/ })).toHaveFocus());

    (document.activeElement as HTMLElement).blur();
    tree.reveal(entries, { path: "docs", key: 2 });
    await waitFor(() => expect(screen.getByRole("treeitem", { name: /docs/ })).toHaveFocus());
  });

  it("lists every entry of an opened folder longer than one page", async () => {
    const names = Array.from({ length: 1500 }, (_, i) => `big/f${String(i).padStart(4, "0")}.txt`);
    const asked = serveListings({ big: names.map(file) });
    renderTree([folder("big")]);
    fireEvent.click(await screen.findByRole("treeitem", { name: "big" }));

    // Rows are virtualized, so the first row's set size says how many the folder holds.
    await waitFor(() =>
      expect(screen.getByRole("treeitem", { name: "f0000.txt" })).toHaveAttribute(
        "aria-setsize",
        "1500",
      ),
    );
    // Two pages of the endpoint's 1,000, the second after the first's last entry.
    expect(asked).toEqual(["big", "big"]);
  });

  it("says when a folder's listing stopped with more entries past it", async () => {
    const entries = Array.from({ length: LISTING_ENTRY_LIMIT }, (_, i) => file(`f${i}.txt`));
    const cutOff: WorkspaceListing = Object.assign(entries, { cutOff: true as const });
    renderTree(cutOff);
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Showing the first 50,000 entries of this folder.",
    );
  });

  it("names a nested folder that stopped short only while it's open", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(async () =>
      // The host says more lie past these two, but gives no cursor to read them by.
      Response.json({
        object: "list",
        data: [file("partial/a.txt"), file("partial/b.txt")].map((entry) => ({
          id: entry.path,
          ...entry,
        })),
        has_more: true,
        last_id: null,
      }),
    );
    // A folder no other case opens: the tree remembers open folders per session.
    renderTree([folder("partial"), file("README.md")]);
    const partial = await screen.findByRole("treeitem", { name: "partial" });

    fireEvent.click(partial);
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Showing the first 2 entries of partial.",
    );
    fireEvent.click(screen.getByRole("treeitem", { name: "partial" }));
    await waitFor(() => expect(screen.queryByRole("status")).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole("treeitem", { name: "partial" }));
    expect(await screen.findByRole("status")).toHaveTextContent("of partial.");
  });

  it("lists a closed folder whose chain row a refreshed listing stops describing", async () => {
    const asked = serveListings({ chained: [folder("chained/inner"), file("chained/b.ts")] });
    // The host first describes `chained` as a chain ending at `chained/inner`.
    const tree = renderTree([
      { ...folder("chained"), leaf_directory_path: "chained/inner" },
      file("README.md"),
    ]);
    expect(await screen.findByRole("treeitem", { name: /inner/ })).toBeInTheDocument();
    expect(asked).toEqual([]);

    // A new file in it ends the chain: the root's listing no longer describes it.
    tree.populate([folder("chained"), file("README.md")]);
    await waitFor(() => expect(asked).toEqual(["chained"]));
    expect(await screen.findByRole("treeitem", { name: "chained" })).toBeInTheDocument();
  });

  it("says nothing of a complete listing as long as the entry limit", async () => {
    // A host that ignores the page size can list a whole folder of this size in one go.
    const entries = Array.from({ length: LISTING_ENTRY_LIMIT }, (_, i) => file(`f${i}.txt`));
    renderTree(entries);
    await screen.findByRole("treeitem", { name: "f0.txt" });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
