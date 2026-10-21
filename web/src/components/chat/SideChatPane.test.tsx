import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createPortal } from "react-dom";
import { StrictMode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { toast } from "sonner";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as filesApi from "@/lib/filesApi";
import { serializeReplyDraft, type StoredReplyDraft } from "@/lib/replyDraft";
import * as sessionsApi from "@/lib/sessionsApi";
import type * as ChatStoreModule from "@/store/chatStore";
import { useChatStore, type ChatState } from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";
import { itemsToBlocks } from "@/lib/itemsToBlocks";
import { SideChatPane } from "./SideChatPane";

vi.mock("@/store/chatStore", async (importOriginal) => ({
  ...(await importOriginal<typeof ChatStoreModule>()),
  ensureConversationStreamed: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("@/components/composer/ComposerAddMenu", () => ({ ComposerAddMenu: () => null }));
vi.mock("@/components/ComposerMicButton", () => ({ ComposerMicButton: () => null }));
const sessionLabels = vi.hoisted(() => ({ current: {} as Record<string, string> }));
// A conversation id whose snapshot the mock should report as still loading.
const loadingSession = vi.hoisted(() => ({ id: null as string | null }));
vi.mock("@/hooks/useSession", () => ({
  useSession: (id?: string | null) => ({
    session: { labels: sessionLabels.current },
    isLoading: id != null && id === loadingSession.id,
    error: null,
  }),
}));
vi.mock("@/hooks/useWorkingLabelTick", () => ({ useWorkingLabelTick: () => 0 }));
const skillsFixture = vi.hoisted(() => ({
  current: {
    skills: [] as { name: string; description: string }[],
    skillsStatus: null as string | null,
  },
  lastOptions: null as { target: unknown; enabled?: boolean; starting?: boolean } | null,
}));
vi.mock("@/hooks/useSkills", () => ({
  useSkills: (options: { target: unknown; enabled?: boolean; starting?: boolean }) => {
    skillsFixture.lastOptions = options;
    return { ...skillsFixture.current, refetch: vi.fn() };
  },
}));

const initialStoreState = useChatStore.getState();
const send = vi.fn<ChatState["send"]>();
const sendSlashCommand = vi.fn<ChatState["sendSlashCommand"]>();
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
  loadingSession.id = null;
  conversationRegistry.clear();
  send.mockReset().mockResolvedValue(undefined);
  sendSlashCommand.mockReset().mockResolvedValue(undefined);
  skillsFixture.current = { skills: [], skillsStatus: null };
  skillsFixture.lastOptions = null;
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
    sendSlashCommand,
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
  it("shows the full session history and live state without exposing a read-only composer", () => {
    const entry = conversationRegistry.acquire(childId);
    entry.setState({ sessionHarness: "openai-agents", loadingConversation: true });
    renderPane(<SideChatPane childId={childId} fullSession readOnly />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading session");
    act(() =>
      entry.setState({
        loadingConversation: false,
        blocks: itemsToBlocks([
          {
            id: "history",
            response_id: "turn",
            type: "message",
            role: "user",
            status: "completed",
            content: [{ type: "input_text", text: "Earlier session history" }],
          },
        ]),
      }),
    );
    expect(screen.getByText("Earlier session history")).toBeInTheDocument();
    expect(screen.getByTestId("working-indicator")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(localStorage.getItem(`omnigent.sideChatInherited:${childId}`)).toBeNull();
  });
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
  it.each([false, true])("preserves list indentation when sending (pending: %s)", (pending) => {
    const onStart = vi.fn().mockResolvedValue(undefined);
    const id = pending ? "pending:side" : childId;
    conversationRegistry.acquire(childId).setState({ sessionStatus: "idle" });
    renderPane(<SideChatPane childId={id} onStart={onStart} />);
    const text = "  - First\n  - Second";
    fireEvent.change(screen.getByTestId("side-chat-input"), { target: { value: text } });
    fireEvent.click(screen.getByRole("button", { name: "Send side question" }));
    if (pending) {
      expect(onStart).toHaveBeenCalledExactlyOnceWith(text);
    } else {
      expect(send).toHaveBeenCalledExactlyOnceWith(text, "agent_side", undefined, {
        pinnedConversationId: childId,
      });
    }
  });

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
    });
    expect(useChatStore.getState().sideChatComposers[childId]).toBeUndefined();
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

  it.each([false, true])(
    "drops only its own draft once sent (full session: %s)",
    async (fullSession) => {
      // Idle, so the trailing button is Send rather than Interrupt.
      conversationRegistry.acquire(childId).setState({ sessionStatus: "idle" });
      useChatStore
        .getState()
        .updateSideChatComposer("other-pane", (current) => ({ ...current, text: "keep me" }));
      renderPane(<SideChatPane childId={childId} fullSession={fullSession} />);
      fireEvent.change(screen.getByTestId("side-chat-input"), { target: { value: "ask this" } });
      fireEvent.click(screen.getByTestId("side-chat-send"));

      await waitFor(() => expect(send).toHaveBeenCalledOnce());
      expect(useChatStore.getState().sideChatComposers[childId]).toBeUndefined();
      expect(screen.getByTestId("side-chat-input")).toHaveValue("");
      expect(send).toHaveBeenCalledWith("ask this", "agent_side", undefined, {
        pinnedConversationId: childId,
      });
      expect(useChatStore.getState().sideChatComposers["other-pane"].text).toBe("keep me");
    },
  );
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

describe("side-chat slash commands", () => {
  const review = { name: "review", description: "Review code" };

  function typeSlash(value: string) {
    const input = screen.getByTestId("side-chat-input");
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value } });
    return input as HTMLTextAreaElement;
  }

  it("offers the child's skills when typing / in a live side chat", () => {
    conversationRegistry.acquire(childId).setState({ sessionHarness: "claude-sdk" });
    skillsFixture.current = { skills: [review], skillsStatus: "ready" };
    renderPane(<SideChatPane childId={childId} />);
    const input = typeSlash("/");
    const row = screen.getByTestId("slash-menu-item-review");
    expect(row).toBeVisible();
    // The menu lists the local /help built-in alongside the session's skills.
    expect(screen.getByTestId("slash-menu-item-help")).toBeVisible();
    // Choosing a row completes the command into the draft instead of sending.
    fireEvent.click(row);
    expect(input.value.startsWith("/review")).toBe(true);
    expect(send).not.toHaveBeenCalled();
    expect(sendSlashCommand).not.toHaveBeenCalled();
  });

  it("routes a known skill through the slash_command path on an in-process harness", () => {
    conversationRegistry
      .acquire(childId)
      .setState({ sessionHarness: "claude-sdk", sessionStatus: "idle" });
    skillsFixture.current = { skills: [review], skillsStatus: "ready" };
    renderPane(<SideChatPane childId={childId} />);
    typeSlash("/review 123");
    fireEvent.click(screen.getByRole("button", { name: "Send side question" }));
    expect(sendSlashCommand).toHaveBeenCalledExactlyOnceWith("review", "123", "agent_side", {
      pinnedConversationId: childId,
    });
    expect(send).not.toHaveBeenCalled();
    expect(useChatStore.getState().sideChatComposers[childId]).toBeUndefined();
  });

  it("prefers a multi-word skill over a built-in matching only its first word", () => {
    conversationRegistry
      .acquire(childId)
      .setState({ sessionHarness: "claude-sdk", sessionStatus: "idle" });
    const helpDesk = { name: "help desk", description: "Open a help-desk ticket" };
    skillsFixture.current = { skills: [helpDesk], skillsStatus: "ready" };
    renderPane(<SideChatPane childId={childId} />);
    typeSlash("/help desk summarize");
    fireEvent.click(screen.getByRole("button", { name: "Send side question" }));
    // "/help desk" is a catalog skill whose first word collides with the /help
    // built-in; the multi-word skill wins and dispatches instead of showing help.
    expect(sendSlashCommand).toHaveBeenCalledExactlyOnceWith(
      "help desk",
      "summarize",
      "agent_side",
      { pinnedConversationId: childId },
    );
    expect(send).not.toHaveBeenCalled();
    expect(screen.queryByText(/Show available slash commands/)).toBeNull();
  });

  it("shows the built-in /help, not a skill that collides with its exact name", () => {
    conversationRegistry
      .acquire(childId)
      .setState({ sessionHarness: "claude-sdk", sessionStatus: "idle" });
    // A catalog skill literally named "help" can never run — the built-in wins —
    // so the menu must advertise the built-in, not the skill's description.
    skillsFixture.current = {
      skills: [{ name: "help", description: "Run the help skill" }],
      skillsStatus: "ready",
    };
    renderPane(<SideChatPane childId={childId} />);
    typeSlash("/");
    expect(screen.getByTestId("slash-menu-item-help")).toHaveTextContent(
      "Show available slash commands",
    );
    expect(screen.queryByText(/Run the help skill/)).toBeNull();
    // Submitting /help runs the built-in locally; the skill is never dispatched.
    typeSlash("/help");
    fireEvent.click(screen.getByTestId("side-chat-send"));
    expect(screen.getByText(/Show available slash commands/)).toBeInTheDocument();
    expect(sendSlashCommand).not.toHaveBeenCalled();
    expect(send).not.toHaveBeenCalled();
  });

  it("treats a still-loading parent lookup as skill discovery in flight", () => {
    conversationRegistry
      .acquire(childId)
      .setState({ sessionHarness: "claude-sdk", sessionStatus: "idle" });
    // The child names its parent, but that snapshot is still loading, so the skill
    // target is unresolved. Reporting discovery as in-flight (not "unavailable")
    // is what later gates a quickly typed /skill from going out as plaintext.
    sessionLabels.current = { "omnigent.side_chat.source_id": "conv_parent" };
    loadingSession.id = "conv_parent";
    skillsFixture.current = { skills: [], skillsStatus: "loading" };
    renderPane(<SideChatPane childId={childId} />);
    expect(skillsFixture.lastOptions?.starting).toBe(true);
    // Submitting a lone command while discovery is in flight holds it locally
    // instead of leaking "/review" to the agent as plaintext.
    typeSlash("/review");
    fireEvent.click(screen.getByTestId("side-chat-send"));
    expect(send).not.toHaveBeenCalled();
    expect(sendSlashCommand).not.toHaveBeenCalled();
  });

  it("sends a lone command with a quote even while skill discovery loads", () => {
    conversationRegistry
      .acquire(childId)
      .setState({ sessionHarness: "claude-sdk", sessionStatus: "idle" });
    useChatStore.setState({
      sideChatComposers: {
        [childId]: { text: "", files: [], quotes: ["a selection"] },
      },
    });
    skillsFixture.current = { skills: [], skillsStatus: "loading" };
    renderPane(<SideChatPane childId={childId} selectionParentId="conv_main" />);
    typeSlash("/review");
    // A quote always routes as an ordinary message, so the discovery hold must
    // not swallow it: the command goes out with the quote instead of stalling.
    fireEvent.click(screen.getByTestId("side-chat-send"));
    expect(send).toHaveBeenCalledExactlyOnceWith(
      "> a selection\n\n/review",
      "agent_side",
      undefined,
      { pinnedConversationId: childId },
    );
    expect(sendSlashCommand).not.toHaveBeenCalled();
  });

  it.each([
    { label: "a typed skill", input: "/review 123", menuTestId: "slash-menu-item-review" },
    { label: "/help", input: "/help", menuTestId: "slash-menu-item-help" },
  ])("sends $label as plaintext on a native terminal harness", ({ input, menuTestId }) => {
    // beforeEach binds the child to codex-native: the vendor TUI reads the typed
    // text itself, so no menu opens and neither local path runs.
    conversationRegistry.acquire(childId).setState({ sessionStatus: "idle" });
    skillsFixture.current = { skills: [review], skillsStatus: "ready" };
    renderPane(<SideChatPane childId={childId} />);
    typeSlash(input);
    expect(screen.queryByTestId(menuTestId)).toBeNull();
    fireEvent.click(screen.getByTestId("side-chat-send"));
    expect(send).toHaveBeenCalledExactlyOnceWith(input, "agent_side", undefined, {
      pinnedConversationId: childId,
    });
    expect(sendSlashCommand).not.toHaveBeenCalled();
  });

  it("handles /help locally instead of forwarding it to the agent", () => {
    conversationRegistry
      .acquire(childId)
      .setState({ sessionHarness: "claude-sdk", sessionStatus: "idle" });
    skillsFixture.current = { skills: [review], skillsStatus: "ready" };
    renderPane(<SideChatPane childId={childId} />);
    typeSlash("/help");
    fireEvent.click(screen.getByTestId("side-chat-send"));
    expect(send).not.toHaveBeenCalled();
    expect(sendSlashCommand).not.toHaveBeenCalled();
    expect(screen.getByText(/Show available slash commands/)).toBeInTheDocument();
  });

  it("runs /help locally when its menu row is chosen, without sending", () => {
    conversationRegistry
      .acquire(childId)
      .setState({ sessionHarness: "claude-sdk", sessionStatus: "idle" });
    skillsFixture.current = { skills: [review], skillsStatus: "ready" };
    renderPane(<SideChatPane childId={childId} />);
    typeSlash("/help");
    fireEvent.click(screen.getByTestId("slash-menu-item-help"));
    // Choosing the built-in row runs it in place: the menu closes to the local
    // notice and neither send path is used.
    expect(screen.queryByTestId("slash-menu-item-help")).toBeNull();
    expect(screen.getByText(/Show available slash commands/)).toBeInTheDocument();
    expect(send).not.toHaveBeenCalled();
    expect(sendSlashCommand).not.toHaveBeenCalled();
  });

  it("fills the draft with a built-in on Tab instead of running it", () => {
    conversationRegistry.acquire(childId).setState({ sessionHarness: "claude-sdk" });
    skillsFixture.current = { skills: [review], skillsStatus: "ready" };
    renderPane(<SideChatPane childId={childId} />);
    const input = typeSlash("/help");
    expect(screen.getByTestId("slash-menu-item-help")).toBeVisible();
    fireEvent.keyDown(input, { key: "Tab" });
    // Running /help would clear the draft; Tab instead leaves the completed
    // command text so the user can edit it before sending.
    expect(input.value.startsWith("/help")).toBe(true);
    expect(send).not.toHaveBeenCalled();
    expect(sendSlashCommand).not.toHaveBeenCalled();
  });

  it("keeps the menu closed on a pending tab that has no session yet", () => {
    skillsFixture.current = { skills: [review], skillsStatus: "ready" };
    renderPane(
      <SideChatPane childId="pending:side" onStart={vi.fn().mockResolvedValue(undefined)} />,
    );
    typeSlash("/");
    expect(screen.queryByTestId("slash-menu-item-review")).toBeNull();
  });
});

it.each([false, true])(
  "recovers grid failures without changing ordinary side chats (grid: %s)",
  async (fullSession) => {
    const entry = conversationRegistry.acquire(childId);
    const file = new File(["notes"], "notes.txt", { type: "text/plain" });
    const replyDraft: StoredReplyDraft = {
      version: 1,
      quotes: [{ before: "Introduction", text: "Quoted" }],
      text: "Retry this",
    };
    const text = serializeReplyDraft(replyDraft);
    const failed = {
      conversationId: childId,
      text,
      replyDraft,
      files: [file],
      stableId: "retry-stable",
    };
    entry.setState({
      sessionStatus: "idle",
      abortController: new AbortController(),
      failedSendDraft: failed,
    });
    useChatStore.setState({ send: initialStoreState.send });
    renderPane(
      <StrictMode>
        <SideChatPane childId={childId} fullSession={fullSession} />
      </StrictMode>,
    );
    expect(screen.getByTestId("side-chat-input")).toHaveValue(fullSession ? text : "");
    expect(entry.getState().failedSendDraft).toBe(fullSession ? null : failed);
    if (!fullSession) return;
    expect(useChatStore.getState().sideChatComposers[childId].files).toEqual([file]);
    vi.spyOn(filesApi, "uploadFile").mockResolvedValue({
      id: "uploaded",
      filename: file.name,
      bytes: 5,
      created_at: 0,
    });
    const post = vi.spyOn(sessionsApi, "postEvent").mockResolvedValue({ queued: true });
    fireEvent.click(screen.getByTestId("side-chat-send"));
    await waitFor(() => expect(post).toHaveBeenCalledOnce());
    expect(post.mock.calls[0][1]).toMatchObject({ data: { stable_id: "retry-stable" } });
    expect(screen.getByTestId("side-chat-input")).toHaveValue("");
  },
);

it.each(["none", "text", "files", "quote", "rejected-file"] as const)(
  "retracts a delivered grid draft only while unchanged: %s",
  (edit) => {
    const entry = conversationRegistry.acquire(childId);
    const file = new File(["notes"], "notes.txt", { type: "text/plain" });
    entry.setState({
      failedSendDraft: {
        conversationId: childId,
        text: "> Quote\n\nRetry",
        files: edit === "rejected-file" ? [new File(["bad"], "bad.exe")] : [file],
        stableId: "stable",
      },
    });
    renderPane(<SideChatPane childId={childId} fullSession />);
    expect(screen.getByTestId("side-chat-input")).toHaveValue("> Quote\n\nRetry");
    act(() =>
      useChatStore.getState().updateSideChatComposer(childId, (current) => ({
        ...current,
        ...{
          none: {},
          text: { text: "Edited" },
          files: { files: [] },
          quote: { quotes: ["New quote"] },
          "rejected-file": {},
        }[edit],
      })),
    );
    act(() =>
      entry.setState({
        restoredSendDraft: { ...entry.getState().restoredSendDraft!, delivered: true },
      }),
    );
    expect(screen.getByTestId("side-chat-input")).toHaveValue(
      {
        none: "",
        text: "Edited",
        files: "> Quote\n\nRetry",
        quote: "> Quote\n\nRetry",
        "rejected-file": "",
      }[edit],
    );
    expect(entry.getState().restoredSendDraft).toBeNull();
  },
);

it.each([false, true])(
  "revalidates recovered files and preserves newer quoted drafts: %s",
  async (hasNewDraft) => {
    const entry = conversationRegistry.acquire(childId);
    entry.setState({ sessionStatus: "idle", abortController: new AbortController() });
    useChatStore.setState({ send: initialStoreState.send });
    renderPane(<SideChatPane childId={childId} fullSession />);
    if (hasNewDraft)
      act(() =>
        useChatStore
          .getState()
          .updateSideChatComposer(childId, () => ({ text: "", files: [], quotes: ["New quote"] })),
      );
    act(() =>
      entry.setState({
        failedSendDraft: {
          conversationId: childId,
          text: "Retry",
          files: [new File(["bad"], "bad.exe")],
          stableId: "stable",
        },
      }),
    );
    expect(screen.getByTestId("side-chat-input")).toHaveValue(hasNewDraft ? "" : "Retry");
    expect(useChatStore.getState().sideChatComposers[childId].files).toEqual([]);
    expect(entry.getState().pendingRetryStableId).toBeNull();
    if (hasNewDraft)
      expect(screen.getByTestId("composer-reply-quote")).toHaveTextContent("New quote");
    else {
      expect(toast.error).toHaveBeenCalledWith(expect.stringContaining("bad.exe"));
      const post = vi.spyOn(sessionsApi, "postEvent").mockResolvedValue({ queued: true });
      fireEvent.click(screen.getByTestId("side-chat-send"));
      await waitFor(() => expect(post).toHaveBeenCalledOnce());
      expect(post.mock.calls[0][1]).toMatchObject({ data: { stable_id: expect.any(String) } });
      expect(post.mock.calls[0][1]).not.toMatchObject({ data: { stable_id: "stable" } });
    }
  },
);
