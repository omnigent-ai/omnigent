import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SandboxStatus } from "@/lib/types";
import type * as ChatStoreModule from "@/store/chatStore";
import { useChatStore } from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";
import { FullSessionChat } from "./FullSessionChat";

// The tests seed the session's live state in its registry entry instead of streaming it.
vi.mock("@/store/chatStore", async (importOriginal) => ({
  ...(await importOriginal<typeof ChatStoreModule>()),
  ensureConversationStreamed: vi.fn().mockResolvedValue(undefined),
}));
vi.mock("@/hooks/useSession", () => ({
  useSession: (id: string | null) => ({
    session: id ? { id, labels: {} } : null,
    isLoading: false,
    error: null,
  }),
}));
vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: () => true }));

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

function renderChat() {
  const queryClient = new QueryClient();
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <FullSessionChat sessionId={SESSION} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => conversationRegistry.clear());

afterEach(() => {
  cleanup();
  conversationRegistry.clear();
  useChatStore.setState(initialStoreState);
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
