import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as sessionsApi from "@/lib/sessionsApi";
import { terminalsQueryKey } from "@/lib/terminals";
import type { Session } from "@/lib/types";
import {
  bindConversationForTest,
  handleSessionEvent,
  initChatStore,
  useChatStore,
} from "./chatStore";
import { conversationRegistry } from "./conversationRegistry";

const SOURCE = "conv_config_source";
const OTHER = "conv_config_other";
const initialState = useChatStore.getInitialState();

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((onResolve, onReject) => {
    resolve = onResolve;
    reject = onReject;
  });
  return { promise, resolve, reject };
}

function session(id: string): Session {
  return {
    id,
    agentId: "agent_claude",
    agentName: "claude-native",
    status: "idle",
    createdAt: 1,
    title: null,
    permissionLevel: null,
    parentSessionId: null,
    subAgentName: null,
    kind: "default",
    items: [],
    labels: { "omnigent.wrapper": "claude-code-native-ui" },
    modelOverride: "opus",
    reasoningEffort: "high",
    costControlModeOverride: "off",
  };
}

describe("session-scoped configuration operations", () => {
  let client: QueryClient;

  beforeEach(() => {
    client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    initChatStore(client);
    useChatStore.setState(initialState, true);
    bindConversationForTest(SOURCE, {
      sessionModelOverride: "opus",
      sessionReasoningEffort: "high",
      costControlModeOverride: "off",
    });
    client.setQueryData(["session", SOURCE], session(SOURCE));
    vi.spyOn(sessionsApi, "retrySession").mockResolvedValue({ queued: false, recovered: true });
    vi.spyOn(sessionsApi, "updateSession").mockImplementation(async (id, updates) => ({
      ...session(id),
      ...updates,
    }));
  });

  afterEach(() => {
    conversationRegistry.clear();
    client.clear();
    useChatStore.setState(initialState, true);
    vi.restoreAllMocks();
  });

  it.each([
    { recovered: true, recovery: "native_terminal_ready" },
    { recovered: false, recovery: "already_connected" },
  ] as const)(
    "publishes startup through $recovery then applying until the change completes",
    async (result) => {
      const startup = deferred<sessionsApi.PostEventResponse>();
      const patch = deferred<void>();
      vi.mocked(sessionsApi.retrySession).mockReturnValueOnce(startup.promise);
      const invalidate = vi.spyOn(client, "invalidateQueries");
      const change = vi.fn().mockReturnValue(patch.promise);
      const operation = useChatStore.getState().applySessionConfig(change, { startTerminal: true });

      expect(useChatStore.getState().sessionConfigPhase).toBe("starting");
      expect(useChatStore.getState().status).toBe("idle");
      expect(useChatStore.getState().blocks).toEqual([]);
      expect(change).not.toHaveBeenCalled();
      expect(sessionsApi.retrySession).toHaveBeenCalledExactlyOnceWith(SOURCE);

      startup.resolve({ queued: false, ...result });
      await vi.waitFor(() => expect(change).toHaveBeenCalledExactlyOnceWith(SOURCE));
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: terminalsQueryKey(SOURCE),
        refetchType: "all",
      });
      expect(useChatStore.getState().sessionConfigPhase).toBe("applying");
      patch.resolve();
      await operation;
      expect(useChatStore.getState().sessionConfigPhase).toBeNull();
      expect(useChatStore.getState().sessionConfigError).toBeNull();
    },
  );

  it("applies immediately without terminal recovery when none is required", async () => {
    const patch = deferred<void>();
    const change = vi.fn().mockReturnValue(patch.promise);
    const operation = useChatStore.getState().applySessionConfig(change);
    expect(change).toHaveBeenCalledExactlyOnceWith(SOURCE);
    expect(useChatStore.getState().sessionConfigPhase).toBe("applying");
    expect(sessionsApi.retrySession).not.toHaveBeenCalled();
    patch.resolve();
    await operation;
    expect(useChatStore.getState().sessionConfigPhase).toBeNull();
  });

  it.each([null, "temp:creating"])("does not recover non-persisted session %s", async (id) => {
    bindConversationForTest(id);
    const change = vi.fn().mockResolvedValue(undefined);
    await useChatStore.getState().applySessionConfig(change, { startTerminal: true });
    expect(change).toHaveBeenCalledExactlyOnceWith(id);
    expect(sessionsApi.retrySession).not.toHaveBeenCalled();
    expect(useChatStore.getState().sessionConfigPhase).toBeNull();
  });

  it.each(["resolve", "reject"] as const)(
    "does not modify a newly active session when a null-target operation completes via %s",
    async (completion) => {
      bindConversationForTest(null);
      const pending = deferred<void>();
      const change = vi.fn().mockReturnValue(pending.promise);
      const operation = useChatStore.getState().applySessionConfig(change);
      expect(change).toHaveBeenCalledExactlyOnceWith(null);
      expect(useChatStore.getState().sessionConfigPhase).toBe("applying");

      bindConversationForTest(OTHER, {
        sessionConfigPhase: "applying",
        sessionConfigError: "Other session error",
      });
      if (completion === "resolve") pending.resolve();
      else pending.reject(new Error("Original operation failed"));
      await operation;

      expect(useChatStore.getState()).toMatchObject({
        conversationId: OTHER,
        sessionConfigPhase: "applying",
        sessionConfigError: "Other session error",
      });
      expect(conversationRegistry.peek(OTHER)!.getState()).toMatchObject({
        sessionConfigPhase: "applying",
        sessionConfigError: "Other session error",
      });
    },
  );

  it("deduplicates synchronously throughout startup and application", async () => {
    const startup = deferred<sessionsApi.PostEventResponse>();
    const patch = deferred<void>();
    vi.mocked(sessionsApi.retrySession).mockReturnValueOnce(startup.promise);
    const change = vi.fn().mockReturnValue(patch.promise);
    const duplicate = vi.fn().mockResolvedValue(undefined);
    const operation = useChatStore.getState().applySessionConfig(change, { startTerminal: true });
    await useChatStore.getState().applySessionConfig(duplicate, { startTerminal: true });
    expect(duplicate).not.toHaveBeenCalled();
    expect(sessionsApi.retrySession).toHaveBeenCalledTimes(1);

    startup.resolve({ queued: false, recovered: true });
    await vi.waitFor(() => expect(change).toHaveBeenCalledTimes(1));
    await useChatStore.getState().applySessionConfig(duplicate, { startTerminal: true });
    expect(duplicate).not.toHaveBeenCalled();
    expect(sessionsApi.retrySession).toHaveBeenCalledTimes(1);
    patch.resolve();
    await operation;
  });

  it("does not start another change while native model confirmation is pending", async () => {
    useChatStore.setState({ pendingModelChange: "sonnet" });
    const change = vi.fn().mockResolvedValue(undefined);
    await useChatStore.getState().applySessionConfig(change, { startTerminal: true });
    expect(change).not.toHaveBeenCalled();
    expect(sessionsApi.retrySession).not.toHaveBeenCalled();
  });

  it("continues a recovered model change on its original session after navigation", async () => {
    const source = conversationRegistry.peek(SOURCE)!;
    const startup = deferred<sessionsApi.PostEventResponse>();
    vi.mocked(sessionsApi.retrySession).mockReturnValueOnce(startup.promise);
    const operation = useChatStore
      .getState()
      .applySessionConfig(
        (id) => useChatStore.getState().setModel("sonnet", { expectConfirmation: false }, id),
        { startTerminal: true },
      );
    bindConversationForTest(OTHER, { sessionModelOverride: "haiku" });
    const otherPatch = deferred<void>();
    const otherOperation = useChatStore.getState().applySessionConfig(() => otherPatch.promise);

    startup.resolve({ queued: false, recovered: true });
    await operation;
    expect(sessionsApi.updateSession).toHaveBeenCalledExactlyOnceWith(SOURCE, {
      modelOverride: "sonnet",
    });
    expect(source.getState().sessionModelOverride).toBe("sonnet");
    expect(source.getState().sessionConfigPhase).toBeNull();
    expect(useChatStore.getState().sessionModelOverride).toBe("haiku");
    expect(useChatStore.getState().sessionConfigPhase).toBe("applying");
    expect(useChatStore.getState().sessionConfigError).toBeNull();
    otherPatch.resolve();
    await otherOperation;
  });

  it.each(["startup", "switch"] as const)(
    "cleans up a background %s failure without affecting the active session",
    async (phase) => {
      const source = conversationRegistry.peek(SOURCE)!;
      const pending = deferred<void>();
      if (phase === "startup")
        vi.mocked(sessionsApi.retrySession).mockReturnValueOnce(
          pending.promise.then(() => ({ queued: false, recovered: true })),
        );
      const operation = useChatStore
        .getState()
        .applySessionConfig(() => pending.promise, { startTerminal: phase === "startup" });
      bindConversationForTest(OTHER, { sessionConfigPhase: "applying" });
      pending.reject(new Error("Host stopped responding"));
      await operation;
      expect(source.getState().sessionConfigPhase).toBeNull();
      expect(source.getState().sessionConfigError).toBe("Host stopped responding");
      expect(useChatStore.getState().sessionConfigPhase).toBe("applying");
      expect(useChatStore.getState().sessionConfigError).toBeNull();

      bindConversationForTest(SOURCE);
      await useChatStore.getState().applySessionConfig(async () => {});
      expect(useChatStore.getState().sessionConfigPhase).toBeNull();
      expect(useChatStore.getState().sessionConfigError).toBeNull();
    },
  );

  describe("dead-stream rebinds", () => {
    beforeEach(() => {
      vi.spyOn(sessionsApi, "getSessionSlim").mockImplementation(async (id) => session(id));
      vi.spyOn(sessionsApi, "fetchSessionItemsPage").mockResolvedValue({
        items: [],
        hasMore: false,
      });
      vi.spyOn(sessionsApi, "openSessionStream").mockImplementation(
        async () =>
          new Response("data: [DONE]\n\n", {
            headers: { "content-type": "text/event-stream" },
          }),
      );
    });

    it.each([
      { phase: "starting", completion: "resolve" },
      { phase: "starting", completion: "reject" },
      { phase: "applying", completion: "resolve" },
      { phase: "applying", completion: "reject" },
    ] as const)(
      "retains a $phase operation on the replacement entry until $completion",
      async ({ phase, completion }) => {
        await useChatStore.getState().switchTo(null);
        await useChatStore.getState().switchTo(SOURCE);
        const source = conversationRegistry.peek(SOURCE)!;
        await vi.waitFor(() => expect(source.getState().abortController).toBeNull());
        const pending = deferred<void>();
        if (phase === "starting") {
          vi.mocked(sessionsApi.retrySession).mockReturnValueOnce(
            pending.promise.then(() => ({ queued: false, recovered: true })),
          );
        }
        const change = vi
          .fn()
          .mockImplementation(() => (phase === "applying" ? pending.promise : Promise.resolve()));
        const operation = useChatStore
          .getState()
          .applySessionConfig(change, { startTerminal: phase === "starting" });
        expect(source.getState().sessionConfigPhase).toBe(phase);

        await useChatStore.getState().switchTo(OTHER);
        const other = conversationRegistry.peek(OTHER)!;
        other.setState({
          sessionConfigPhase: "applying",
          sessionConfigError: "Other session error",
        });
        await useChatStore.getState().switchTo(SOURCE);
        const replacement = conversationRegistry.peek(SOURCE)!;
        expect(replacement).not.toBe(source);
        expect(source.disposed).toBe(true);
        expect(replacement.getState()).toMatchObject({
          sessionConfigPhase: phase,
          sessionConfigError: null,
        });
        expect(useChatStore.getState().sessionConfigPhase).toBe(phase);
        const duplicate = vi.fn().mockResolvedValue(undefined);
        await useChatStore.getState().applySessionConfig(duplicate);
        expect(duplicate).not.toHaveBeenCalled();

        if (completion === "resolve") pending.resolve();
        else pending.reject(new Error("Original operation failed"));
        await operation;
        const expected = {
          sessionConfigPhase: null,
          sessionConfigError: completion === "reject" ? "Original operation failed" : null,
        };
        expect(replacement.getState()).toMatchObject(expected);
        expect(useChatStore.getState()).toMatchObject(expected);
        expect(other.getState()).toMatchObject({
          sessionConfigPhase: "applying",
          sessionConfigError: "Other session error",
        });
        if (phase === "starting" && completion === "reject") {
          expect(change).not.toHaveBeenCalled();
        } else {
          expect(change).toHaveBeenCalledExactlyOnceWith(SOURCE);
        }
      },
    );

    it("applies canonical effort to the replacement entry after a held PATCH completes", async () => {
      await useChatStore.getState().switchTo(null);
      await useChatStore.getState().switchTo(SOURCE);
      const source = conversationRegistry.peek(SOURCE)!;
      await vi.waitFor(() => expect(source.getState().abortController).toBeNull());
      const patch = deferred<Session>();
      vi.mocked(sessionsApi.updateSession).mockReturnValueOnce(patch.promise);
      const operation = useChatStore
        .getState()
        .applySessionConfig((id) => useChatStore.getState().setEffort("low", id));
      await vi.waitFor(() =>
        expect(sessionsApi.updateSession).toHaveBeenCalledExactlyOnceWith(SOURCE, {
          reasoningEffort: "low",
        }),
      );
      expect(source.getState().sessionReasoningEffort).toBe("low");

      await useChatStore.getState().switchTo(OTHER);
      const other = conversationRegistry.peek(OTHER)!;
      await useChatStore.getState().switchTo(SOURCE);
      const replacement = conversationRegistry.peek(SOURCE)!;
      expect(replacement).not.toBe(source);
      expect(replacement.getState()).toMatchObject({
        sessionConfigPhase: "applying",
        sessionReasoningEffort: "high",
      });

      patch.resolve({ ...session(SOURCE), reasoningEffort: "medium" });
      await operation;
      const expected = {
        sessionConfigPhase: null,
        sessionConfigError: null,
        sessionReasoningEffort: "medium",
      };
      expect(replacement.getState()).toMatchObject(expected);
      expect(useChatStore.getState()).toMatchObject(expected);
      expect(other.getState().sessionReasoningEffort).toBe("high");
    });

    it("preserves a settled configuration error through a rebind", async () => {
      const source = conversationRegistry.peek(SOURCE)!;
      const state = { sessionConfigPhase: null, sessionConfigError: "Original operation failed" };
      source.setState(state);
      await useChatStore.getState().switchTo(OTHER);
      await useChatStore.getState().switchTo(SOURCE);
      const replacement = conversationRegistry.peek(SOURCE)!;
      expect(replacement).not.toBe(source);
      expect(replacement.getState()).toMatchObject(state);
      expect(useChatStore.getState()).toMatchObject(state);
    });

    it("keeps native confirmation pending through a rebind until the harness reports its model", async () => {
      await useChatStore.getState().switchTo(null);
      await useChatStore.getState().switchTo(SOURCE);
      const source = conversationRegistry.peek(SOURCE)!;
      await vi.waitFor(() => expect(source.getState().abortController).toBeNull());
      await useChatStore
        .getState()
        .applySessionConfig((id) =>
          useChatStore.getState().setModel("sonnet", { expectConfirmation: true }, id),
        );
      expect(source.getState()).toMatchObject({
        sessionConfigPhase: null,
        pendingModelChange: "sonnet",
      });

      await useChatStore.getState().switchTo(OTHER);
      await useChatStore.getState().switchTo(SOURCE);
      const replacement = conversationRegistry.peek(SOURCE)!;
      expect(replacement).not.toBe(source);
      expect(replacement.getState().pendingModelChange).toBe("sonnet");
      expect(useChatStore.getState().pendingModelChange).toBe("sonnet");
      const duplicate = vi.fn().mockResolvedValue(undefined);
      await useChatStore.getState().applySessionConfig(duplicate);
      expect(duplicate).not.toHaveBeenCalled();

      handleSessionEvent(
        { type: "session_model", conversationId: SOURCE, model: "claude-sonnet-5" },
        SOURCE,
      );
      expect(replacement.getState()).toMatchObject({
        pendingModelChange: null,
        llmModel: "claude-sonnet-5",
      });
      expect(useChatStore.getState().pendingModelChange).toBeNull();
      await useChatStore.getState().applySessionConfig(duplicate);
      expect(duplicate).toHaveBeenCalledExactlyOnceWith(SOURCE);
    });
  });

  it.each(["model", "effort", "routing"] as const)(
    "targets the original session when changing %s while another session is active",
    async (kind) => {
      const source = conversationRegistry.peek(SOURCE)!;
      bindConversationForTest(OTHER, {
        sessionModelOverride: "haiku",
        sessionReasoningEffort: "medium",
        costControlModeOverride: "off",
      });
      const store = useChatStore.getState();
      if (kind === "model") await store.setModel("sonnet", { expectConfirmation: false }, SOURCE);
      else if (kind === "effort") await store.setEffort("low", SOURCE);
      else await store.setCostControlMode("on", SOURCE);

      const expected =
        kind === "model"
          ? { modelOverride: "sonnet" }
          : kind === "effort"
            ? { reasoningEffort: "low" }
            : { costControlModeOverride: "on", modelOverride: null };
      expect(sessionsApi.updateSession).toHaveBeenCalledExactlyOnceWith(SOURCE, expected);
      expect(source.getState()).toMatchObject(
        kind === "model"
          ? { sessionModelOverride: "sonnet" }
          : kind === "effort"
            ? { sessionReasoningEffort: "low" }
            : { costControlModeOverride: "on", sessionModelOverride: null },
      );
      expect(useChatStore.getState()).toMatchObject({
        sessionModelOverride: "haiku",
        sessionReasoningEffort: "medium",
        costControlModeOverride: "off",
      });
    },
  );

  it("rolls a failed background routing change back on its original session", async () => {
    const source = conversationRegistry.peek(SOURCE)!;
    bindConversationForTest(OTHER, { sessionModelOverride: "haiku" });
    vi.mocked(sessionsApi.updateSession).mockRejectedValueOnce(new Error("Switch rejected"));
    await expect(useChatStore.getState().setCostControlMode("on", SOURCE)).rejects.toThrow(
      "Switch rejected",
    );
    expect(source.getState()).toMatchObject({
      costControlModeOverride: "off",
      sessionModelOverride: "opus",
    });
    expect(useChatStore.getState().sessionModelOverride).toBe("haiku");
  });

  it("does not reinterpret an explicit null target as the newly active session", async () => {
    const store = useChatStore.getState();
    await store.setModel("sonnet", { expectConfirmation: false }, null);
    await store.setEffort("low", null);
    await store.setCostControlMode("on", null);
    expect(sessionsApi.updateSession).not.toHaveBeenCalled();
    expect(useChatStore.getState()).toMatchObject({
      sessionModelOverride: "opus",
      sessionReasoningEffort: "high",
      costControlModeOverride: "off",
    });
  });
});
