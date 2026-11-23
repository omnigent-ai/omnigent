import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { Tabs, TabsContent } from "@/components/ui/tabs";
import { ProjectViewNavigation } from "./ProjectViewNavigation";
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
            <Tabs value={view}>
              <ProjectViewNavigation projectName="Billing & Payments" view={view} />
              <TabsContent value={view}>Project {view}</TabsContent>
            </Tabs>
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

describe("Board response count", () => {
  it.each(["chat", "board"] as const)("shows a project-wide badge in %s", async (view) => {
    const { client } = renderNavigation(view, true, true);
    const badge = await screen.findByLabelText("3 tickets need a response");
    expect(badge).toHaveTextContent(/^3$/);
    expect(badge).toHaveClass("rounded-full", "bg-primary-cta");
    expect(ticketsApi.getTicketSummary).toHaveBeenCalledWith("project-2");
    expect(screen.getByRole("tab", { name: /Board/ })).toContainElement(badge);
    expect(screen.getByRole("tab", { name: "Chat" })).not.toContainElement(badge);
    act(() =>
      client.setQueryData(["project-tickets", "project-2", "summary"], {
        needs_response_count: 1,
        is_complete: true,
      }),
    );
    expect(await screen.findByLabelText("1 ticket needs a response")).toHaveTextContent(/^1$/);
    act(() =>
      client.setQueryData(["project-tickets", "project-2", "summary"], {
        needs_response_count: 0,
        is_complete: true,
      }),
    );
    await waitFor(() => expect(screen.queryByLabelText(/needs? a response/)).toBeNull());
    expect(screen.getByRole("tab", { name: "Board" })).toHaveTextContent(/^Board$/);
  });

  it.each([0, 2])("marks an incomplete count of %s explicitly", async (count) => {
    vi.mocked(ticketsApi.getTicketSummary).mockResolvedValue({
      needs_response_count: count,
      is_complete: false,
    });
    renderNavigation("chat", true, true);
    expect(
      await screen.findByLabelText(
        count ? "At least 2 tickets need a response" : "Response count is incomplete",
      ),
    ).toHaveTextContent(count ? "2+" : "+");
  });

  it("does not request or display a count on older servers", async () => {
    const { client } = renderNavigation();
    act(() =>
      client.setQueryData(["project-tickets", "project-2", "summary"], {
        needs_response_count: 3,
        is_complete: true,
      }),
    );
    await act(async () => {});
    expect(ticketsApi.getTicketSummary).not.toHaveBeenCalled();
    expect(screen.getByRole("tab", { name: "Board" })).toHaveTextContent(/^Board$/);
  });

  it("drops a stale count when the summary request fails", async () => {
    const { client } = renderNavigation("board", true, true);
    expect(await screen.findByLabelText("3 tickets need a response")).toBeVisible();
    vi.mocked(ticketsApi.getTicketSummary).mockRejectedValue(new Error("Project unavailable"));
    await act(() => client.invalidateQueries({ queryKey: ["project-tickets", "project-2"] }));
    await waitFor(() => expect(screen.queryByLabelText(/tickets need a response/)).toBeNull());
  });
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

  it("puts the view tabs beside the title and leaves ticket actions on the board", () => {
    renderNavigation();
    const header = screen.getByRole("banner");
    const tablist = within(header).getByRole("tablist", { name: "Project views" });
    const board = within(tablist).getByRole("tab", { name: "Board" });
    expect(tablist).toHaveAttribute("data-variant", "pill");
    expect(screen.getAllByRole("tablist", { name: "Project views" })).toHaveLength(1);
    expect(within(header).getByRole("button", { name: "Board actions" })).toBeVisible();
    expect(within(header).queryByRole("group", { name: "Ticket actions" })).toBeNull();
    expect(screen.getByRole("group", { name: "Ticket actions" })).toBeVisible();
    expect(screen.getByRole("tabpanel")).toHaveAttribute("id", board.getAttribute("aria-controls"));
    expect(screen.getByRole("tabpanel")).toHaveAttribute("aria-labelledby", board.id);
  });

  it("keeps a single accessible view row below the compact phone header", () => {
    mocks.isMobile = true;
    renderNavigation();
    expect(within(screen.getByRole("banner")).queryByRole("tablist")).toBeNull();
    expect(screen.getAllByRole("tablist", { name: "Project views" })).toHaveLength(1);
    expect(screen.getByRole("tab", { name: "Chat" })).toBeVisible();
    expect(screen.getByRole("tab", { name: "Board" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Board actions" })).toBeVisible();
    expect(screen.getByRole("link", { name: "New ticket" })).toBeVisible();
  });

  it("suppresses pointer-click outlines without losing keyboard focus feedback", async () => {
    const user = userEvent.setup();
    renderNavigation("chat");
    const chat = screen.getByRole("tab", { name: "Chat" });
    const board = screen.getByRole("tab", { name: "Board" });
    await user.click(board);
    expect(board).toHaveFocus();
    expect(board).toHaveClass("focus-visible:ring-0", "focus-visible:outline-none");
    await user.keyboard("{ArrowLeft}");
    await waitFor(() => expect(chat).toHaveFocus());
    expect(chat).not.toHaveClass("focus-visible:ring-0");
    await user.click(chat);
    expect(chat).toHaveClass("focus-visible:ring-0");
    await user.click(screen.getByRole("button", { name: "Project settings" }));
    expect(chat).not.toHaveClass("focus-visible:ring-0");
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
    const trigger = screen.getByRole("button", { name: "Board actions" });
    expect(
      within(screen.getByRole("tablist")).queryByRole("button", { name: "Board actions" }),
    ).toBeNull();
    expect(trigger.querySelector(".lucide-ellipsis-vertical")).not.toBeNull();
    await user.click(trigger);
    expect(screen.getAllByRole("menuitem")).toHaveLength(1);
    await user.click(screen.getByRole("menuitem", { name: "Edit stages" }));
    expect(
      screen.getByRole("dialog", { name: "Edit Billing & Payments stages" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close editor" })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Close editor" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(trigger).toHaveFocus();
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
  it("keeps ticket actions simple and sends New session to the full project composer", () => {
    renderNavigation();
    expect(screen.queryByRole("link", { name: "Orchestrator" })).toBeNull();
    expect(screen.getByRole("link", { name: "New ticket" })).toHaveAttribute(
      "href",
      "/projects/Billing%20%26%20Payments?view=board&ticket=new",
    );
    expect(screen.queryByRole("searchbox")).toBeNull();
    expect(screen.queryByRole("combobox")).toBeNull();
    const button = screen.getByRole("button", { name: "New session" });
    const source = screen.getByRole("button", { name: "Connect source" });
    expect(source).toHaveAttribute("data-project-id", "project-2");
    expect(source).toHaveAttribute("data-project-name", "Billing & Payments");
    expect(button.previousElementSibling).toBe(
      screen.getByRole("button", { name: "Project settings" }),
    );
    expect(
      screen.queryByRole("button", { name: "Project actions for Billing & Payments" }),
    ).toBeNull();
    expect(button).not.toHaveAttribute("aria-expanded");
    fireEvent.click(button);
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
    expect(screen.getByRole("button", { name: "New session" })).toBeInTheDocument();
  });

  it("shows no ticket actions in Chat and gates stage editing on the server capability", () => {
    const chat = renderNavigation("chat");
    expect(screen.queryByRole("button", { name: "Board actions" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Connect source" })).toBeNull();
    expect(screen.queryByRole("link", { name: "New ticket" })).toBeNull();
    chat.unmount();
    renderNavigation("board", false);
    expect(screen.queryByRole("button", { name: "Board actions" })).toBeNull();
    expect(screen.getByRole("link", { name: "New ticket" })).toBeInTheDocument();
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
