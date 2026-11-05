import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import * as identity from "@/lib/identity";
import { AddAgentDialog } from "./AddAgentDialog";
import { TEAM_ROLE_LABEL_KEY } from "./canvasTeam";

vi.mock("@/lib/identity", async (importActual) => ({
  ...(await importActual<typeof identity>()),
  authenticatedFetch: vi.fn(),
}));
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: () => ({ data: [{ id: "agent_1", display_name: "Claude Code" }] }),
}));
// Radix Select needs layout APIs jsdom lacks, so a native select stands in.
vi.mock("@/components/ui/select", () => ({
  Select: ({
    value,
    onValueChange,
    children,
  }: {
    value: string;
    onValueChange: (next: string) => void;
    children: React.ReactNode;
  }) => (
    <select aria-label="Agent" value={value} onChange={(e) => onValueChange(e.target.value)}>
      <option value="" />
      {children}
    </select>
  ),
  SelectTrigger: () => null,
  SelectValue: () => null,
  SelectContent: ({ children }: { children: React.ReactNode }) => children,
  SelectItem: ({ value, children }: { value: string; children: React.ReactNode }) => (
    <option value={value}>{children}</option>
  ),
}));

const SESSION = {
  id: "work",
  title: "Coding session",
  host_id: "host_1",
  workspace: "/repo",
  project_id: "project_a",
  labels: {},
} as unknown as Conversation;

const onAdded = vi.fn(async () => {});

function renderDialog(session: Conversation | null, onClose = vi.fn()) {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <AddAgentDialog session={session} onAdded={onAdded} onClose={onClose} />
    </QueryClientProvider>,
  );
  return onClose;
}

function requestBody(): Record<string, unknown> {
  const init = vi.mocked(identity.authenticatedFetch).mock.calls[0][1] as RequestInit;
  return JSON.parse(init.body as string) as Record<string, unknown>;
}

describe("AddAgentDialog", () => {
  beforeEach(() => {
    onAdded.mockClear();
    vi.mocked(identity.authenticatedFetch).mockReset();
    vi.mocked(identity.authenticatedFetch).mockResolvedValue(new Response("{}", { status: 201 }));
  });

  it("creates a lead in the card's project and folder without a worktree", async () => {
    const onClose = renderDialog(SESSION);
    expect(screen.getByRole("button", { name: "Add agent" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Agent"), { target: { value: "agent_1" } });
    fireEvent.click(screen.getByRole("button", { name: "Add agent" }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(onAdded).toHaveBeenCalled();
    expect(requestBody()).toEqual({
      agent_id: "agent_1",
      title: "Team lead",
      host_id: "host_1",
      workspace: "/repo",
      project_id: "project_a",
      labels: { [TEAM_ROLE_LABEL_KEY]: "Team lead" },
    });
  });

  it("gives other roles their own worktree branch by default", async () => {
    const onClose = renderDialog(SESSION);
    fireEvent.click(screen.getByRole("button", { name: "Backend" }));
    fireEvent.change(screen.getByLabelText("Agent"), { target: { value: "agent_1" } });
    fireEvent.click(screen.getByRole("button", { name: "Add agent" }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    const git = requestBody().git as { branch_name: string };
    expect(git.branch_name).toMatch(/^team\/backend-[0-9a-f]{4}$/);
  });

  it("keeps a legacy project's label so the agent joins that project", async () => {
    const onClose = renderDialog({
      ...SESSION,
      project_id: null,
      labels: { omni_project: "Legacy" },
    } as unknown as Conversation);
    fireEvent.change(screen.getByLabelText("Agent"), { target: { value: "agent_1" } });
    fireEvent.click(screen.getByRole("button", { name: "Add agent" }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(requestBody()).not.toHaveProperty("project_id");
    expect(requestBody().labels).toMatchObject({ omni_project: "Legacy" });
  });

  it("stays open and shows the server's reason when creation fails", async () => {
    vi.mocked(identity.authenticatedFetch).mockResolvedValue(
      new Response(JSON.stringify({ error: { message: "not a git repository" } }), { status: 400 }),
    );
    const onClose = renderDialog(SESSION);
    fireEvent.change(screen.getByLabelText("Agent"), { target: { value: "agent_1" } });
    fireEvent.click(screen.getByRole("button", { name: "Add agent" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("not a git repository");
    expect(onClose).not.toHaveBeenCalled();
    expect(onAdded).not.toHaveBeenCalled();
  });

  it("explains why a session without a host cannot add an agent", () => {
    renderDialog({ ...SESSION, host_id: null } as unknown as Conversation);
    expect(screen.getByRole("alert")).toHaveTextContent("no connected host");
    fireEvent.change(screen.getByLabelText("Agent"), { target: { value: "agent_1" } });
    expect(screen.getByRole("button", { name: "Add agent" })).toBeDisabled();
  });
});
