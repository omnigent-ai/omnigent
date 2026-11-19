import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProjectTicketBoard } from "./ProjectTicketBoard";
import { ProjectOrchestratorPanel, ProjectTicketPanel } from "./ProjectTicketPanel";
import { CompleteTicketDialog } from "./TicketControls";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { fetchConversationById } from "@/hooks/useOpenSessionRow";
import { conversation } from "@/test/sidebarMockHelpers";
import type { ProjectTicket, TicketChanges, TicketEvent } from "@/lib/projectTicketsApi";
import * as api from "@/lib/projectTicketsApi";
import * as projectsApi from "@/lib/projectsApi";
import { Link } from "@/lib/routing";

const capabilities = vi.hoisted(() => ({ lifecycle: true }));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({
    project_tickets: true,
    project_ticket_lifecycle: capabilities.lifecycle,
  }),
}));

vi.mock("@/hooks/useConversations", () => ({
  useProjects: () => ({
    data: [{ id: "project", name: "Shipping" }],
    isLoading: false,
    error: null,
  }),
}));
vi.mock("@/projectHome/ProjectSessionPanel", () => ({
  ProjectSessionPanel: ({
    sessionId,
    ticket,
  }: {
    sessionId: string;
    ticket?: { id: string; title: string };
  }) => (
    <div>
      <output aria-label="Existing session chat">{sessionId}</output>
      {ticket && (
        <Link to={`?ticket=${ticket.id}`} aria-label="Back to ticket">
          {ticket.title}
        </Link>
      )}
    </div>
  ),
}));
vi.mock("@/hooks/useOpenSessionRow", () => ({ fetchConversationById: vi.fn() }));
vi.mock("@/hooks/useIsMobileViewport", () => ({ useIsMobileViewport: vi.fn(() => false) }));
vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => "local" }));
vi.mock("@/lib/projectTicketsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof api>()),
  listTickets: vi.fn(),
  getTicket: vi.fn(),
  createTicket: vi.fn(),
  updateTicket: vi.fn(),
  listTicketEvents: vi.fn(),
  listTicketSessions: vi.fn(),
  commentOnTicket: vi.fn(),
  listRelatedTickets: vi.fn(),
  linkRelatedTicket: vi.fn(),
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
  const rendered = render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <MemoryRouter>{element}</MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
  return { ...rendered, client };
}

beforeEach(() => {
  vi.clearAllMocks();
  capabilities.lifecycle = true;
  vi.mocked(useIsMobileViewport).mockReturnValue(false);
  vi.mocked(fetchConversationById).mockResolvedValue(null);
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
      const { comment: _comment, ...fields } = changes;
      ticket = { ...ticket, ...fields };
      if (changes.status && !["review", "done"].includes(changes.status))
        ticket.review_summary = "";
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
  vi.mocked(api.listRelatedTickets).mockResolvedValue({ data: [], has_more: false, last_id: null });
  vi.mocked(api.linkRelatedTicket).mockResolvedValue(ticket);
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
});

describe("Project ticket board", () => {
  it("shows five phases and human-input tags, without filter controls", async () => {
    renderBoard(<ProjectTicketBoard projectName="Shipping" selectedTicketId={null} />);
    const board = screen.getByRole("region", { name: "Shipping ticket board" });
    for (const label of ["Open", "Planning", "Working", "Review", "Done"])
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
    const outputs = await screen.findByRole("region", { name: "Outputs" });
    expect(within(outputs).getAllByRole("link")).toHaveLength(1);
    expect(within(outputs).getByRole("link", { name: /org\/repo #42/ })).toHaveAttribute(
      "href",
      url,
    );
    const activity = await screen.findByRole("region", { name: "Ticket activity" });
    expect(within(activity).getAllByRole("link", { name: "org/repo #42" })).toHaveLength(2);
    expect(screen.queryByLabelText("Output URL")).toBeNull();
    fireEvent.change(screen.getByLabelText("Add a comment"), {
      target: { value: "Ready for review" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Attach link" }));
    fireEvent.change(screen.getByLabelText("Output URL"), { target: { value: url } });
    fireEvent.click(screen.getByRole("button", { name: "Comment" }));
    await waitFor(() =>
      expect(api.commentOnTicket).toHaveBeenCalledWith("project", "ticket", {
        text: "Ready for review",
        outputs: [{ type: "pull_request", url }],
      }),
    );
    await waitFor(() => expect(screen.getByLabelText("Add a comment")).toHaveValue(""));
    expect(screen.queryByLabelText("Output URL")).toBeNull();
  });

  it("edits key/value tags without changing ticket status", async () => {
    renderBoard(
      <ProjectTicketPanel
        projectName="Shipping"
        ticketId="ticket"
        onClose={vi.fn()}
        onCreated={vi.fn()}
      />,
    );
    await screen.findByText("Needs human input");
    expect(screen.getByText("approval")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Edit tags" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove tag needs-human" }));
    await waitFor(() => expect(screen.queryByText("Needs human input")).toBeNull());
    expect(api.updateTicket).toHaveBeenCalledWith("project", "ticket", { tags: [] });
    fireEvent.change(screen.getByLabelText("Tag key"), { target: { value: "area" } });
    fireEvent.change(screen.getByLabelText("Tag value"), { target: { value: "board" } });
    fireEvent.click(screen.getByRole("button", { name: "Save tag" }));
    await waitFor(() =>
      expect(api.updateTicket).toHaveBeenLastCalledWith("project", "ticket", {
        tags: [{ key: "area", value: "board" }],
      }),
    );
    expect(ticket.status).toBe("open");
  });

  it("renders ticket prose as Markdown and hides empty optional sections", async () => {
    ticket.description = "## Acceptance\n\n- **Readable** descriptions\n- Keep `status` simple";
    ticket.tags = [];
    ticket.completion_summary = "**Delivered** and verified.";
    vi.mocked(api.listTicketEvents).mockImplementation(async (_project, _ticket, _after, kind) => ({
      data: kind
        ? []
        : [
            {
              id: "comment",
              ticket_id: ticket.id,
              position: 0,
              kind: "comment",
              user_id: "local",
              session_id: null,
              agent_id: null,
              payload: { text: "**Reviewed** the result." },
              created_at: 1000,
            },
          ],
      has_more: false,
      last_position: 0,
    }));
    renderBoard(
      <ProjectTicketPanel
        projectName="Shipping"
        ticketId="ticket"
        onClose={vi.fn()}
        onCreated={vi.fn()}
      />,
    );
    expect(
      await screen.findByRole("heading", { name: "Ship the board", level: 2 }),
    ).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Acceptance" })).toBeInTheDocument();
    const emphasized = await Promise.all(
      ["Readable", "Delivered", "Reviewed"].map((word) => screen.findByText(word)),
    );
    for (const element of emphasized) expect(element).toHaveAttribute("data-streamdown", "strong");
    expect(screen.getByText("status")).toHaveProperty("tagName", "CODE");
    expect(screen.getByText("You")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add tags" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Tags" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Outputs" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Sessions" })).toBeNull();
    expect(screen.queryByRole("combobox", { name: "Output type" })).toBeNull();
  });

  it.each([
    { mobile: false, href: "/?ticket=ticket&session=worker" },
    { mobile: true, href: "/?ticket=ticket&session=worker" },
  ])(
    "names workers and actors using one shared lookup per session (mobile=$mobile)",
    async ({ mobile, href }) => {
      vi.mocked(useIsMobileViewport).mockReturnValue(mobile);
      const worker = conversation("worker", { title: "Build ticket panel", status: "running" });
      vi.mocked(fetchConversationById).mockImplementation(async (id) =>
        id === "worker" ? worker : conversation(id, { title: "Shipping orchestrator" }),
      );
      vi.mocked(api.listTicketSessions).mockResolvedValue({
        data: [{ session_id: "worker", created_at: 1000 }],
        has_more: false,
        last_id: "worker",
      });
      const event: TicketEvent = {
        id: "created",
        ticket_id: ticket.id,
        position: 0,
        kind: "created",
        user_id: null,
        session_id: "orchestrator",
        agent_id: "agent-id",
        payload: {},
        created_at: 1000,
      };
      vi.mocked(api.listTicketEvents).mockImplementation(
        async (_project, _ticket, _after, kind) => ({
          data: kind
            ? []
            : [
                event,
                {
                  ...event,
                  id: "linked",
                  position: 1,
                  kind: "updated",
                  payload: { action: "attach", session_id: "worker" },
                },
              ],
          has_more: false,
          last_position: 1,
        }),
      );
      const { client } = renderBoard(
        <ProjectTicketPanel
          projectName="Shipping"
          ticketId="ticket"
          onClose={vi.fn()}
          onCreated={vi.fn()}
        />,
      );
      const sessions = await screen.findByRole("region", { name: "Linked sessions" });
      expect(
        await within(sessions).findByRole("link", { name: /Build ticket panel/ }),
      ).toHaveAttribute("href", href);
      expect(within(sessions).getByRole("img", { name: "Session running" })).toBeInTheDocument();
      expect(within(sessions).getByText("Working")).toBeInTheDocument();
      expect(await screen.findAllByRole("link", { name: "Shipping orchestrator" })).toHaveLength(2);
      expect(screen.queryByText(/Agent agent-id/)).toBeNull();
      expect(
        vi
          .mocked(fetchConversationById)
          .mock.calls.map(([id]) => id)
          .sort(),
      ).toEqual(["orchestrator", "worker"]);
      act(() =>
        client.setQueryData(["conversation-backfill", "worker"], {
          ...worker,
          pending_elicitations_count: 1,
        }),
      );
      expect(await screen.findByText("Needs response")).toBeInTheDocument();
      expect(screen.queryByText("Working")).toBeNull();
    },
  );

  it("keeps session navigation available when metadata cannot be loaded", async () => {
    vi.mocked(fetchConversationById).mockRejectedValue(new Error("Session unavailable"));
    vi.mocked(api.listTicketSessions).mockResolvedValue({
      data: [{ session_id: "worker", created_at: 1000 }],
      has_more: false,
      last_id: "worker",
    });
    renderBoard(
      <ProjectTicketPanel
        projectName="Shipping"
        ticketId="ticket"
        onClose={vi.fn()}
        onCreated={vi.fn()}
      />,
    );
    const sessions = await screen.findByRole("region", { name: "Linked sessions" });
    expect(await within(sessions).findByText("Unavailable")).toBeInTheDocument();
    expect(within(sessions).getByRole("link", { name: /Session worker/ })).toHaveAttribute(
      "href",
      "/?ticket=ticket&session=worker",
    );
    expect(screen.queryByText("Idle")).toBeNull();
  });

  it("preserves a failed comment and lets its attachment be removed before retrying", async () => {
    vi.mocked(api.commentOnTicket).mockRejectedValueOnce(new Error("Comment could not be saved"));
    renderBoard(
      <ProjectTicketPanel
        projectName="Shipping"
        ticketId="ticket"
        onClose={vi.fn()}
        onCreated={vi.fn()}
      />,
    );
    const input = await screen.findByLabelText("Add a comment");
    fireEvent.change(input, { target: { value: "Please review" } });
    fireEvent.click(screen.getByRole("button", { name: "Attach link" }));
    fireEvent.change(screen.getByLabelText("Output URL"), {
      target: { value: "https://example.com/result" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Comment" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Comment could not be saved");
    expect(input).toHaveValue("Please review");
    expect(screen.getByLabelText("Output URL")).toHaveValue("https://example.com/result");
    fireEvent.click(screen.getByRole("button", { name: "Remove attachment" }));
    fireEvent.click(screen.getByRole("button", { name: "Comment" }));
    await waitFor(() =>
      expect(api.commentOnTicket).toHaveBeenLastCalledWith("project", "ticket", {
        text: "Please review",
        outputs: [],
      }),
    );
    await waitFor(() => expect(input).toHaveValue(""));
  });

  it("retains title editing and completion through the compact status control", async () => {
    const user = userEvent.setup();
    renderBoard(
      <ProjectTicketPanel
        projectName="Shipping"
        ticketId="ticket"
        onClose={vi.fn()}
        onCreated={vi.fn()}
      />,
    );
    await user.click(await screen.findByRole("button", { name: "Edit ticket" }));
    fireEvent.change(screen.getByLabelText("Title"), {
      target: { value: "Ship a polished board" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(
      await screen.findByRole("heading", { name: "Ship a polished board", level: 2 }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: "Ticket status" }));
    await user.click(screen.getByRole("option", { name: "Done" }));
    expect(screen.getByRole("button", { name: "Mark done" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Completion summary"), {
      target: { value: "Built and tested" },
    });
    await user.click(screen.getByRole("button", { name: "Mark done" }));
    await waitFor(() =>
      expect(api.updateTicket).toHaveBeenLastCalledWith("project", "ticket", {
        status: "done",
        completion_summary: "Built and tested",
        tags: [],
      }),
    );
    expect(await screen.findByText("Built and tested")).toBeInTheDocument();
  });

  it("keeps earlier activity when loading another page", async () => {
    vi.mocked(api.listTicketEvents).mockImplementation(async (_project, _ticket, after, kind) => ({
      data: kind
        ? []
        : [
            {
              id: after === -1 ? "first" : "second",
              ticket_id: ticket.id,
              position: after === -1 ? 0 : 1,
              kind: "comment",
              user_id: "local",
              session_id: null,
              agent_id: null,
              payload: { text: after === -1 ? "First update" : "Second update" },
              created_at: 1000,
            },
          ],
      has_more: !kind && after === -1,
      last_position: after === -1 ? 0 : 1,
    }));
    renderBoard(
      <ProjectTicketPanel
        projectName="Shipping"
        ticketId="ticket"
        onClose={vi.fn()}
        onCreated={vi.fn()}
      />,
    );
    expect(await screen.findByText("First update")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "More activity" }));
    expect(await screen.findByText("Second update")).toBeInTheDocument();
    expect(screen.getByText("First update")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "More activity" })).toBeNull();
  });

  it("surfaces load failures instead of showing an empty board", async () => {
    vi.mocked(api.listTickets).mockRejectedValue(new Error("Board unavailable"));
    renderBoard(<ProjectTicketBoard projectName="Shipping" selectedTicketId={null} />);
    await waitFor(() => expect(screen.getAllByRole("alert")).toHaveLength(5));
    expect(screen.queryByText("No tickets")).toBeNull();
  });

  it("does not call new lifecycle endpoints against an older backend", async () => {
    capabilities.lifecycle = false;
    renderBoard(<ProjectTicketBoard projectName="Shipping" selectedTicketId={null} />);
    const card = await screen.findByRole("link", { name: "Open ticket: Ship the board" });
    expect(screen.queryByRole("region", { name: "Planning" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Review" })).toBeNull();
    fireEvent.mouseEnter(card);
    expect(api.listRelatedTickets).not.toHaveBeenCalled();
  });

  it("saves the current plan and renders its acceptance criteria as Markdown", async () => {
    ticket.status = "planning";
    renderBoard(
      <ProjectTicketPanel
        projectName="Shipping"
        ticketId="ticket"
        onClose={vi.fn()}
        onCreated={vi.fn()}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Edit plan" }));
    fireEvent.change(screen.getByLabelText("Plan"), {
      target: { value: "## Acceptance\n\n- **Readable** UI" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save plan" }));
    expect(await screen.findByRole("heading", { name: "Acceptance" })).toBeInTheDocument();
    expect(api.updateTicket).toHaveBeenCalledWith("project", "ticket", {
      plan: "## Acceptance\n\n- **Readable** UI",
    });
  });

  it("requires a review brief and returns feedback to Working atomically", async () => {
    const user = userEvent.setup();
    renderBoard(
      <ProjectTicketPanel
        projectName="Shipping"
        ticketId="ticket"
        onClose={vi.fn()}
        onCreated={vi.fn()}
      />,
    );
    await user.click(await screen.findByRole("combobox", { name: "Ticket status" }));
    await user.click(screen.getByRole("option", { name: "Review" }));
    expect(screen.getByRole("button", { name: "Send for review" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Review brief"), {
      target: { value: "**Verified** tests. Please inspect the demo." },
    });
    await user.click(screen.getByRole("button", { name: "Send for review" }));
    expect(await screen.findByRole("region", { name: "Human review" })).toHaveTextContent(
      "Verified",
    );
    await user.click(screen.getByRole("button", { name: "Request changes" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("button", { name: "Request changes" })).toBeDisabled();
    fireEvent.change(within(dialog).getByLabelText("Feedback"), {
      target: { value: "Increase the contrast" },
    });
    await user.click(within(dialog).getByRole("button", { name: "Request changes" }));
    await waitFor(() =>
      expect(api.updateTicket).toHaveBeenLastCalledWith("project", "ticket", {
        status: "in_progress",
        comment: "Increase the contrast",
        tags: [],
      }),
    );
    await waitFor(() => expect(screen.queryByRole("region", { name: "Human review" })).toBeNull());
  });

  it("records human acceptance with a completion summary", async () => {
    ticket.status = "review";
    ticket.review_summary = "Tested and ready; check the demo.";
    renderBoard(
      <ProjectTicketPanel
        projectName="Shipping"
        ticketId="ticket"
        onClose={vi.fn()}
        onCreated={vi.fn()}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Approve & mark done" }));
    const dialog = screen.getByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("Completion summary"), {
      target: { value: "Reviewed the demo and accepted" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Approve & mark done" }));
    await waitFor(() =>
      expect(api.updateTicket).toHaveBeenLastCalledWith("project", "ticket", {
        status: "done",
        completion_summary: "Reviewed the demo and accepted",
        tags: [],
      }),
    );
  });

  it("links and unlinks related tickets by title without changing phase", async () => {
    const peer: ProjectTicket = {
      ...ticket,
      id: "peer",
      title: "Related work",
      status: "planning",
    };
    let related: ProjectTicket[] = [];
    vi.mocked(api.listTickets).mockResolvedValue({
      data: [ticket, peer],
      has_more: false,
      last_id: "peer",
    });
    vi.mocked(api.listRelatedTickets).mockImplementation(async () => ({
      data: related,
      has_more: false,
      last_id: related.at(-1)?.id ?? null,
    }));
    vi.mocked(api.linkRelatedTicket).mockImplementation(
      async (_project, _ticket, _related, action) => {
        related = action === "attach" ? [peer] : [];
        return ticket;
      },
    );
    renderBoard(
      <ProjectTicketPanel
        projectName="Shipping"
        ticketId="ticket"
        onClose={vi.fn()}
        onCreated={vi.fn()}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Link ticket" }));
    fireEvent.click(await screen.findByRole("button", { name: "Related work" }));
    const section = screen.getByRole("region", { name: "Related tickets" });
    expect(
      await within(section).findByRole("link", { name: /Related work.*Planning/ }),
    ).toHaveAttribute("href", "/?ticket=peer");
    fireEvent.click(within(section).getByRole("button", { name: "Unlink Related work" }));
    await waitFor(() => expect(within(section).queryByRole("link")).toBeNull());
    expect(api.linkRelatedTicket).toHaveBeenLastCalledWith("project", "ticket", "peer", "remove");
    expect(api.updateTicket).not.toHaveBeenCalled();
  });

  it.each([false, true])(
    "opens the designated planner chat and keeps ticket drafts (mobile=%s)",
    async (mobile) => {
      vi.mocked(useIsMobileViewport).mockReturnValue(mobile);
      ticket.status = "planning";
      ticket.lead_session_id = "planner";
      ticket.plan = "Original plan";
      vi.mocked(fetchConversationById).mockResolvedValue(
        conversation("planner", { title: "Design the board", status: "idle" }),
      );
      const user = userEvent.setup();
      renderBoard(
        <ProjectTicketPanel
          projectName="Shipping"
          ticketId="ticket"
          onClose={vi.fn()}
          onCreated={vi.fn()}
        />,
      );
      const chat = await screen.findByRole("link", { name: "Discuss plan: Design the board" });
      fireEvent.change(screen.getByLabelText("Add a comment"), {
        target: { value: "Comment draft" },
      });
      await user.click(screen.getByRole("button", { name: "Edit plan" }));
      fireEvent.change(screen.getByLabelText("Plan"), { target: { value: "Plan draft" } });
      await user.click(chat);
      expect(screen.getByLabelText("Existing session chat")).toHaveTextContent("planner");
      expect(screen.queryByRole("textbox", { name: "Add a comment" })).toBeNull();
      await user.click(screen.getByRole("link", { name: "Back to ticket" }));
      expect(screen.getByLabelText("Add a comment")).toHaveValue("Comment draft");
      expect(screen.getByLabelText("Plan")).toHaveValue("Plan draft");
      expect(projectsApi.initializeProjectOrchestrator).not.toHaveBeenCalled();
    },
  );

  it("assigns and clears a linked lead without creating a session", async () => {
    ticket.status = "planning";
    vi.mocked(api.listTicketSessions).mockResolvedValue({
      data: [{ session_id: "planner", created_at: 1000 }],
      has_more: false,
      last_id: "planner",
    });
    vi.mocked(fetchConversationById).mockResolvedValue(
      conversation("planner", { title: "Plan the work" }),
    );
    renderBoard(
      <ProjectTicketPanel
        projectName="Shipping"
        ticketId="ticket"
        onClose={vi.fn()}
        onCreated={vi.fn()}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Make Plan the work lead" }));
    expect(
      await screen.findByRole("link", { name: "Discuss plan: Plan the work" }),
    ).toHaveAttribute("href", "/?ticket=ticket&session=planner");
    fireEvent.click(screen.getByRole("button", { name: "Clear lead agent" }));
    expect(await screen.findByRole("link", { name: "Ask orchestrator" })).toHaveAttribute(
      "href",
      "/?ticket=ticket&orchestrator=",
    );
    expect(api.updateTicket).toHaveBeenLastCalledWith("project", "ticket", {
      lead_session_id: null,
    });
    expect(projectsApi.initializeProjectOrchestrator).not.toHaveBeenCalled();
  });

  it("opens the existing orchestrator with a return to the unassigned ticket", async () => {
    vi.mocked(projectsApi.getProject).mockResolvedValue({
      id: "project",
      name: "Shipping",
      orchestrator_session_id: "orchestrator",
    });
    const user = userEvent.setup();
    renderBoard(
      <ProjectTicketPanel
        projectName="Shipping"
        ticketId="ticket"
        onClose={vi.fn()}
        onCreated={vi.fn()}
      />,
    );
    await user.click(await screen.findByRole("link", { name: "Ask orchestrator" }));
    expect(await screen.findByLabelText("Existing session chat")).toHaveTextContent("orchestrator");
    expect(screen.getByRole("link", { name: "Back to ticket" })).toHaveAttribute(
      "href",
      "/?ticket=ticket",
    );
    expect(projectsApi.initializeProjectOrchestrator).not.toHaveBeenCalled();
  });

  it.each(["hover", "focus"])(
    "highlights all direct related tickets across paginated results on %s",
    async (interaction) => {
      const first: ProjectTicket = {
        ...ticket,
        id: "first",
        title: "Plan part",
        status: "planning",
      };
      const second: ProjectTicket = { ...ticket, id: "second", title: "Done part", status: "done" };
      const unrelated: ProjectTicket = {
        ...ticket,
        id: "unrelated",
        title: "Other work",
        status: "in_progress",
      };
      vi.mocked(api.listTickets).mockImplementation(async (_project, options) => ({
        data: [ticket, first, second, unrelated].filter((item) => item.status === options?.status),
        has_more: false,
        last_id: null,
      }));
      vi.mocked(api.listRelatedTickets).mockImplementation(async (_project, _ticket, after) => ({
        data: after ? [second] : [first],
        has_more: !after,
        last_id: after ? "second" : "first",
      }));
      renderBoard(<ProjectTicketBoard projectName="Shipping" selectedTicketId={null} />);
      const source = await screen.findByRole("link", { name: "Open ticket: Ship the board" });
      const neighbor = await screen.findByRole("link", { name: "Open ticket: Done part" });
      expect(api.listRelatedTickets).not.toHaveBeenCalled();
      if (interaction === "hover") fireEvent.mouseEnter(source);
      else fireEvent.focus(source);
      await waitFor(() => expect(neighbor).toHaveAttribute("data-related-highlight", "true"));
      expect(screen.getByRole("link", { name: "Open ticket: Plan part" })).toHaveAttribute(
        "data-related-highlight",
        "true",
      );
      expect(screen.getByRole("link", { name: "Open ticket: Other work" })).not.toHaveAttribute(
        "data-related-highlight",
      );
      if (interaction === "hover") fireEvent.mouseLeave(source);
      else fireEvent.blur(source);
      await waitFor(() => expect(neighbor).not.toHaveAttribute("data-related-highlight"));
      expect(api.updateTicket).not.toHaveBeenCalled();
    },
  );
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
