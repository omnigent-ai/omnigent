import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type * as ConversationsModule from "@/hooks/useConversations";
import type { ProjectUsage } from "@/lib/projectsApi";
import {
  ProjectBoardSearch,
  ProjectHomeHeaderActions,
  ProjectHomeTitle,
  ProjectNewSessionButton,
  useProjectBoardSearchStore,
  useProjectComposerStore,
} from "./ProjectHomeHeader";

const mocks = vi.hoisted(() => ({
  isMobile: false,
  menuArgs: [] as unknown[],
  usage: undefined as ProjectUsage | undefined,
  usageProjectIds: [] as (string | null)[],
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
    useProjectUsage: (projectId: string | null) => {
      mocks.usageProjectIds.push(projectId);
      return { data: mocks.usage };
    },
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
  const location = useLocation();
  return <output data-testid="location">{location.pathname + location.search}</output>;
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
  mocks.usage = undefined;
  mocks.usageProjectIds = [];
  useProjectComposerStore.setState({ open: false });
  useProjectBoardSearchStore.setState({ query: "" });
  vi.clearAllMocks();
});

afterEach(cleanup);

describe("ProjectHomeTitle", () => {
  function renderTitle() {
    return render(
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

  it("shows the project's cost beside its name, including a priced $0.00", () => {
    mocks.usage = { project_id: "project-1", total_cost_usd: 12.3, total_tokens: 4200 };
    const { unmount } = renderTitle();
    expect(mocks.usageProjectIds).toContain("project-1");
    expect(screen.getByTestId("project-home-cost")).toHaveTextContent("$12.30");
    expect(screen.getByLabelText("Project cost $12.30")).toBeInTheDocument();
    unmount();

    mocks.usage = { project_id: "project-1", total_cost_usd: 0 };
    renderTitle();
    expect(screen.getByTestId("project-home-cost")).toHaveTextContent("$0.00");
  });

  it("shows no cost while loading or when nothing in the project is priced", () => {
    const { unmount } = renderTitle();
    expect(screen.queryByTestId("project-home-cost")).toBeNull();
    unmount();

    mocks.usage = { project_id: "project-1", input_tokens: 900, total_tokens: 900 };
    renderTitle();
    expect(screen.queryByTestId("project-home-cost")).toBeNull();
  });

  it("hides the title and moves the project actions beside New session on phones", () => {
    mocks.isMobile = true;
    render(
      <MemoryRouter initialEntries={["/projects/Payments"]}>
        <TooltipProvider>
          <header>
            <ProjectHomeTitle projectName="Payments" />
          </header>
          <nav aria-label="Header actions">
            <ProjectHomeHeaderActions projectName="Payments" />
          </nav>
        </TooltipProvider>
      </MemoryRouter>,
    );
    expect(screen.getByRole("heading", { name: "Payments" })).toHaveClass("sr-only");
    // Phones show no project cost, so they never poll for it.
    expect(mocks.usageProjectIds).toEqual([]);
    expect(screen.getByRole("banner").querySelectorAll("button")).toHaveLength(0);
    const actions = screen.getByRole("navigation", { name: "Header actions" });
    expect(
      [...actions.querySelectorAll("button")].map((button) => button.getAttribute("aria-label")),
    ).toEqual(["New session", "Project settings", "Project actions for Payments"]);
  });
});

describe("ProjectBoardSearch", () => {
  it("shares its text with the board, clears it, and is left off phones", () => {
    const { unmount } = render(<ProjectBoardSearch />);
    fireEvent.change(screen.getByRole("searchbox", { name: "Filter sessions" }), {
      target: { value: "login" },
    });
    expect(useProjectBoardSearchStore.getState().query).toBe("login");
    fireEvent.click(screen.getByRole("button", { name: "Clear filter" }));
    expect(useProjectBoardSearchStore.getState().query).toBe("");
    expect(screen.queryByRole("button", { name: "Clear filter" })).toBeNull();
    unmount();

    mocks.isMobile = true;
    render(<ProjectBoardSearch />);
    expect(screen.queryByRole("searchbox")).toBeNull();
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

  it("sends phones to the full-screen project composer", () => {
    mocks.isMobile = true;
    render(
      <MemoryRouter initialEntries={["/projects/Payments"]}>
        <ProjectNewSessionButton projectName="Payments" />
        <LocationProbe />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: "New session" }));
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/\?project=Payments$/);
  });
});
