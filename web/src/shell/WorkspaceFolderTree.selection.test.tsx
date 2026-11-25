// The breadcrumb picker's folder tree, on the real tree: a file opens from the click or key
// that chose it, never from a selection change such as select-all or a Shift+Arrow.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type * as IdentityModule from "@/lib/identity";
import { authenticatedFetch } from "@/lib/identity";
import { isMacPlatform } from "@/lib/hotkeys";
import type * as WorkspaceApiModule from "@/lib/workspaceApi";

import { TooltipProvider } from "@/components/ui/tooltip";

import { FileBreadcrumbs } from "./FileBreadcrumbs";
import { WorkspaceFolderTree } from "./WorkspaceFolderTree";

// Listings come over REST here: the workspace API has no route for this session.
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  authenticatedFetch: vi.fn(),
}));
vi.mock("@/lib/workspaceApi", async (importOriginal) => ({
  ...(await importOriginal<typeof WorkspaceApiModule>()),
  listDirectoryViaWorkspaceApi: vi.fn(async () => null),
}));

const SIBLINGS = ["a.ts", "b.ts", "c.ts"];

function serveSiblings(names: () => string[] = () => SIBLINGS) {
  vi.mocked(authenticatedFetch).mockImplementation(async () => {
    const data = names().map((name) => ({
      id: `src/${name}`,
      name,
      path: `src/${name}`,
      type: "file",
      bytes: 1,
      modified_at: null,
    }));
    return new Response(JSON.stringify({ object: "list", data, has_more: false }));
  });
}

function renderPicker() {
  serveSiblings();
  const onOpenFile = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <WorkspaceFolderTree
        conversationId="conv_picker"
        folder="src"
        revealPath="a.ts"
        label="Files in src"
        dark={false}
        onOpenFile={onOpenFile}
      />
    </QueryClientProvider>,
  );
  return { onOpenFile };
}

const row = (name: string) =>
  screen.findByRole("treeitem", { name: new RegExp(`^${name.replace(".", "\\.")}`) });
const selected = () =>
  screen
    .queryAllByRole("treeitem")
    .filter((item) => item.getAttribute("aria-selected") === "true")
    .map((item) => item.getAttribute("data-item-path"));
const command = () => (isMacPlatform() ? { metaKey: true } : { ctrlKey: true });

afterEach(cleanup);

describe("WorkspaceFolderTree selection", () => {
  it("opens nothing on select-all, toggle-select or Shift+Arrow", async () => {
    const { onOpenFile } = renderPicker();
    const first = await row("a.ts");
    await waitFor(() => expect(selected()).toEqual(["a.ts"]));
    first.focus();

    fireEvent.keyDown(first, { key: "a", ...command() });
    fireEvent.keyDown(first, { key: " ", ...command() });
    fireEvent.keyDown(first, { key: "ArrowDown", shiftKey: true });
    fireEvent.keyDown(document.activeElement ?? first, { key: "ArrowDown", shiftKey: true });

    expect(onOpenFile).not.toHaveBeenCalled();
    expect(selected().length).toBeLessThanOrEqual(1);
  });

  it("opens the file a person clicks or presses Enter on, but not the one already open", async () => {
    const { onOpenFile } = renderPicker();
    fireEvent.click(await row("a.ts"));
    expect(onOpenFile).not.toHaveBeenCalled();

    fireEvent.click(await row("b.ts"));
    const third = await row("c.ts");
    third.focus();
    fireEvent.keyDown(third, { key: "Enter" });

    expect(onOpenFile.mock.calls).toEqual([["b.ts"], ["c.ts"]]);
  });

  it("opens files that arrive after the folder first listed empty", async () => {
    let names: string[] = [];
    serveSiblings(() => names);
    const onOpenFile = vi.fn();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <WorkspaceFolderTree
          conversationId="conv_picker"
          folder="src"
          revealPath="a.ts"
          label="Files in src"
          dark={false}
          onOpenFile={onOpenFile}
        />
      </QueryClientProvider>,
    );
    await screen.findByText("This folder is empty.");

    names = SIBLINGS;
    await client.refetchQueries();
    fireEvent.click(await row("b.ts"));
    const third = await row("c.ts");
    third.focus();
    fireEvent.keyDown(third, { key: "Enter" });

    expect(onOpenFile.mock.calls).toEqual([["b.ts"], ["c.ts"]]);
    expect(screen.queryByText("This folder is empty.")).toBeNull();
  });

  it("opens a sibling a person clicks in the breadcrumb popover, and closes it", async () => {
    serveSiblings();
    const onOpenFile = vi.fn();
    const user = userEvent.setup();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <TooltipProvider>
          <FileBreadcrumbs
            conversationId="conv_picker"
            path="src/a.ts"
            workspaceName="repo"
            onOpenFile={onOpenFile}
          />
        </TooltipProvider>
      </QueryClientProvider>,
    );

    await user.click(screen.getByRole("button", { name: "a.ts" }));
    const popover = await screen.findByRole("dialog", { name: "Browse src" });
    fireEvent.click(await within(popover).findByRole("treeitem", { name: /^b\.ts/ }));

    expect(onOpenFile).toHaveBeenCalledWith("src/b.ts");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
});
