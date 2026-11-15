import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useSessionBadgeStates } from "./useSessionBadgeStates";
import type { Conversation } from "./useConversations";
import type { AnyBlock } from "@/lib/blocks";
import { LATEST_ACTIVITY_ERROR_LABEL_KEY } from "@/lib/sessionError";
import { conversationRegistry } from "@/store/conversationRegistry";

const ctx = { agent: null, depth: 0, turn: 0, timestamp: 0, responseId: "r1", itemId: "i1" };
const text = (fullText: string): AnyBlock => ({
  type: "text_done",
  ctx,
  fullText,
  hasCodeBlocks: false,
});
const disconnect: AnyBlock = {
  type: "error",
  ctx,
  source: "execution",
  code: "runner_disconnected",
  message: "Tunnel dropped",
};
const row = (value = "", status: Conversation["status"] = "idle") => ({
  id: "session",
  status,
  labels: { [LATEST_ACTIVITY_ERROR_LABEL_KEY]: value },
  host_online: true,
});
function live(blocks: AnyBlock[] = []) {
  const entry = conversationRegistry.acquire("session");
  entry.setState({
    loadingConversation: false,
    conversationLoadError: null,
    abortController: new AbortController(),
    sessionStatus: "idle",
    blocks,
  });
  return entry;
}
afterEach(() => {
  cleanup();
  conversationRegistry.clear();
  vi.restoreAllMocks();
});

describe("useSessionBadgeStates", () => {
  it("uses labels and failed status for unopened rows without starting reads or streams", () => {
    const fetch = vi.spyOn(globalThis, "fetch");
    const { result } = renderHook(() =>
      useSessionBadgeStates([
        row("error"),
        { ...row("", "failed"), id: "failed" },
        { ...row(), id: "idle" },
      ]),
    );
    expect(result.current).toEqual([{ kind: "error" }, { kind: "error" }, null]);
    expect(fetch).not.toHaveBeenCalled();
    expect(conversationRegistry.ids()).toEqual([]);
  });

  it("shows live errors before a label update and clears them despite a stale label", () => {
    const entry = live([text("Ready.")]);
    const { result, rerender } = renderHook(
      ({ conversation }) => useSessionBadgeStates([conversation]),
      {
        initialProps: { conversation: row() },
      },
    );
    expect(result.current).toEqual([null]);
    act(() => entry.setState({ blocks: [text("API Error: Rate limited")] }));
    expect(result.current).toEqual([{ kind: "error" }]);
    rerender({ conversation: row("error") });
    act(() => entry.setState({ blocks: [text("Recovered successfully.")] }));
    expect(result.current).toEqual([null]);
    act(() => conversationRegistry.release("session"));
    expect(result.current).toEqual([{ kind: "error" }]);
  });

  it("keeps a newer failed row red while a retained transcript still looks idle", () => {
    // A background transcript can lag while its stream reconnects; the row is authoritative.
    live([text("Ready.")]);
    const { result, rerender } = renderHook(
      ({ conversation }) => useSessionBadgeStates([conversation]),
      { initialProps: { conversation: row() } },
    );
    expect(result.current).toEqual([null]);
    rerender({ conversation: row("", "failed") });
    expect(result.current).toEqual([{ kind: "error" }]);
    rerender({ conversation: row() });
    expect(result.current).toEqual([null]);
  });

  it("keeps running and approval rows ahead of live errors", () => {
    live([text("API Error: Rate limited")]);
    const { result, rerender } = renderHook(
      ({ pending }) =>
        useSessionBadgeStates([
          { ...row("error", "running"), pending_elicitations_count: pending },
        ]),
      { initialProps: { pending: 0 } },
    );
    expect(result.current).toEqual([{ kind: "running" }]);
    rerender({ pending: 2 });
    expect(result.current).toEqual([{ kind: "awaiting", count: 2 }]);
  });

  it.each(["loading", "error", "aborted"])(
    "falls back to the row when the transcript is %s",
    (state) => {
      const entry = live([text("Recovered.")]);
      if (state === "loading") entry.setState({ loadingConversation: true });
      if (state === "error") entry.setState({ conversationLoadError: new Error("Unavailable") });
      if (state === "aborted") entry.getState().abortController!.abort();
      const { result } = renderHook(() => useSessionBadgeStates([row("error")]));
      expect(result.current).toEqual([{ kind: "error" }]);
    },
  );

  it("does not rerender badges for unrelated changes or tokens with the same classification", () => {
    const entry = live([text("Hello")]);
    let renders = 0;
    renderHook(() => {
      renders++;
      return useSessionBadgeStates([row()]);
    });
    const initialRenders = renders;
    act(() => entry.setState({ blocks: [text("Hello again")] }));
    act(() => conversationRegistry.acquire("unrelated").setState({ sessionStatus: "failed" }));
    expect(renders).toBe(initialRenders);
  });

  it.each(["label", "loaded transcript"])(
    "updates disconnect recovery from a %s when host liveness changes",
    (source) => {
      if (source === "loaded transcript") live([disconnect]);
      const label = source === "label" ? "disconnected" : "";
      const { result, rerender } = renderHook(
        ({ online }) => useSessionBadgeStates([{ ...row(label), host_online: online }]),
        { initialProps: { online: false } },
      );
      expect(result.current).toEqual([{ kind: "disconnected" }]);
      rerender({ online: true });
      expect(result.current).toEqual([null]);
    },
  );

  it("suppresses an earlier live error while local streaming starts", () => {
    const entry = live([text("API Error: Rate limited")]);
    const { result } = renderHook(() => useSessionBadgeStates([row("error")]));
    expect(result.current).toEqual([{ kind: "error" }]);
    act(() => entry.setState({ status: "streaming" }));
    expect(result.current).toEqual([null]);
  });
});
