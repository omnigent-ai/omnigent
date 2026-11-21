import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";
import { isMacPlatform } from "@/lib/hotkeys";

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

function renderTree(rootEntries: readonly WorkspaceFile[], onFileSelect = vi.fn()) {
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
    refreshToken: 0,
    onFileSelect,
    reveal,
    label: "Workspace files",
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

describe("FilesTree", () => {
  it("opens a file in a folder that was empty when the tree first showed it", async () => {
    const tree = renderTree([]);
    expect(screen.getByText("This folder is empty.")).toBeInTheDocument();

    await act(async () => tree.populate([file("README.md")]));
    const row = await screen.findByRole("treeitem", { name: /README\.md/ });
    fireEvent.click(row);

    expect(tree.onFileSelect.mock.calls.map(([path]) => path)).toEqual(["README.md"]);
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
