import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { AddAgentDialog } from "./AddAgentDialog";
import { useAvailableAgents, type AvailableAgent } from "@/hooks/useAvailableAgents";
import { createSession } from "@/lib/sessionsApi";

const navigateMock = vi.fn();
vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return { ...actual, useNavigate: () => navigateMock };
});
vi.mock("@/hooks/useAvailableAgents", () => ({ useAvailableAgents: vi.fn() }));
vi.mock("@/lib/sessionsApi", () => ({ createSession: vi.fn() }));

const useAvailableAgentsMock = vi.mocked(useAvailableAgents);
const createSessionMock = vi.mocked(createSession);

const AGENTS: AvailableAgent[] = [
  {
    id: "ag_claude",
    name: "claude-native-ui",
    display_name: "Claude Code",
    description: "Claude Code agent",
    harness: "claude-native",
    skills: [],
  },
  {
    id: "ag_codex",
    name: "codex",
    display_name: "codex",
    description: null,
    harness: "codex",
    skills: [],
  },
];

function mockAgents(agents: AvailableAgent[]) {
  useAvailableAgentsMock.mockReturnValue({
    data: agents,
  } as unknown as ReturnType<typeof useAvailableAgents>);
}

function renderDialog(parentSessionId = "conv_parent") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidateSpy = vi.spyOn(client, "invalidateQueries");
  const utils = render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AddAgentDialog parentSessionId={parentSessionId} open onOpenChange={vi.fn()} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...utils, invalidateSpy };
}

beforeEach(() => {
  useAvailableAgentsMock.mockReset();
  createSessionMock.mockReset();
  navigateMock.mockReset();
  mockAgents(AGENTS);
});

afterEach(cleanup);

describe("AddAgentDialog", () => {
  it("lists the available agents from the catalog", () => {
    renderDialog();
    expect(screen.getByTestId("agent-card-ag_claude")).toHaveTextContent("Claude Code");
    expect(screen.getByTestId("agent-card-ag_codex")).toHaveTextContent("codex");
  });

  it("submits ui:<agent>:<name> with the parent link and a null sub_agent_name", async () => {
    createSessionMock.mockResolvedValue({
      id: "conv_child",
    } as unknown as Awaited<ReturnType<typeof createSession>>);

    const { invalidateSpy } = renderDialog("conv_parent");

    fireEvent.click(screen.getByTestId("agent-card-ag_claude"));
    // Nothing is prefilled — the user types the name themselves.
    fireEvent.change(screen.getByTestId("add-agent-name-input"), {
      target: { value: "jimmy" },
    });
    fireEvent.click(screen.getByTestId("add-agent-submit"));

    await waitFor(() => expect(createSessionMock).toHaveBeenCalledTimes(1));
    // Whole call asserted: the 3-segment title carries the typed name, the
    // parent link, and sub_agent_name=null (so the runner resolves the
    // child's own agent_id).
    expect(createSessionMock).toHaveBeenCalledWith("ag_claude", [], {
      parentSessionId: "conv_parent",
      subAgentName: null,
      title: "ui:claude-native-ui:jimmy",
    });
    // Rail refreshed for the parent, then navigated into the new child.
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_child"));
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["conversation", "conv_parent", "child_sessions"],
    });
  });

  it("starts the name empty and blocks submit until the user types one", async () => {
    createSessionMock.mockResolvedValue({
      id: "conv_child",
    } as unknown as Awaited<ReturnType<typeof createSession>>);
    renderDialog("conv_parent");

    fireEvent.click(screen.getByTestId("agent-card-ag_codex"));
    // Empty by default — the user must name the agent themselves.
    const input = screen.getByTestId("add-agent-name-input");
    expect(input).toHaveValue("");
    expect(screen.getByTestId("add-agent-submit")).toBeDisabled();

    // Once named, submit enables and the title carries the name verbatim.
    fireEvent.change(input, { target: { value: "reviewer" } });
    expect(screen.getByTestId("add-agent-submit")).toBeEnabled();
    fireEvent.click(screen.getByTestId("add-agent-submit"));

    await waitFor(() => expect(createSessionMock).toHaveBeenCalledTimes(1));
    expect(createSessionMock).toHaveBeenCalledWith("ag_codex", [], {
      parentSessionId: "conv_parent",
      subAgentName: null,
      title: "ui:codex:reviewer",
    });
  });

  // A planned feature wants the user to task the newly-added Codex reviewer at
  // creation time (e.g. "review the implementation against the design").
  // The dialog has no initial-prompt field yet, so it always sends []
  // initial items and the child opens untasked. `it.fails` is the strict
  // tripwire: the body fails today (no such field to type into), and goes
  // red the moment a prompt field lands and its text flows into
  // createSession — at which point promote this to a normal assertion.
  it.fails("seeds the user's initial review prompt into the child transcript", async () => {
    createSessionMock.mockResolvedValue({
      id: "conv_child",
    } as unknown as Awaited<ReturnType<typeof createSession>>);
    renderDialog("conv_parent");

    fireEvent.click(screen.getByTestId("agent-card-ag_codex"));
    fireEvent.change(screen.getByTestId("add-agent-name-input"), {
      target: { value: "reviewer" },
    });
    // No initial-prompt field exists today — getByTestId throws, which is
    // the expected failure that keeps this xfail-equivalent green.
    fireEvent.change(screen.getByTestId("add-agent-initial-prompt-input"), {
      target: { value: "review the implementation against designs/feature-x.md" },
    });
    fireEvent.click(screen.getByTestId("add-agent-submit"));

    await waitFor(() => expect(createSessionMock).toHaveBeenCalledTimes(1));
    // The prompt must travel as initial_items (a seeded user message), not
    // the empty [] the dialog sends today.
    const initialItems = createSessionMock.mock.calls[0][1];
    expect(initialItems).not.toEqual([]);
    expect(JSON.stringify(initialItems)).toContain("designs/feature-x.md");
  });

  it("shows an empty-state and a disabled submit when no agents are available", () => {
    mockAgents([]);
    renderDialog();
    expect(screen.getByTestId("add-agent-empty")).toBeInTheDocument();
    expect(screen.getByTestId("add-agent-submit")).toBeDisabled();
  });

  it("surfaces the server error inline on failure and does not navigate", async () => {
    createSessionMock.mockRejectedValue(new Error("409 label already in use"));
    renderDialog();

    fireEvent.click(screen.getByTestId("agent-card-ag_codex"));
    fireEvent.change(screen.getByTestId("add-agent-name-input"), {
      target: { value: "reviewer" },
    });
    fireEvent.click(screen.getByTestId("add-agent-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("add-agent-error")).toHaveTextContent("409 label already in use"),
    );
    // A failed create must not navigate the user away from the parent.
    expect(navigateMock).not.toHaveBeenCalled();
  });
});
