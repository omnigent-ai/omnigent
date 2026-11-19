import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProjectTicketBoard } from "./ProjectTicketBoard";
import { ProjectOrchestratorPanel, ProjectTicketPanel } from "./ProjectTicketPanel";
import { CompleteTicketDialog } from "./TicketControls";
import type { ProjectTicket, TicketChanges, TicketEvent } from "@/lib/projectTicketsApi";
import * as api from "@/lib/projectTicketsApi";
import * as projectsApi from "@/lib/projectsApi";

vi.mock("@/hooks/useConversations", () => ({
  useProjects: () => ({
    data: [{ id: "project", name: "Shipping" }],
    isLoading: false,
    error: null,
  }),
}));
vi.mock("@/projectHome/ProjectSessionPanel", () => ({
  ProjectSessionPanel: ({ sessionId }: { sessionId: string }) => (
    <output aria-label="Existing session chat">{sessionId}</output>
  ),
}));
vi.mock("@/lib/projectTicketsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof api>()),
  listTickets: vi.fn(),
  getTicket: vi.fn(),
  createTicket: vi.fn(),
  updateTicket: vi.fn(),
  listTicketEvents: vi.fn(),
  listTicketSessions: vi.fn(),
  commentOnTicket: vi.fn(),
}));
vi.mock("@/lib/projectsApi", () => ({
  getProject: vi.fn(),
  initializeProjectOrchestrator: vi.fn(),
  createProject: vi.fn(),
}));

let ticket: ProjectTicket;
const clients: QueryClient[] = [];
function renderBoard(element: React.ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{element}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  ticket = {
    id: "ticket",
    project_id: "project",
    title: "Ship the board",
    description: "",
    status: "open",
    tags: [{ key: "needs-human", value: "approval" }],
    completion_summary: null,
    latest_update: "Created the ticket",
    comment_count: 0,
    created_at: 1000,
    updated_at: 1000,
  };
  vi.mocked(api.listTickets).mockImplementation(async (_projectId, options) => ({
    data: options?.status === ticket.status ? [ticket] : [],
    has_more: false,
    last_id: ticket.id,
  }));
  vi.mocked(api.getTicket).mockImplementation(async () => ticket);
  vi.mocked(api.createTicket).mockImplementation(async (_id, body) => ({ ...ticket, ...body }));
  vi.mocked(api.updateTicket).mockImplementation(
    async (_projectId, _id, changes: TicketChanges) => {
      ticket = { ...ticket, ...changes };
      return ticket;
    },
  );
  vi.mocked(api.commentOnTicket).mockResolvedValue(ticket);
  vi.mocked(api.listTicketEvents).mockResolvedValue({
    data: [],
    has_more: false,
    last_position: -1,
  });
  vi.mocked(api.listTicketSessions).mockResolvedValue({ data: [], has_more: false, last_id: null });
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
});

describe("Project ticket board", () => {
  it("shows three lanes and human-input tags, without filter controls", async () => {
    renderBoard(<ProjectTicketBoard projectName="Shipping" selectedTicketId={null} />);
    const board = screen.getByRole("region", { name: "Shipping ticket board" });
    for (const label of ["Open", "In progress", "Done"])
      expect(within(board).getByRole("region", { name: label })).toBeInTheDocument();
    expect(
      await screen.findByRole("link", { name: "Open ticket: Ship the board" }),
    ).toHaveAttribute("href", "/?ticket=ticket");
    expect(screen.getByText("Needs human input")).toBeInTheDocument();
    expect(screen.queryByRole("searchbox")).toBeNull();
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("creates a title-only ticket without creating a session", async () => {
    const onCreated = vi.fn();
    renderBoard(
      <ProjectTicketPanel
        projectName="Shipping"
        ticketId="new"
        onClose={vi.fn()}
        onCreated={onCreated}
      />,
    );
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "A small task" } });
    fireEvent.click(screen.getByRole("button", { name: "Create ticket" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("ticket"));
    expect(api.createTicket).toHaveBeenCalledWith("project", {
      title: "A small task",
      description: "",
    });
    expect(projectsApi.initializeProjectOrchestrator).not.toHaveBeenCalled();
  });

  it("requires a summary before completing", () => {
    const complete = vi.fn();
    renderBoard(
      <CompleteTicketDialog
        title="Ship"
        onClose={vi.fn()}
        onComplete={complete}
        pending={false}
        error={null}
      />,
    );
    expect(screen.getByRole("button", { name: "Mark done" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Completion summary"), {
      target: { value: " Delivered and tested " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Mark done" }));
    expect(complete).toHaveBeenCalledWith("Delivered and tested");
  });

  it("deduplicates PR outputs while retaining both events, and appends comments", async () => {
    const url = "https://git.example.com/org/repo/pull/42";
    const event: TicketEvent = {
      id: "event1",
      ticket_id: ticket.id,
      position: 0,
      kind: "output",
      user_id: null,
      session_id: null,
      agent_id: null,
      payload: { type: "pull_request", url },
      created_at: 1000,
    };
    vi.mocked(api.listTicketEvents).mockResolvedValue({
      data: [event, { ...event, id: "event2", position: 1 }],
      has_more: false,
      last_position: 1,
    });
    renderBoard(
      <ProjectTicketPanel
        projectName="Shipping"
        ticketId="ticket"
        onClose={vi.fn()}
        onCreated={vi.fn()}
      />,
    );
    await screen.findByText("Ship the board");
    await waitFor(() => expect(screen.getAllByRole("link", { name: url })).toHaveLength(1));
    expect(screen.getAllByText(`Added pull request: ${url}`)).toHaveLength(2);
    fireEvent.change(screen.getByLabelText("Add a comment"), {
      target: { value: "Ready for review" },
    });
    fireEvent.change(screen.getByLabelText("Output URL"), { target: { value: url } });
    fireEvent.click(screen.getByRole("button", { name: "Comment" }));
    await waitFor(() =>
      expect(api.commentOnTicket).toHaveBeenCalledWith("project", "ticket", {
        text: "Ready for review",
        outputs: [{ type: "pull_request", url }],
      }),
    );
  });

  it("removes the needs-human tag without changing ticket status", async () => {
    renderBoard(
      <ProjectTicketPanel
        projectName="Shipping"
        ticketId="ticket"
        onClose={vi.fn()}
        onCreated={vi.fn()}
      />,
    );
    await screen.findByText("Needs human input");
    fireEvent.click(screen.getByRole("button", { name: "Edit tags" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove tag needs-human" }));
    await waitFor(() => expect(screen.queryByText("Needs human input")).toBeNull());
    expect(api.updateTicket).toHaveBeenCalledWith("project", "ticket", { tags: [] });
    expect(ticket.status).toBe("open");
  });

  it("surfaces load failures instead of showing an empty board", async () => {
    vi.mocked(api.listTickets).mockRejectedValue(new Error("Board unavailable"));
    renderBoard(<ProjectTicketBoard projectName="Shipping" selectedTicketId={null} />);
    await waitFor(() => expect(screen.getAllByRole("alert")).toHaveLength(3));
    expect(screen.queryByText("No tickets")).toBeNull();
  });
});

describe("Project orchestrator", () => {
  it("opens the existing session component without creating another session", async () => {
    vi.mocked(projectsApi.getProject).mockResolvedValue({
      id: "project",
      name: "Shipping",
      orchestrator_session_id: "orchestrator",
    });
    renderBoard(<ProjectOrchestratorPanel projectName="Shipping" onClose={vi.fn()} />);
    expect(await screen.findByLabelText("Existing session chat")).toHaveTextContent("orchestrator");
    expect(projectsApi.initializeProjectOrchestrator).not.toHaveBeenCalled();
  });

  it("initializes an older project only on request and uses the same session component", async () => {
    vi.mocked(projectsApi.getProject).mockResolvedValue({ id: "project", name: "Shipping" });
    vi.mocked(projectsApi.initializeProjectOrchestrator).mockResolvedValue({
      id: "project",
      name: "Shipping",
      orchestrator_session_id: "new-orchestrator",
    });
    renderBoard(<ProjectOrchestratorPanel projectName="Shipping" onClose={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Create orchestrator" }));
    expect(await screen.findByLabelText("Existing session chat")).toHaveTextContent(
      "new-orchestrator",
    );
    expect(projectsApi.initializeProjectOrchestrator).toHaveBeenCalledWith("project");
  });
});
