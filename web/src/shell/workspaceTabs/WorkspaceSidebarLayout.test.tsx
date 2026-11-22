import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as ReviewLayout from "@/components/diff-review/reviewLayout";

import { resetRememberedPaneWidthForTesting } from "@/hooks/useRememberedPaneWidth";
import { readPanelSharePreference, writePanelSizePreference } from "@/lib/panelSizePreferences";

import { FILES_TREE_DEFAULT_WIDTH, FILES_TREE_WIDTH } from "./placeViews/filesTreeWidth";
import {
  WORKSPACE_SIDEBAR_LAYOUT,
  WorkspaceSidebarLayout,
  WorkspaceSidebarToggle,
  useWorkspaceSidebar,
  useWorkspaceSidebarWidth,
} from "./WorkspaceSidebarLayout";

const page = vi.hoisted(() => ({ width: 1000 }));
vi.mock("@/components/diff-review/reviewLayout", async (importOriginal) => ({
  ...(await importOriginal<typeof ReviewLayout>()),
  useElementWidth: () => page.width,
}));

const STORAGE_KEY = "test:workspace-sidebar-visible";

const FOLDER = <button type="button">Folder</button>;

function Page({ sidebar = FOLDER }: { sidebar?: ReactNode }) {
  const state = useWorkspaceSidebar(STORAGE_KEY);
  return (
    <WorkspaceSidebarLayout state={state} sidebarLabel="Workspace files" sidebar={sidebar}>
      <div className={WORKSPACE_SIDEBAR_LAYOUT.page}>
        <div className={WORKSPACE_SIDEBAR_LAYOUT.toolbar}>
          <WorkspaceSidebarToggle state={state} showLabel="Show tree" hideLabel="Hide tree" />
        </div>
      </div>
    </WorkspaceSidebarLayout>
  );
}

function ResizablePage({ fillWhenNarrow = false }: { fillWhenNarrow?: boolean }) {
  const state = useWorkspaceSidebar(STORAGE_KEY, { fillWhenNarrow });
  const resize = useWorkspaceSidebarWidth(state, FILES_TREE_WIDTH);
  return (
    <WorkspaceSidebarLayout
      state={state}
      resize={resize}
      sidebarLabel="Workspace files"
      sidebar={FOLDER}
    >
      <div className={WORKSPACE_SIDEBAR_LAYOUT.page}>
        <div className={WORKSPACE_SIDEBAR_LAYOUT.toolbar}>
          <WorkspaceSidebarToggle state={state} showLabel="Show tree" hideLabel="Hide tree" />
        </div>
      </div>
    </WorkspaceSidebarLayout>
  );
}

beforeEach(() => {
  window.localStorage.clear();
  resetRememberedPaneWidthForTesting(FILES_TREE_WIDTH);
});
afterEach(cleanup);

describe("WorkspaceSidebarToggle", () => {
  it("is a pressed toggle for the docked tree on a wide page", async () => {
    page.width = 1000;
    render(<Page />);
    const toggle = screen.getByRole("button", { name: "Hide tree" });
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    expect(toggle).not.toHaveAttribute("aria-expanded");
    expect(toggle).not.toHaveAttribute("aria-haspopup");

    await userEvent.click(toggle);

    expect(screen.queryByRole("button", { name: "Folder" })).toBeNull();
    expect(screen.getByRole("button", { name: "Show tree" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe("false");
  });

  it("discloses the tree popover on a narrow page and returns focus on Escape", async () => {
    page.width = 500;
    render(<Page />);
    const toggle = screen.getByRole("button", { name: "Show tree" });
    expect(toggle).toHaveAttribute("aria-haspopup", "dialog");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).not.toHaveAttribute("aria-pressed");

    toggle.focus();
    await userEvent.keyboard("{Enter}");

    const popover = screen.getByRole("dialog", { name: "Workspace files" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle).toHaveAttribute("aria-controls", popover.id);

    await userEvent.keyboard("{Escape}");

    expect(screen.queryByRole("dialog", { name: "Workspace files" })).toBeNull();
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(document.activeElement).toBe(toggle);
    // Opening the popover leaves the remembered docked choice alone.
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("starts the popover on the control the sidebar marks, past the ones before it", async () => {
    page.width = 500;
    render(
      <Page
        sidebar={
          <>
            <button type="button">Reveal Active File</button>
            <button type="button" data-sidebar-initial-focus="">
              Go to File
            </button>
          </>
        }
      />,
    );
    screen.getByRole("button", { name: "Show tree" }).focus();
    await userEvent.keyboard("{Enter}");

    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Go to File" }));
  });

  it("starts on the popover's first control when the marked one can't take focus", async () => {
    page.width = 500;
    render(
      <Page
        sidebar={
          <>
            <button type="button">Reveal Active File</button>
            <button type="button" disabled data-sidebar-initial-focus="">
              Go to File
            </button>
          </>
        }
      />,
    );
    screen.getByRole("button", { name: "Show tree" }).focus();
    await userEvent.keyboard("{Enter}");

    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Reveal Active File" }));
  });
});

describe("WorkspaceSidebarLayout resize", () => {
  const handle = () => screen.queryByRole("separator", { name: "Resize the file tree" });

  it("gives a docked sidebar its chosen width and a handle that resizes it", () => {
    page.width = 720;
    render(<ResizablePage />);
    const sidebar = screen.getByRole("button", { name: "Folder" }).parentElement!;
    expect(sidebar).toHaveStyle({ width: `${FILES_TREE_DEFAULT_WIDTH}px` });

    fireEvent.keyDown(handle()!, { key: "ArrowLeft" });
    expect(sidebar).toHaveStyle({ width: `${FILES_TREE_DEFAULT_WIDTH - 16}px` });
  });

  it("widens the docked sidebar with its page, so widening the panel needs no second drag", () => {
    page.width = 720;
    const view = render(<ResizablePage />);
    const sidebar = () => screen.getByRole("button", { name: "Folder" }).parentElement!;
    fireEvent.keyDown(handle()!, { key: "ArrowRight" });
    expect(sidebar()).toHaveStyle({ width: "272px" });

    page.width = 900;
    view.rerender(<ResizablePage />);
    expect(sidebar()).toHaveStyle({ width: "340px" });
    expect(handle()).toHaveAttribute("aria-valuenow", "340");
  });

  it("converts an old pixel width only once the tree docks beside the page", async () => {
    writePanelSizePreference("filesTreeWidthPx", 300);
    resetRememberedPaneWidthForTesting(FILES_TREE_WIDTH);
    page.width = 500;
    const view = render(<ResizablePage />);
    await userEvent.click(screen.getByRole("button", { name: "Show tree" }));
    expect(readPanelSharePreference("filesTreeShare")).toBeNull();
    view.unmount();

    page.width = 1000;
    render(<ResizablePage />);
    const sidebar = screen.getByRole("button", { name: "Folder" }).parentElement!;
    expect(sidebar).toHaveStyle({ width: "300px" });
    expect(readPanelSharePreference("filesTreeShare")).toBe(0.3);
  });

  it("has no handle in the narrow popover or a sidebar filling the page", async () => {
    page.width = 500;
    const view = render(<ResizablePage />);
    await userEvent.click(screen.getByRole("button", { name: "Show tree" }));
    expect(screen.getByRole("dialog", { name: "Workspace files" })).toBeInTheDocument();
    expect(handle()).toBeNull();
    view.unmount();

    render(<ResizablePage fillWhenNarrow />);
    expect(screen.getByRole("button", { name: "Folder" })).toBeInTheDocument();
    expect(handle()).toBeNull();
  });

  it("opens and closes the narrow popover once per click of its toggle", async () => {
    page.width = 500;
    render(<ResizablePage />);
    const toggle = screen.getByRole("button", { name: "Show tree" });
    await userEvent.click(toggle);
    expect(screen.getByRole("dialog", { name: "Workspace files" })).toBeInTheDocument();
    expect(toggle).toHaveAttribute("aria-expanded", "true");

    await userEvent.click(screen.getByRole("button", { name: "Hide tree" }));
    expect(screen.queryByRole("dialog", { name: "Workspace files" })).toBeNull();
  });
});
