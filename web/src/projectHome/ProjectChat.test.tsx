import { useSyncExternalStore } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { ChatPresentation } from "@/components/chat/assistantPresentation";
import type { Session } from "@/lib/types";
import type * as AgentsModule from "@/hooks/useAgents";
import type * as SessionsModule from "@/lib/sessionsApi";
import * as projectsApi from "@/lib/projectsApi";
import { ApiError, getSessionSlim } from "@/lib/sessionsApi";
import {
  clearSessionDrafts,
  getSessionDraft,
  setSessionDraft,
  subscribeSessionDrafts,
  type SessionDraft,
} from "@/lib/sessionDrafts";
import { addTicketToSessionDraft } from "./projectTicketDraft";
import { restoreReplyDraft } from "@/lib/replyDraft";
import { ProjectAgentChat, ProjectChat } from "./ProjectChat";

const fixture = vi.hoisted(() => ({
  session: {} as Session,
  agent: { name: "codex-native-ui", description: "A reusable coding agent" },
  conflictDraft: { text: "", files: [] } as SessionDraft,
  conflictRecovery: vi.fn(),
  state: {
    sessionHarness: "codex-native" as string | null,
    llmModel: "model-a" as string | null,
    sessionModelOverride: null,
    sessionReasoningEffort: "high" as string | null,
    codexModelOptions: [],
  },
}));
vi.mock("@/hooks/useConversations", () => ({
  useProjects: () => ({
    data: [{ id: "project", name: "Shipping" }],
    isLoading: false,
    error: null,
  }),
}));
vi.mock("@/hooks/useSession", () => ({ useSession: () => ({ session: fixture.session }) }));
vi.mock("@/hooks/useConversationEntryState", () => ({
  useConversationEntryState: () => fixture.state,
}));
vi.mock("@/hooks/useAgents", async (importOriginal) => ({
  ...(await importOriginal<typeof AgentsModule>()),
  useSessionAgent: () => ({ data: fixture.agent, isError: false }),
}));
vi.mock("@/lib/projectsApi", () => ({
  getProject: vi.fn(),
  createProject: vi.fn(),
}));
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsModule>()),
  getSessionSlim: vi.fn(),
}));
vi.mock("@/shell/NewChatDialog", () => ({
  NewChatLandingScreen: ({
    projectMainSession,
    onMainSessionConflict,
  }: {
    projectMainSession: { projectId: string; expectedMainSessionId: string | null };
    onMainSessionConflict?: (draft: SessionDraft) => Promise<boolean>;
  }) => (
    <>
      <output aria-label="Replacement composer">{JSON.stringify(projectMainSession)}</output>
      <button
        type="button"
        onClick={() =>
          void onMainSessionConflict?.(fixture.conflictDraft).then(fixture.conflictRecovery)
        }
      >
        Submit replacement
      </button>
    </>
  ),
}));
vi.mock("@/components/chat/FullSessionChat", () => ({
  FullSessionChat: ({
    sessionId,
    sessionComposer,
    presentation,
  }: {
    sessionId: string;
    sessionComposer?: boolean;
    presentation?: ChatPresentation;
  }) => {
    const draft = useSyncExternalStore(subscribeSessionDrafts, () => getSessionDraft(sessionId));
    return (
      <>
        {presentation?.header?.(false)}
        <span data-testid="assistant-icon">{presentation?.assistant?.icon}</span>
        <output aria-label="Existing session" data-shared-composer={sessionComposer}>
          {sessionId}
        </output>
        <textarea aria-label="Session draft" readOnly value={draft?.text ?? ""} />
        <output aria-label="Draft attachments">
          {draft?.files.map((file) => file.name).join(", ")}
        </output>
      </>
    );
  },
}));

const clients: QueryClient[] = [];
function renderChat(element = <ProjectChat projectName="Shipping" />) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <MemoryRouter>{element}</MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  fixture.session = {
    id: "lead",
    agentId: "agent",
    agentName: "codex-native-ui",
    status: "idle",
    createdAt: 0,
    title: "Existing conversation",
    harness: "codex-native",
    llmModel: "model-a",
    labels: {},
    items: [],
    permissionLevel: null,
    parentSessionId: null,
    subAgentName: null,
    kind: "default",
    projectId: "project",
  };
  fixture.state.sessionHarness = "codex-native";
  fixture.state.llmModel = "model-a";
  fixture.state.sessionReasoningEffort = "high";
  fixture.agent = { name: "codex-native-ui", description: "A reusable coding agent" };
  vi.mocked(projectsApi.getProject).mockResolvedValue({
    id: "project",
    name: "Shipping",
    main_session_id: "lead",
    main_session_state: "active",
  });
  vi.mocked(getSessionSlim).mockImplementation(async () => fixture.session);
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  clearSessionDrafts();
});

describe("project Chat", () => {
  it("only reads the saved lead session and opens that exact conversation full screen", async () => {
    renderChat();
    expect(await screen.findByLabelText("Existing session")).toHaveTextContent("lead");
    expect(screen.getByLabelText("Existing session")).toHaveAttribute(
      "data-shared-composer",
      "true",
    );
    expect(screen.getByRole("link", { name: "Open full session" })).toHaveAttribute(
      "href",
      "/c/lead",
    );
    expect(projectsApi.createProject).not.toHaveBeenCalled();
    expect(getSessionSlim).toHaveBeenCalledExactlyOnceWith("lead");
    expect(screen.getByRole("button", { name: "About Codex" })).toHaveTextContent("");
    expect(screen.queryByText("Maestro")).toBeNull();
    expect(
      screen
        .getByRole("button", { name: "About Codex" })
        .querySelector('[data-agent-icon="maestro"]'),
    ).toBeNull();
    expect(
      screen.getByTestId("assistant-icon").querySelector('[data-agent-icon="maestro"]'),
    ).toBeNull();
    expect(screen.queryByText("model-a")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "About Codex" }));
    expect(await screen.findByRole("region", { name: "About Codex" })).toHaveTextContent(
      fixture.agent.description,
    );
    expect(screen.queryByText("model-a")).toBeNull();
    expect(screen.queryByText("Harness")).toBeNull();
    expect(screen.queryByText("Effort")).toBeNull();
    expect(screen.queryByText("Role")).toBeNull();
    expect(screen.queryByText("Project Lead")).toBeNull();
  });

  it("uses a registered Maestro's description without displaying its execution configuration", async () => {
    fixture.session = { ...fixture.session, agentName: "team-agent", harness: "pi" };
    fixture.state.sessionHarness = "pi";
    fixture.agent = {
      name: "maestro (fork session-a)",
      description: "Coordinates this project's agents.",
    };
    renderChat();
    fireEvent.focus(await screen.findByRole("button", { name: "About Maestro" }));
    expect(await screen.findByRole("region", { name: "About Maestro" })).toHaveTextContent(
      fixture.agent.description,
    );
    expect(
      screen.getByTestId("assistant-icon").querySelector('[data-agent-icon="maestro"]'),
    ).not.toBeNull();
    expect(screen.queryByText("Model")).toBeNull();
    expect(screen.queryByText("Effort")).toBeNull();
  });

  it.each(["absent", "missing", "archived"] as const)(
    "shows the existing composer for %s Main without writing",
    async (state) => {
      const oldId = state === "absent" ? null : "old-main";
      vi.mocked(projectsApi.getProject).mockResolvedValue({
        id: "project",
        name: "Shipping",
        main_session_id: oldId,
        main_session_state: state,
      });
      renderChat();
      expect(await screen.findByLabelText("Replacement composer")).toHaveTextContent(
        JSON.stringify({
          projectId: "project",
          expectedMainSessionId: oldId,
        }),
      );
      expect(getSessionSlim).not.toHaveBeenCalled();
      expect(projectsApi.createProject).not.toHaveBeenCalled();
    },
  );

  it("does not offer replacement for an unavailable or foreign-project Main", async () => {
    fixture.session = { ...fixture.session, projectId: "another-project" };
    renderChat();
    expect(await screen.findByRole("alert")).toHaveTextContent("Main chat is unavailable.");
    expect(screen.queryByLabelText("Replacement composer")).toBeNull();
  });

  it("keeps an offline Main in the existing chat", async () => {
    fixture.session = { ...fixture.session, runnerOnline: false };
    renderChat();
    expect(await screen.findByLabelText("Existing session")).toHaveTextContent("lead");
    expect(screen.queryByLabelText("Replacement composer")).toBeNull();
  });

  it("carries a conflicted replacement into the winning Main without replacing its unsent input", async () => {
    const existingFile = new File(["earlier"], "earlier.txt");
    const recoveredFile = new File(["recovered"], "recovered.txt");
    setSessionDraft("lead", {
      text: "Keep @Plan",
      files: [existingFile],
      mentions: { files: [], sessions: [{ id: "first", title: "Plan", isSubagent: false }] },
    });
    addTicketToSessionDraft("lead", { id: "ticket-a", project_id: "project", title: "Ship it" });
    fixture.conflictDraft = {
      text: "Ask @Plan",
      files: [recoveredFile],
      mentions: { files: [], sessions: [{ id: "second", title: "Plan", isSubagent: false }] },
    };
    vi.mocked(projectsApi.getProject).mockResolvedValueOnce({
      id: "project",
      name: "Shipping",
      main_session_id: "old-main",
      main_session_state: "archived",
    });
    renderChat();
    fireEvent.click(await screen.findByRole("button", { name: "Submit replacement" }));
    expect(await screen.findByLabelText("Existing session")).toHaveTextContent("lead");
    await waitFor(() => expect(fixture.conflictRecovery).toHaveBeenCalledWith(true));
    const draft = getSessionDraft("lead")!;
    expect(screen.getByLabelText("Session draft")).toHaveValue(draft.text);
    expect(draft.text).toContain("Keep @Plan");
    expect(draft.text).toContain("Ask @Plan (");
    expect(restoreReplyDraft(draft.text, draft.replyDraft).quotes).toHaveLength(1);
    expect(draft.mentions?.sessions.map((session) => session.id)).toEqual(["first", "second"]);
    expect(draft.files).toEqual([existingFile, recoveredFile]);
    expect(screen.getByLabelText("Draft attachments")).toHaveTextContent(
      "earlier.txt, recovered.txt",
    );
    expect(screen.queryByLabelText("Replacement composer")).toBeNull();
    expect(projectsApi.createProject).not.toHaveBeenCalled();
  });

  it.each(["unavailable", "refresh failure"])(
    "retains the replacement draft after a conflict with %s Main",
    async (state) => {
      const oldProject = {
        id: "project",
        name: "Shipping",
        main_session_id: "old-main",
        main_session_state: "missing" as const,
      };
      vi.mocked(projectsApi.getProject).mockReset().mockResolvedValueOnce(oldProject);
      if (state === "unavailable")
        vi.mocked(projectsApi.getProject).mockResolvedValue({
          ...oldProject,
          main_session_state: "unavailable",
        });
      else vi.mocked(projectsApi.getProject).mockRejectedValue(new Error("Connection lost"));
      renderChat();
      fireEvent.click(await screen.findByRole("button", { name: "Submit replacement" }));
      await waitFor(() => expect(fixture.conflictRecovery).toHaveBeenCalledWith(false));
      expect(getSessionDraft("old-main")).toBeUndefined();
      expect(getSessionSlim).not.toHaveBeenCalled();
    },
  );

  it("refetches authoritative Main state after a session 404 before offering replacement", async () => {
    vi.mocked(getSessionSlim).mockRejectedValue(new ApiError("Session not found", 404, null));
    vi.mocked(projectsApi.getProject)
      .mockResolvedValueOnce({
        id: "project",
        name: "Shipping",
        main_session_id: "lead",
        main_session_state: "active",
      })
      .mockResolvedValue({
        id: "project",
        name: "Shipping",
        main_session_id: "lead",
        main_session_state: "missing",
      });
    renderChat();
    await screen.findByLabelText("Replacement composer");
    expect(projectsApi.getProject).toHaveBeenCalledTimes(2);
    expect(projectsApi.createProject).not.toHaveBeenCalled();
  });

  it.each([403, 404])(
    "does not replace Main when a session %s is not confirmed missing",
    async (status) => {
      vi.mocked(getSessionSlim).mockRejectedValue(
        new ApiError("Chat could not be loaded", status, null),
      );
      renderChat();
      expect(await screen.findByRole("alert")).toHaveTextContent("Chat could not be loaded");
      await waitFor(() =>
        expect(projectsApi.getProject).toHaveBeenCalledTimes(status === 404 ? 2 : 1),
      );
      expect(screen.queryByLabelText("Replacement composer")).toBeNull();
    },
  );

  it("carries ticket context without replacing a draft or re-adding it after sending", () => {
    setSessionDraft("lead", { text: "My question", files: [] });
    const ticket = { id: "ticket-a", title: "Ship it" };
    const chat = renderChat(
      <ProjectAgentChat sessionId="lead" projectId="project" ticket={ticket} />,
    );
    expect(getSessionDraft("lead")?.text).toContain("My question");
    expect(getSessionDraft("lead")?.text).toContain("Ticket ID: ticket-a");
    expect(screen.getByRole("link", { name: "Back to ticket" })).toHaveAttribute(
      "href",
      "/?view=board&ticket=ticket-a",
    );
    setSessionDraft("lead", { text: "", files: [] });
    // The current ticket may refresh while this chat remains open.
    chat.rerender(
      <QueryClientProvider client={clients.at(-1)!}>
        <TooltipProvider>
          <MemoryRouter>
            <ProjectAgentChat
              sessionId="lead"
              projectId="project"
              ticket={{ ...ticket, title: "New title" }}
            />
          </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>,
    );
    expect(getSessionDraft("lead")).toBeUndefined();
  });
});
