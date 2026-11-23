import { useSyncExternalStore, type ComponentProps } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useParams } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { FullSessionChat } from "@/components/chat/FullSessionChat";
import { useSessionAgent } from "@/hooks/useAgents";
import type { Session } from "@/lib/types";
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
import { ProjectOrchestrator } from "./ProjectOrchestrator";

const fixture = vi.hoisted(() => ({
  session: {} as Session,
  conflictDraft: { text: "", files: [] } as SessionDraft,
  conflictRecovery: vi.fn(),
}));
vi.mock("@/hooks/useConversations", () => ({
  useProjects: () => ({
    data: [{ id: "project", name: "Shipping" }],
    isLoading: false,
    error: null,
  }),
}));
vi.mock("@/lib/projectsApi", () => ({
  getProject: vi.fn(),
  createProject: vi.fn(),
}));
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsModule>()),
  getSessionSlim: vi.fn(),
}));
vi.mock("@/hooks/useAgents", () => ({
  useSessionAgent: vi.fn(() => ({
    data: { name: "codex-native-ui", harness: "codex-native", description: "Coding agent" },
    isError: false,
  })),
}));
vi.mock("@/components/chat/FullSessionChat", () => ({
  FullSessionChat: vi.fn(({ sessionId }: ComponentProps<typeof FullSessionChat>) => (
    <SessionView sessionId={sessionId} />
  )),
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
function SessionView({ sessionId: suppliedSessionId }: { sessionId?: string }) {
  const params = useParams();
  const sessionId = suppliedSessionId ?? params.sessionId ?? "";
  const draft = useSyncExternalStore(subscribeSessionDrafts, () => getSessionDraft(sessionId));
  return (
    <>
      <output aria-label="Existing session">{sessionId}</output>
      <textarea aria-label="Session draft" readOnly value={draft?.text ?? ""} />
      <output aria-label="Draft attachments">
        {draft?.files.map((file) => file.name).join(", ")}
      </output>
    </>
  );
}

const clients: QueryClient[] = [];
function renderOrchestrator(element = <ProjectOrchestrator projectName="Shipping" />) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <MemoryRouter initialEntries={["/projects/Shipping/orchestrator"]}>
          <Routes>
            <Route path="/projects/:projectName/orchestrator" element={element} />
            <Route path="/c/:sessionId" element={<SessionView />} />
          </Routes>
        </MemoryRouter>
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

describe("project Orchestrator navigation", () => {
  it("uses the saved session and shared composer in a closable panel with a full-session link", async () => {
    const onClose = vi.fn();
    setSessionDraft("lead", { text: "Keep my draft", files: [] });
    renderOrchestrator(<ProjectOrchestrator projectName="Shipping" onClose={onClose} />);
    expect(await screen.findByLabelText("Existing session")).toHaveTextContent("lead");
    expect(screen.getByRole("region", { name: "Orchestrator chat" })).toBeVisible();
    expect(screen.getByLabelText("Session draft")).toHaveValue("Keep my draft");
    expect(useSessionAgent).toHaveBeenCalledWith("lead");
    expect(vi.mocked(FullSessionChat).mock.calls.at(-1)?.[0]).toEqual(
      expect.objectContaining({
        sessionId: "lead",
        sessionComposer: true,
        evenComposerPadding: true,
        presentation: expect.objectContaining({
          assistant: expect.objectContaining({ name: "Codex" }),
        }),
      }),
    );
    expect(getSessionSlim).toHaveBeenCalledExactlyOnceWith("lead");
    expect(projectsApi.createProject).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Close orchestrator" }));
    expect(onClose).toHaveBeenCalledOnce();
    const expand = screen.getByRole("link", { name: "Open orchestrator in full session" });
    expect(expand).toHaveAttribute("href", "/c/lead");
    fireEvent.click(expand);
    expect(screen.queryByRole("region", { name: "Orchestrator chat" })).toBeNull();
    expect(screen.getByLabelText("Existing session")).toHaveTextContent("lead");
    expect(screen.getByLabelText("Session draft")).toHaveValue("Keep my draft");
  });

  it("opens the saved session in the normal view without creating or forking", async () => {
    setSessionDraft("lead", { text: "Keep my draft", files: [] });
    renderOrchestrator();
    expect(await screen.findByLabelText("Existing session")).toHaveTextContent("lead");
    expect(screen.getByLabelText("Session draft")).toHaveValue("Keep my draft");
    expect(screen.queryByLabelText("Replacement composer")).toBeNull();
    expect(projectsApi.createProject).not.toHaveBeenCalled();
    expect(getSessionSlim).toHaveBeenCalledExactlyOnceWith("lead");
  });

  it.each(["absent", "missing", "archived"] as const)(
    "shows the existing composer for %s Orchestrator without writing",
    async (state) => {
      const oldId = state === "absent" ? null : "old-main";
      vi.mocked(projectsApi.getProject).mockResolvedValue({
        id: "project",
        name: "Shipping",
        main_session_id: oldId,
        main_session_state: state,
      });
      renderOrchestrator(<ProjectOrchestrator projectName="Shipping" onClose={vi.fn()} />);
      expect(await screen.findByLabelText("Replacement composer")).toHaveTextContent(
        JSON.stringify({
          projectId: "project",
          expectedMainSessionId: oldId,
        }),
      );
      expect(getSessionSlim).not.toHaveBeenCalled();
      expect(projectsApi.createProject).not.toHaveBeenCalled();
      expect(screen.getByRole("region", { name: "Orchestrator chat" })).toBeVisible();
      expect(screen.queryByRole("link", { name: "Open orchestrator in full session" })).toBeNull();
    },
  );

  it("does not offer replacement for an unavailable or foreign-project Orchestrator", async () => {
    fixture.session = { ...fixture.session, projectId: "another-project" };
    renderOrchestrator();
    expect(await screen.findByRole("alert")).toHaveTextContent("Orchestrator is unavailable.");
    expect(screen.queryByLabelText("Replacement composer")).toBeNull();
  });

  it("keeps an offline Orchestrator in the normal session view", async () => {
    fixture.session = { ...fixture.session, runnerOnline: false };
    renderOrchestrator();
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
    renderOrchestrator();
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
      renderOrchestrator();
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
    renderOrchestrator();
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
      renderOrchestrator();
      expect(await screen.findByRole("alert")).toHaveTextContent("Chat could not be loaded");
      await waitFor(() =>
        expect(projectsApi.getProject).toHaveBeenCalledTimes(status === 404 ? 2 : 1),
      );
      expect(screen.queryByLabelText("Replacement composer")).toBeNull();
    },
  );

  it("carries legacy ticket context to the normal session without replacing its draft", async () => {
    setSessionDraft("lead", { text: "My question", files: [] });
    renderOrchestrator(
      <ProjectOrchestrator projectName="Shipping" ticket={{ id: "ticket-a", title: "Ship it" }} />,
    );
    await screen.findByLabelText("Existing session");
    expect(getSessionDraft("lead")?.text).toContain("My question");
    expect(getSessionDraft("lead")?.text).toContain("Ticket ID: ticket-a");
    expect(screen.queryByRole("link", { name: "Back to ticket" })).toBeNull();
    setSessionDraft("lead", { text: "", files: [] });
    await waitFor(() => expect(screen.getByLabelText("Session draft")).toHaveValue(""));
    expect(getSessionDraft("lead")).toBeUndefined();
  });
});
