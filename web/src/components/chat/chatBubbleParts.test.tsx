import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Bubble } from "@/lib/renderItems";
import { useChatStore, type ChatState } from "@/store/chatStore";
import { BubbleView } from "./chatBubbleParts";

const fetchMock = vi.fn();
const initialStoreState = useChatStore.getState();
const continuation = "Please continue from where you left off before the rate limit error.";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function errorBubble(code = "rate_limit_exceeded"): Extract<Bubble, { kind: "assistant" }> {
  return {
    kind: "assistant",
    responseId: "resp_failed",
    stableId: "error_failed",
    lifecycle: "failed",
    error: null,
    items: [
      {
        kind: "error",
        itemId: "error_failed",
        message: "API Error: Request rejected (429): workspace input tokens per minute rate limit",
        source: "execution",
        code,
      },
    ],
  };
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  useChatStore.setState({
    conversationId: "conv_retry",
    sessionStatus: "failed",
    status: "idle",
    activeResponse: null,
    blocks: [],
    pendingUserMessages: [],
    failedSendDraft: null,
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  useChatStore.setState(initialStoreState);
});

describe("AssistantBubble error retry", () => {
  it("submits one continuation for a rate limit without replaying the original input", async () => {
    let finishRetry: ((response: Response) => void) | undefined;
    fetchMock.mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          finishRetry = resolve;
        }),
    );
    const draft = { conversationId: "conv_retry", text: "My unsent draft", files: [] };
    useChatStore.setState({ failedSendDraft: draft });
    render(<BubbleView bubble={errorBubble()} isLastAssistant />);

    const retry = screen.getByRole("button", { name: "Retry" });
    fireEvent.click(retry);
    fireEvent.click(retry);

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_retry/events");
    expect(JSON.parse(init.body as string)).toEqual({
      type: "message",
      data: { role: "user", content: [{ type: "input_text", text: continuation }] },
    });
    expect(useChatStore.getState().failedSendDraft).toBe(draft);

    await act(async () => {
      finishRetry?.(jsonResponse({ queued: true, pending_id: "pending_retry" }));
    });

    expect(screen.queryByTestId("error-pill")).toBeNull();
    expect(useChatStore.getState().failedSendDraft).toBe(draft);
  });

  it("coalesces retry clicks from separate rate-limit cards in the same turn", async () => {
    let finishRetry: ((response: Response) => void) | undefined;
    fetchMock.mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          finishRetry = resolve;
        }),
    );
    const bubble = errorBubble();
    bubble.items.push({
      kind: "error",
      itemId: "error_second",
      code: "rate_limit_exceeded",
      source: "execution",
      message: "Too many requests: request rate limit exceeded",
    });
    render(<BubbleView bubble={bubble} isLastAssistant />);

    const retryButtons = screen.getAllByRole("button", { name: "Retry" });
    expect(retryButtons).toHaveLength(2);
    fireEvent.click(retryButtons[0]!);
    fireEvent.click(retryButtons[1]!);

    expect(fetchMock).toHaveBeenCalledOnce();

    await act(async () => {
      finishRetry?.(jsonResponse({ queued: true, pending_id: "pending_retry" }));
    });

    expect(screen.queryAllByTestId("error-pill")).toHaveLength(0);
  });

  it.each([
    {
      response: () =>
        jsonResponse({ error: { code: "runner_unavailable", message: "Host is offline" } }, 503),
      message: "Host is offline",
    },
    {
      response: () => jsonResponse({ queued: false, denied: true }),
      message: "The retry was blocked by a policy",
    },
  ])("preserves the card and composer draft when retry fails: $message", async (testCase) => {
    fetchMock.mockResolvedValueOnce(testCase.response());
    const draft = { conversationId: "conv_retry", text: "Keep this draft", files: [] };
    useChatStore.setState({ failedSendDraft: draft });
    render(<BubbleView bubble={errorBubble()} isLastAssistant />);

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(`Retry failed: ${testCase.message}`),
    );
    expect(screen.getByRole("button", { name: "Retry" })).toBeEnabled();
    expect(useChatStore.getState().failedSendDraft).toBe(draft);
  });

  it.each<Partial<ChatState>>([
    { sessionStatus: "launching" },
    { sessionStatus: "running" },
    { sessionStatus: "waiting" },
    { status: "streaming" },
    {
      pendingUserMessages: [
        {
          tempId: "pending_user",
          content: [{ type: "input_text", text: "A new request" }],
          createdAtS: 1,
        },
      ],
    },
  ])("does not queue a continuation while the session is busy: %o", async (state) => {
    useChatStore.setState(state);
    render(<BubbleView bubble={errorBubble()} isLastAssistant />);

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        "Wait for the current turn to finish before retrying",
      ),
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not continue an old failed turn after a newer assistant response", async () => {
    render(<BubbleView bubble={errorBubble()} isLastAssistant={false} />);

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        "Only the latest failed turn can be retried",
      ),
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not send a stale click to a newly selected session", async () => {
    render(<BubbleView bubble={errorBubble()} isLastAssistant />);
    const current = useChatStore.getState();
    vi.spyOn(useChatStore, "getState").mockReturnValue({
      ...current,
      conversationId: "conv_other",
    });

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("The selected session has changed"),
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("keeps infrastructure-error retry on the runner recovery path", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ queued: false, recovered: true, recovery: "native_terminal_ready" }),
    );
    render(<BubbleView bubble={errorBubble("required_terminal_exited")} />);

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() => expect(screen.queryByTestId("error-pill")).toBeNull());
    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_retry/events");
    expect(JSON.parse(init.body as string)).toEqual({ type: "retry_session", data: {} });
  });
});
