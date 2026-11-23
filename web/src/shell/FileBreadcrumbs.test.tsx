import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { FileBreadcrumbs } from "./FileBreadcrumbs";
import { FileViewerContext } from "./FileViewerContext";
import { rememberBrowseLocation, rememberedBrowseLocation } from "./filesPane/browseLocations";

const { copyText } = vi.hoisted(() => ({ copyText: vi.fn(() => Promise.resolve()) }));
vi.mock("@/lib/clipboard", () => ({ copyText }));

// The real tree is a virtualized web component; this stand-in shows what it
// was asked to render and lets a test choose a file.
vi.mock("./WorkspaceFolderTree", () => ({
  WorkspaceFolderTree: (props: {
    folder: string;
    revealPath: string;
    label: string;
    onOpenFile: (path: string) => void;
  }) => (
    <div data-testid="folder-tree" data-folder={props.folder} data-reveal={props.revealPath}>
      {props.label}
      <button type="button" onClick={() => props.onOpenFile("other.ts")}>
        Choose other.ts
      </button>
    </div>
  ),
}));

afterEach(() => {
  cleanup();
  // The tree's root is kept per session, so a test that roots it can't leak into the next.
  rememberBrowseLocation("conv_1", null);
});

function renderBreadcrumbs(path: string, { canOpen = true } = {}) {
  const onOpenFile = vi.fn();
  render(
    <TooltipProvider>
      <FileBreadcrumbs
        conversationId="conv_1"
        path={path}
        workspaceName="repo"
        onOpenFile={canOpen ? onOpenFile : undefined}
      />
    </TooltipProvider>,
  );
  return { onOpenFile, breadcrumbs: screen.getByRole("navigation", { name: "File path" }) };
}

function popover() {
  return screen.getByRole("dialog");
}

describe("FileBreadcrumbs", () => {
  it.each([
    ["Macterm", "", "repo", "Macterm"],
    ["App", "Macterm", "Macterm", "App"],
    ["AppState.swift", "Macterm/App", "Macterm/App", "AppState.swift"],
  ])(
    "lists the siblings of %s with it selected",
    (segment, expectedFolder, expectedTitle, expectedReveal) => {
      const { breadcrumbs } = renderBreadcrumbs("Macterm/App/AppState.swift");
      const trigger = within(breadcrumbs).getByRole("button", { name: segment });
      expect(trigger).toHaveAttribute("title", `Show items beside ${segment}`);

      fireEvent.click(trigger);

      expect(
        within(popover()).getByText(expectedTitle, { selector: "[data-slot=popover-title]" }),
      ).toBeTruthy();
      const tree = screen.getByTestId("folder-tree");
      expect(tree).toHaveAttribute("data-folder", expectedFolder);
      expect(tree).toHaveAttribute("data-reveal", expectedReveal);
    },
  );

  it.each([
    ["tmp", "/", "tmp"],
    ["notes.md", "/tmp", "notes.md"],
  ])("lists an absolute path's %s beside its siblings", (segment, expectedFolder, reveal) => {
    const { breadcrumbs } = renderBreadcrumbs("/tmp/notes.md");

    fireEvent.click(within(breadcrumbs).getByRole("button", { name: segment }));

    const tree = screen.getByTestId("folder-tree");
    expect(tree).toHaveAttribute("data-folder", expectedFolder);
    expect(tree).toHaveAttribute("data-reveal", reveal);
  });

  it.each([
    ["docs", "", "docs"],
    ["sub", "docs", "sub"],
    ["notes.md", "docs/sub", "notes.md"],
  ])("lists a Windows path's %s beside its siblings", (segment, expectedFolder, expectedReveal) => {
    const { breadcrumbs } = renderBreadcrumbs("docs\\sub\\notes.md");

    fireEvent.click(within(breadcrumbs).getByRole("button", { name: segment }));

    const tree = screen.getByTestId("folder-tree");
    expect(tree).toHaveAttribute("data-folder", expectedFolder);
    expect(tree).toHaveAttribute("data-reveal", expectedReveal);
  });

  it("titles a root-level file's siblings with the workspace name", () => {
    const { breadcrumbs } = renderBreadcrumbs("README.md");

    fireEvent.click(within(breadcrumbs).getByRole("button", { name: "README.md" }));

    expect(
      within(popover()).getByText("repo", { selector: "[data-slot=popover-title]" }),
    ).toBeTruthy();
    expect(screen.getByTestId("folder-tree")).toHaveAttribute("data-folder", "");
  });

  it("opens a chosen file by its full path and closes the tree", () => {
    const { breadcrumbs, onOpenFile } = renderBreadcrumbs("src/shell/viewer.ts");
    fireEvent.click(within(breadcrumbs).getByRole("button", { name: "shell" }));

    fireEvent.click(screen.getByRole("button", { name: "Choose other.ts" }));

    expect(onOpenFile).toHaveBeenCalledWith("src/other.ts");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("closes the tree from its close button and from Escape", () => {
    const { breadcrumbs } = renderBreadcrumbs("src/shell/viewer.ts");
    const shell = within(breadcrumbs).getByRole("button", { name: "shell" });

    fireEvent.click(shell);
    fireEvent.click(within(popover()).getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();

    fireEvent.click(shell);
    fireEvent.keyDown(popover(), { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("shows every folder of a long path, each opening its siblings", () => {
    const { breadcrumbs } = renderBreadcrumbs("a/b/c/d/e/file.ts");
    const labels = within(breadcrumbs)
      .getAllByRole("button")
      .map((button) => button.textContent);
    expect(labels).toEqual(["a", "b", "c", "d", "e", "file.ts", ""]);
    expect(within(breadcrumbs).getByRole("button", { name: "file.ts" })).toHaveAttribute(
      "aria-current",
      "page",
    );

    fireEvent.click(within(breadcrumbs).getByRole("button", { name: "c" }));

    expect(screen.getByTestId("folder-tree")).toHaveAttribute("data-folder", "a/b");
    expect(screen.getByTestId("folder-tree")).toHaveAttribute("data-reveal", "c");
  });

  it("closes an open tree when the viewer moves to another file", () => {
    const onOpenFile = vi.fn();
    const view = (path: string, conversationId = "conv_1") => (
      <TooltipProvider>
        <FileBreadcrumbs
          conversationId={conversationId}
          path={path}
          workspaceName="repo"
          onOpenFile={onOpenFile}
        />
      </TooltipProvider>
    );
    const { rerender } = render(view("src/shell/viewer.ts"));
    fireEvent.click(screen.getByRole("button", { name: "src" }));
    expect(screen.getByRole("dialog")).toBeTruthy();

    // The same folder segment stays in the next path, but its tree belongs to the old file.
    rerender(view("src/other.ts"));
    expect(screen.queryByRole("dialog")).toBeNull();

    // Returning to the file it was opened from doesn't bring the old tree back.
    rerender(view("src/shell/viewer.ts"));
    expect(screen.queryByRole("dialog")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "src" }));
    rerender(view("src/shell/viewer.ts", "conv_2"));
    expect(screen.queryByRole("dialog")).toBeNull();
    rerender(view("src/shell/viewer.ts"));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("scrolls a new file's name into view when it replaces one of the same width", () => {
    // A 100px strip whose file name sits at 120–180px of its content, as the paths
    // below lay out the same; nothing resizes when one replaces the other.
    const scroll = { left: 0 };
    const rects = vi
      .spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockImplementation(function (this: HTMLElement) {
        if (this.hasAttribute("data-tab-strip-viewport")) return new DOMRect(0, 0, 100, 20);
        if (this.getAttribute("aria-current") === "page") {
          return new DOMRect(120 - scroll.left, 0, 60, 20);
        }
        return new DOMRect(0, 0, 0, 0);
      });
    const view = (path: string) => (
      <TooltipProvider>
        <FileBreadcrumbs conversationId="conv_1" path={path} workspaceName="repo" />
      </TooltipProvider>
    );
    try {
      const { rerender } = render(view("src/file-a.ts"));
      const strip = document.querySelector<HTMLElement>("[data-tab-strip-viewport]")!;
      Object.defineProperty(strip, "clientWidth", { configurable: true, value: 100 });
      Object.defineProperty(strip, "scrollLeft", {
        configurable: true,
        get: () => scroll.left,
        set: (value: number) => {
          scroll.left = value;
        },
      });
      // The reader scrolled back to the folders.
      scroll.left = 0;

      rerender(view("src/file-b.ts"));

      expect(scroll.left).toBe(80);
      // An unrelated render leaves the reader's scroll alone.
      scroll.left = 0;
      rerender(view("src/file-b.ts"));
      expect(scroll.left).toBe(0);
    } finally {
      rects.mockRestore();
    }
  });

  it.each([
    ["a drive", "C:\\Users\\dev\\notes.txt", ["C:", "Users", "dev"]],
    ["a network share", "\\\\server\\share\\notes.txt", ["server", "share"]],
  ])(
    "shows plain segments for a file on %s, whose folders it can't list",
    (_case, path, folders) => {
      const { breadcrumbs } = renderBreadcrumbs(path);

      for (const folder of folders) {
        expect(within(breadcrumbs).getByText(folder).tagName).toBe("SPAN");
      }
      expect(within(breadcrumbs).getByText("notes.txt")).toHaveAttribute("aria-current", "page");
      expect(within(breadcrumbs).queryByRole("button", { name: "Users" })).toBeNull();
    },
  );

  it("shows plain segments when the viewer can't open other files", () => {
    const { breadcrumbs } = renderBreadcrumbs("src/viewer.ts", { canOpen: false });

    expect(within(breadcrumbs).queryByRole("button", { name: "src" })).toBeNull();
    expect(within(breadcrumbs).getByText("viewer.ts")).toHaveAttribute("aria-current", "page");
    expect(
      within(breadcrumbs).getByRole("button", { name: "Copy relative path: viewer.ts" }),
    ).toBeTruthy();
  });
});

describe("FileBreadcrumbs copy button", () => {
  function renderInWorkspace(path: string) {
    const context = repoViewerContext();
    render(
      <FileViewerContext.Provider value={context}>
        <TooltipProvider>
          <FileBreadcrumbs conversationId="conv_1" path={path} workspaceName="repo" />
        </TooltipProvider>
      </FileViewerContext.Provider>,
    );
  }

  it.each(["src/shell/viewer.ts", "/repo/src/shell/viewer.ts"])(
    "copies the relative path of %s",
    async (path) => {
      renderInWorkspace(path);

      fireEvent.click(screen.getByRole("button", { name: "Copy relative path: viewer.ts" }));

      await waitFor(() => expect(copyText).toHaveBeenLastCalledWith("src/shell/viewer.ts"));
    },
  );

  it("says it copies the full path of a file outside the workspace", async () => {
    renderInWorkspace("/etc/hosts");

    fireEvent.click(screen.getByRole("button", { name: "Copy path: hosts" }));

    await waitFor(() => expect(copyText).toHaveBeenLastCalledWith("/etc/hosts"));
  });
});

describe("FileBreadcrumbs segment menu", () => {
  function renderInWorkspace(path: string) {
    const context = repoViewerContext();
    // The menu's file actions read the session's host and code links through queries.
    render(
      <QueryClientProvider client={new QueryClient()}>
        <FileViewerContext.Provider value={context}>
          <TooltipProvider>
            <FileBreadcrumbs
              conversationId="conv_1"
              path={path}
              workspaceName="repo"
              onOpenFile={vi.fn()}
            />
          </TooltipProvider>
        </FileViewerContext.Provider>
      </QueryClientProvider>,
    );
    return screen.getByRole("navigation", { name: "File path" });
  }
  const menuItems = () => screen.getAllByRole("menuitem").map((item) => item.textContent);

  it.each(["src", "viewer.ts"])("offers the %s segment its file actions", (segment) => {
    const breadcrumbs = renderInWorkspace("src/viewer.ts");
    fireEvent.contextMenu(within(breadcrumbs).getByRole("button", { name: segment }));
    expect(menuItems()).toEqual(expect.arrayContaining(["Copy path", "Copy relative path"]));
  });

  it("roots the Files tree at a folder segment, but not at the folder it already shows", () => {
    const breadcrumbs = renderInWorkspace("src/lib/viewer.ts");
    fireEvent.contextMenu(within(breadcrumbs).getByRole("button", { name: "lib" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Open folder as root" }));
    expect(rememberedBrowseLocation("conv_1")).toBe("/repo/src/lib");
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
    fireEvent.contextMenu(within(breadcrumbs).getByRole("button", { name: "lib" }));
    expect(menuItems()).not.toContain("Open folder as root");
  });

  it("offers no root for a file segment", () => {
    const breadcrumbs = renderInWorkspace("src/viewer.ts");
    fireEvent.contextMenu(within(breadcrumbs).getByRole("button", { name: "viewer.ts" }));
    expect(menuItems()).not.toContain("Open folder as root");
  });
});

/** The viewer context of a session whose workspace is at /repo. */
function repoViewerContext() {
  return {
    openFile: vi.fn(),
    registerNavigationGuard: () => () => {},
    openGithubTab: vi.fn(),
    isChangedPath: () => false,
    conversationId: "conv_1",
    workspaceRoot: "/repo",
    workspaceHome: null,
    sessionHostId: "this-mac",
  };
}
