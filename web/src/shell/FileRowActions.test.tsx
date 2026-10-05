import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { copyTextMock, downloadMock, revealMock, toastSuccessMock } = vi.hoisted(() => ({
  copyTextMock: vi.fn(() => Promise.resolve()),
  downloadMock: vi.fn(() => Promise.resolve()),
  revealMock: vi.fn(),
  toastSuccessMock: vi.fn(),
}));

vi.mock("@/lib/clipboard", () => ({ copyText: copyTextMock }));
vi.mock("@/hooks/useFileContent", () => ({ downloadWorkspaceFile: downloadMock }));
vi.mock("sonner", () => ({ toast: { success: toastSuccessMock, error: vi.fn() } }));
vi.mock("./RevealInFileManager", () => ({
  revealInFileManager: revealMock,
  revealLabel: (directory: boolean) => (directory ? "Open in Finder" : "Show in Finder"),
  useRevealTarget: (path: string | null) =>
    path && path !== "no-reveal" ? { hostId: "local", path: `/workspace/${path}` } : null,
}));

import { FileRowActions, FilesPanelFocusContext, type FileRowInfo } from "./FileRowActions";
import { FileInfoDialog } from "./FileInfoDialog";

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

function DrawerHarness({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(true);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, []);

  return (
    <div data-testid="drawer" data-state={open ? "open" : "closed"}>
      {children}
    </div>
  );
}

function renderActions(
  props: Partial<Parameters<typeof FileRowActions>[0]> = {},
  inDrawer = false,
) {
  const onOpenInfo = vi.fn();
  const actions = (
    <FileRowActions {...file} downloadable onOpenInfo={onOpenInfo} {...props}>
      {(moreActions, rowRef, primaryActionRef) => (
        <div ref={rowRef} data-testid="file-row" tabIndex={-1}>
          <button ref={primaryActionRef} type="button">
            Open {props.name ?? file.name}
          </button>
          {moreActions}
        </div>
      )}
    </FileRowActions>
  );
  render(inDrawer ? <DrawerHarness>{actions}</DrawerHarness> : actions);
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
    const contextMenu = await screen.findByRole("menu", {
      name: `More actions for ${file.name}`,
    });
    const contextLabels = menuLabels(contextMenu);
    expect(contextLabels).toEqual([
      "Download",
      "Copy relative path",
      "Show in Finder",
      "File info",
    ]);
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
      "Copy relative path",
      "Open in Finder",
      "Folder info",
    ]);
    await user.click(within(menu).getByRole("menuitem", { name: "Browse folder" }));
    expect(onBrowse).toHaveBeenCalledOnce();
    expect(downloadMock).not.toHaveBeenCalled();
  });

  it("returns focus to the Files panel when Browse unmounts its folder row", async () => {
    const user = userEvent.setup();
    function RerootingFolderHarness() {
      const [browsingChild, setBrowsingChild] = useState(false);
      const panelRef = useRef<HTMLDivElement>(null);
      return (
        <FilesPanelFocusContext.Provider value={panelRef}>
          <div ref={panelRef} role="region" aria-label="Files" tabIndex={-1}>
            {!browsingChild && (
              <FileRowActions
                name="folder"
                path="folder"
                kind="folder"
                revealPath="no-reveal"
                onBrowse={() => setBrowsingChild(true)}
                onOpenInfo={vi.fn()}
              >
                {(moreActions, rowRef) => (
                  <div ref={rowRef} data-testid="browsed-folder-row">
                    <button type="button">folder</button>
                    {moreActions}
                  </div>
                )}
              </FileRowActions>
            )}
            {browsingChild && <div>Child folder contents</div>}
          </div>
        </FilesPanelFocusContext.Provider>
      );
    }

    render(<RerootingFolderHarness />);
    await user.click(screen.getByRole("button", { name: "More actions for folder" }));
    await user.click(await screen.findByRole("menuitem", { name: "Browse folder" }));

    expect(screen.queryByTestId("browsed-folder-row")).not.toBeInTheDocument();
    expect(screen.getByText("Child folder contents")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Files" })).toHaveFocus();
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
      "Copy relative path",
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
    expect(menuLabels(menu)).toEqual(["Download", "Copy absolute path", "File info"]);
    await user.click(within(menu).getByRole("menuitem", { name: "Copy absolute path" }));
    expect(copyTextMock).toHaveBeenCalledWith("/tmp/duplicate #.txt");
    expect(toastSuccessMock).toHaveBeenCalledWith("Copied to clipboard.");

    fireEvent.contextMenu(screen.getByTestId("file-row"));
    await user.click(await screen.findByRole("menuitem", { name: "Download" }));
    expect(downloadMock).toHaveBeenCalledWith("session-1", "/tmp/duplicate #.txt");
  });

  it.each(["context menu", "kebab"])(
    "keeps the drawer open when Escape closes the %s",
    async (entry) => {
      const user = userEvent.setup();
      renderActions({}, true);
      const row = screen.getByTestId("file-row");
      const kebab = screen.getByRole("button", { name: `More actions for ${file.name}` });

      if (entry === "context menu") {
        row.focus();
        fireEvent.contextMenu(row);
      } else {
        kebab.focus();
        await user.keyboard("{Enter}");
      }
      expect(await screen.findByRole("menu")).toBeInTheDocument();
      await user.keyboard("{Escape}");

      expect(screen.getByTestId("drawer")).toHaveAttribute("data-state", "open");
      expect(
        entry === "context menu"
          ? screen.getByRole("button", { name: `Open ${file.name}` })
          : kebab,
      ).toHaveFocus();
    },
  );

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

  it("opens Info without a request and returns its focus target", async () => {
    const user = userEvent.setup();
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const { onOpenInfo } = renderActions();
    const row = screen.getByTestId("file-row");
    const primary = screen.getByRole("button", { name: `Open ${file.name}` });
    primary.focus();
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
      primary,
    );
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("passes the kebab as the Info focus target when opened from its menu", async () => {
    const user = userEvent.setup();
    const { onOpenInfo } = renderActions();
    const kebab = screen.getByRole("button", { name: `More actions for ${file.name}` });

    await user.click(kebab);
    await user.click(await screen.findByRole("menuitem", { name: "File info" }));

    expect(onOpenInfo.mock.calls[0]?.[1]).toBe(kebab);
  });

  it("returns focus to the kebab when a deleted row primary action is disabled", async () => {
    const user = userEvent.setup();
    function DeletedRowHarness() {
      const [info, setInfo] = useState<FileRowInfo | null>(null);
      const [returnFocus, setReturnFocus] = useState<HTMLElement | null>(null);
      return (
        <>
          <FileRowActions
            {...file}
            isDeleted
            downloadable={false}
            revealPath={null}
            onOpenInfo={(nextInfo, target) => {
              setInfo(nextInfo);
              setReturnFocus(target);
            }}
          >
            {(moreActions, rowRef, primaryActionRef) => (
              <div ref={rowRef} data-testid="deleted-row">
                <button ref={primaryActionRef} type="button" disabled>
                  Open {file.name}
                </button>
                {moreActions}
              </div>
            )}
          </FileRowActions>
          <FileInfoDialog
            info={info}
            onOpenChange={(open) => {
              if (!open) setInfo(null);
            }}
            returnFocus={returnFocus}
          />
        </>
      );
    }

    render(<DeletedRowHarness />);
    const kebab = screen.getByRole("button", { name: `More actions for ${file.name}` });
    fireEvent.contextMenu(screen.getByTestId("deleted-row"));
    await user.click(await screen.findByRole("menuitem", { name: "File info (last known)" }));
    await user.click(await screen.findByRole("button", { name: "Close" }));

    expect(kebab).toHaveFocus();
  });

  it("returns context-menu focus to the active in-row control", async () => {
    const user = userEvent.setup();
    renderActions();
    const kebab = screen.getByRole("button", { name: `More actions for ${file.name}` });
    kebab.focus();
    fireEvent.contextMenu(kebab);

    expect(await screen.findByRole("menu")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(kebab).toHaveFocus();
  });

  it("omits Browse folder when no browse action is provided", async () => {
    renderActions({ kind: "folder", name: "src", path: "src", revealPath: null });

    fireEvent.contextMenu(screen.getByTestId("file-row"));

    expect(await screen.findByRole("menuitem", { name: "Folder info" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Browse folder" })).not.toBeInTheDocument();
  });
});
