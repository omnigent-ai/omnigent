import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { appendPromptHistoryEntry, usePromptHistory } from "./usePromptHistory";
import { serializeReplyDraft, type ComposerDraft, type StoredReplyDraft } from "@/lib/replyDraft";

const STORAGE_PREFIX = "omnigent:prompt-history";

// History is keyed per conversation; tests must read the same scoped key the
// production code writes, or they'd assert against an always-empty slot.
function scopedKey(scope?: string | null): string {
  return scope ? `${STORAGE_PREFIX}:${scope}` : STORAGE_PREFIX;
}

function storedHistory(scope?: string | null): string[] {
  const raw = localStorage.getItem(scopedKey(scope));
  return raw ? (JSON.parse(raw) as string[]) : [];
}

describe("appendPromptHistoryEntry", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => localStorage.clear());

  it("persists a trimmed entry under the conversation's key", () => {
    // The leading/trailing whitespace must be stripped before storage so
    // recall returns the prompt the way it was composed, not with stray
    // padding — and the return value is what the hook syncs its ref to.
    const result = appendPromptHistoryEntry("  read the README  ", "conv_a");
    expect(result).toEqual([{ text: "read the README" }]);
    expect(storedHistory("conv_a")).toEqual(["read the README"]);
  });

  it("isolates history per conversation — one chat never sees another's", () => {
    // The core bug: chats shared one global key, so ArrowUp recalled the last
    // prompt sent anywhere. Fails if scoping regresses to a single key.
    appendPromptHistoryEntry("sent in A", "conv_a");
    expect(storedHistory("conv_a")).toEqual(["sent in A"]);
    expect(storedHistory("conv_b")).toEqual([]);

    appendPromptHistoryEntry("sent in B", "conv_b");
    // Each conversation's stack holds only its own prompt — no cross-bleed.
    expect(storedHistory("conv_a")).toEqual(["sent in A"]);
    expect(storedHistory("conv_b")).toEqual(["sent in B"]);
  });

  it("skips empty / whitespace-only text without writing", () => {
    // A blank landing composer must never push an empty entry — pressing
    // ArrowUp afterwards would otherwise recall "" and clear the input.
    expect(appendPromptHistoryEntry("   ", "conv_a")).toBeNull();
    expect(localStorage.getItem(scopedKey("conv_a"))).toBeNull();
  });

  it("collapses a consecutive duplicate against the persisted tail", () => {
    // Sending the same prompt from the landing composer and then again in the
    // chat must not store it twice (shell HISTCONTROL=ignoredups). The second
    // call returns the unchanged history so the caller still syncs correctly.
    appendPromptHistoryEntry("hello", "conv_a");
    const result = appendPromptHistoryEntry("hello", "conv_a");
    expect(result).toEqual([{ text: "hello" }]);
    expect(storedHistory("conv_a")).toEqual(["hello"]);
  });

  it("keeps a non-consecutive repeat (only the immediate tail dedupes)", () => {
    appendPromptHistoryEntry("a", "conv_a");
    appendPromptHistoryEntry("b", "conv_a");
    appendPromptHistoryEntry("a", "conv_a");
    expect(storedHistory("conv_a")).toEqual(["a", "b", "a"]);
  });

  it("caps at 100 entries with FIFO eviction of the oldest", () => {
    for (let i = 0; i < 105; i++) appendPromptHistoryEntry(`p${i}`, "conv_a");
    const history = storedHistory("conv_a");
    expect(history).toHaveLength(100);
    // The five oldest (p0..p4) are evicted; the newest survives at the tail.
    expect(history[0]).toBe("p5");
    expect(history[history.length - 1]).toBe("p104");
  });

  it("falls back to the bare legacy key when no scope is given", () => {
    // Surfaces with no bound conversation (null scope) share the prefix-only
    // key. Guards the degenerate path the hook uses before a conversation id
    // is known.
    appendPromptHistoryEntry("unscoped", null);
    expect(localStorage.getItem(STORAGE_PREFIX)).toBe(JSON.stringify(["unscoped"]));
  });
});

describe("usePromptHistory — per-conversation recall", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => {
    cleanup();
    localStorage.clear();
  });

  it("recalls an entry written by appendPromptHistoryEntry before the hook mounted", () => {
    // Landing-composer handoff: home page writes under the new session id, then
    // the chat composer mounts bound to that id and hydrates from the same key.
    appendPromptHistoryEntry("the prompt I just sent", "conv_a");
    const { result } = renderHook(() => usePromptHistory("conv_a"));
    let recalled: ComposerDraft | null = null;
    act(() => {
      recalled = result.current.recallPrevious("");
    });
    expect(recalled).toEqual({ text: "the prompt I just sent" });
  });

  it("does not recall a prompt that belongs to a different conversation", () => {
    // The bug through the composer's actual recall path: conv_a's prompt must
    // be invisible to a composer bound to conv_b.
    appendPromptHistoryEntry("typed in conv_a", "conv_a");
    const { result } = renderHook(() => usePromptHistory("conv_b"));
    let recalled: ComposerDraft | null = { text: "sentinel" };
    act(() => {
      recalled = result.current.recallPrevious("");
    });
    // null = nothing to recall in conv_b. If this returns "typed in conv_a",
    // the global-history bug is back.
    expect(recalled).toBeNull();
  });

  it("re-hydrates when the bound conversation changes", () => {
    // The composer persists across some session switches; flipping the scope
    // prop must swap which conversation's stack ArrowUp walks.
    appendPromptHistoryEntry("a-prompt", "conv_a");
    appendPromptHistoryEntry("b-prompt", "conv_b");
    const { result, rerender } = renderHook(({ scope }) => usePromptHistory(scope), {
      initialProps: { scope: "conv_a" },
    });
    let recalled: ComposerDraft | null = null;
    act(() => {
      recalled = result.current.recallPrevious("");
    });
    expect(recalled).toEqual({ text: "a-prompt" });

    rerender({ scope: "conv_b" });
    act(() => {
      recalled = result.current.recallPrevious("");
    });
    // After the scope change the hook re-read conv_b's key and dropped the
    // conv_a cursor, so the first ArrowUp lands on b's prompt, not a's.
    expect(recalled).toEqual({ text: "b-prompt" });
  });

  it("appendEntry from the chat composer is itself recallable in that conversation", () => {
    const { result } = renderHook(() => usePromptHistory("conv_a"));
    act(() => result.current.appendEntry("typed in chat"));
    let recalled: ComposerDraft | null = null;
    act(() => {
      recalled = result.current.recallPrevious("");
    });
    expect(recalled).toEqual({ text: "typed in chat" });
    // appendEntry routed the write to conv_a's scoped key, not the global one.
    expect(storedHistory("conv_a")).toEqual(["typed in chat"]);
  });

  it("keeps per-entry quote provenance even when adjacent entries have identical text", () => {
    const replyDraft: StoredReplyDraft = {
      version: 1,
      quotes: [{ before: "", text: "A passage" }],
      text: "Answer",
    };
    const text = serializeReplyDraft(replyDraft);
    appendPromptHistoryEntry(text, "conv_a", replyDraft);
    appendPromptHistoryEntry(text, "conv_a", replyDraft);
    appendPromptHistoryEntry(text, "conv_a");
    expect(storedHistory("conv_a")).toHaveLength(2);

    const { result } = renderHook(() => usePromptHistory("conv_a"));
    expect(result.current.recallPrevious("in progress")).toEqual({ text });
    expect(result.current.recallPrevious("unused")).toEqual({ text, replyDraft });
    expect(result.current.recallPrevious("unused")).toBeNull();
    expect(result.current.recallNext()).toEqual({ text });
    expect(result.current.recallNext()).toEqual({ text: "in progress" });
    expect(result.current.recallNext()).toBeNull();
  });

  it("captures the structured current draft literally while navigating history", () => {
    const replyDraft: StoredReplyDraft = {
      version: 1,
      quotes: [{ before: "\n> authored\ncontinued\n\n", text: "A real quote" }],
      text: "~~~markdown\n> example\n",
    };
    const text = serializeReplyDraft(replyDraft);
    appendPromptHistoryEntry("earlier", "conv_a");
    const { result } = renderHook(() => usePromptHistory("conv_a"));
    expect(result.current.recallPrevious(text, replyDraft)).toEqual({ text: "earlier" });
    expect(result.current.recallNext()).toEqual({ text, replyDraft });
  });

  it("keeps authored blockquotes literal in legacy and unsupported history entries", () => {
    const legacy = "intro\n> quote\nreply\n\n";
    const unsupported = "> quoted\ncontinued\n";
    localStorage.setItem(
      scopedKey("conv_a"),
      JSON.stringify([
        legacy,
        { text: unsupported, replyDraft: { version: 9, quotes: [], text: "" } },
        { text: 42 },
        null,
      ]),
    );
    const { result } = renderHook(() => usePromptHistory("conv_a"));
    expect(result.current.recallPrevious("\nunsent\n")).toEqual({ text: unsupported });
    expect(result.current.recallPrevious("")).toEqual({ text: legacy });
    expect(result.current.recallNext()).toEqual({ text: unsupported });
    expect(result.current.recallNext()).toEqual({ text: "\nunsent\n" });
  });
});
