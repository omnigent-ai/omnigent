// Unit tests for the OS-share FILE intake pipeline (useSharedFileIntake):
// decoding base64 payloads into real Files, and the conversationId-gated
// queue (mirrors shareIntake.test.ts's useShareIntake tests, but the source
// here is a live bridge subscription rather than a durable sessionStorage
// read -- see shareFileIntake.ts's header comment for why).

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useChatStore } from "@/store/chatStore";
import type { NativeSharedFile } from "@/lib/nativeBridge";

type SharedFilesCallback = (files: NativeSharedFile[]) => void;
const subscribers: SharedFilesCallback[] = [];

vi.mock("@/lib/nativeBridge", () => ({
  onNativeSharedFiles: vi.fn((callback: SharedFilesCallback) => {
    subscribers.push(callback);
    return () => {
      const i = subscribers.indexOf(callback);
      if (i >= 0) subscribers.splice(i, 1);
    };
  }),
}));

const { useSharedFileIntake } = await import("./shareFileIntake");

function emit(files: NativeSharedFile[]): void {
  act(() => {
    for (const cb of [...subscribers]) cb(files);
  });
}

function b64(text: string): string {
  return btoa(text);
}

function reset(): void {
  subscribers.length = 0;
  useChatStore.setState({ conversationId: null, pendingComposerFiles: null });
}

describe("useSharedFileIntake", () => {
  beforeEach(reset);
  afterEach(reset);

  it("decodes and queues files into the composer when landing on the new-chat route", async () => {
    useChatStore.setState({ conversationId: null });
    renderHook(() => useSharedFileIntake());

    emit([{ name: "note.txt", mimeType: "text/plain", base64: b64("hello") }]);

    const files = useChatStore.getState().pendingComposerFiles;
    expect(files).toHaveLength(1);
    expect(files?.[0].name).toBe("note.txt");
    expect(files?.[0].type).toBe("text/plain");
    await expect(files?.[0].text()).resolves.toBe("hello");
  });

  it("drops a malformed payload without throwing, still queues a valid sibling", () => {
    useChatStore.setState({ conversationId: null });
    renderHook(() => useSharedFileIntake());

    expect(() =>
      emit([
        { name: "bad.txt", mimeType: "text/plain", base64: "not valid base64!!" },
        { name: "good.txt", mimeType: "text/plain", base64: b64("ok") },
      ]),
    ).not.toThrow();

    const files = useChatStore.getState().pendingComposerFiles;
    expect(files).toHaveLength(1);
    expect(files?.[0].name).toBe("good.txt");
  });

  it("ignores an empty emission", () => {
    useChatStore.setState({ conversationId: null });
    renderHook(() => useSharedFileIntake());

    emit([]);

    expect(useChatStore.getState().pendingComposerFiles).toBeNull();
  });

  it("does NOT queue into an already-open conversation (intended recipient is a new chat)", () => {
    // The real path this guards: a share arrives (app foregrounded via
    // onNewIntent) while the user already has a conversation open.
    useChatStore.setState({ conversationId: "conv_existing" });
    renderHook(() => useSharedFileIntake());

    emit([{ name: "note.txt", mimeType: "text/plain", base64: b64("hello") }]);

    expect(useChatStore.getState().pendingComposerFiles).toBeNull();
  });

  it("re-queues held files once conversationId actually transitions to null later", () => {
    useChatStore.setState({ conversationId: "conv_existing" });
    const { rerender } = renderHook(() => useSharedFileIntake());
    emit([{ name: "note.txt", mimeType: "text/plain", base64: b64("hello") }]);
    expect(useChatStore.getState().pendingComposerFiles).toBeNull();

    useChatStore.setState({ conversationId: null });
    rerender();

    const files = useChatStore.getState().pendingComposerFiles;
    expect(files).toHaveLength(1);
    expect(files?.[0].name).toBe("note.txt");
  });

  it("unsubscribes on unmount", () => {
    useChatStore.setState({ conversationId: null });
    const { unmount } = renderHook(() => useSharedFileIntake());
    expect(subscribers).toHaveLength(1);

    unmount();

    expect(subscribers).toHaveLength(0);
  });
});
