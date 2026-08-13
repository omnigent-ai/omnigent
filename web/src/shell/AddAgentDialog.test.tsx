import type * as ReactRouterDomModule from "react-router-dom";
import type * as UseChildSessionsModule from "@/hooks/useChildSessions";

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { AddAgentDialog } from "./AddAgentDialog";
import { useAvailableAgents, type AvailableAgent } from "@/hooks/useAvailableAgents";
import { createSession } from "@/lib/sessionsApi";
import { setPendingInitialPrompt } from "@/store/chatStore";
import { useChildSessions } from "@/hooks/useChildSessions";
import type { AskSubagentSelection } from "@/shell/AskSubagentContext";

const navigateMock = vi.fn();
vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof ReactRouterDomModule>();
  return { ...actual, useNavigate: () => navigateMock };
});
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: vi.fn(),
  prefetchAvailableAgentDetails: vi.fn(),
}));
vi.mock("@/lib/sessionsApi", () => ({ createSession: vi.fn() }));
// Only ``setPendingInitialPrompt`` is used from the store, and nothing else in
// the dialog's import graph needs the real module, so a bare stub is enough.
vi.mock("@/store/chatStore", () => ({ setPendingInitialPrompt: vi.fn() }));
// The dialog reads existing children to prefill a collision-free "ask" name.
vi.mock("@/hooks/useChildSessions", async (importOriginal) => ({
  ...(await importOriginal<typeof UseChildSessionsModule>()),
  useChildSessions: vi.fn(() => ({ children: [], isLoading: false, error: null })),
}));

const useAvailableAgentsMock = vi.mocked(useAvailableAgents);
const createSessionMock = vi.mocked(createSession);
const setPendingInitialPromptMock = vi.mocked(setPendingInitialPrompt);
const useChildSessionsMock = vi.mocked(useChildSessions);

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

function renderDialog(
  parentSessionId = "conv_parent",
  onOpenChange = vi.fn(),
  initialContext?: AskSubagentSelection,
  existingNames?: readonly string[],
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidateSpy = vi.spyOn(client, "invalidateQueries");
  const utils = render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AddAgentDialog
          parentSessionId={parentSessionId}
          open
          onOpenChange={onOpenChange}
          initialContext={initialContext}
          existingNames={existingNames}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...utils, invalidateSpy, onOpenChange };
}

/** Select an agent card and fill the name + task so submit is enabled. */
function fillForm(agentTestId: string, name: string, task: string) {
  fireEvent.click(screen.getByTestId(agentTestId));
  fireEvent.change(screen.getByTestId("add-agent-name-input"), { target: { value: name } });
  fireEvent.change(screen.getByTestId("add-agent-initial-prompt-input"), {
    target: { value: task },
  });
}

beforeEach(() => {
  useAvailableAgentsMock.mockReset();
  createSessionMock.mockReset();
  setPendingInitialPromptMock.mockReset();
  navigateMock.mockReset();
  useChildSessionsMock.mockReturnValue({
    children: [],
    isLoading: false,
    error: null,
  } as unknown as ReturnType<typeof useChildSessions>);
  mockAgents(AGENTS);
});

afterEach(cleanup);

describe("AddAgentDialog", () => {
  it("lists the available agents from the catalog", () => {
    renderDialog();
    expect(screen.getByTestId("agent-card-ag_claude")).toHaveTextContent("Claude Code");
    expect(screen.getByTestId("agent-card-ag_codex")).toHaveTextContent("codex");
  });

  it("creates the child with the parent link + null sub_agent, queues the task, and navigates", async () => {
    createSessionMock.mockResolvedValue({
      id: "conv_child",
    } as unknown as Awaited<ReturnType<typeof createSession>>);

    const { invalidateSpy } = renderDialog("conv_parent");

    fillForm("agent-card-ag_claude", "jimmy", "review the current diff");
    fireEvent.click(screen.getByTestId("add-agent-submit"));

    await waitFor(() => expect(createSessionMock).toHaveBeenCalledTimes(1));
    // Whole call asserted: empty initial_items, the 3-segment title carrying
    // the typed name, the parent link, and sub_agent_name=null (so the runner
    // resolves the child's own agent_id).
    expect(createSessionMock).toHaveBeenCalledWith("ag_claude", [], {
      parentSessionId: "conv_parent",
      subAgentName: null,
      title: "ui:claude-native-ui:jimmy",
    });
    // The task is queued for the freshly-created child exactly once, keyed by
    // its id — the same first-prompt handoff New Chat uses.
    expect(setPendingInitialPromptMock).toHaveBeenCalledTimes(1);
    expect(setPendingInitialPromptMock).toHaveBeenCalledWith("conv_child", {
      text: "review the current diff",
      skill: null,
    });
    // Rail refreshed for the parent, then navigated into the new child.
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_child"));
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["conversation", "conv_parent", "child_sessions"],
    });
  });

  it("keeps submit disabled until an agent, name, and task are all provided", () => {
    renderDialog("conv_parent");

    // No agent picked yet — submit is disabled and the fields aren't shown.
    expect(screen.getByTestId("add-agent-submit")).toBeDisabled();

    fireEvent.click(screen.getByTestId("agent-card-ag_codex"));
    // Both fields start empty; submit stays disabled.
    expect(screen.getByTestId("add-agent-name-input")).toHaveValue("");
    expect(screen.getByTestId("add-agent-initial-prompt-input")).toHaveValue("");
    expect(screen.getByTestId("add-agent-submit")).toBeDisabled();

    // Name alone is not enough — the task is required too.
    fireEvent.change(screen.getByTestId("add-agent-name-input"), { target: { value: "reviewer" } });
    expect(screen.getByTestId("add-agent-submit")).toBeDisabled();

    // Task alone is not enough either: clear the name, add a task.
    fireEvent.change(screen.getByTestId("add-agent-name-input"), { target: { value: "" } });
    fireEvent.change(screen.getByTestId("add-agent-initial-prompt-input"), {
      target: { value: "review the diff" },
    });
    expect(screen.getByTestId("add-agent-submit")).toBeDisabled();

    // Both present → enabled.
    fireEvent.change(screen.getByTestId("add-agent-name-input"), { target: { value: "reviewer" } });
    expect(screen.getByTestId("add-agent-submit")).toBeEnabled();
  });

  it("delivers the task through setPendingInitialPrompt, not initial_items", async () => {
    createSessionMock.mockResolvedValue({
      id: "conv_child",
    } as unknown as Awaited<ReturnType<typeof createSession>>);
    renderDialog("conv_parent");

    fillForm(
      "agent-card-ag_codex",
      "reviewer",
      "review the implementation against designs/feature-x.md",
    );
    fireEvent.click(screen.getByTestId("add-agent-submit"));

    await waitFor(() => expect(createSessionMock).toHaveBeenCalledTimes(1));
    // The task must NOT travel as initial_items — the browser flow seeds an
    // empty transcript and delivers the first message after the child binds.
    const [, initialItems] = createSessionMock.mock.calls[0]!;
    expect(initialItems).toEqual([]);
    expect(JSON.stringify(createSessionMock.mock.calls[0])).not.toContain("designs/feature-x.md");
    // It rides the pending-prompt queue instead, exactly once, for the child.
    expect(setPendingInitialPromptMock).toHaveBeenCalledTimes(1);
    expect(setPendingInitialPromptMock).toHaveBeenCalledWith("conv_child", {
      text: "review the implementation against designs/feature-x.md",
      skill: null,
    });
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

    fillForm("agent-card-ag_codex", "reviewer", "review the diff");
    fireEvent.click(screen.getByTestId("add-agent-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("add-agent-error")).toHaveTextContent("409 label already in use"),
    );
    // A failed create must not queue a prompt or navigate the user away.
    expect(setPendingInitialPromptMock).not.toHaveBeenCalled();
    expect(navigateMock).not.toHaveBeenCalled();
  });

  it("clears the selection, name, and task when cancelled (creating nothing)", () => {
    const { onOpenChange } = renderDialog("conv_parent");

    fillForm("agent-card-ag_codex", "reviewer", "review the diff");
    expect(screen.getByTestId("add-agent-name-input")).toHaveValue("reviewer");
    expect(screen.getByTestId("add-agent-initial-prompt-input")).toHaveValue("review the diff");

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    // Cancel closes the dialog (parent controls visibility) and resets state:
    // the per-agent fields unmount because the selection was cleared.
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(screen.queryByTestId("add-agent-name-input")).toBeNull();
    expect(screen.queryByTestId("add-agent-initial-prompt-input")).toBeNull();
    expect(createSessionMock).not.toHaveBeenCalled();
    expect(setPendingInitialPromptMock).not.toHaveBeenCalled();
  });

  it("shows a spinner and cannot be submitted twice while in flight", async () => {
    // A create that never settles keeps the dialog in its submitting state.
    let resolveCreate: ((session: { id: string }) => void) | undefined;
    createSessionMock.mockReturnValue(
      new Promise<{ id: string }>((resolve) => {
        resolveCreate = resolve;
      }) as unknown as ReturnType<typeof createSession>,
    );
    renderDialog("conv_parent");

    fillForm("agent-card-ag_codex", "reviewer", "review the diff");
    const submit = screen.getByTestId("add-agent-submit");
    fireEvent.click(submit);

    // The submit is now busy/disabled, so a second click is a no-op.
    await waitFor(() => expect(submit).toBeDisabled());
    expect(submit).toHaveAttribute("aria-busy", "true");
    fireEvent.click(submit);
    expect(createSessionMock).toHaveBeenCalledTimes(1);

    // Let it settle so the test doesn't leak a pending promise.
    resolveCreate?.({ id: "conv_child" });
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/c/conv_child"));
  });

  describe("Ask sub-agent flow (initialContext)", () => {
    it("shows the selected context, labels the field Question, and derives the name from the selection", () => {
      renderDialog("conv_parent", vi.fn(), {
        selectedText: "the auth flow validates tokens",
        surroundingExcerpt: null,
      });

      expect(screen.getByTestId("add-agent-context")).toHaveTextContent(
        "the auth flow validates tokens",
      );
      // Name + Question appear once an agent is picked; the name is derived
      // from the selected text (editable).
      fireEvent.click(screen.getByTestId("agent-card-ag_claude"));
      expect(screen.getByTestId("add-agent-name-input")).toHaveValue(
        "the auth flow validates tokens",
      );
      expect(screen.getByText("Question")).toBeInTheDocument();
    });

    it("disambiguates a colliding derived name across the root tree with ' (2)'", () => {
      renderDialog("conv_parent", vi.fn(), { selectedText: "Snappy", surroundingExcerpt: null }, [
        "Snappy",
      ]);
      fireEvent.click(screen.getByTestId("agent-card-ag_codex"));
      expect(screen.getByTestId("add-agent-name-input")).toHaveValue("Snappy (2)");
    });

    it("keeps a user-edited name even when tree names change later (editable override)", () => {
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      const selection = { selectedText: "Snappy", surroundingExcerpt: null };
      const tree = (names: readonly string[]) => (
        <QueryClientProvider client={client}>
          <MemoryRouter>
            <AddAgentDialog
              parentSessionId="conv_parent"
              open
              onOpenChange={vi.fn()}
              initialContext={selection}
              existingNames={names}
            />
          </MemoryRouter>
        </QueryClientProvider>
      );
      const { rerender } = render(tree([]));
      fireEvent.click(screen.getByTestId("agent-card-ag_codex"));
      expect(screen.getByTestId("add-agent-name-input")).toHaveValue("Snappy");
      // User overrides the derived name.
      fireEvent.change(screen.getByTestId("add-agent-name-input"), {
        target: { value: "my reviewer" },
      });
      // A late tree-name arrival would re-derive to "Snappy (2)" — the edit wins.
      rerender(tree(["Snappy"]));
      expect(screen.getByTestId("add-agent-name-input")).toHaveValue("my reviewer");
    });

    it("delivers the composed context + question exactly once, under the derived-name title", async () => {
      createSessionMock.mockResolvedValue({ id: "conv_child" } as unknown as Awaited<
        ReturnType<typeof createSession>
      >);
      renderDialog("conv_parent", vi.fn(), {
        selectedText: "auth flow validates tokens",
        surroundingExcerpt: "The auth flow validates tokens before trusting claims.",
      });

      fireEvent.click(screen.getByTestId("agent-card-ag_codex"));
      // Name is derived from the selection; type the question and submit.
      fireEvent.change(screen.getByTestId("add-agent-initial-prompt-input"), {
        target: { value: "is the check constant-time?" },
      });
      fireEvent.click(screen.getByTestId("add-agent-submit"));

      await waitFor(() => expect(setPendingInitialPromptMock).toHaveBeenCalledTimes(1));
      const [, payload] = setPendingInitialPromptMock.mock.calls[0]!;
      const text = (payload as { text: string }).text;
      expect(text).toContain("Selected from the source response:");
      expect(text).toContain("> auth flow validates tokens");
      expect(text).toContain("Surrounding excerpt:");
      expect(text).toContain("> The auth flow validates tokens before trusting claims.");
      expect(text).toContain("Question: is the check constant-time?");
      // createSession uses empty initial_items; the title carries the derived name.
      expect(createSessionMock).toHaveBeenCalledWith("ag_codex", [], {
        parentSessionId: "conv_parent",
        subAgentName: null,
        title: "ui:codex:auth flow validates tokens",
      });
    });
  });
});
