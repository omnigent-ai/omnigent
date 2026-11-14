import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";

import * as sessionsApi from "@/lib/sessionsApi";
import { subagentTabId } from "@/lib/subagentTabs";
import { useChatStore } from "@/store/chatStore";

import { startSideChat, stopSideChat, type StartSideChatOptions } from "./sideChatLifecycle";

vi.mock("sonner", () => ({ toast: { error: vi.fn() } }));

type ChatSend = ReturnType<typeof useChatStore.getState>["send"];
type CreatedSideChat = Awaited<ReturnType<typeof sessionsApi.createSideChat>>;

const initialChatState = useChatStore.getState();

function options(overrides: Partial<StartSideChatOptions> = {}): StartSideChatOptions {
  return {
    conversationId: "conv_parent",
    harness: "openai-agents",
    parentAgentId: "agent_parent",
    draftKey: "pending:one",
    text: "What changed?",
    awaitingDrafts: { current: [] },
    onCreated: vi.fn(),
    ...overrides,
  };
}

beforeEach(() => {
  useChatStore.setState({ sideChatComposers: {}, sideChatDrafts: {}, queuedMessages: [] });
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.mocked(toast.error).mockClear();
  useChatStore.setState(initialChatState, true);
});

describe("startSideChat on a server-side fork", () => {
  it("creates the child with the draft's settings, then hands it the question", async () => {
    const settings = { model: "model-a" } as never;
    useChatStore.setState({
      sideChatComposers: { "pending:one": { text: "What changed?", files: [], settings } },
    });
    const create = vi
      .spyOn(sessionsApi, "createSideChat")
      .mockResolvedValueOnce({ childSessionId: "conv_child" } as CreatedSideChat);
    const onCreated = vi.fn();

    await startSideChat(options({ onCreated }));

    expect(create).toHaveBeenCalledWith("conv_parent", settings);
    expect(onCreated).toHaveBeenCalledWith("conv_child");
    const state = useChatStore.getState();
    expect(state.sideChatComposers["pending:one"]).toBeUndefined();
    expect(state.sideChatDrafts.conv_child).toBe("What changed?");
    expect(state.sideChatToOpen).toEqual({ childId: "conv_child", parentId: "conv_parent" });
  });

  it("keeps the draft and rejects with a toast when the create fails", async () => {
    const draft = { text: "What changed?", files: [] };
    useChatStore.setState({ sideChatComposers: { "pending:one": draft } });
    vi.spyOn(sessionsApi, "createSideChat").mockRejectedValueOnce(new Error("offline"));
    const onCreated = vi.fn();

    await expect(startSideChat(options({ onCreated }))).rejects.toThrow("offline");

    expect(toast.error).toHaveBeenCalledWith("Couldn't start a side chat for this session.");
    expect(onCreated).not.toHaveBeenCalled();
    expect(useChatStore.getState().sideChatComposers["pending:one"]).toEqual(draft);
  });
});

describe("startSideChat on a native fork", () => {
  it("sends /side to the parent and queues the draft for the child that arrives later", async () => {
    const send = vi.fn<ChatSend>(() => Promise.resolve());
    useChatStore.setState({ send });
    const create = vi.spyOn(sessionsApi, "createSideChat");
    const awaitingDrafts = { current: ["pending:zero"] };

    await startSideChat(options({ harness: "codex-native", awaitingDrafts }));

    expect(send).toHaveBeenCalledWith("/side What changed?", "agent_parent", undefined, {
      pinnedConversationId: "conv_parent",
      onError: expect.any(Function),
    });
    expect(awaitingDrafts.current).toEqual(["pending:zero", "pending:one"]);
    expect(create).not.toHaveBeenCalled();
  });

  it("drops the queued draft and rejects when the send fails", async () => {
    const send = vi.fn<ChatSend>(async (_text, _agent, _files, sendOptions) => {
      sendOptions?.onError?.("This host can't fork side chats.");
    });
    useChatStore.setState({ send });
    const awaitingDrafts = { current: ["pending:zero"] };

    await expect(
      startSideChat(options({ harness: "codex-native", awaitingDrafts })),
    ).rejects.toThrow("side chat send failed");

    expect(awaitingDrafts.current).toEqual(["pending:zero"]);
    expect(toast.error).toHaveBeenCalledWith("This host can't fork side chats.");
  });

  it("rejects without sending when there is no parent agent", async () => {
    const send = vi.fn<ChatSend>(() => Promise.resolve());
    useChatStore.setState({ send });
    const awaitingDrafts = { current: [] };

    await expect(
      startSideChat(options({ harness: "codex-native", parentAgentId: null, awaitingDrafts })),
    ).rejects.toThrow("no agent");

    expect(send).not.toHaveBeenCalled();
    expect(awaitingDrafts.current).toEqual([]);
  });
});

describe("stopSideChat", () => {
  const followUp = { queueId: "q_side", text: "Follow-up", conversationId: "conv_side" };
  const otherChat = { queueId: "q_other", text: "Other", conversationId: "conv_other" };

  it("stops the child and clears everything the tab held", async () => {
    const stop = vi.spyOn(sessionsApi, "stopSession").mockResolvedValueOnce({ queued: false });
    useChatStore.setState({
      queuedMessages: [followUp, otherChat],
      sideChatDrafts: { conv_side: "Seeded question" },
      sideChatComposers: { conv_side: { text: "Unsent", files: [] } },
    });

    const onStopped = vi.fn();
    await stopSideChat("conv_side", onStopped);

    expect(stop).toHaveBeenCalledWith("conv_side");
    expect(onStopped).toHaveBeenCalledOnce();
    const state = useChatStore.getState();
    expect(state.queuedMessages).toEqual([otherChat]);
    expect(state.sideChatDrafts.conv_side).toBeUndefined();
    expect(state.sideChatComposers.conv_side).toBeUndefined();
  });

  it("restores the queue and keeps the tab when the stop fails", async () => {
    vi.spyOn(sessionsApi, "stopSession").mockRejectedValueOnce(new Error("offline"));
    useChatStore.setState({
      queuedMessages: [followUp, otherChat],
      sideChatDrafts: { conv_side: "Seeded question" },
    });

    const onStopped = vi.fn();
    await stopSideChat("conv_side", onStopped);

    expect(onStopped).not.toHaveBeenCalled();
    expect(toast.error).toHaveBeenCalledWith("Couldn't close side chat. Try again.");
    const state = useChatStore.getState();
    expect(state.queuedMessages).toEqual(expect.arrayContaining([followUp, otherChat]));
    expect(state.sideChatDrafts.conv_side).toBe("Seeded question");
  });

  it("closes a pending draft in the same tick, without a server stop", () => {
    const stop = vi.spyOn(sessionsApi, "stopSession");
    useChatStore.setState({ sideChatComposers: { "pending:one": { text: "Unsent", files: [] } } });

    const onStopped = vi.fn();
    void stopSideChat("pending:one", onStopped);

    expect(onStopped).toHaveBeenCalledOnce();
    expect(stop).not.toHaveBeenCalled();
    expect(useChatStore.getState().sideChatComposers["pending:one"]).toBeUndefined();
  });

  it("only hides a sub-agent tab, in the same tick, leaving the sub-agent running", () => {
    const stop = vi.spyOn(sessionsApi, "stopSession");
    const tab = subagentTabId("conv_child");
    useChatStore.setState({ queuedMessages: [{ ...followUp, conversationId: tab }] });

    const onStopped = vi.fn();
    void stopSideChat(tab, onStopped);

    expect(onStopped).toHaveBeenCalledOnce();
    expect(stop).not.toHaveBeenCalled();
    expect(useChatStore.getState().queuedMessages).toHaveLength(1);
  });
});
