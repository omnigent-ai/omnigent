import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
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

describe("UserBubble literal text", () => {
  it.each([
    [
      "unfinished placeholder",
      "how about to reduce the output you can do like\n• ••\n" +
        "<exact line(s) that needs to be seen without edit\n" +
        "so for any matching line in the output which shows it, dont edit it or excerpt it, " +
        "if any of the line shows important info",
    ],
    ["complete placeholder", "Keep <exact lines> visible."],
    ["HTML example", '<div class="example">Keep this text</div>'],
    ["HTML comment", "Keep <!-- this comment --> visible."],
    ["multiline HTML", "<div>\n  first line\n  second line\n</div>"],
  ])("preserves %s", (_name, text) => {
    render(
      <BubbleView
        bubble={{
          kind: "user",
          itemId: "user_literal",
          content: [{ type: "input_text", text }],
        }}
        isLastAssistant={false}
      />,
    );

    const bubble = screen.getByTestId("message-bubble");
    for (const line of text.split("\n")) {
      expect(bubble).toHaveTextContent(line.trim());
    }
  });

  it("keeps Markdown formatting and inline code alongside literal tags", () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <BubbleView
          bubble={{
            kind: "user",
            itemId: "user_markdown",
            content: [
              {
                type: "input_text",
                text: "**Keep** <exact lines> and `<code>`\n\n- first\n- second",
              },
            ],
          }}
          isLastAssistant={false}
        />
      </QueryClientProvider>,
    );

    expect(screen.getByText("Keep")).toHaveAttribute("data-streamdown", "strong");
    expect(screen.getByText("<code>").tagName).toBe("CODE");
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByTestId("message-bubble")).toHaveTextContent("<exact lines>");
  });
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

describe("UserBubble long-prompt collapse", () => {
  // A unique tail that appears only after the 8 000-code-point boundary.
  const TAIL = "UNIQUE_TAIL";

  // 8 000 ASCII characters followed by the tail
  const LONG_TEXT = "a".repeat(8000) + TAIL;

  // An emoji placed at exactly the 8 000th code-point position.
  const EMOJI_AT_BOUNDARY = "a".repeat(7999) + "🔥" + "b".repeat(100);

  const SHORT_TEXT = "Hello, world!";

  function userBubble(text: string): Extract<Bubble, { kind: "user" }> {
    return {
      kind: "user",
      itemId: "user_collapse_test",
      content: [{ type: "input_text", text }],
    };
  }

  it("renders a short prompt fully without a collapse button", () => {
    render(<BubbleView bubble={userBubble(SHORT_TEXT)} isLastAssistant={false} />);

    expect(screen.getByTestId("message-bubble")).toHaveTextContent(SHORT_TEXT);
    expect(screen.queryByRole("button", { name: /show full prompt/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /collapse prompt/i })).toBeNull();
  });

  it("hides the tail when collapsed, shows it when expanded, hides it again when re-collapsed", () => {
    render(<BubbleView bubble={userBubble(LONG_TEXT)} isLastAssistant={false} />);

    const bubble = screen.getByTestId("message-bubble");

    // Initially collapsed
    expect(bubble).not.toHaveTextContent(TAIL);
    expect(screen.getByRole("button", { name: /show full prompt/i })).toBeInTheDocument();

    // After expanding
    fireEvent.click(screen.getByRole("button", { name: /show full prompt/i }));
    expect(bubble).toHaveTextContent(TAIL);

    // After collapsing again
    fireEvent.click(screen.getByRole("button", { name: /collapse prompt/i }));
    expect(bubble).not.toHaveTextContent(TAIL);
  });

  it("Copy always writes the full text to the clipboard regardless of collapse state", async () => {
    const writtenTexts: string[] = [];
    vi.stubGlobal("navigator", {
      clipboard: {
        writeText: vi.fn((text: string) => {
          writtenTexts.push(text);
          return Promise.resolve();
        }),
      },
    });

    render(<BubbleView bubble={userBubble(LONG_TEXT)} isLastAssistant={false} />);

    const copyButton = screen.getByRole("button", { name: /^copy$/i });
    fireEvent.click(copyButton);

    await waitFor(() => expect(writtenTexts).toHaveLength(1));
    expect(writtenTexts[0]).toBe(LONG_TEXT);
    expect(writtenTexts[0]).toContain(TAIL);
  });

  it("does not corrupt an emoji at the 8000-code-point slice boundary", () => {
    render(<BubbleView bubble={userBubble(EMOJI_AT_BOUNDARY)} isLastAssistant={false} />);

    const bubble = screen.getByTestId("message-bubble");

    expect(screen.getByRole("button", { name: /show full prompt/i })).toBeInTheDocument();
    expect(bubble).toHaveTextContent("🔥");
  });
});
