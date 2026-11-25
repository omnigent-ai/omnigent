import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";
import { isMacPlatform } from "@/lib/hotkeys";

import { useFileActionStore } from "../fileCommands/fileActionStore";
import { pathToCopy } from "../fileCommands/target";
import { FileViewerContext } from "../FileViewerContext";
import { FilesTree, type FilesTreeProps, type FilesTreeReveal } from "./FilesTree";

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
});
