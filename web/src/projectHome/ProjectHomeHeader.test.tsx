import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import * as ticketsApi from "@/lib/projectTicketsApi";
import type * as ConversationsModule from "@/hooks/useConversations";
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
  projectsLoading: false,
  projectsError: null as Error | null,
  menuArgs: [] as unknown[],
  mandateProposals: [] as { id: string; text: string; created_at: number }[],
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
    useProjects: () => ({
      data: [
        { id: "project-1", name: "Payments" },
        { id: "project-2", name: "Billing & Payments" },
      ],
      isLoading: mocks.projectsLoading,
      error: mocks.projectsError,
      isError: mocks.projectsError !== null,
    }),
    useProjectConfig: () => ({ data: { icon: "💸" } }),
  };
});

vi.mock("@/components/ProjectIconPicker", () => ({ ProjectLandingIcon: () => null }));
vi.mock("./ProjectCost", () => ({ ProjectCostBadge: () => null }));

vi.mock("./ProjectMandateDialog", () => ({
  useProjectMandate: () => ({ data: { mandate_proposals: mocks.mandateProposals } }),
  ProjectMandateDialog: ({ projectName }: { projectName: string }) => (
    <Dialog open>
      <DialogContent aria-describedby={undefined}>
        <DialogTitle>{`${projectName} mandate`}</DialogTitle>
      </DialogContent>
    </Dialog>
  ),
}));

vi.mock("@/lib/projectTicketsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof ticketsApi>()),
  getTicketSummary: vi.fn(),
}));

vi.mock("./ProjectTicketSources", () => ({
  ProjectTicketSources: ({
    projectId,
    projectName,
  }: {
    projectId: string | null;
    projectName: string;
  }) => (
    <section aria-label="Project sources">
      <button type="button" data-project-id={projectId} data-project-name={projectName}>
        Connect source
      </button>
    </section>
  ),
}));

vi.mock("./ProjectTicketStagesDialog", () => ({
  ProjectTicketStagesDialog: ({
    projectName,
    onClose,
    onCloseAutoFocus,
  }: {
    projectName: string;
    onClose: () => void;
    onCloseAutoFocus?: (event: Event) => void;
  }) => (
    <Dialog open>
      <DialogContent onCloseAutoFocus={onCloseAutoFocus}>
        <DialogTitle>Edit {projectName} stages</DialogTitle>
        <DialogDescription>Configure ticket stages.</DialogDescription>
        <button type="button" onClick={onClose}>
          Close editor
        </button>
      </DialogContent>
    </Dialog>
  ),
}));

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
      <TooltipProvider>
        <ProjectNewSessionButton projectName="Payments" />
      </TooltipProvider>
    </MemoryRouter>,
  );
}

const clients: QueryClient[] = [];

function renderNavigation(view: "chat" | "board" = "board", stages = true, summary = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  const rendered = render(
    <QueryClientProvider client={client}>
      <CapabilitiesProvider
        info={{
          ...FALLBACK_SERVER_INFO,
          project_tickets: true,
          project_ticket_stages: stages,
          project_ticket_summary: summary,
        }}
      >
        <MemoryRouter initialEntries={[`/projects/Billing%20%26%20Payments?view=${view}`]}>
          <TooltipProvider>
            <header>
              <ProjectHomeTitle projectName="Billing & Payments" />
              <ProjectHomeHeaderActions projectName="Billing & Payments" />
            </header>
          </TooltipProvider>
          <LocationProbe />
        </MemoryRouter>
      </CapabilitiesProvider>
    </QueryClientProvider>,
  );
  return { ...rendered, client };
}

beforeEach(() => {
  mocks.isMobile = false;
  mocks.mandateProposals = [];
  mocks.projectsLoading = false;
  mocks.projectsError = null;
  useProjectComposerStore.setState({ open: false });
  useProjectBoardSearchStore.setState({ query: "" });
  vi.clearAllMocks();
  vi.mocked(ticketsApi.getTicketSummary).mockResolvedValue({
    needs_response_count: 3,
    is_complete: true,
  });
});

afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
});

describe("ProjectHomeTitle", () => {
  function renderTitle(stagesEnabled = false) {
    render(
      <CapabilitiesProvider
        info={{ ...FALLBACK_SERVER_INFO, project_ticket_stages: stagesEnabled }}
      >
        <MemoryRouter initialEntries={["/projects/Payments"]}>
          <TooltipProvider>
            <ProjectHomeTitle projectName="Payments" />
          </TooltipProvider>
          <LocationProbe />
        </MemoryRouter>
      </CapabilitiesProvider>,
    );
  }

  it("places board actions in the project header without a chat view", () => {
    renderNavigation("board", true, true);
    const header = screen.getByRole("banner");
    expect(screen.queryByRole("tablist")).toBeNull();
    expect(screen.queryByLabelText(/needs? a response/)).toBeNull();
    expect(ticketsApi.getTicketSummary).not.toHaveBeenCalled();
    const actions = within(header).getByRole("group", { name: "Project actions" });
    expect(within(actions).getByRole("link", { name: "Lead" })).toHaveAttribute(
      "href",
      "/projects/Billing%20%26%20Payments/orchestrator",
    );
    expect(within(actions).getByRole("button", { name: "Connect source" })).toBeVisible();
    expect(within(actions).queryByRole("button", { name: "New session" })).toBeNull();
    expect(within(actions).getByRole("link", { name: "New ticket" })).toBeVisible();
  });

  it("keeps all project actions accessible in the phone header", () => {
    mocks.isMobile = true;
    renderNavigation();
    const header = screen.getByRole("banner");
    expect(screen.queryByRole("tablist")).toBeNull();
    // Phones reach the Lead and personas through one Team menu.
    expect(within(header).queryByRole("link", { name: "Lead" })).toBeNull();
    expect(within(header).getByRole("button", { name: "Team" })).toBeVisible();
    expect(within(header).queryByRole("button", { name: "New session" })).toBeNull();
    expect(within(header).getByRole("link", { name: "New ticket" })).toBeVisible();
    expect(
      within(header).getByRole("button", { name: "Project actions for Billing & Payments" }),
    ).toBeVisible();
  });

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

  it("opens the project stage editor from the menu and hands it focus", async () => {
    const user = userEvent.setup();
    renderNavigation();
    const trigger = screen.getByRole("button", { name: "Project actions for Billing & Payments" });
    await user.click(trigger);
    expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toEqual([
      "New session",
      "Edit stages",
      "Rename project",
      "Delete project",
    ]);
    await user.click(screen.getByRole("menuitem", { name: "Edit stages" }));
    expect(
      screen.getByRole("dialog", { name: "Edit Billing & Payments stages" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close editor" })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Close editor" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(trigger).toHaveFocus();
  });

  function renderMandateHeader() {
    const client = new QueryClient();
    clients.push(client);
    render(
      <QueryClientProvider client={client}>
        <CapabilitiesProvider info={{ ...FALLBACK_SERVER_INFO, project_mandate: true }}>
          <MemoryRouter initialEntries={["/projects/Payments"]}>
            <TooltipProvider>
              <ProjectHomeTitle projectName="Payments" />
            </TooltipProvider>
          </MemoryRouter>
        </CapabilitiesProvider>
      </QueryClientProvider>,
    );
  }

  it("offers the mandate editor in the project menu", async () => {
    const user = userEvent.setup();
    renderMandateHeader();
    expect(screen.queryByTestId("project-mandate-proposals-button")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Project actions for Payments" }));
    await user.click(screen.getByRole("menuitem", { name: "Edit mandate" }));
    expect(screen.getByRole("dialog", { name: "Payments mandate" })).toBeInTheDocument();
  });

  it("flags proposed mandate additions waiting for the owner", async () => {
    mocks.mandateProposals = [
      { id: "a", text: "Attach a video.", created_at: 1 },
      { id: "b", text: "Never push.", created_at: 2 },
    ];
    renderMandateHeader();
    await userEvent.click(screen.getByRole("button", { name: "2 proposed mandate additions" }));
    expect(screen.getByRole("dialog", { name: "Payments mandate" })).toBeInTheDocument();
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
  it("makes New ticket the board's primary action and moves New session to the project menu", () => {
    renderNavigation();
    expect(screen.getByRole("link", { name: "Lead" })).toHaveAttribute(
      "href",
      "/projects/Billing%20%26%20Payments/orchestrator",
    );
    const newTicket = screen.getByRole("link", { name: "New ticket" });
    expect(newTicket).toHaveAttribute(
      "href",
      "/projects/Billing%20%26%20Payments?view=board&ticket=new",
    );
    expect(screen.queryByRole("searchbox")).toBeNull();
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(screen.queryByRole("button", { name: "New session" })).toBeNull();
    const source = screen.getByRole("button", { name: "Connect source" });
    expect(source).toHaveAttribute("data-project-id", "project-2");
    expect(source).toHaveAttribute("data-project-name", "Billing & Payments");
    fireEvent.pointerDown(
      screen.getByRole("button", { name: "Project actions for Billing & Payments" }),
      { button: 0 },
    );
    fireEvent.click(screen.getByRole("menuitem", { name: "New session" }));
    expect(screen.getByTestId("location")).toHaveTextContent(
      /^\/\?project=Billing%20%26%20Payments$/,
    );
    expect(useProjectComposerStore.getState().open).toBe(false);
  });

  it.each(["loading", "failed"])("hides sources while project lookup is %s", (state) => {
    mocks.projectsLoading = state === "loading";
    mocks.projectsError = state === "failed" ? new Error("Projects unavailable") : null;
    renderNavigation();
    expect(screen.queryByRole("button", { name: "Connect source" })).toBeNull();
    expect(screen.getByRole("link", { name: "New ticket" })).toBeInTheDocument();
  });

  it("gates stage editing on the server capability", () => {
    renderNavigation("board", false);
    expect(screen.getByRole("link", { name: "New ticket" })).toBeInTheDocument();
    fireEvent.pointerDown(
      screen.getByRole("button", { name: "Project actions for Billing & Payments" }),
      { button: 0 },
    );
    expect(screen.queryByRole("menuitem", { name: "Edit stages" })).toBeNull();
    expect(screen.getByRole("menuitem", { name: "Rename project" })).toBeVisible();
  });

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
        <TooltipProvider>
          <ProjectNewSessionButton projectName="Payments" />
        </TooltipProvider>
        <LocationProbe />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: "New session" }));
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/\?project=Payments$/);
  });
});
