import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createPortal } from "react-dom";
import { StrictMode } from "react";
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
    sideChatComposers: {},
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
    const pane = (
      <SideChatPane childId="pending:side" selectionParentId="conv_main" onStart={onStart} />
    );
    const view = renderPane(pane);
    const input = screen.getByTestId("side-chat-input");
    fireEvent.change(input, { target: { value: "Explain the approach" } });
    fireEvent.click(screen.getByRole("button", { name: "Send side question" }));

    expect(onStart).toHaveBeenCalledExactlyOnceWith("Explain the approach");
    expect(screen.getByTestId("working-indicator")).toHaveTextContent("Working…");
    expect(screen.queryByTestId("side-chat-interrupt")).toBeNull();
    expect(input).toBeDisabled();
    expect(useChatStore.getState().sideChatSelectionTarget).toBeNull();

    view.unmount();
    renderPane(pane);
    expect(screen.getByTestId("side-chat-input")).toBeDisabled();
    expect(useChatStore.getState().sideChatSelectionTarget).toBeNull();

    await act(async () => rejectStart?.(new Error("Fork creation failed")));

    expect(screen.queryByTestId("working-indicator")).toBeNull();
    expect(screen.getByTestId("side-chat-input")).toBeEnabled();
    expect(screen.getByTestId("side-chat-input")).toHaveValue("Explain the approach");
    expect(useChatStore.getState().sideChatSelectionTarget?.childId).toBe("pending:side");
    expect(screen.getByRole("button", { name: "Send side question" })).toBeEnabled();
  });
});

describe("side chat opened from a text selection", () => {
  it("quotes the selection in the pending tab and sends it with the question", async () => {
    const onStart = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ sideChatDrafts: { "pending:quoted": "restore the row" } });
    renderPane(<SideChatPane childId="pending:quoted" onStart={onStart} />);

    expect(screen.getByTestId("composer-reply-quote")).toHaveTextContent("restore the row");
    expect(screen.getByTestId("side-chat-input")).toHaveFocus();
    fireEvent.change(screen.getByTestId("side-chat-input"), { target: { value: "why?" } });
    fireEvent.click(screen.getByRole("button", { name: "Send side question" }));

    expect(onStart).toHaveBeenCalledExactlyOnceWith("> restore the row\n\nwhy?");
    await waitFor(() => expect(useChatStore.getState().sideChatDrafts).toEqual({}));
  });

  it("adds quotes to a live draft without sending until the user submits", () => {
    conversationRegistry.acquire(childId).setState({ sessionStatus: "idle" });
    useChatStore.setState({
      sideChatComposers: {
        [childId]: { text: "why?", files: [], quotes: ["first selection", "second selection"] },
      },
    });
    renderPane(<SideChatPane childId={childId} selectionParentId="conv_main" />);

    expect(useChatStore.getState().sideChatSelectionTarget).toEqual({
      childId,
      parentId: "conv_main",
    });
    expect(screen.getAllByTestId("composer-reply-quote")).toHaveLength(2);
    expect(screen.getByTestId("side-chat-input")).toHaveFocus();
    expect(send).not.toHaveBeenCalled();
    fireEvent.click(screen.getAllByRole("button", { name: "Remove quote" })[0]);
    fireEvent.click(screen.getByRole("button", { name: "Send side question" }));
    expect(send).toHaveBeenCalledWith("> second selection\n\nwhy?", "agent_side", undefined, {
      pinnedConversationId: childId,
      onError: expect.any(Function),
    });
    expect(useChatStore.getState().sideChatComposers[childId]).toBeUndefined();
  });

  it("restores the quotes with the text when a live send fails", () => {
    conversationRegistry.acquire(childId).setState({ sessionStatus: "idle" });
    useChatStore.setState({
      sideChatComposers: { [childId]: { text: "why?", files: [], quotes: ["the selection"] } },
    });
    renderPane(<SideChatPane childId={childId} selectionParentId="conv_main" />);

    fireEvent.click(screen.getByRole("button", { name: "Send side question" }));
    expect(useChatStore.getState().sideChatComposers[childId]).toBeUndefined();
    act(() => send.mock.calls[0]?.[3]?.onError?.("Send failed"));

    expect(useChatStore.getState().sideChatComposers[childId]).toMatchObject({
      text: "why?",
      quotes: ["the selection"],
    });
    expect(screen.getByTestId("composer-reply-quote")).toHaveTextContent("the selection");
    expect(screen.getByRole("alert")).toHaveTextContent("Send failed");
  });

  it("clears the selection target when the pane is hidden or read-only", () => {
    const view = renderPane(<SideChatPane childId={childId} selectionParentId="conv_main" />);
    expect(useChatStore.getState().sideChatSelectionTarget?.childId).toBe(childId);
    view.rerender(<SideChatPane childId={childId} />);
    expect(useChatStore.getState().sideChatSelectionTarget).toBeNull();
    view.rerender(<SideChatPane childId={childId} selectionParentId="conv_main" readOnly />);
    expect(useChatStore.getState().sideChatSelectionTarget).toBeNull();
  });

  it("drops the quote when its card is removed", () => {
    const onStart = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ sideChatDrafts: { "pending:quoted": "restore the row" } });
    renderPane(<SideChatPane childId="pending:quoted" onStart={onStart} />);

    fireEvent.click(screen.getByRole("button", { name: "Remove quote" }));
    fireEvent.change(screen.getByTestId("side-chat-input"), { target: { value: "why?" } });
    fireEvent.click(screen.getByRole("button", { name: "Send side question" }));

    expect(screen.queryByTestId("composer-reply-quote")).toBeNull();
    expect(onStart).toHaveBeenCalledExactlyOnceWith("why?");
  });
});

describe("side-chat selection reply", () => {
  function selectText(element: HTMLElement) {
    const range = document.createRange();
    range.selectNodeContents(element);
    range.getBoundingClientRect = () => ({ left: 10, top: 30, width: 80 }) as DOMRect;
    const selection = window.getSelection()!;
    selection.removeAllRanges();
    selection.addRange(range);
    fireEvent(document, new Event("selectionchange"));
  }

  afterEach(() => window.getSelection()?.removeAllRanges());

  it("quotes selected side-chat text in its own composer and preserves the draft", () => {
    conversationRegistry.acquire(childId).setState({ sessionStatus: "idle" });
    useChatStore.setState({
      sideChatComposers: {
        [childId]: { text: "my follow-up", files: [], quotes: ["existing quote"] },
      },
    });
    renderPane(<SideChatPane childId={childId} />);
    selectText(screen.getByText("Ask a question here without affecting the main conversation."));
    fireEvent.click(screen.getByRole("button", { name: "Reply ↵" }));

    expect(useChatStore.getState().sideChatComposers[childId]).toEqual({
      text: "my follow-up",
      files: [],
      quotes: ["existing quote", "Ask a question here without affecting the main conversation."],
    });
    expect(screen.getByTestId("side-chat-input")).toHaveFocus();
    expect(screen.queryByRole("button", { name: "Reply ↵" })).toBeNull();
    expect(useChatStore.getState().sideChatToOpen).toBeNull();
    expect(send).not.toHaveBeenCalled();
  });

  it("does not offer Reply in a read-only side chat", () => {
    renderPane(<SideChatPane childId={childId} readOnly />);
    selectText(screen.getByText("This side chat has ended and can’t be continued."));
    expect(screen.queryByRole("button", { name: "Reply ↵" })).toBeNull();
  });

  it("does not offer Reply while a side chat is starting", () => {
    useChatStore.setState({
      sideChatComposers: { "pending:starting": { text: "Question", files: [], starting: true } },
    });
    renderPane(<SideChatPane childId="pending:starting" />);
    selectText(screen.getByTestId("working-indicator"));
    expect(screen.queryByRole("button", { name: "Reply ↵" })).toBeNull();
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
    expect(sessionsApi.stopSession).not.toHaveBeenCalled();
  });
});

describe("seeded /side question survives an unmount before the child binds", () => {
  // Switching rail tabs (or crossing the mobile breakpoint) unmounts this pane.
  // The question must not die with it: it stays in the store until the send
  // actually dispatches, which needs the child's agent binding.
  it("defers the send while unbound, then sends exactly once on remount", async () => {
    conversationRegistry.acquire(childId).setState({ boundAgentId: null });
    useChatStore.setState({ sideChatDrafts: { [childId]: "why backoff?" } });

    const first = renderPane(<SideChatPane childId={childId} />);
    expect(send).not.toHaveBeenCalled();
    // Unmount before the binding arrives (drawer closed / tab switched).
    first.unmount();
    expect(useChatStore.getState().sideChatDrafts[childId]).toBe("why backoff?");

    // Remount with the binding known: the held question goes out, once.
    conversationRegistry.acquire(childId).setState({ boundAgentId: "agent_side" });
    renderPane(<SideChatPane childId={childId} />);

    await waitFor(() => expect(send).toHaveBeenCalledOnce());
    expect(send).toHaveBeenCalledWith("why backoff?", "agent_side", undefined, {
      pinnedConversationId: childId,
    });
    // Consumed, so a later remount can't send it a second time.
    expect(useChatStore.getState().sideChatDrafts[childId]).toBeUndefined();
    cleanup();
    renderPane(<SideChatPane childId={childId} />);
    await waitFor(() => expect(send).toHaveBeenCalledOnce());
  });

  it("sends once under StrictMode's replayed mount effect", async () => {
    // React StrictMode runs mount effects twice in development. The send must
    // consume the live store draft, not the one captured at render, or the
    // question goes out twice.
    useChatStore.setState({ sideChatDrafts: { [childId]: "why?" } });

    renderPane(
      <StrictMode>
        <SideChatPane childId={childId} />
      </StrictMode>,
    );

    await waitFor(() => expect(send).toHaveBeenCalledOnce());
    expect(useChatStore.getState().sideChatDrafts[childId]).toBeUndefined();
  });

  it("sends once when the binding arrives while still mounted", async () => {
    conversationRegistry.acquire(childId).setState({ boundAgentId: null });
    useChatStore.setState({ sideChatDrafts: { [childId]: "why?" } });
    renderPane(<SideChatPane childId={childId} />);
    expect(send).not.toHaveBeenCalled();

    act(() => conversationRegistry.acquire(childId).setState({ boundAgentId: "agent_side" }));

    await waitFor(() => expect(send).toHaveBeenCalledOnce());
    expect(useChatStore.getState().sideChatDrafts[childId]).toBeUndefined();
  });
});

describe("unsent composer state survives the pane moving", () => {
  // The pane mounts in the desktop rail OR the mobile drawer's portal, so
  // crossing the `md` breakpoint (a phone rotating) moves it between subtrees
  // and unmounts it — as does switching rail tabs. Its text and attachments
  // must not go with it, so they live in the store keyed by child id.
  function Host({ mobile }: { mobile: boolean }) {
    const pane = <SideChatPane childId={childId} />;
    return (
      <>
        <div data-testid="rail">{mobile ? <div data-testid="files" /> : pane}</div>
        {mobile && createPortal(<div data-testid="drawer">{pane}</div>, document.body)}
      </>
    );
  }

  it("keeps text and attachments across the mobile/desktop breakpoint", () => {
    const view = renderPane(<Host mobile />);
    fireEvent.change(screen.getByTestId("side-chat-input"), {
      target: { value: "half-typed question" },
    });
    const file = new File(["x"], "notes.txt", { type: "text/plain" });
    // The attach input is hidden (the "+" tray proxies to it), so no test id.
    fireEvent.change(document.querySelector('input[type="file"]')!, {
      target: { files: [file] },
    });
    expect(screen.getByText("notes.txt")).toBeInTheDocument();

    // Rotate to landscape: the rail takes over and the portal goes away.
    act(() => view.rerender(<Host mobile={false} />));

    expect(screen.getByTestId("side-chat-input")).toHaveValue("half-typed question");
    expect(screen.getByText("notes.txt")).toBeInTheDocument();

    // And back to portrait.
    act(() => view.rerender(<Host mobile />));
    expect(screen.getByTestId("side-chat-input")).toHaveValue("half-typed question");
  });

  it("drops the draft once it is sent", async () => {
    // Idle, so the trailing button is Send rather than Interrupt.
    conversationRegistry.acquire(childId).setState({ sessionStatus: "idle" });
    renderPane(<SideChatPane childId={childId} />);
    fireEvent.change(screen.getByTestId("side-chat-input"), { target: { value: "ask this" } });
    fireEvent.click(screen.getByTestId("side-chat-send"));

    await waitFor(() => expect(send).toHaveBeenCalledOnce());
    expect(useChatStore.getState().sideChatComposers[childId]).toBeUndefined();
    expect(screen.getByTestId("side-chat-input")).toHaveValue("");
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
    expect(sessionsApi.stopSession).not.toHaveBeenCalled();
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

describe("side-chat composer auto-grow", () => {
  it("applies auto-grow to the text input so it grows with its content", () => {
    renderPane(<SideChatPane childId={childId} />);
    const input = screen.getByTestId("side-chat-input") as HTMLTextAreaElement;
    // useAutoGrowTextarea sets style.height="auto" during its initial measure
    // when scrollHeight is 0 (jsdom has no layout). A textarea that doesn't
    // have auto-grow wired up keeps style.height="" (the browser default).
    expect(input.style.height).toBe("auto");
  });
});
