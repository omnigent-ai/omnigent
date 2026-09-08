// Unit tests for useShareIntake — the OS-share URL-fragment boot handoff
// that queues text into the composer via chatStore.pendingComposerText.

import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { useChatStore } from "@/store/chatStore";
import { useShareIntake } from "./shareIntake";

function setHash(hash: string): void {
  window.history.replaceState(null, "", "/" + hash);
}

describe("useShareIntake", () => {
  beforeEach(() => {
    setHash("");
    useChatStore.getState().clearPendingComposerText();
  });

  afterEach(() => {
    setHash("");
  });

  it("queues decoded text from a #shared-text= fragment", () => {
    setHash("#shared-text=hello%20world");

    renderHook(() => useShareIntake());

    expect(useChatStore.getState().pendingComposerText).toBe("hello world");
  });

  it("strips the fragment from the URL so a refresh can't re-apply it", () => {
    setHash("#shared-text=hello");

    renderHook(() => useShareIntake());

    expect(window.location.hash).toBe("");
  });

  it("leaves pendingComposerText untouched when there is no share fragment", () => {
    setHash("#some-other-fragment");

    renderHook(() => useShareIntake());

    expect(useChatStore.getState().pendingComposerText).toBeNull();
    // An unrelated fragment isn't ours to clear.
    expect(window.location.hash).toBe("#some-other-fragment");
  });

  it("drops malformed percent-encoding rather than queuing garbage", () => {
    setHash("#shared-text=%E0%A4%A");

    renderHook(() => useShareIntake());

    expect(useChatStore.getState().pendingComposerText).toBeNull();
    expect(window.location.hash).toBe("");
  });

  it("does not re-queue on rerender (runs once at boot)", () => {
    setHash("#shared-text=first");
    const { rerender } = renderHook(() => useShareIntake());
    expect(useChatStore.getState().pendingComposerText).toBe("first");

    useChatStore.getState().clearPendingComposerText();
    setHash("#shared-text=second");
    rerender();

    expect(useChatStore.getState().pendingComposerText).toBeNull();
  });
});
