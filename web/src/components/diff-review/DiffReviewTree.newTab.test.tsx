import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { CopyIcon } from "lucide-react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { OWNS_COMMAND_ENTER_ATTRIBUTE } from "@/hooks/useApproveHotkey";
import { isMacPlatform } from "@/lib/hotkeys";
import { findTreeRow, treeRowPaths, treeRows } from "@/test/fileTree";

import type { DiffReviewFileAction } from "./diffReviewFileAction";
import { DiffReviewTree } from "./DiffReviewTree";
import type { DiffReviewFile } from "./diffReviewModel";

const FIRST = "src/app.ts";
const SECOND = "src/lib/util.ts";

function file(path: string): DiffReviewFile {
  return { path, change: "modified", additions: 1, deletions: 1 } as DiffReviewFile;
}

const modifier = () => (isMacPlatform() ? { metaKey: true } : { ctrlKey: true });

function renderTree(
  extra: {
    onOpenInNewTab?: (path: string, options: { background: boolean }) => void;
    rowActions?: (path: string) => readonly DiffReviewFileAction[];
    folderActions?: (path: string) => readonly DiffReviewFileAction[];
    appearance?: "default" | "reference";
    files?: readonly DiffReviewFile[];
  } = {},
) {
  const navigate = vi.fn();
  const { files = [file(FIRST), file(SECOND)], ...rest } = extra;
  render(
    <DiffReviewTree
      files={files}
      navigate={navigate}
      searchQuery={null}
      setSearchQuery={() => undefined}
      searchFocusRequest={0}
      dark={false}
      {...rest}
    />,
  );
  return { navigate };
}

/** A key on a row, composed so it reaches the tree's host as a real keypress does. */
function pressOn(row: HTMLElement, init: KeyboardEventInit): KeyboardEvent {
  const event = new KeyboardEvent("keydown", {
    bubbles: true,
    cancelable: true,
    composed: true,
    ...init,
  });
  row.dispatchEvent(event);
  return event;
}

function middleClick(row: HTMLElement, init: MouseEventInit = {}): MouseEvent {
  const event = new MouseEvent("auxclick", {
    bubbles: true,
    cancelable: true,
    composed: true,
    button: 1,
    ...init,
  });
  row.dispatchEvent(event);
  return event;
}

/** The row with keyboard focus, if any. */
const focusedRowPath = () => {
  const active = document.activeElement;
  return active instanceof HTMLElement && active.closest("[data-file-tree]")
    ? active.dataset.itemPath
    : undefined;
};

const selectedPaths = () =>
  treeRows()
    .filter((row) => row.getAttribute("aria-selected") === "true")
    .map((row) => row.getAttribute("data-item-path"));

afterEach(cleanup);

describe("DiffReviewTree new tab", () => {
  it("opens a file row behind on a middle click, and switches to it with Shift", async () => {
    const onOpenInNewTab = vi.fn();
    renderTree({ onOpenInNewTab });
    const row = await findTreeRow(SECOND);

    expect(middleClick(row).defaultPrevented).toBe(true);
    middleClick(row, { shiftKey: true });

    expect(onOpenInNewTab.mock.calls).toEqual([
      [SECOND, { background: true }],
      [SECOND, { background: false }],
    ]);
  });

  it("opens a focused file row on ⌘Enter and ⇧⌘Enter, consuming the key", async () => {
    const onOpenInNewTab = vi.fn();
    renderTree({ onOpenInNewTab });
    const row = await findTreeRow(FIRST);

    expect(pressOn(row, { key: "Enter", ...modifier() }).defaultPrevented).toBe(true);
    pressOn(row, { key: "Enter", shiftKey: true, ...modifier() });
    // A plain Enter is the tree's own.
    expect(pressOn(row, { key: "Enter" }).defaultPrevented).toBe(false);

    expect(onOpenInNewTab.mock.calls).toEqual([
      [FIRST, { background: true }],
      [FIRST, { background: false }],
    ]);
  });

  it("leaves folder rows alone", async () => {
    const onOpenInNewTab = vi.fn();
    renderTree({ onOpenInNewTab });
    const folder = treeRows().find((row) => row.getAttribute("data-item-path")?.endsWith("/"));
    if (!folder) throw new Error("expected a folder row");
    middleClick(folder);
    pressOn(folder, { key: "Enter", ...modifier() });
    fireEvent.click(folder, modifier());
    expect(onOpenInNewTab).not.toHaveBeenCalled();
  });

  it("opens a file row behind on ⌘-click (Ctrl-click off macOS), and switches to it with Shift", async () => {
    const onOpenInNewTab = vi.fn();
    const { navigate } = renderTree({ onOpenInNewTab });
    fireEvent.click(await findTreeRow(FIRST));
    await waitFor(() => expect(navigate).toHaveBeenCalledTimes(1));

    expect(fireEvent.click(await findTreeRow(SECOND), modifier())).toBe(false);
    fireEvent.click(await findTreeRow(SECOND), { shiftKey: true, ...modifier() });
    await act(async () => {});

    expect(onOpenInNewTab.mock.calls).toEqual([
      [SECOND, { background: true }],
      [SECOND, { background: false }],
    ]);
    // The review stays on the file it showed.
    expect(selectedPaths()).toEqual([FIRST]);
    expect(navigate).toHaveBeenCalledTimes(1);
  });

  it("keeps ⌘-click as multi-select where files can't open in tabs", async () => {
    renderTree();
    fireEvent.click(await findTreeRow(FIRST));
    fireEvent.click(await findTreeRow(SECOND), modifier());

    await waitFor(() => expect(selectedPaths().sort()).toEqual([FIRST, SECOND].sort()));
  });

  it("lists the row's menu from its context-menu trigger", async () => {
    const ran: string[] = [];
    renderTree({
      onOpenInNewTab: () => undefined,
      rowActions: (path) => [
        {
          id: "open-in-new-tab",
          label: "Open in new tab",
          icon: CopyIcon,
          run: () => ran.push(`tab ${path}`),
        },
        {
          id: "copy-path",
          label: "Copy path",
          icon: CopyIcon,
          run: () => ran.push(`copy ${path}`),
        },
      ],
    });
    fireEvent.contextMenu(await findTreeRow(SECOND));

    const menu = await screen.findByTestId("diff-review-tree-row-menu");
    const items = [...menu.querySelectorAll('[role="menuitem"]')].map((item) => item.textContent);
    expect(items).toEqual(["Open in new tab", "Copy path"]);
    act(() => fireEvent.click(screen.getByRole("menuitem", { name: "Open in new tab" })));
    expect(ran).toEqual([`tab ${SECOND}`]);
  });

  it("keeps ⌘Enter from a pending prompt and returns focus to its row when the menu closes", async () => {
    const opened: string[] = [];
    renderTree({
      onOpenInNewTab: () => undefined,
      rowActions: (path) => [
        {
          id: "open-in-new-tab",
          label: "Open in new tab",
          icon: CopyIcon,
          run: () => opened.push(path),
        },
      ],
    });
    const row = await findTreeRow(SECOND);
    row.focus();
    pressOn(row, { key: "F10", shiftKey: true });
    const menu = await screen.findByTestId("diff-review-tree-row-menu");
    // Portalled outside the tree, the menu owns the chord itself.
    expect(menu).toHaveAttribute(OWNS_COMMAND_ENTER_ATTRIBUTE);

    fireEvent.keyDown(menu, { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("diff-review-tree-row-menu")).toBeNull());
    await waitFor(() => expect(focusedRowPath()).toBe(SECOND));

    pressOn(row, { key: "F10", shiftKey: true });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Open in new tab" }));
    expect(opened).toEqual([SECOND]);
    await waitFor(() => expect(focusedRowPath()).toBe(SECOND));
  });

  it("offers none of it where the surface has no tabs", async () => {
    renderTree();
    const row = await findTreeRow(SECOND);
    expect(middleClick(row).defaultPrevented).toBe(false);
    expect(pressOn(row, { key: "Enter", ...modifier() }).defaultPrevented).toBe(false);
    fireEvent.contextMenu(row);
    expect(screen.queryByTestId("diff-review-tree-row-menu")).toBeNull();
  });
});

describe("a row menu without tabs, as Changes review has", () => {
  const viewFile = (opened: string[]) => (path: string) => [
    { id: "view-file", label: "View File", icon: CopyIcon, run: () => opened.push(path) },
  ];

  it("lists View File from right-click and the keyboard, with no new-tab gestures", async () => {
    const opened: string[] = [];
    renderTree({ rowActions: viewFile(opened) });
    const row = await findTreeRow(SECOND);
    expect(middleClick(row).defaultPrevented).toBe(false);

    fireEvent.contextMenu(row);
    fireEvent.click(await screen.findByRole("menuitem", { name: "View File" }));
    expect(opened).toEqual([SECOND]);

    row.focus();
    pressOn(row, { key: "F10", shiftKey: true });
    fireEvent.click(await screen.findByRole("menuitem", { name: "View File" }));
    expect(opened).toEqual([SECOND, SECOND]);
  });
});

describe("a folder row's menu", () => {
  const copyAction = (copied: string[]) => (path: string) => [
    { id: "copy-path", label: "Copy path", icon: CopyIcon, run: () => copied.push(path) },
  ];
  const menuItems = async () =>
    [
      ...(await screen.findByTestId("diff-review-tree-row-menu")).querySelectorAll(
        '[role="menuitem"]',
      ),
    ].map((item) => item.textContent);

  it("collapses and expands the folder, and copies its whole flattened path", async () => {
    const copied: string[] = [];
    renderTree({
      files: [file("web/src/shell/a.ts"), file("web/src/shell/b.ts")],
      rowActions: () => [],
      folderActions: copyAction(copied),
    });
    const folder = await findTreeRow("web/src/shell/");

    fireEvent.contextMenu(folder);
    expect(await menuItems()).toEqual(["Collapse folder", "Copy path"]);
    fireEvent.click(screen.getByRole("menuitem", { name: "Collapse folder" }));
    await waitFor(() => expect(treeRowPaths()).toEqual(["web/src/shell/"]));

    fireEvent.contextMenu(await findTreeRow("web/src/shell/"));
    expect(await menuItems()).toEqual(["Expand folder", "Copy path"]);
    fireEvent.click(screen.getByRole("menuitem", { name: "Copy path" }));
    expect(copied).toEqual(["web/src/shell"]);

    fireEvent.contextMenu(await findTreeRow("web/src/shell/"));
    fireEvent.click(await screen.findByRole("menuitem", { name: "Expand folder" }));
    await waitFor(() =>
      expect(treeRowPaths()).toEqual([
        "web/src/shell/",
        "web/src/shell/a.ts",
        "web/src/shell/b.ts",
      ]),
    );
  });

  it("still collapses a folder where the review has no folder actions", async () => {
    renderTree({ rowActions: () => [] });
    fireEvent.contextMenu(await findTreeRow("src/lib/"));
    expect(await menuItems()).toEqual(["Collapse folder"]);
  });

  it("opens a file row's menu from its ⋯ button", async () => {
    renderTree({
      rowActions: () => [{ id: "copy-path", label: "Copy path", icon: CopyIcon, run: () => {} }],
    });
    const row = await findTreeRow(SECOND);
    fireEvent.pointerMove(row);
    const trigger = await screen.findByRole("button", { name: "Actions for util.ts" });
    fireEvent.click(trigger);
    expect(await menuItems()).toEqual(["Copy path"]);
  });
});

describe("the reference appearance", () => {
  it("heads the tree CHANGED FILES and searches with Search…", () => {
    const setSearchQuery = vi.fn();
    const { rerender } = render(
      <DiffReviewTree
        files={[file(FIRST)]}
        navigate={() => undefined}
        searchQuery={null}
        setSearchQuery={setSearchQuery}
        searchFocusRequest={0}
        dark={false}
        appearance="reference"
      />,
    );
    expect(screen.getByText("Changed Files")).toHaveClass("uppercase");
    fireEvent.click(screen.getByRole("button", { name: "Search changed files" }));
    expect(setSearchQuery).toHaveBeenLastCalledWith("");
    rerender(
      <DiffReviewTree
        files={[file(FIRST)]}
        navigate={() => undefined}
        searchQuery=""
        setSearchQuery={setSearchQuery}
        searchFocusRequest={0}
        dark={false}
        appearance="reference"
      />,
    );
    const search = screen.getByRole("textbox", { name: "Filter changed files" });
    expect(search).toHaveAttribute("placeholder", "Search…");
    fireEvent.keyDown(search, { key: "Escape" });
    expect(setSearchQuery).toHaveBeenLastCalledWith(null);
  });

  it("keeps today's header without it", () => {
    renderTree();
    expect(screen.getByText("Changed files")).not.toHaveClass("uppercase");
  });
});

// A pick scrolls the review to the file and flashes its header, so the eye lands on it.
describe("picking a file in the tree", () => {
  it("goes to the file again when its already-selected row is clicked again", async () => {
    const { navigate } = renderTree();
    fireEvent.click(await findTreeRow(FIRST));
    await waitFor(() => expect(navigate).toHaveBeenCalledTimes(1));
    fireEvent.click(await findTreeRow(FIRST));
    await waitFor(() => expect(navigate).toHaveBeenCalledTimes(2));
    expect(navigate.mock.calls).toEqual([[FIRST], [FIRST]]);
    expect(selectedPaths()).toEqual([FIRST]);
  });

  it("goes to each newly picked file once", async () => {
    const { navigate } = renderTree();
    fireEvent.click(await findTreeRow(FIRST));
    fireEvent.click(await findTreeRow(SECOND));
    await waitFor(() => expect(navigate.mock.calls).toEqual([[FIRST], [SECOND]]));
  });

  it("leaves a ⌘-click on the selected row to multi-select, without going to it", async () => {
    const { navigate } = renderTree();
    fireEvent.click(await findTreeRow(FIRST));
    await waitFor(() => expect(navigate).toHaveBeenCalledTimes(1));
    fireEvent.click(await findTreeRow(FIRST), modifier());
    await act(async () => {});
    expect(navigate).toHaveBeenCalledTimes(1);
  });
});
