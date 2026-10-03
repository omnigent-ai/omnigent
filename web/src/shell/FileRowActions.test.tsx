import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const { copyTextMock, downloadMock, revealMock } = vi.hoisted(() => ({
  copyTextMock: vi.fn(() => Promise.resolve()),
  downloadMock: vi.fn(() => Promise.resolve()),
  revealMock: vi.fn(),
}));

vi.mock("@/lib/clipboard", () => ({ copyText: copyTextMock }));
vi.mock("@/hooks/useFileContent", () => ({ downloadWorkspaceFile: downloadMock }));
vi.mock("./RevealInFileManager", () => ({
  revealInFileManager: revealMock,
  revealLabel: (directory: boolean) => (directory ? "Open in Finder" : "Show in Finder"),
  useRevealTarget: (path: string | null) =>
    path && path !== "no-reveal" ? { hostId: "local", path: `/workspace/${path}` } : null,
}));

import { FileRowActions, type FileRowInfo } from "./FileRowActions";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const file = {
  name: "space # % ' Ω.ts",
  path: "src/space # % ' Ω.ts",
  revealPath: "space # % ' Ω.ts",
  kind: "file" as const,
  conversationId: "session-1",
  bytes: 42,
  modifiedAt: 1_700_000_000,
  status: "modified" as const,
  linesAdded: 4,
  linesRemoved: 2,
};

function renderActions(props: Partial<Parameters<typeof FileRowActions>[0]> = {}) {
  const onOpenInfo = vi.fn();
  render(
    <FileRowActions {...file} downloadable onOpenInfo={onOpenInfo} {...props}>
      {(moreActions, rowRef) => (
        <div ref={rowRef} data-testid="file-row" tabIndex={-1}>
          <button type="button">Open {props.name ?? file.name}</button>
          {moreActions}
        </div>
      )}
    </FileRowActions>,
  );
  return { onOpenInfo };
}

function menuLabels(menu: HTMLElement): string[] {
  return within(menu)
    .getAllByRole("menuitem")
    .map((item) => item.textContent?.replace(/\s+/g, " ").trim() ?? "");
}

describe("FileRowActions", () => {
  it("uses the same file actions for right-click and the kebab", async () => {
    const user = userEvent.setup();
    renderActions();
    const row = screen.getByTestId("file-row");

    fireEvent.contextMenu(row);
    const contextMenu = await screen.findByRole("menu");
    const contextLabels = menuLabels(contextMenu);
    expect(contextLabels).toEqual(["Download", "Copy path", "Show in Finder", "File info"]);
    await user.keyboard("{Escape}");

    await user.click(screen.getByRole("button", { name: `More actions for ${file.name}` }));
    expect(menuLabels(await screen.findByRole("menu"))).toEqual(contextLabels);
  });

  it("offers folder browsing and hides download", async () => {
    const user = userEvent.setup();
    const onBrowse = vi.fn();
    renderActions({
      name: "src",
      path: "packages/app/src",
      revealPath: "src",
      kind: "folder",
      onBrowse,
    });

    fireEvent.contextMenu(screen.getByTestId("file-row"));
    const menu = await screen.findByRole("menu");
    expect(menuLabels(menu)).toEqual([
      "Browse folder",
      "Copy path",
      "Open in Finder",
      "Folder info",
    ]);
    await user.click(within(menu).getByRole("menuitem", { name: "Browse folder" }));
    expect(onBrowse).toHaveBeenCalledOnce();
    expect(downloadMock).not.toHaveBeenCalled();
  });

  it("limits last-known Changes rows to copy and Info", async () => {
    renderActions({
      isDeleted: true,
      downloadable: false,
      revealPath: null,
      status: "deleted",
      linesAdded: null,
      linesRemoved: null,
    });

    fireEvent.contextMenu(screen.getByTestId("file-row"));
    expect(menuLabels(await screen.findByRole("menu"))).toEqual([
      "Copy path",
      "File info (last known)",
    ]);
  });

  it("gates Finder actions and uses the canonical path for copy and download", async () => {
    const user = userEvent.setup();
    renderActions({
      path: "/tmp/duplicate #.txt",
      revealPath: "no-reveal",
      name: "duplicate #.txt",
    });

    fireEvent.contextMenu(screen.getByTestId("file-row"));
    const menu = await screen.findByRole("menu");
    expect(menuLabels(menu)).toEqual(["Download", "Copy path", "File info"]);
    await user.click(within(menu).getByRole("menuitem", { name: "Copy path" }));
    expect(copyTextMock).toHaveBeenCalledWith("/tmp/duplicate #.txt");

    fireEvent.contextMenu(screen.getByTestId("file-row"));
    await user.click(await screen.findByRole("menuitem", { name: "Download" }));
    expect(downloadMock).toHaveBeenCalledWith("session-1", "/tmp/duplicate #.txt");
  });

  it.each(["Enter", " "])("opens the kebab with %s and returns focus after Escape", async (key) => {
    const user = userEvent.setup();
    renderActions();
    const kebab = screen.getByRole("button", { name: `More actions for ${file.name}` });
    kebab.focus();
    await user.keyboard(key === " " ? " " : "{Enter}");
    expect(await screen.findByRole("menu")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(kebab).toHaveFocus();
  });

  it("opens Info without a request and returns focus to the row", async () => {
    const user = userEvent.setup();
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const { onOpenInfo } = renderActions();
    const row = screen.getByTestId("file-row");
    row.focus();
    fireEvent.contextMenu(row);
    await user.click(await screen.findByRole("menuitem", { name: "File info" }));

    expect(onOpenInfo).toHaveBeenCalledWith(
      expect.objectContaining<FileRowInfo>({
        name: file.name,
        path: file.path,
        kind: "file",
        bytes: 42,
        modifiedAt: 1_700_000_000,
        status: "modified",
        linesAdded: 4,
        linesRemoved: 2,
      }),
      row,
    );
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
