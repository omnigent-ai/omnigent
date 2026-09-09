// Unit tests for the OS-share intake pipeline:
//   captureIncomingShareFragment (module-load-time, sessionStorage-backed)
//   peekPendingShareText / takePendingShareText (durable read/consume)
//   useShareIntake (conversation-gated queue into the composer)
//
// The split exists because an unauthenticated share hard-navigates through
// /login and back (see main.tsx's call-ordering comment), which wipes every
// in-memory value -- sessionStorage is the only thing that survives that,
// so the capture step is tested independently of any React lifecycle.

import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { useChatStore } from "@/store/chatStore";
import {
  captureIncomingShareFragment,
  peekPendingShareText,
  takePendingShareText,
  useShareIntake,
} from "./shareIntake";

const STORAGE_KEY = "omnigent:pendingShareText";

function setHash(hash: string): void {
  window.history.replaceState(null, "", "/" + hash);
}

function reset(): void {
  setHash("");
  window.sessionStorage.removeItem(STORAGE_KEY);
  useChatStore.setState({ conversationId: null, pendingComposerText: null });
}

describe("captureIncomingShareFragment", () => {
  beforeEach(reset);
  afterEach(reset);

  it("persists decoded text to sessionStorage", () => {
    setHash("#shared-text=hello%20world");

    captureIncomingShareFragment();

    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBe("hello world");
  });

  it("strips the fragment from the URL so a refresh can't re-apply it", () => {
    setHash("#shared-text=hello");

    captureIncomingShareFragment();

    expect(window.location.hash).toBe("");
  });

  it("leaves sessionStorage untouched when there is no share fragment", () => {
    setHash("#some-other-fragment");

    captureIncomingShareFragment();

    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull();
    // An unrelated fragment isn't ours to clear.
    expect(window.location.hash).toBe("#some-other-fragment");
  });

  it("drops malformed percent-encoding rather than persisting garbage", () => {
    setHash("#shared-text=%E0%A4%A");

    captureIncomingShareFragment();

    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBeNull();
    expect(window.location.hash).toBe("");
  });

  it("survives being called with no prior React/store state at all", () => {
    // Simulates the real call site: main.tsx calls this at module scope,
    // before the store or any component exists yet.
    setHash("#shared-text=cold%20start");

    expect(() => captureIncomingShareFragment()).not.toThrow();
    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBe("cold start");
  });
});

describe("peekPendingShareText / takePendingShareText", () => {
  beforeEach(reset);
  afterEach(reset);

  it("peek reads without removing", () => {
    window.sessionStorage.setItem(STORAGE_KEY, "hello");

    expect(peekPendingShareText()).toBe("hello");
    expect(peekPendingShareText()).toBe("hello");
  });

  it("take removes the durable copy", () => {
    window.sessionStorage.setItem(STORAGE_KEY, "hello");

    takePendingShareText();

    expect(peekPendingShareText()).toBeNull();
  });

  it("take on an already-empty key is a harmless no-op", () => {
    expect(() => takePendingShareText()).not.toThrow();
    expect(peekPendingShareText()).toBeNull();
  });
});

describe("useShareIntake", () => {
  beforeEach(reset);
  afterEach(reset);

  it("queues durably-captured text into the composer when landing on the new-chat route", () => {
    window.sessionStorage.setItem(STORAGE_KEY, "hello world");
    useChatStore.setState({ conversationId: null });

    renderHook(() => useShareIntake());

    expect(useChatStore.getState().pendingComposerText).toBe("hello world");
  });

  it("does NOT queue into an already-open conversation (intended recipient is a new chat)", () => {
    // The real path this guards: a share fragment arrives on a URL that
    // still resolves to an existing conversation (e.g. a restored /c/:id
    // tab), not the fresh landing page.
    window.sessionStorage.setItem(STORAGE_KEY, "hello world");
    useChatStore.setState({ conversationId: "conv_existing" });

    renderHook(() => useShareIntake());

    expect(useChatStore.getState().pendingComposerText).toBeNull();
    // Not lost either -- still sitting in the durable store, waiting.
    expect(peekPendingShareText()).toBe("hello world");
  });

  it("re-queues once conversationId actually transitions to null later", () => {
    // Exercises the real re-render path (not a presumed effect order): the
    // hook re-runs because its own dependency (conversationId) changed, the
    // same way switchTo(null) would drive it in the real app.
    window.sessionStorage.setItem(STORAGE_KEY, "hello world");
    useChatStore.setState({ conversationId: "conv_existing" });
    const { rerender } = renderHook(() => useShareIntake());
    expect(useChatStore.getState().pendingComposerText).toBeNull();

    useChatStore.setState({ conversationId: null });
    rerender();

    expect(useChatStore.getState().pendingComposerText).toBe("hello world");
  });

  it("is a no-op once the durable copy has already been consumed", () => {
    useChatStore.setState({ conversationId: null });

    renderHook(() => useShareIntake());

    expect(useChatStore.getState().pendingComposerText).toBeNull();
  });

  it("does not re-queue on a rerender with unchanged conversationId", () => {
    window.sessionStorage.setItem(STORAGE_KEY, "first");
    useChatStore.setState({ conversationId: null });
    const { rerender } = renderHook(() => useShareIntake());
    expect(useChatStore.getState().pendingComposerText).toBe("first");

    useChatStore.getState().clearPendingComposerText();
    window.sessionStorage.setItem(STORAGE_KEY, "second");
    rerender();

    // conversationId didn't change, so the effect's dependency didn't fire.
    expect(useChatStore.getState().pendingComposerText).toBeNull();
  });
});
