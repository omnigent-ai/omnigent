import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SandboxStatus } from "@/lib/types";
import type * as ChatStoreModule from "@/store/chatStore";
import {
  useChatStore,
  consumePendingInitialPrompt,
  setPendingInitialPrompt,
} from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";
import { FullSessionChat } from "./FullSessionChat";
import type { ChatPresentation } from "./assistantPresentation";
import { MaestroIcon } from "@/components/icons/MaestroIcon";
import { clearSessionDrafts, getSessionDraft, setSessionDraft } from "@/lib/sessionDrafts";

const access = vi.hoisted(() => ({ canEdit: true, offline: false }));
vi.mock("@/components/HostBadge", () => ({ HostBadge: () => null }));
vi.mock("@/components/composer/ScopedSessionComposerControls", () => ({
  SessionPermissionPicker: () => null,
  ScopedSessionHarnessPicker: ({
    sessionId,
    agentIcon,
  }: {
    sessionId: string;
    agentIcon?: ReactNode;
  }) => (
    <span aria-label="Session controls">
      {agentIcon}
      {sessionId}
    </span>
  ),
}));
vi.mock("@/hooks/useComposerGitStatus", () => ({
  useComposerGitStatus: () => ({
    branchState: "unknown",
    branch: null,
    isWorktree: null,
    worktreePath: null,
    creationBranch: null,
  }),
}));
vi.mock("@/hooks/useSessionLiveness", () => ({
  livenessRowFromSession: () => null,
  useSessionLiveness: () => ({ kind: access.offline ? "host_offline" : "online" }),
}));

// The tests seed the session's live state in its registry entry instead of streaming it.
vi.mock("@/store/chatStore", async (importOriginal) => ({
  ...(await importOriginal<typeof ChatStoreModule>()),
  ensureConversationStreamed: vi.fn().mockResolvedValue(undefined),
}));
vi.mock("@/hooks/useSession", () => ({
  useSession: (id: string | null) => ({
    session: id ? { id, agentId: "agent", labels: {} } : null,
    isLoading: false,
    error: null,
  }),
}));
vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: () => access.canEdit }));

const SESSION = "c".repeat(32);
const FIRST_MESSAGE = "Help me design the reusable workflow.";
const initialStoreState = useChatStore.getState();

function seedSession({
  sandboxStatus,
  firstMessage = true,
}: {
  sandboxStatus: SandboxStatus | null;
  firstMessage?: boolean;
}) {
  conversationRegistry.acquire(SESSION).setState({
    loadingConversation: false,
    sessionStatus: "idle",
    sandboxStatus,
    // A sandbox create carries the first message, which waits until the sandbox is up.
    pendingUserMessages: firstMessage
      ? [{ tempId: "pending_first", content: [{ type: "input_text", text: FIRST_MESSAGE }] }]
      : [],
  });
}

function renderChat(sessionComposer = false, presentation?: ChatPresentation) {
  const queryClient = new QueryClient();
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <FullSessionChat
          sessionId={SESSION}
          sessionComposer={sessionComposer}
          presentation={presentation}
        />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  conversationRegistry.clear();
  clearSessionDrafts();
  consumePendingInitialPrompt(SESSION);
  access.canEdit = true;
  access.offline = false;
});

afterEach(() => {
  cleanup();
  conversationRegistry.clear();
  useChatStore.setState(initialStoreState);
  clearSessionDrafts();
});

describe("FullSessionChat shared composer", () => {
  it.each([false, true])(
    "delivers Main's stashed first input once to its own session (skill: %s)",
    (skill) => {
      seedSession({ sandboxStatus: null, firstMessage: false });
      const send = vi.fn().mockResolvedValue(undefined);
      const sendSlashCommand = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ send, sendSlashCommand });
      const files = [new File(["notes"], "notes.txt")];
      setPendingInitialPrompt(SESSION, {
        text: FIRST_MESSAGE,
        files: skill ? [] : files,
        skill: skill ? { name: "plan", args: "the next release" } : null,
      });
      const chat = renderChat(true);
      if (skill) {
        expect(sendSlashCommand).toHaveBeenCalledExactlyOnceWith(
          "plan",
          "the next release",
          "agent",
          { pinnedConversationId: SESSION },
        );
        expect(send).not.toHaveBeenCalled();
      } else {
        expect(send).toHaveBeenCalledExactlyOnceWith(FIRST_MESSAGE, "agent", files, {
          pinnedConversationId: SESSION,
        });
        expect(sendSlashCommand).not.toHaveBeenCalled();
      }
      chat.unmount();
      renderChat(true);
      expect(send.mock.calls.length + sendSlashCommand.mock.calls.length).toBe(1);
    },
  );

  it("reuses the full session draft across embedded chat mounts and targets its own controls", () => {
    seedSession({ sandboxStatus: null, firstMessage: false });
    conversationRegistry.acquire(SESSION).setState({ boundAgentId: "agent" });
    setSessionDraft(SESSION, { text: "From the full session", files: [] });
    const chat = renderChat(true, { assistant: { name: "Maestro", icon: <MaestroIcon /> } });
    expect(screen.getByLabelText("Message the agent")).toHaveValue("From the full session");
    expect(screen.getByLabelText("Session controls")).toHaveTextContent(SESSION);
    expect(
      screen.getByLabelText("Session controls").querySelector('[data-agent-icon="maestro"]'),
    ).not.toBeNull();
    fireEvent.change(screen.getByLabelText("Message the agent"), {
      target: { value: "From project Chat" },
    });
    chat.unmount();
    expect(getSessionDraft(SESSION)?.text).toBe("From project Chat");
    setSessionDraft(SESSION, { text: "Changed in the full session", files: [] });
    renderChat(true);
    expect(screen.getByLabelText("Session controls").querySelector("[data-agent-icon]")).toBeNull();
    expect(screen.getByLabelText("Message the agent")).toHaveValue("Changed in the full session");
  });

  it("keeps offline drafts editable but disables sending, and hides the read-only composer", () => {
    seedSession({ sandboxStatus: null, firstMessage: false });
    conversationRegistry.acquire(SESSION).setState({ boundAgentId: "agent" });
    access.offline = true;
    setSessionDraft(SESSION, { text: "Keep this offline draft", files: [] });
    const chat = renderChat(true);
    expect(screen.getByLabelText("Message the agent")).toBeEnabled();
    expect(screen.getByTestId("side-chat-send")).toBeDisabled();
    chat.unmount();
    access.canEdit = false;
    renderChat(true);
    expect(screen.queryByLabelText("Message the agent")).toBeNull();
    expect(getSessionDraft(SESSION)?.text).toBe("Keep this offline draft");
  });
});

describe("FullSessionChat sandbox launch", () => {
  it("shows its own session's launch, not the open session's", () => {
    const openSessionAsks = "Sign in on your laptop to continue.";
    // The session open on the page is launching too, and waits on the user.
    useChatStore.setState({
      conversationId: "d".repeat(32),
      sandboxStatus: { stage: "action_required", error: null, details: openSessionAsks },
    });
    seedSession({ sandboxStatus: { stage: "cloning", error: null } });
    renderChat();

    expect(screen.getByTestId("runner-starting-indicator")).toHaveTextContent(
      "Cloning repository…",
    );
    expect(screen.queryByText(openSessionAsks)).toBeNull();

    act(() => conversationRegistry.acquire(SESSION).setState({ sandboxStatus: null }));
    expect(screen.queryByTestId("runner-starting-indicator")).toBeNull();
    expect(screen.queryByText(openSessionAsks)).toBeNull();
  });

  it("shows the launch stage below the first message while the sandbox starts", () => {
    seedSession({ sandboxStatus: { stage: "provisioning", error: null } });
    renderChat();

    expect(screen.getByText(FIRST_MESSAGE)).toBeInTheDocument();
    expect(screen.getByTestId("runner-starting-indicator")).toHaveTextContent(
      "Provisioning sandbox…",
    );

    act(() =>
      conversationRegistry
        .acquire(SESSION)
        .setState({ sandboxStatus: { stage: "connecting", error: null } }),
    );
    expect(screen.getByTestId("runner-starting-indicator")).toHaveTextContent("Starting agent…");
  });

  it("shows the launch in place of the empty state before any message", () => {
    seedSession({ sandboxStatus: { stage: "cloning", error: null }, firstMessage: false });
    renderChat();

    expect(screen.getByTestId("runner-starting-indicator")).toHaveTextContent(
      "Cloning repository…",
    );
    expect(screen.queryByText("No messages yet")).toBeNull();
  });

  it.each([
    { name: "below the first message", firstMessage: true },
    { name: "before any message", firstMessage: false },
  ])("shows a failed launch $name", ({ firstMessage }) => {
    seedSession({
      sandboxStatus: { stage: "failed", error: "Spend limit reached" },
      firstMessage,
    });
    renderChat();

    expect(screen.getByTestId("sandbox-failed-indicator")).toHaveTextContent(
      "Sandbox launch failed",
    );
    expect(screen.queryByTestId("runner-starting-indicator")).toBeNull();
  });

  it("says what a launch waiting on the user needs until it's dismissed", () => {
    const details = "Sign in on your laptop to continue.";
    seedSession({ sandboxStatus: { stage: "action_required", error: null, details } });
    renderChat();

    expect(screen.getByTestId("runner-starting-indicator")).toHaveTextContent(
      "Provisioning sandbox…",
    );
    expect(screen.getByText(details)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText(details)).toBeNull();
    expect(conversationRegistry.peek(SESSION)?.getState().sandboxActionDismissed).toBe(details);
    expect(screen.getByTestId("runner-starting-indicator")).toBeInTheDocument();
  });

  it("shows the launch stage, not Working, while a send waits on the sandbox", () => {
    seedSession({ sandboxStatus: { stage: "provisioning", error: null } });
    // A follow-up from this client, which the server holds until the sandbox is up.
    conversationRegistry.acquire(SESSION).setState({ status: "streaming" });
    renderChat();

    expect(screen.getByTestId("runner-starting-indicator")).toHaveTextContent(
      "Provisioning sandbox…",
    );
    expect(screen.queryByTestId("working-indicator")).toBeNull();

    act(() => conversationRegistry.acquire(SESSION).setState({ sandboxStatus: null }));
    expect(screen.queryByTestId("runner-starting-indicator")).toBeNull();
    expect(screen.getByTestId("working-indicator")).toBeInTheDocument();
  });

  it("shows a failed launch in place of Working", () => {
    seedSession({ sandboxStatus: { stage: "failed", error: "Spend limit reached" } });
    conversationRegistry.acquire(SESSION).setState({ status: "streaming" });
    renderChat();

    expect(screen.getByTestId("sandbox-failed-indicator")).toBeInTheDocument();
    expect(screen.queryByTestId("working-indicator")).toBeNull();
  });

  it("clears the launch state once the sandbox is ready", () => {
    seedSession({ sandboxStatus: { stage: "provisioning", error: null } });
    renderChat();
    expect(screen.getByTestId("runner-starting-indicator")).toBeInTheDocument();

    // A `ready` stage clears the session's launch status.
    act(() => conversationRegistry.acquire(SESSION).setState({ sandboxStatus: null }));

    expect(screen.queryByTestId("runner-starting-indicator")).toBeNull();
    expect(screen.queryByTestId("sandbox-failed-indicator")).toBeNull();
    expect(screen.getByText(FIRST_MESSAGE)).toBeInTheDocument();
  });

  it.each([
    { name: "with a message", firstMessage: true },
    { name: "without messages", firstMessage: false },
  ])("shows no launch state for a session not on a sandbox, $name", ({ firstMessage }) => {
    seedSession({ sandboxStatus: null, firstMessage });
    renderChat();

    expect(screen.queryByTestId("runner-starting-indicator")).toBeNull();
    expect(screen.queryByTestId("sandbox-failed-indicator")).toBeNull();
    if (!firstMessage) expect(screen.getByText("No messages yet")).toBeInTheDocument();
  });
});
