import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type * as ConversationsModule from "@/hooks/useConversations";
import {
  ProjectHomeTitle,
  ProjectNewSessionButton,
  useProjectComposerStore,
} from "./ProjectHomeHeader";

const mocks = vi.hoisted(() => ({
  isMobile: false,
  menuArgs: [] as unknown[],
  actions: {
    openRename: vi.fn(),
    openSettings: vi.fn(),
    openDelete: vi.fn(),
    onMenuOpen: vi.fn(),
    onMenuClose: vi.fn(),
    onCloseAutoFocus: vi.fn(),
  },
}));

vi.mock("@/hooks/useIsMobileViewport", () => ({
  useIsMobileViewport: () => mocks.isMobile,
}));

vi.mock("@/hooks/useConversations", async (importOriginal) => {
  const actual = await importOriginal<typeof ConversationsModule>();
  return {
    ...actual,
    useProjects: () => ({ data: [{ id: "project-1", name: "Payments" }], isLoading: false }),
    useProjectConfig: () => ({ data: { icon: "💸" } }),
  };
});

vi.mock("@/components/ProjectIconPicker", () => ({ ProjectLandingIcon: () => null }));

vi.mock("@/shell/Sidebar", () => ({
  useProjectFolderMenu: (...args: unknown[]) => {
    mocks.menuArgs = args;
    return { actions: mocks.actions, dialogs: null };
  },
}));

function LocationProbe() {
  return <output data-testid="location">{useLocation().pathname}</output>;
}

function renderButton() {
  return render(
    <MemoryRouter>
      <ProjectNewSessionButton projectName="Payments" />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mocks.isMobile = false;
  useProjectComposerStore.setState({ open: false });
  vi.clearAllMocks();
});

afterEach(cleanup);

describe("ProjectHomeTitle", () => {
  function renderTitle() {
    render(
      <MemoryRouter initialEntries={["/projects/Payments"]}>
        <TooltipProvider>
          <ProjectHomeTitle projectName="Payments" />
        </TooltipProvider>
        <LocationProbe />
      </MemoryRouter>,
    );
  }

  it("opens project settings and offers only rename and delete in the overflow menu", () => {
    renderTitle();
    fireEvent.click(screen.getByRole("button", { name: "Project settings" }));
    expect(mocks.actions.openSettings).toHaveBeenCalledOnce();

    fireEvent.pointerDown(screen.getByRole("button", { name: "Project actions for Payments" }), {
      button: 0,
    });
    expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toEqual([
      "Rename project",
      "Delete project",
    ]);
    fireEvent.click(screen.getByRole("menuitem", { name: "Delete project" }));
    expect(mocks.actions.openDelete).toHaveBeenCalledOnce();
  });

  it("follows a rename to the new project home and leaves after a delete", () => {
    renderTitle();
    const [, , , onRenamed, onDeleted] = mocks.menuArgs as [
      unknown,
      unknown,
      unknown,
      (oldName: string, newName: string) => void,
      () => void,
    ];

    act(() => onRenamed("Payments", "Billing & Payments"));
    expect(screen.getByTestId("location")).toHaveTextContent("/projects/Billing%20%26%20Payments");

    act(() => onDeleted());
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/$/);
  });
});

describe("ProjectNewSessionButton", () => {
  it("shows and hides the floating composer", () => {
    renderButton();
    const button = screen.getByRole("button", { name: "New session" });
    expect(button).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(button);
    expect(useProjectComposerStore.getState().open).toBe(true);
    expect(button).toHaveAttribute("aria-expanded", "true");

    fireEvent.click(button);
    expect(useProjectComposerStore.getState().open).toBe(false);
  });

  it("links phones to the full-screen project composer", () => {
    mocks.isMobile = true;
    renderButton();
    expect(screen.getByRole("link", { name: "New session" })).toHaveAttribute(
      "href",
      "/?project=Payments",
    );
  });
});
