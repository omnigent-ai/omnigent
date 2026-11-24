import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/lib/projectsApi";
import { ProjectTeamDialog } from "./ProjectTeamDialog";

vi.mock("@/lib/projectsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof api>()),
  listProjectIdentities: vi.fn(),
  listProjectIdentityProposals: vi.fn(),
  acceptProjectIdentityProposal: vi.fn(),
  dismissProjectIdentityProposal: vi.fn(),
}));

const PROPOSAL = {
  kind: "create" as const,
  identity_id: null,
  instructions: "You own the roadmap.",
  rationale: "The user asked for a PM.",
  session_id: "session",
  created_at: 1,
};
const PM = { ...PROPOSAL, id: "pm", name: "PM" };
const DESIGNER = { ...PROPOSAL, id: "designer", name: "Designer", rationale: "" };

const clients: QueryClient[] = [];
function renderDialog() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(client);
  render(<ProjectTeamDialog projectId="project" projectName="Shipping" onClose={vi.fn()} />, {
    wrapper: ({ children }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.listProjectIdentities).mockResolvedValue([]);
  vi.mocked(api.listProjectIdentityProposals).mockResolvedValue([PM, DESIGNER]);
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
});

describe("Project team proposals", () => {
  it("lists agents' drafts and lets the owner accept or dismiss each", async () => {
    vi.mocked(api.acceptProjectIdentityProposal).mockResolvedValue({
      id: "identity",
      project_id: "project",
      name: "PM",
      builtin: false,
      instructions: "You own the roadmap.",
      agent_id: null,
      model: null,
      effort: null,
      active_session_id: null,
      active_session_state: "absent",
    });
    renderDialog();
    const cards = await screen.findAllByTestId("project-identity-proposal");
    expect(cards[0]).toHaveTextContent("PMNew identity");
    expect(cards[1]).toHaveTextContent("DesignerNew identity");
    expect(within(cards[0]).getByText("The user asked for a PM.")).toBeInTheDocument();
    await userEvent.click(within(cards[0]).getByRole("button", { name: "Accept" }));
    expect(api.acceptProjectIdentityProposal).toHaveBeenCalledWith("project", "pm");
    await waitFor(() => expect(screen.getByTestId("project-team-name")).toHaveValue("PM"));
    await userEvent.click(within(cards[1]).getByRole("button", { name: "Dismiss" }));
    expect(api.dismissProjectIdentityProposal).toHaveBeenCalledWith("project", "designer");
  });

  it("hides the section when nothing is proposed", async () => {
    vi.mocked(api.listProjectIdentityProposals).mockResolvedValue([]);
    renderDialog();
    await screen.findByTestId("project-team-name");
    expect(screen.queryByTestId("project-identity-proposals")).toBeNull();
  });
});
