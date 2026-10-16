import { webcrypto } from "node:crypto";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ConversationScopeContext } from "@/components/chat/conversationScope";
import { getSessionDraft, setSessionDraft, clearSessionDrafts } from "@/lib/sessionDrafts";
import { itemsToBlocks } from "@/lib/itemsToBlocks";
import { BlockStream } from "@/lib/blockStream";
import { buildBubbles } from "@/lib/renderItems";
import { parseEventLines } from "@/lib/sse";
import type { AsyncUserInput } from "@/lib/asyncQuestions";
import { conversationRegistry } from "@/store/conversationRegistry";
import { useChatStore } from "@/store/chatStore";
import { AsyncQuestionCard } from "./AsyncQuestionCard";

const prompt: AsyncUserInput = {
  call_id: "call_q",
  reply_id: "a".repeat(32),
  questions: [
    { title: "Pick a route", options: ["First (Recommended)", "Second"] },
    { title: "Pick a route", options: null },
  ],
};
const initialStore = useChatStore.getState();
const send = vi.fn<typeof initialStore.send>();
const card = () => (
  <ConversationScopeContext.Provider value="child">
    <AsyncQuestionCard itemId="source" prompt={prompt} />
  </ConversationScopeContext.Provider>
);
beforeEach(() => {
  vi.stubGlobal("crypto", webcrypto);
  send.mockReset().mockResolvedValue(undefined);
  conversationRegistry.clear();
  clearSessionDrafts();
  conversationRegistry.acquire("child").setState({ boundAgentId: "agent_child" });
  useChatStore.setState({ ...initialStore, conversationId: "parent", send });
});
afterEach(() => {
  cleanup();
  conversationRegistry.clear();
  clearSessionDrafts();
  useChatStore.setState(initialStore);
  vi.unstubAllGlobals();
});
it("submits without subtle crypto, keeps a preselected recommendation unsent, restores drafts, and pins a custom answer after focus changes", async () => {
  vi.stubGlobal("crypto", { getRandomValues: webcrypto.getRandomValues.bind(webcrypto) });
  setSessionDraft("parent", { text: "Keep my composer", files: [] });
  const first = render(card());
  expect(screen.getByLabelText("First (Recommended)")).toBeChecked();
  expect(send).not.toHaveBeenCalled();
  fireEvent.click(screen.getByLabelText("Second"));
  fireEvent.click(screen.getByRole("button", { name: /next/i }));
  fireEvent.change(screen.getByPlaceholderText("Type something"), {
    target: { value: "Custom answer" },
  });
  first.unmount();
  render(card());
  expect(screen.getByPlaceholderText("Type something")).toHaveValue("Custom answer");
  useChatStore.setState({ conversationId: "third" });
  fireEvent.click(screen.getByRole("button", { name: "Submit" }));
  await waitFor(() => expect(send).toHaveBeenCalledTimes(1));
  const args = send.mock.calls[0]!;
  expect(args[1]).toBe("agent_child");
  expect(args[3]).toMatchObject({
    pinnedConversationId: "child",
    asyncQuestionReply: {
      source_item_id: "source",
      answers: { "call_q:0": "Second", "call_q:1": "Custom answer" },
      cancelled: false,
    },
  });
  expect(args[3]!.stableId).toMatch(/^[0-9a-f]{32}$/);
  expect(getSessionDraft("parent")?.text).toBe("Keep my composer");
  expect(screen.queryByRole("button", { name: "Submit" })).toBeNull();
});
it("retries the same correlated reply and never automatically changes it into an authorization", async () => {
  send.mockImplementation(async (_text, _agent, _files, opts) => {
    opts?.onError?.("Temporary outage");
  });
  render(card());
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Temporary outage"));
  const first = send.mock.calls[0]![3]!;
  expect(first.asyncQuestionReply).toEqual({
    source_item_id: "source",
    answers: {},
    cancelled: true,
  });
  fireEvent.click(screen.getByRole("button", { name: "Retry delivery" }));
  await waitFor(() => expect(send).toHaveBeenCalledTimes(2));
  expect(send.mock.calls[1]![3]!.stableId).toBe(first.stableId);
  expect(send.mock.calls[1]![3]!.asyncQuestionReply).toEqual(first.asyncQuestionReply);
});
it("restores the server-confirmed answer and clears only its own draft", async () => {
  render(card());
  act(() =>
    conversationRegistry.peek("child")!.setState({
      blocks: [
        {
          type: "user_message",
          ctx: { agent: null, depth: 0, turn: 0, timestamp: 0, responseId: "r", itemId: "reply" },
          content: [],
          asyncQuestionReply: {
            source_item_id: "source",
            answers: { "call_q:0": "Second", "call_q:1": "Custom" },
            cancelled: false,
          },
        },
      ],
    }),
  );
  expect(screen.getByRole("status")).toHaveTextContent("Answer submitted");
  expect(screen.queryByRole("button", { name: "Submit" })).toBeNull();
  expect(send).not.toHaveBeenCalled();
  await waitFor(() => expect(getSessionDraft("async-question:child:source")).toBeUndefined());
});
it("keeps async questions and original text on both live and history paths between independent messages", () => {
  const item = {
    id: "source",
    type: "message" as const,
    role: "assistant" as const,
    model: "codex-native-ui",
    response_id: "r",
    status: "completed",
    content: [{ type: "output_text" as const, text: "Original assistant text" }],
    async_user_input: prompt,
  };
  const histories = itemsToBlocks([item]);
  const live = new BlockStream().reduceSync([
    ...parseEventLines([JSON.stringify({ event: "response.output_item.done", data: { item } })]),
  ]);
  expect(histories[0]).toMatchObject({
    type: "text_done",
    asyncUserInput: prompt,
    fullText: "Original assistant text",
  });
  expect(live[0]).toMatchObject({
    type: "text_done",
    asyncUserInput: prompt,
    fullText: "Original assistant text",
  });
  const streamed = new BlockStream().reduceSync([
    { type: "text_delta", delta: "Original assistant text" },
    ...parseEventLines([JSON.stringify({ event: "response.output_item.done", data: { item } })]),
  ]);
  expect(streamed.filter((block) => block.type === "text_done")).toEqual([
    expect.objectContaining({
      fullText: "Original assistant text",
      asyncUserInput: prompt,
      ctx: expect.objectContaining({ itemId: "source" }),
    }),
  ]);
  const bubbles = buildBubbles(
    [
      ...histories,
      {
        ...histories[0]!,
        ctx: { ...histories[0]!.ctx, itemId: "later" },
        type: "text_done",
        fullText: "Independent work continues",
        hasCodeBlocks: false,
        asyncUserInput: undefined,
      },
    ],
    null,
  );
  expect(bubbles).toHaveLength(2);
});
