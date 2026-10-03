import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { toast } from "sonner";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as sessionsApi from "@/lib/sessionsApi";
import type * as ChatStoreModule from "@/store/chatStore";
import { useChatStore, type ChatState } from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";
import { SideChatPane } from "./SideChatPane";

vi.mock("@/store/chatStore", async (importOriginal) => ({
  ...(await importOriginal<typeof ChatStoreModule>()),
  ensureConversationStreamed: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("@/components/composer/ComposerAddMenu", () => ({ ComposerAddMenu: () => null }));
vi.mock("@/components/ComposerMicButton", () => ({ ComposerMicButton: () => null }));
const sessionLabels = vi.hoisted(() => ({ current: {} as Record<string, string> }));
vi.mock("@/hooks/useSession", () => ({
  useSession: () => ({ session: { labels: sessionLabels.current }, isLoading: false, error: null }),
}));
vi.mock("@/hooks/useWorkingLabelTick", () => ({ useWorkingLabelTick: () => 0 }));

const initialStoreState = useChatStore.getState();
const send = vi.fn<ChatState["send"]>();
const childId = "conv_side_child";
const queryClient = new QueryClient();
const renderPane = (ui: ReactNode) =>
  render(ui, {
    wrapper: ({ children }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    ),
  });

beforeEach(() => {
  sessionLabels.current = {};
  conversationRegistry.clear();
  send.mockReset().mockResolvedValue(undefined);
  vi.spyOn(sessionsApi, "interrupt").mockResolvedValue({ queued: true });
  vi.spyOn(sessionsApi, "stopSession").mockResolvedValue({ queued: true });
  vi.spyOn(toast, "error").mockReturnValue("interrupt_error");
  useChatStore.setState({
    ...initialStoreState,
    conversationId: "conv_main",
    sessionStatus: "idle",
    status: "idle",
    blockedOn: null,
    backgroundTaskCount: 0,
    sideChatDrafts: {},
    send,
  });
  conversationRegistry.acquire(childId).setState({
    sessionHarness: "codex-native",
    boundAgentId: "agent_side",
    sessionStatus: "running",
    loadingConversation: false,
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  conversationRegistry.clear();
  useChatStore.setState(initialStoreState);
  localStorage.clear();
});

describe("side-chat working indicator", () => {
  it("shows progress before a native child's first transcript bubble arrives", () => {
    renderPane(<SideChatPane childId={childId} />);

    expect(screen.getByTestId("working-indicator")).toHaveTextContent("Working…");
    expect(
      screen.queryByText("Ask a question here without affecting the main conversation."),
    ).toBeNull();

    act(() => conversationRegistry.acquire(childId).setState({ sessionStatus: "idle" }));

    expect(screen.queryByTestId("working-indicator")).toBeNull();
    expect(
      screen.getByText("Ask a question here without affecting the main conversation."),
    ).toBeInTheDocument();
  });

  it.each([
    { blockedOn: "dialog open", backgroundTaskCount: 0 },
    { blockedOn: null, backgroundTaskCount: 1 },
  ])("does not inherit the parent's blocked/background state: %o", (parentState) => {
    useChatStore.setState(parentState);
    renderPane(<SideChatPane childId={childId} />);

    expect(screen.getByTestId("working-indicator")).toHaveTextContent("Working…");
  });

  it("updates the working label from the child's own blocked state", () => {
    renderPane(<SideChatPane childId={childId} />);

    act(() =>
      conversationRegistry.acquire(childId).setState({
        sessionStatus: "waiting",
        blockedOn: "tool approval",
        activeResponse: { responseId: "codex_turn_side", state: "streaming", error: null },
      }),
    );

    expect(screen.getByTestId("working-indicator")).toHaveTextContent("Blocked on: tool approval");
    expect(screen.getByRole("button", { name: "Interrupt side chat" })).toBeEnabled();
  });

  it("shows progress while creating a fork and restores the draft after failure", async () => {
    let rejectStart: ((error: Error) => void) | undefined;
    const onStart = vi.fn(
      () =>
        new Promise<void>((_resolve, reject) => {
          rejectStart = reject;
        }),
    );
    useChatStore.setState({ blockedOn: "dialog open", backgroundTaskCount: 1 });
    renderPane(<SideChatPane childId="pending:side" onStart={onStart} />);
    const input = screen.getByTestId("side-chat-input");
    fireEvent.change(input, { target: { value: "Explain the approach" } });
    fireEvent.click(screen.getByRole("button", { name: "Send side question" }));

    expect(onStart).toHaveBeenCalledExactlyOnceWith("Explain the approach");
    expect(screen.getByTestId("working-indicator")).toHaveTextContent("Working…");
    expect(screen.queryByTestId("side-chat-interrupt")).toBeNull();
    expect(input).toBeDisabled();

    await act(async () => rejectStart?.(new Error("Fork creation failed")));

    expect(screen.queryByTestId("working-indicator")).toBeNull();
    expect(input).toBeEnabled();
    expect(input).toHaveValue("Explain the approach");
    expect(screen.getByRole("button", { name: "Send side question" })).toBeEnabled();
  });
});

describe("side-chat interrupt", () => {
  beforeEach(() => {
    conversationRegistry.acquire(childId).setState({
      activeResponse: { responseId: "codex_turn_side", state: "streaming", error: null },
    });
  });

  it("waits for an observed native turn while showing immediate follow-up progress", async () => {
    conversationRegistry.acquire(childId).setState({
      sessionStatus: "idle",
      status: "streaming",
      activeResponse: null,
    });
    renderPane(<SideChatPane childId={childId} />);

    expect(screen.getByTestId("working-indicator")).toHaveTextContent("Working…");
    const interrupt = screen.getByRole("button", { name: "Interrupt side chat" });
    expect(interrupt).toBeDisabled();
    fireEvent.click(interrupt);
    expect(sessionsApi.interrupt).not.toHaveBeenCalled();

    act(() =>
      conversationRegistry.acquire(childId).setState({
        activeResponse: { responseId: "codex_turn_followup", state: "streaming", error: null },
      }),
    );

    expect(interrupt).toBeEnabled();
    fireEvent.click(interrupt);
    expect(sessionsApi.interrupt).toHaveBeenCalledExactlyOnceWith(childId, "codex_turn_followup");
    await waitFor(() => expect(interrupt).toBeEnabled());
  });

  it("keeps generic side chats interruptible before a response ID arrives", async () => {
    conversationRegistry.acquire(childId).setState({
      sessionHarness: "claude-native",
      sessionStatus: "idle",
      status: "streaming",
      activeResponse: null,
    });
    renderPane(<SideChatPane childId={childId} />);

    expect(screen.getByTestId("working-indicator")).toHaveTextContent("Working…");
    const interrupt = screen.getByRole("button", { name: "Interrupt side chat" });
    expect(interrupt).toBeEnabled();
    fireEvent.click(interrupt);
    expect(sessionsApi.interrupt).toHaveBeenCalledExactlyOnceWith(childId, undefined);
    await waitFor(() => expect(interrupt).toBeEnabled());
  });

  it("does not target a completed native response while the session status settles", () => {
    conversationRegistry.acquire(childId).setState({
      activeResponse: { responseId: "codex_turn_side", state: "completed", error: null },
    });
    renderPane(<SideChatPane childId={childId} />);

    const interrupt = screen.getByRole("button", { name: "Interrupt side chat" });
    expect(interrupt).toBeDisabled();
    fireEvent.click(interrupt);
    expect(sessionsApi.interrupt).not.toHaveBeenCalled();
  });

  it("targets the native child's active response rather than the parent's response", async () => {
    useChatStore.setState({
      sessionStatus: "running",
      activeResponse: { responseId: "resp_main", state: "streaming", error: null },
    });
    renderPane(<SideChatPane childId={childId} />);

    fireEvent.click(screen.getByRole("button", { name: "Interrupt side chat" }));

    expect(sessionsApi.interrupt).toHaveBeenCalledExactlyOnceWith(childId, "codex_turn_side");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Interrupt side chat" })).toBeEnabled(),
    );
    expect(useChatStore.getState().activeResponse?.responseId).toBe("resp_main");
  });

  it("interrupts only the child, coalesces clicks, and preserves the unsent draft", async () => {
    let finishInterrupt: ((result: sessionsApi.PostEventResponse) => void) | undefined;
    vi.mocked(sessionsApi.interrupt).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishInterrupt = resolve;
        }),
    );
    useChatStore.setState({ sessionStatus: "running" });
    renderPane(<SideChatPane childId={childId} />);
    const input = screen.getByTestId("side-chat-input");
    fireEvent.change(input, { target: { value: "Keep this follow-up" } });
    fireEvent.keyDown(input, { key: "Enter" });
    const interrupt = screen.getByRole("button", { name: "Interrupt side chat" });
    fireEvent.click(interrupt);
    fireEvent.click(interrupt);

    expect(sessionsApi.interrupt).toHaveBeenCalledExactlyOnceWith(childId, "codex_turn_side");
    expect(interrupt).toBeDisabled();
    expect(input).toHaveValue("Keep this follow-up");
    expect(send).not.toHaveBeenCalled();
    expect(useChatStore.getState().sessionStatus).toBe("running");

    await act(async () => finishInterrupt?.({ queued: true }));
    act(() => conversationRegistry.acquire(childId).setState({ sessionStatus: "idle" }));

    expect(screen.queryByTestId("side-chat-interrupt")).toBeNull();
    expect(input).toHaveValue("Keep this follow-up");
    fireEvent.click(screen.getByRole("button", { name: "Send side question" }));
    expect(send).toHaveBeenCalledExactlyOnceWith("Keep this follow-up", "agent_side", undefined, {
      pinnedConversationId: childId,
      onError: expect.any(Function),
    });
    expect(useChatStore.getState().sessionStatus).toBe("running");
  });

  it("reports a failed interrupt and allows retry without clearing the draft", async () => {
    vi.mocked(sessionsApi.interrupt).mockRejectedValueOnce(new Error("Host unavailable"));
    renderPane(<SideChatPane childId={childId} />);
    const input = screen.getByTestId("side-chat-input");
    fireEvent.change(input, { target: { value: "Keep this draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Interrupt side chat" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledOnce());
    expect(toast.error).toHaveBeenCalledWith(expect.stringMatching(/interrupt.*try again/i));
    const interrupt = screen.getByRole("button", { name: "Interrupt side chat" });
    await waitFor(() => expect(interrupt).toBeEnabled());
    expect(input).toHaveValue("Keep this draft");
    expect(send).not.toHaveBeenCalled();

    fireEvent.click(interrupt);
    await waitFor(() => expect(sessionsApi.interrupt).toHaveBeenCalledTimes(2));
    expect(sessionsApi.interrupt).toHaveBeenLastCalledWith(childId, "codex_turn_side");
  });

  it.each([
    { name: "idle", id: childId, readOnly: false, sessionStatus: "idle" as const },
    { name: "pending", id: "pending:side", readOnly: false, sessionStatus: "running" as const },
    { name: "read-only", id: childId, readOnly: true, sessionStatus: "running" as const },
  ])("has no interrupt button for a $name side chat", ({ id, readOnly, sessionStatus }) => {
    conversationRegistry.acquire(childId).setState({ sessionStatus });
    useChatStore.setState({ sessionStatus: "running" });

    renderPane(<SideChatPane childId={id} readOnly={readOnly} />);

    expect(screen.queryByTestId("side-chat-interrupt")).toBeNull();
    expect(sessionsApi.interrupt).not.toHaveBeenCalled();
  });
});

describe("side chat sealed by the server", () => {
  it("is read-only once the server marks the child closed", () => {
    sessionLabels.current = { "omnigent.closed": "true" };
    renderPane(<SideChatPane childId={childId} />);

    expect(
      screen.getByText("This side chat has ended and can’t be continued."),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("side-chat-input")).toBeNull();
  });

  it("re-reads the child's labels after the opening /side send settles", async () => {
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    useChatStore.setState({ sideChatDrafts: { [childId]: "why?" } });
    renderPane(<SideChatPane childId={childId} />);

    await waitFor(() =>
      expect(send).toHaveBeenCalledWith("why?", "agent_side", undefined, {
        pinnedConversationId: childId,
      }),
    );
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ["session", childId] }),
    );
  });

  it("re-reads the child's labels after a send settles", async () => {
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    act(() => conversationRegistry.acquire(childId).setState({ sessionStatus: "idle" }));
    renderPane(<SideChatPane childId={childId} />);

    fireEvent.change(screen.getByTestId("side-chat-input"), { target: { value: "hi" } });
    fireEvent.keyDown(screen.getByTestId("side-chat-input"), { key: "Enter" });

    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ["session", childId] }),
    );
  });
});
