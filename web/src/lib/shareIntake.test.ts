// Unit tests for the OS-share TEXT intake pipeline:
//   captureIncomingShareFragment (module-load-time, sessionStorage-backed)
//   receiveNativeSharedText (native ACTION_SEND EXTRA_TEXT, live bridge)
//   peekPendingShareText / takePendingShareText (durable read/consume)
//   useShareIntake (conversation-gated queue into the composer, both origins)
//
// The split exists because an unauthenticated share hard-navigates through
// /login and back (see main.tsx's call-ordering comment), which wipes every
// in-memory value -- sessionStorage is the only thing that survives that,
// so the capture step is tested independently of any React lifecycle.

import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useChatStore } from "@/store/chatStore";

type SharedTextCallback = (text: string) => void;
const textSubscribers: SharedTextCallback[] = [];

vi.mock("@/lib/nativeBridge", () => ({
  onNativeSharedText: vi.fn((callback: SharedTextCallback) => {
    textSubscribers.push(callback);
    return () => {
      const i = textSubscribers.indexOf(callback);
      if (i >= 0) textSubscribers.splice(i, 1);
    };
  }),
}));

const {
  captureIncomingShareFragment,
  peekPendingShareText,
  takePendingShareText,
  receiveNativeSharedText,
  useShareIntake,
} = await import("./shareIntake");

const STORAGE_KEY = "omnigent:pendingShareText";

function setHash(hash: string): void {
  window.history.replaceState(null, "", "/" + hash);
}

function reset(): void {
  setHash("");
  window.sessionStorage.removeItem(STORAGE_KEY);
  textSubscribers.length = 0;
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

  it("appends to an unconsumed durable share rather than overwriting it", () => {
    // Real path: a native EXTRA_TEXT share lands and is never consumed (the
    // composer's banner is still awaiting a decision), then a SECOND share
    // arrives via the fragment route before the first is acted on.
    window.sessionStorage.setItem(STORAGE_KEY, "first share");
    setHash("#shared-text=second%20share");

    captureIncomingShareFragment();

    expect(window.sessionStorage.getItem(STORAGE_KEY)).toBe("first share\nsecond share");
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

describe("receiveNativeSharedText", () => {
  beforeEach(reset);
  afterEach(reset);

  it("persists durably AND applies immediately when the new-chat composer is current", () => {
    useChatStore.setState({ conversationId: null });

    receiveNativeSharedText("hello from native share");

    expect(peekPendingShareText()).toBe("hello from native share");
    expect(useChatStore.getState().pendingComposerText).toBe("hello from native share");
  });

  it("persists durably but does NOT apply into an already-open conversation", () => {
    useChatStore.setState({ conversationId: "conv_existing" });

    receiveNativeSharedText("hello from native share");

    expect(peekPendingShareText()).toBe("hello from native share");
    expect(useChatStore.getState().pendingComposerText).toBeNull();
  });

  it("merges with an unconsumed durable share rather than overwriting it", () => {
    // Two native shares in a row before the first was ever drained (e.g.
    // both arrived while conversationId was already non-null and held).
    useChatStore.setState({ conversationId: "conv_existing" });
    receiveNativeSharedText("first");
    receiveNativeSharedText("second");

    expect(peekPendingShareText()).toBe("first\nsecond");
  });

  it("re-applies the full merged text, not just the latest increment, once landing on a new chat", () => {
    useChatStore.setState({ conversationId: "conv_existing" });
    receiveNativeSharedText("first");
    receiveNativeSharedText("second");

    useChatStore.setState({ conversationId: null });
    receiveNativeSharedText("third");

    // pendingComposerText must reflect ALL three, not just "third" -- a
    // consumer draining pendingComposerText alone (not re-peeking storage)
    // would otherwise silently drop "first" and "second".
    expect(useChatStore.getState().pendingComposerText).toBe("first\nsecond\nthird");
  });

  it("ignores an empty string", () => {
    useChatStore.setState({ conversationId: null });

    receiveNativeSharedText("");

    expect(peekPendingShareText()).toBeNull();
    expect(useChatStore.getState().pendingComposerText).toBeNull();
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

  it("subscribes to native shared-text delivery exactly once for its lifetime", () => {
    useChatStore.setState({ conversationId: null });
    const { unmount } = renderHook(() => useShareIntake());

    expect(textSubscribers).toHaveLength(1);

    textSubscribers[0]("hello from a live native share");
    expect(useChatStore.getState().pendingComposerText).toBe("hello from a live native share");

    unmount();
    expect(textSubscribers).toHaveLength(0);
  });
});
