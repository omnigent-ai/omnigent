import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProjectTicketBoard } from "./ProjectTicketBoard";
import { ProjectOrchestratorPanel, ProjectTicketPanel } from "./ProjectTicketPanel";
import { TicketCommentBody } from "./TicketCommentBody";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { fetchConversationById } from "@/hooks/useOpenSessionRow";
import { conversation } from "@/test/sidebarMockHelpers";
import type { ProjectTicket, TicketChanges, TicketEvent } from "@/lib/projectTicketsApi";
import * as api from "@/lib/projectTicketsApi";
import * as projectsApi from "@/lib/projectsApi";
import { Link, useLocation } from "@/lib/routing";
import { buildMessageDeepLink } from "@/lib/messageDeepLink";

const capabilities = vi.hoisted(() => ({ lifecycle: true, involvement: true, stages: true }));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({
    project_tickets: true,
    project_ticket_lifecycle: capabilities.lifecycle,
    project_ticket_agent_involvement: capabilities.involvement,
    project_ticket_stages: capabilities.stages,
  }),
}));

vi.mock("@/hooks/useConversations", () => ({
  useAutomationsEnabled: () => false,
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
  setTicketSessionInvolvement: vi.fn(),
  getTicketStages: vi.fn(),
}));
vi.mock("@/lib/projectsApi", () => ({
  getProject: vi.fn(),
  initializeProjectOrchestrator: vi.fn(),
  createProject: vi.fn(),
}));

let ticket: ProjectTicket;
const clients: QueryClient[] = [];
function LocationProbe() {
  return <output aria-label="Current location">{useLocation().search}</output>;
}

function renderBoard(element: React.ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(client);
  const rendered = render(element, {
    wrapper: ({ children }) => (
      <QueryClientProvider client={client}>
        <TooltipProvider>
          <MemoryRouter>{children}</MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>
    ),
  });
  return { ...rendered, client };
}

beforeEach(() => {
  vi.clearAllMocks();
  capabilities.lifecycle = true;
  capabilities.involvement = true;
  capabilities.stages = true;
  vi.mocked(api.getTicketStages).mockResolvedValue(structuredClone(api.DEFAULT_TICKET_STAGES));
  vi.mocked(useIsMobileViewport).mockReturnValue(false);
  vi.mocked(fetchConversationById).mockImplementation(async (id) =>
    id === "orchestrator" ? conversation(id, { title: "Shipping orchestrator" }) : null,
  );
  vi.mocked(projectsApi.getProject).mockResolvedValue({
    id: "project",
    name: "Shipping",
    orchestrator_session_id: "orchestrator",
  });
  ticket = {
    id: "ticket",
    project_id: "project",
    title: "Ship the board",
    description: "",
    stage_id: "open",
    tags: [{ key: "needs-human", value: "approval" }],
    latest_update: "Created the ticket",
    comment_count: 0,
    created_at: 1000,
    updated_at: 1000,
  };
  vi.mocked(api.listTickets).mockImplementation(async (_projectId, options) => ({
    data: options?.stage_id === ticket.stage_id ? [ticket] : [],
    has_more: false,
    last_id: ticket.id,
  }));
  vi.mocked(api.getTicket).mockImplementation(async () => ticket);
  vi.mocked(api.createTicket).mockImplementation(async (_id, body) => ({ ...ticket, ...body }));
  vi.mocked(api.updateTicket).mockImplementation(
    async (_projectId, _id, changes: TicketChanges) => {
      const { comment: _comment, ...fields } = changes;
      ticket = { ...ticket, ...fields };
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
  vi.mocked(api.setTicketSessionInvolvement).mockResolvedValue(ticket);
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
});

describe("Ticket comment previews", () => {
  const messageUrl = buildMessageDeepLink("response:part/2", "https://app.example/c/worker");
  const diagnostics = "Diagnostic record. ".repeat(80);

  it("keeps whole Markdown blocks and exact-message links, with keyboard access to the full comment", async () => {
    const user = userEvent.setup();
    renderBoard(
      <TicketCommentBody
        body={[
          "**Outcome**",
          "",
          "Built the requested change.",
          "",
          "**Evidence**",
          "",
          `[Full verification](${messageUrl})`,
          "",
          "**Details**",
          "",
          diagnostics,
          "",
          "Pending owner confirmation.",
        ].join("\n")}
      />,
    );
    expect(screen.getByText("Outcome")).toHaveAttribute("data-streamdown", "strong");
    expect(screen.getByText("Evidence")).toHaveAttribute("data-streamdown", "strong");
    expect(screen.getByRole("link", { name: "Full verification" })).toHaveAttribute(
      "href",
      messageUrl,
    );
    expect(screen.queryByText("Details")).toBeNull();
    expect(screen.queryByText("Pending owner confirmation.")).toBeNull();
    const toggle = screen.getByRole("button", { name: "Show full comment" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(document.getElementById(toggle.getAttribute("aria-controls")!)).toBeInTheDocument();
    toggle.focus();
    await user.keyboard("{Enter}");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle).toHaveFocus();
    expect(screen.getByText("Pending owner confirmation.")).toBeInTheDocument();
    expect(screen.getByText("Details")).toHaveAttribute("data-streamdown", "strong");
    expect(screen.getByText(diagnostics.trim())).toBeInTheDocument();
    expect(screen.queryByText("Preview")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Show less" }));
    expect(screen.queryByText("Pending owner confirmation.")).toBeNull();
    expect(screen.getByRole("link", { name: "Full verification" })).toHaveAttribute(
      "href",
      messageUrl,
    );
  });

  it.each([
    ["one oversized paragraph", `**Outcome:** ${diagnostics}[Full verification](${messageUrl})`],
    ["a fenced block", `\`\`\`text\n${diagnostics}\n\`\`\`\n\n[Full verification](${messageUrl})`],
    [
      "reference definitions",
      `[Full verification][source]\n\n${diagnostics}\n\n[source]: ${messageUrl}`,
    ],
    [
      "a label before a large block",
      `**Evidence**\n\n${diagnostics}\n\n[Full verification](${messageUrl})`,
    ],
  ])(
    "uses a bounded plain preview for %s without damaging the full Markdown",
    async (_label, body) => {
      const user = userEvent.setup();
      renderBoard(<TicketCommentBody body={body} />);
      const toggle = screen.getByRole("button", { name: "Show full comment" });
      const content = document.getElementById(toggle.getAttribute("aria-controls")!)!;
      expect(content.textContent!.length).toBeLessThanOrEqual(601);
      expect(content).toHaveTextContent("Diagnostic record.");
      expect(content).not.toHaveTextContent("```");
      expect(content).not.toHaveTextContent("**Outcome:**");
      expect(screen.queryByRole("link")).toBeNull();
      await user.click(toggle);
      expect(screen.getByRole("link", { name: "Full verification" })).toHaveAttribute(
        "href",
        messageUrl,
      );
      expect(content).toHaveTextContent(diagnostics.trim());
    },
  );

  it("bounds tall comments without cutting Markdown list items", async () => {
    const user = userEvent.setup();
    const body = Array.from({ length: 16 }, (_, index) => `- Check ${index + 1}`).join("\n");
    renderBoard(<TicketCommentBody body={body} />);
    expect(screen.queryByRole("list")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Show full comment" }));
    expect(screen.getAllByRole("listitem")).toHaveLength(16);
    expect(screen.getByText("Check 16")).toBeInTheDocument();
  });
});

describe("Project ticket board", () => {
  it("shows five phases and human-input tags, without filter controls", async () => {
    renderBoard(<ProjectTicketBoard projectName="Shipping" selectedTicketId={null} />);
    await screen.findByRole("region", { name: "Open" });
    const board = screen.getByRole("region", { name: "Shipping ticket board" });
    for (const [label, icon, color] of [
      ["Open", "circle-dashed", "text-muted-foreground"],
      ["Planning", "clipboard-list", "text-session-active"],
      ["Working", "circle-dot", "text-session-active"],
      ["Review", "eye", "text-warning"],
      ["Done", "circle-check", "text-success"],
    ]) {
      const lane = within(board).getByRole("region", { name: label });
      expect(lane.querySelector(`.lucide-${icon}`)).toHaveClass(color);
    }
    expect(
      await screen.findByRole("link", { name: "Open ticket: Ship the board" }),
    ).toHaveAttribute("href", "/?ticket=ticket");
    expect(screen.getByText("Needs response")).toBeInTheDocument();
    expect(screen.queryByText("Created the ticket")).toBeNull();
    expect(screen.queryByRole("searchbox")).toBeNull();
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it.each([false, true])(
    "reveals compact card tags on hover and focus and keeps detail tags complete (needs response=%s)",
    async (needsResponse) => {
      ticket.tags = [
        { key: "source", value: "ISSUE-123" },
        { key: "status", value: "Waiting for the investigation to finish" },
        { key: "assignee", value: "Unassigned" },
        ...(needsResponse ? [{ key: "needs-human", value: "approval" }] : []),
      ];
      const { rerender } = renderBoard(
        <>
          <ProjectTicketBoard projectName="Shipping" selectedTicketId={null} />
          <LocationProbe />
        </>,
      );
      const card = await screen.findByRole("link", { name: "Open ticket: Ship the board" });
      const source = within(card).getByText("source: ISSUE-123");
      const badge = source.closest('[data-slot="hover-card-trigger"]')!;
      expect(source).toHaveClass("truncate");
      expect(badge).toHaveClass("text-xs", "rounded-md", "shrink");
      expect(badge.parentElement).not.toHaveClass("flex-wrap");
      expect(
        within(card).getByLabelText(needsResponse ? "2 more tags" : "1 more tag"),
      ).toHaveTextContent(needsResponse ? "+2" : "+1");
      expect(within(card).queryByText("assignee: Unassigned")).toBeNull();
      if (needsResponse) {
        expect(within(card).getByText("Needs response")).toHaveClass("bg-tag-pink");
        expect(badge.parentElement?.firstElementChild).toHaveTextContent("Needs response");
        expect(within(card).queryByText(/status:/)).toBeNull();
      } else {
        expect(within(card).getByText(/status:/)).toHaveClass("truncate");
      }

      const triggers = card.querySelectorAll('[data-slot="hover-card-trigger"]');
      expect(triggers).toHaveLength(needsResponse ? 1 : 2);
      const trigger = triggers[needsResponse ? 0 : 1];
      const label = trigger.textContent!;
      const contentSelector = { selector: '[data-slot="hover-card-content"]' };
      expect(trigger).toHaveAttribute("tabindex", "0");
      expect(trigger).not.toHaveAttribute("title");
      expect(trigger.closest("a")).toBe(card);
      expect(screen.queryByText(label, contentSelector)).toBeNull();
      fireEvent.pointerEnter(trigger, { pointerType: "mouse" });
      const content = await screen.findByText(label, contentSelector);
      expect(content).toHaveClass("bg-popover", "shadow-menu", "wrap-anywhere");
      expect(content).toHaveAttribute("id", trigger.getAttribute("aria-describedby"));
      expect(card).not.toContainElement(content);
      fireEvent.click(content);
      expect(screen.getByLabelText("Current location")).toBeEmptyDOMElement();
      fireEvent.pointerLeave(trigger, { pointerType: "mouse" });
      await waitFor(() => expect(screen.queryByText(label, contentSelector)).toBeNull());
      fireEvent.focus(trigger);
      expect(await screen.findByText(label, contentSelector)).toBeInTheDocument();
      fireEvent.blur(trigger);
      await waitFor(() => expect(screen.queryByText(label, contentSelector)).toBeNull());
      fireEvent.click(source);
      expect(screen.getByLabelText("Current location")).toHaveTextContent("?ticket=ticket");

      rerender(
        <ProjectTicketPanel
          projectName="Shipping"
          ticketId="ticket"
          onClose={vi.fn()}
          onCreated={vi.fn()}
        />,
      );
      expect(await screen.findByText("assignee: Unassigned")).toBeInTheDocument();
      expect(screen.getByText("source: ISSUE-123")).not.toHaveClass("truncate");
      expect(screen.getByText("status: Waiting for the investigation to finish")).not.toHaveClass(
        "truncate",
      );
      expect(screen.queryByLabelText(/more tags?/)).toBeNull();
      expect(api.updateTicket).not.toHaveBeenCalled();
    },
  );

  it.each(["Created the ticket", "Confirmed the issue is resolved"])(
    "retains a real comment preview: %s",
    async (text) => {
      ticket.latest_update = text;
      ticket.comment_count = 1;
      renderBoard(<ProjectTicketBoard projectName="Shipping" selectedTicketId={null} />);
      const card = await screen.findByRole("link", { name: "Open ticket: Ship the board" });
      expect(within(card).getByText(text)).toBeInTheDocument();
    },
  );

  it("renders configured lane order, instructions and the first starting lane", async () => {
    ticket.stage_id = "investigating";
    vi.mocked(api.getTicketStages).mockResolvedValue({
      stages: [
        { id: "new", name: "New", instructions: "", is_complete: false },
        {
          id: "investigating",
          name: "Investigating",
          instructions: "Gather evidence before resolving.",
          is_complete: false,
          color: "purple",
          icon: "search",
        },
        {
          id: "resolved",
          name: "Resolved",
          instructions: "",
          is_complete: true,
          color: "blue",
          icon: "rocket",
        },
      ],
      initial_stage_id: "new",
    });
    renderBoard(<ProjectTicketBoard projectName="Shipping" selectedTicketId={null} />);
    const investigating = await screen.findByRole("region", { name: "Investigating" });
    const initial = screen.getByRole("region", { name: "New" });
    const board = screen.getByRole("region", { name: "Shipping ticket board" });
    expect(investigating.querySelector(".lucide-search")).toHaveClass("text-purple-600");
    const badge = within(investigating)
      .getByText("Investigating")
      .closest('[data-slot="hover-card-trigger"]')!;
    expect(badge).toHaveClass("bg-purple-500/10");
    expect(
      screen.getByRole("region", { name: "Resolved" }).querySelector(".lucide-rocket"),
    ).toHaveClass("text-session-active");
    expect(
      within(board)
        .getAllByRole("region")
        .map((lane) => lane.getAttribute("aria-label")),
    ).toEqual(["New", "Investigating", "Resolved"]);
    const instructions = "Gather evidence before resolving.";
    expect(screen.queryByText(instructions)).toBeNull();
    expect(badge).not.toHaveAttribute("title");
    fireEvent.pointerEnter(badge, { pointerType: "mouse" });
    const description = await screen.findByText(instructions);
    expect(description.closest('[data-slot="hover-card-content"]')).toHaveClass(
      "bg-popover",
      "shadow-menu",
    );
    expect(within(investigating).queryByText(instructions)).toBeNull();
    fireEvent.pointerLeave(badge, { pointerType: "mouse" });
    await waitFor(() => expect(screen.queryByText(instructions)).toBeNull());
    fireEvent.focus(badge);
    expect(await screen.findByText(instructions)).toHaveAttribute(
      "id",
      badge.getAttribute("aria-describedby"),
    );
    fireEvent.blur(badge);
    await waitFor(() => expect(screen.queryByText(instructions)).toBeNull());
    expect(within(initial).getByRole("link", { name: "New ticket" })).toHaveAttribute(
      "href",
      "/?ticket=new",
    );
    expect(within(board).getAllByRole("link", { name: "New ticket" })).toHaveLength(1);
    expect(
      await within(investigating).findByRole("link", { name: "Open ticket: Ship the board" }),
    ).toBeInTheDocument();
    expect(within(investigating).getByText("Needs response")).toBeInTheDocument();
    expect(api.listTickets).toHaveBeenCalledWith("project", {
      stage_id: "investigating",
      after: undefined,
    });
  });

  it("does not show default lanes when a capable server cannot load its stages", async () => {
    vi.mocked(api.getTicketStages).mockRejectedValue(new Error("Stages unavailable"));
    renderBoard(<ProjectTicketBoard projectName="Shipping" selectedTicketId={null} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Stages unavailable");
    expect(screen.queryByRole("region", { name: "Open" })).toBeNull();
    expect(api.listTickets).not.toHaveBeenCalled();
  });

  it.each([false, null, undefined])(
    "refreshes projected running activity to %s without session lookups",
    async (activity) => {
      ticket.has_running_agents = true;
      const quiet = { ...ticket, id: "quiet", title: "Other work", has_running_agents: false };
      vi.mocked(api.listTickets).mockImplementation(async (_projectId, options) => ({
        data: options?.stage_id === ticket.stage_id ? [ticket, quiet] : [],
        has_more: false,
        last_id: quiet.id,
      }));
      const { client } = renderBoard(
        <ProjectTicketBoard projectName="Shipping" selectedTicketId={null} />,
      );
      const card = await screen.findByRole("link", { name: "Open ticket: Ship the board" });
      expect(within(card).getByRole("img", { name: "Running" })).toBeInTheDocument();
      expect(within(card).getByTestId("running-dot")).toBeInTheDocument();
      expect(within(card).getByText("Needs response")).toBeInTheDocument();
      expect(card).toHaveAttribute("href", "/?ticket=ticket");
      expect(
        within(screen.getByRole("link", { name: "Open ticket: Other work" })).queryByRole("img", {
          name: "Running",
        }),
      ).toBeNull();
      ticket = { ...ticket, has_running_agents: activity };
      await act(async () =>
        client.refetchQueries({ queryKey: ["project-tickets", "project", "board"] }),
      );
      await waitFor(() => expect(within(card).queryByRole("img", { name: "Running" })).toBeNull());
      expect(within(card).getByText("Needs response")).toBeInTheDocument();
      ticket = { ...ticket, has_running_agents: true };
      await act(async () =>
        client.refetchQueries({ queryKey: ["project-tickets", "project", "board"] }),
      );
      expect(await within(card).findByRole("img", { name: "Running" })).toBeInTheDocument();
      expect(api.listTicketSessions).not.toHaveBeenCalled();
      expect(fetchConversationById).not.toHaveBeenCalled();
    },
  );

  it("moves between project stages without special actions and preserves ordinary content", async () => {
    ticket.stage_id = "verifying";
    ticket.description = "Gather recovery evidence.";
    vi.mocked(api.getTicketStages).mockResolvedValue({
      stages: [
        {
          id: "verifying",
          name: "Verifying",
          instructions: "Confirm recovery before resolving.",
          is_complete: false,
          color: "orange",
          icon: "wrench",
        },
        { id: "done", name: "Done", instructions: "", is_complete: false },
        { id: "resolved", name: "Resolved", instructions: "", is_complete: false },
        { id: "archived", name: "Archived", instructions: "", is_complete: true },
      ],
      initial_stage_id: "verifying",
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
    await waitFor(() =>
      expect(
        screen.getByRole("combobox", { name: "Ticket stage" }).querySelector(".lucide-wrench"),
      ).toHaveClass("text-orange-600"),
    );
    expect(screen.queryByText("Confirm recovery before resolving.")).toBeNull();
    await user.click(screen.getByRole("combobox", { name: "Ticket stage" }));
    await user.click(screen.getByRole("option", { name: "Done" }));
    await waitFor(() =>
      expect(api.updateTicket).toHaveBeenLastCalledWith("project", "ticket", { stage_id: "done" }),
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    await user.click(screen.getByRole("combobox", { name: "Ticket stage" }));
    await user.click(screen.getByRole("option", { name: "Resolved" }));
    await waitFor(() =>
      expect(api.updateTicket).toHaveBeenLastCalledWith("project", "ticket", {
        stage_id: "resolved",
      }),
    );
    await user.click(screen.getByRole("combobox", { name: "Ticket stage" }));
    await user.click(screen.getByRole("option", { name: "Archived" }));
    await waitFor(() =>
      expect(api.updateTicket).toHaveBeenLastCalledWith("project", "ticket", {
        stage_id: "archived",
      }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(ticket.tags).toEqual([{ key: "needs-human", value: "approval" }]);
    expect(ticket.description).toBe("Gather recovery evidence.");
    expect(screen.getByText("Gather recovery evidence.")).toBeInTheDocument();
    expect(screen.getByText("Needs response")).toBeInTheDocument();
  });

  it("keeps snapshot and legacy history readable after stage rename or removal", async () => {
    vi.mocked(api.getTicketStages).mockResolvedValue({
      stages: [
        { id: "open", name: "New", instructions: "", is_complete: false },
        { id: "done", name: "Closed", instructions: "", is_complete: true },
      ],
      initial_stage_id: "open",
    });
    vi.mocked(api.listTicketEvents).mockImplementation(async (_project, _ticket, _after, kind) => ({
      data: kind
        ? []
        : [
            {
              id: "custom",
              ticket_id: "ticket",
              position: 0,
              kind: "updated",
              user_id: "local",
              session_id: null,
              agent_id: null,
              created_at: 1000,
              payload: {
                changes: { stage_id: "removed" },
                stage: {
                  id: "removed",
                  name: "Investigating",
                  is_complete: false,
                  color: "purple",
                  icon: "search",
                },
              },
            },
            {
              id: "legacy",
              ticket_id: "ticket",
              position: 1,
              kind: "updated",
              user_id: "local",
              session_id: null,
              agent_id: null,
              created_at: 1000,
              payload: { changes: { status: "done" } },
            },
          ],
      has_more: false,
      last_position: 1,
    }));
    renderBoard(
      <ProjectTicketPanel
        projectName="Shipping"
        ticketId="ticket"
        onClose={vi.fn()}
        onCreated={vi.fn()}
      />,
    );
    const activity = await screen.findByRole("region", { name: "Ticket activity" });
    expect(await within(activity).findByText("Investigating")).toBeInTheDocument();
    expect(activity.querySelector(".lucide-search")).toHaveClass("text-purple-600");
    expect(activity.querySelector(".lucide-circle-check")).toHaveClass("text-success");
    expect(within(activity).getByText("Done")).toBeInTheDocument();
    expect(within(activity).queryByText("Closed")).toBeNull();
    expect(await screen.findByRole("combobox", { name: "Ticket stage" })).toHaveTextContent("New");
  });

  it("creates a title-only ticket without creating a session", async () => {
    const onCreated = vi.fn();
    const onExpandedChange = vi.fn();
    const props = {
      projectName: "Shipping",
      ticketId: "new",
      onClose: vi.fn(),
      onCreated,
      onExpandedChange,
    };
    const { rerender } = renderBoard(<ProjectTicketPanel {...props} />);
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "A small task" } });
    fireEvent.click(screen.getByRole("button", { name: "Expand ticket" }));
    expect(onExpandedChange).toHaveBeenLastCalledWith(true);
    rerender(<ProjectTicketPanel {...props} expanded />);
    expect(screen.getByLabelText("Title")).toHaveValue("A small task");
    fireEvent.click(screen.getByRole("button", { name: "Restore ticket panel" }));
    expect(onExpandedChange).toHaveBeenLastCalledWith(false);
    rerender(<ProjectTicketPanel {...props} expanded={false} />);
    expect(screen.getByLabelText("Title")).toHaveValue("A small task");
    fireEvent.click(screen.getByRole("button", { name: "Create ticket" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("ticket"));
    expect(api.createTicket).toHaveBeenCalledWith("project", {
      title: "A small task",
      description: "",
    });
    expect(projectsApi.initializeProjectOrchestrator).not.toHaveBeenCalled();
  });

  it.each([false, true])(
    "groups and deduplicates attachments without losing activity (links=%s)",
    async (includeLink) => {
      const url = "https://git.example.com/org/repo/pull/42";
      const linkUrl = "https://example.test/decisions";
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
        data: [
          event,
          { ...event, id: "event2", position: 1 },
          ...(includeLink
            ? [{ ...event, id: "link", position: 2, payload: { type: "link", url: linkUrl } }]
            : []),
        ],
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
      const outputs = await screen.findByRole("region", { name: "Pull requests" });
      expect(within(outputs).getAllByRole("link")).toHaveLength(1);
      expect(within(outputs).getByRole("link", { name: /org\/repo #42/ })).toHaveAttribute(
        "href",
        url,
      );
      expect(screen.queryByRole("region", { name: "Outputs" })).toBeNull();
      if (includeLink) {
        const links = screen.getByRole("region", { name: "Links" });
        expect(within(links).getAllByRole("link")).toHaveLength(1);
        expect(within(links).getByRole("link")).toHaveAttribute("href", linkUrl);
      } else expect(screen.queryByRole("region", { name: "Links" })).toBeNull();
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
    },
  );

  it("edits key/value tags without changing ticket status", async () => {
    renderBoard(
      <ProjectTicketPanel
        projectName="Shipping"
        ticketId="ticket"
        onClose={vi.fn()}
        onCreated={vi.fn()}
      />,
    );
    expect(await screen.findByText("Needs response")).toHaveAttribute("title", "approval");
    fireEvent.click(screen.getByRole("button", { name: "Edit tags" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove tag needs-human" }));
    await waitFor(() => expect(screen.queryByText("Needs response")).toBeNull());
    expect(api.updateTicket).toHaveBeenCalledWith("project", "ticket", { tags: [] });
    fireEvent.change(screen.getByLabelText("Tag key"), { target: { value: "area" } });
    fireEvent.change(screen.getByLabelText("Tag value"), { target: { value: "board" } });
    fireEvent.click(screen.getByRole("button", { name: "Save tag" }));
    await waitFor(() =>
      expect(api.updateTicket).toHaveBeenLastCalledWith("project", "ticket", {
        tags: [{ key: "area", value: "board" }],
      }),
    );
    expect(ticket.stage_id).toBe("open");
  });

  it("renders ticket prose as Markdown and hides empty optional sections", async () => {
    ticket.description = "## Acceptance\n\n- **Readable** descriptions\n- Keep `status` simple";
    ticket.tags = [];
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
            {
              id: "imported-result",
              ticket_id: ticket.id,
              position: 1,
              kind: "comment",
              user_id: null,
              session_id: null,
              agent_id: null,
              payload: { text: "### Result\n\n**Delivered** and verified.", imported: true },
              created_at: 1001,
            },
          ],
      has_more: false,
      last_position: 1,
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
    expect(screen.getByRole("heading", { name: "Description", level: 3 })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Acceptance" })).toBeInTheDocument();
    const emphasized = await Promise.all(
      ["Readable", "Delivered", "Reviewed"].map((word) => screen.findByText(word)),
    );
    for (const element of emphasized) expect(element).toHaveAttribute("data-streamdown", "strong");
    expect(screen.getByText("status")).toHaveProperty("tagName", "CODE");
    expect(screen.getByText("You")).toBeInTheDocument();
    expect(screen.getByText("System")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add tags" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Tags" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Pull requests" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Links" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Show full comment" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Sessions" })).toBeNull();
    expect(screen.queryByRole("combobox", { name: "Output type" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Add plan" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Request review" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Ticket plan" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Human review" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Approve & complete" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Request changes" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Completion summary" })).toBeNull();
    await waitFor(() =>
      expect(screen.queryByRole("region", { name: "Related tickets" })).toBeNull(),
    );
    expect(screen.queryByRole("button", { name: "Link ticket" })).toBeNull();
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
                  payload: {
                    action: "updated",
                    session_id: "worker",
                    involvement: "Building the panel",
                  },
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
      const sessions = await screen.findByRole("region", { name: "Agents" });
      expect(
        await within(sessions).findByRole("link", { name: /Build ticket panel/ }),
      ).toHaveAttribute("href", href);
      expect(within(sessions).getByRole("img", { name: "Session running" })).toBeInTheDocument();
      expect(within(sessions).getByText("Running")).toBeInTheDocument();
      expect(await screen.findAllByRole("link", { name: "Shipping orchestrator" })).toHaveLength(2);
      expect(screen.getByRole("region", { name: "Ticket activity" })).toHaveTextContent(
        "updated involvement for",
      );
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
      expect(
        await within(sessions).findByRole("link", { name: "Respond to: Build ticket panel" }),
      ).toHaveAttribute("href", href);
      expect(within(sessions).queryByText("Needs response")).toBeNull();
      expect(within(sessions).queryByText("Running")).toBeNull();
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
    const sessions = await screen.findByRole("region", { name: "Agents" });
    const worker = await within(sessions).findByRole("link", { name: /Session worker/ });
    await waitFor(() => expect(worker).toHaveTextContent("Unavailable"));
    expect(worker).toHaveAttribute("href", "/?ticket=ticket&session=worker");
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

  it("retains title editing and direct moves through the compact stage control", async () => {
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
    await user.click(screen.getByRole("combobox", { name: "Ticket stage" }));
    await user.click(screen.getByRole("option", { name: "Done" }));
    await waitFor(() =>
      expect(api.updateTicket).toHaveBeenLastCalledWith("project", "ticket", { stage_id: "done" }),
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(ticket.tags).toEqual([{ key: "needs-human", value: "approval" }]);
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
    capabilities.stages = false;
    renderBoard(<ProjectTicketBoard projectName="Shipping" selectedTicketId={null} />);
    const card = await screen.findByRole("link", { name: "Open ticket: Ship the board" });
    expect(screen.queryByRole("region", { name: "Planning" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Review" })).toBeNull();
    fireEvent.mouseEnter(card);
    expect(api.listRelatedTickets).not.toHaveBeenCalled();
    expect(api.getTicketStages).not.toHaveBeenCalled();
  });

  it("saves the description and renders its acceptance criteria as Markdown", async () => {
    ticket.stage_id = "planning";
    ticket.description = "Approach and acceptance criteria";
    renderBoard(
      <ProjectTicketPanel
        projectName="Shipping"
        ticketId="ticket"
        onClose={vi.fn()}
        onCreated={vi.fn()}
      />,
    );
    expect(await screen.findByText("Approach and acceptance criteria")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Edit ticket" }));
    expect(screen.getByLabelText("Description (optional)")).toHaveValue(
      "Approach and acceptance criteria",
    );
    fireEvent.change(screen.getByLabelText("Description (optional)"), {
      target: { value: "## Acceptance\n\n- **Readable** UI" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByRole("heading", { name: "Acceptance" })).toBeInTheDocument();
    expect(api.updateTicket).toHaveBeenCalledWith("project", "ticket", {
      title: "Ship the board",
      description: "## Acceptance\n\n- **Readable** UI",
    });
  });

  it("records feedback as an ordinary comment independently of stages and response routing", async () => {
    ticket.stage_id = "review";
    ticket.description = "Inspect the existing evidence.";
    renderBoard(
      <ProjectTicketPanel
        projectName="Shipping"
        ticketId="ticket"
        onClose={vi.fn()}
        onCreated={vi.fn()}
      />,
    );
    await screen.findByText("Inspect the existing evidence.");
    expect(screen.queryByRole("button", { name: "Request changes" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Approve & complete" })).toBeNull();
    fireEvent.change(screen.getByLabelText("Add a comment"), {
      target: { value: "Increase the contrast" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Comment" }));
    await waitFor(() =>
      expect(api.commentOnTicket).toHaveBeenLastCalledWith("project", "ticket", {
        text: "Increase the contrast",
        outputs: [],
      }),
    );
    expect(api.updateTicket).not.toHaveBeenCalled();
    expect(ticket.stage_id).toBe("review");
    expect(ticket.description).toBe("Inspect the existing evidence.");
    expect(ticket.tags).toEqual([{ key: "needs-human", value: "approval" }]);
  });

  it("keeps a description draft through fullscreen changes and save failure", async () => {
    ticket.stage_id = "planning";
    ticket.description = "Original description";
    let rejectSave!: (error: Error) => void;
    vi.mocked(api.updateTicket).mockImplementationOnce(
      () =>
        new Promise((_resolve, reject) => {
          rejectSave = reject;
        }),
    );
    const onExpandedChange = vi.fn();
    const props = {
      projectName: "Shipping",
      ticketId: "ticket",
      onClose: vi.fn(),
      onCreated: vi.fn(),
      onExpandedChange,
    };
    const { rerender } = renderBoard(<ProjectTicketPanel {...props} />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit ticket" }));
    fireEvent.change(screen.getByLabelText("Description (optional)"), {
      target: { value: "Keep the draft" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Expand ticket" }));
    expect(onExpandedChange).toHaveBeenLastCalledWith(true);
    rerender(<ProjectTicketPanel {...props} expanded />);
    expect(screen.getByLabelText("Description (optional)")).toHaveValue("Keep the draft");
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(api.updateTicket).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Restore ticket panel" }));
    expect(onExpandedChange).toHaveBeenLastCalledWith(false);
    rerender(<ProjectTicketPanel {...props} expanded={false} />);
    await act(async () => rejectSave(new Error("Could not save description")));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not save description");
    expect(screen.getByLabelText("Description (optional)")).toHaveValue("Keep the draft");
    expect(ticket.description).toBe("Original description");
  });

  it("shows related tickets read-only with pagination and visible query errors", async () => {
    const peer: ProjectTicket = {
      ...ticket,
      id: "peer",
      title: "Related work",
      stage_id: "planning",
    };
    const followup = { ...peer, id: "followup", title: "Follow-up work" };
    vi.mocked(api.listRelatedTickets)
      .mockRejectedValueOnce(new Error("Could not load related tickets"))
      .mockImplementation(async (_project, _ticket, after) => ({
        data: after ? [followup] : [peer],
        has_more: !after,
        last_id: after ? followup.id : peer.id,
      }));
    const { client } = renderBoard(
      <ProjectTicketPanel
        projectName="Shipping"
        ticketId="ticket"
        onClose={vi.fn()}
        onCreated={vi.fn()}
      />,
    );
    const section = await screen.findByRole("region", { name: "Related tickets" });
    expect(await within(section).findByRole("alert")).toHaveTextContent(
      "Could not load related tickets",
    );
    await act(async () =>
      client.refetchQueries({
        queryKey: ["project-tickets", "project", "ticket", "related"],
        exact: true,
      }),
    );
    expect(
      await within(section).findByRole("link", { name: /Related work.*Planning/ }),
    ).toHaveAttribute("href", "/?ticket=peer");
    expect(within(section).queryByRole("alert")).toBeNull();
    expect(within(section).queryByRole("button", { name: "Link ticket" })).toBeNull();
    expect(within(section).queryByRole("button", { name: /Unlink/ })).toBeNull();
    fireEvent.click(within(section).getByRole("button", { name: "More related tickets" }));
    expect(
      await within(section).findByRole("link", { name: /Follow-up work.*Planning/ }),
    ).toHaveAttribute("href", "/?ticket=followup");
    expect(within(section).getAllByRole("link")).toHaveLength(2);
    expect(within(section).queryByRole("button", { name: "More related tickets" })).toBeNull();
    expect(api.linkRelatedTicket).not.toHaveBeenCalled();
    expect(api.updateTicket).not.toHaveBeenCalled();
  });

  it.each([false, true])(
    "opens a linked planning chat and keeps ticket drafts (mobile=%s)",
    async (mobile) => {
      vi.mocked(useIsMobileViewport).mockReturnValue(mobile);
      ticket.stage_id = "planning";
      ticket.lead_session_id = "planner";
      vi.mocked(fetchConversationById).mockImplementation(async (id) =>
        conversation(id, {
          title: id === "planner" ? "Design the board" : "Shipping orchestrator",
          status: "idle",
        }),
      );
      vi.mocked(api.listTicketSessions).mockResolvedValue({
        data: [{ session_id: "planner", created_at: 1000, involvement: "Planning the approach" }],
        has_more: false,
        last_id: "planner",
      });
      const user = userEvent.setup();
      const onExpandedChange = vi.fn();
      const props = {
        projectName: "Shipping",
        ticketId: "ticket",
        onClose: vi.fn(),
        onCreated: vi.fn(),
        onExpandedChange: mobile ? undefined : onExpandedChange,
      };
      const { rerender } = renderBoard(<ProjectTicketPanel {...props} />);
      const agents = await screen.findByRole("list", { name: "Involved agents" });
      const chat = await within(agents).findByRole("link", {
        name: "Respond to: Design the board",
      });
      expect(screen.queryByText("Your response is needed")).toBeNull();
      expect(screen.queryByRole("region", { name: "Response needed" })).toBeNull();
      expect(screen.getAllByText("Needs response")).toHaveLength(1);
      expect(chat).toHaveClass("bg-tag-pink");
      expect(chat).not.toHaveTextContent("Needs response");
      expect(chat).toHaveTextContent("Idle");
      expect(within(chat).queryByRole("img", { name: /approval prompt/ })).toBeNull();
      expect(
        vi.mocked(fetchConversationById).mock.calls.filter(([id]) => id === "planner"),
      ).toHaveLength(1);
      fireEvent.change(screen.getByLabelText("Add a comment"), {
        target: { value: "Comment draft" },
      });
      if (!mobile) {
        await user.click(screen.getByRole("button", { name: "Expand ticket" }));
        expect(onExpandedChange).toHaveBeenLastCalledWith(true);
        rerender(<ProjectTicketPanel {...props} expanded />);
      }
      await user.click(chat);
      expect(screen.getByLabelText("Existing session chat")).toHaveTextContent("planner");
      expect(screen.queryByRole("textbox", { name: "Add a comment" })).toBeNull();
      await user.click(screen.getByRole("link", { name: "Back to ticket" }));
      expect(screen.getByLabelText("Add a comment")).toHaveValue("Comment draft");
      if (!mobile) {
        await user.click(screen.getByRole("button", { name: "Restore ticket panel" }));
        expect(onExpandedChange).toHaveBeenLastCalledWith(false);
        rerender(<ProjectTicketPanel {...props} expanded={false} />);
        expect(screen.getByLabelText("Add a comment")).toHaveValue("Comment draft");
      }
      expect(projectsApi.initializeProjectOrchestrator).not.toHaveBeenCalled();
    },
  );

  it("lists paginated agents with read-only involvement and keeps chat routing intact", async () => {
    ticket.stage_id = "planning";
    ticket.lead_session_id = "reviewer";
    vi.mocked(api.listTicketSessions).mockImplementation(async (_project, _ticket, after) => ({
      data: after
        ? [
            { session_id: "orchestrator", created_at: 1000 },
            { session_id: "reviewer", created_at: 1000, involvement: "Verifying the result" },
          ]
        : [{ session_id: "planner", created_at: 1000, involvement: "Investigating the approach" }],
      has_more: !after,
      last_id: after ? "reviewer" : "planner",
    }));
    vi.mocked(fetchConversationById).mockImplementation(async (id) =>
      conversation(id, {
        title:
          id === "orchestrator"
            ? "Shipping orchestrator"
            : id === "planner"
              ? "Plan the work"
              : "Verify the result",
      }),
    );
    renderBoard(
      <ProjectTicketPanel
        projectName="Shipping"
        ticketId="ticket"
        onClose={vi.fn()}
        onCreated={vi.fn()}
      />,
    );
    const agents = await screen.findByRole("region", { name: "Agents" });
    expect(await within(agents).findByText("Investigating the approach")).toBeInTheDocument();
    expect(
      await within(agents).findByRole("link", { name: "Open chat with: Plan the work" }),
    ).toHaveAttribute("href", "/?ticket=ticket&session=planner");
    fireEvent.click(screen.getByRole("button", { name: "More agents" }));
    expect(
      await within(agents).findByRole("link", {
        name: "Respond to: Verify the result",
      }),
    ).toHaveAttribute("href", "/?ticket=ticket&session=reviewer");
    expect(await within(agents).findByText("Verifying the result")).toBeInTheDocument();
    expect(within(agents).getAllByRole("link", { name: /Shipping orchestrator/ })).toHaveLength(1);
    expect(
      within(screen.getByRole("list", { name: "Involved agents" })).getAllByRole("link"),
    ).toHaveLength(3);
    expect(screen.queryByRole("button", { name: "More agents" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Responsible agent" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Linked sessions" })).toBeNull();
    expect(within(agents).queryByRole("button", { name: "Edit agent involvement" })).toBeNull();
    expect(screen.queryByLabelText("Involvement")).toBeNull();
    expect(api.setTicketSessionInvolvement).not.toHaveBeenCalled();
    expect(api.updateTicket).not.toHaveBeenCalled();
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
    await user.click(
      await screen.findByRole("link", { name: "Open chat with: Shipping orchestrator" }),
    );
    expect(await screen.findByLabelText("Existing session chat")).toHaveTextContent("orchestrator");
    expect(screen.getByRole("link", { name: "Back to ticket" })).toHaveAttribute(
      "href",
      "/?ticket=ticket",
    );
    expect(projectsApi.initializeProjectOrchestrator).not.toHaveBeenCalled();
  });

  it("keeps agents accessible without the new involvement capability", async () => {
    capabilities.involvement = false;
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
    expect(
      await screen.findByRole("link", { name: "Open chat with: Session worker" }),
    ).toHaveAttribute("href", "/?ticket=ticket&session=worker");
    expect(screen.queryByRole("button", { name: "Edit agent involvement" })).toBeNull();
    expect(api.setTicketSessionInvolvement).not.toHaveBeenCalled();
    expect(screen.queryByRole("region", { name: "Response needed" })).toBeNull();
    expect(
      await screen.findByRole("link", { name: "Open chat with: Shipping orchestrator" }),
    ).toHaveAttribute("href", "/?ticket=ticket&session=orchestrator");
  });

  it("routes responses to the prompt owner and does not treat a comment as a prompt answer", async () => {
    vi.mocked(api.listTicketSessions).mockResolvedValue({
      data: [{ session_id: "worker", created_at: 1000, involvement: "Awaiting plan approval" }],
      has_more: false,
      last_id: "worker",
    });
    vi.mocked(fetchConversationById).mockImplementation(async (id) =>
      conversation(id, {
        title: id === "worker" ? "Plan the work" : "Shipping orchestrator",
        pending_elicitations_count: id === "worker" ? 1 : 0,
      }),
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
    const agents = await screen.findByRole("region", { name: "Agents" });
    const respond = await within(agents).findByRole("link", { name: "Respond to: Plan the work" });
    expect(respond).toHaveTextContent("Respond");
    expect(respond).not.toHaveTextContent("Needs response");
    fireEvent.change(screen.getByLabelText("Add a comment"), { target: { value: "I approve" } });
    await user.click(screen.getByRole("button", { name: "Comment" }));
    await waitFor(() =>
      expect(api.commentOnTicket).toHaveBeenCalledWith("project", "ticket", {
        text: "I approve",
        outputs: [],
      }),
    );
    expect(
      within(agents).getByRole("link", { name: "Respond to: Plan the work" }),
    ).toHaveTextContent("Respond");
    expect(within(agents).queryByText("Needs response")).toBeNull();
    expect(api.updateTicket).not.toHaveBeenCalled();
    await user.click(respond);
    expect(screen.getByLabelText("Existing session chat")).toHaveTextContent("worker");
    expect(projectsApi.initializeProjectOrchestrator).not.toHaveBeenCalled();
  });

  it.each(["hover", "focus"])(
    "tints the source and outlines direct related tickets across paginated results on %s",
    async (interaction) => {
      const first: ProjectTicket = {
        ...ticket,
        id: "first",
        title: "Plan part",
        stage_id: "planning",
      };
      const second: ProjectTicket = {
        ...ticket,
        id: "second",
        title: "Done part",
        stage_id: "done",
      };
      const unrelated: ProjectTicket = {
        ...ticket,
        id: "unrelated",
        title: "Other work",
        stage_id: "in_progress",
      };
      vi.mocked(api.listTickets).mockImplementation(async (_project, options) => ({
        data: [ticket, first, second, unrelated].filter(
          (item) => item.stage_id === options?.stage_id,
        ),
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
      expect(source).toHaveClass("hover:bg-primary/5", "focus-visible:bg-primary/5");
      expect(source).not.toHaveAttribute("data-related-highlight");
      if (interaction === "hover") fireEvent.mouseEnter(source);
      else fireEvent.focus(source);
      await waitFor(() => expect(neighbor).toHaveAttribute("data-related-highlight", "true"));
      for (const related of [
        neighbor,
        screen.getByRole("link", { name: "Open ticket: Plan part" }),
      ]) {
        expect(related).toHaveAttribute("data-related-highlight", "true");
        expect(related).toHaveClass("border-muted-foreground/50", "bg-card");
        expect(related).not.toHaveClass("bg-primary/5");
        expect(related).not.toHaveClass("ring-2");
      }
      expect(screen.getByRole("link", { name: "Open ticket: Other work" })).not.toHaveAttribute(
        "data-related-highlight",
      );
      if (interaction === "hover") fireEvent.mouseLeave(source);
      else fireEvent.blur(source);
      await waitFor(() => expect(neighbor).not.toHaveAttribute("data-related-highlight"));
      expect(neighbor).not.toHaveClass("border-muted-foreground/50");
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
