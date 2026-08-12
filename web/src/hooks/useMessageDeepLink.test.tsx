import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { ReactNode } from "react";
import { useChatStore } from "@/store/chatStore";
import { useMessageDeepLink } from "./useMessageDeepLink";

const releaseScrollLock = vi.hoisted(() => vi.fn());
vi.mock("@/components/ai-elements/conversation", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/ai-elements/conversation")>();
  return {
    ...actual,
    releaseConversationScrollLock: releaseScrollLock,
  };
});

if (!("scrollIntoView" in Element.prototype)) {
  Object.defineProperty(Element.prototype, "scrollIntoView", {
    configurable: true,
    writable: true,
    value: () => {},
  });
}

const SETTLE_MS = 200;

function wrapperFor(path: string) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/c/:conversationId" element={children} />
        </Routes>
      </MemoryRouter>
    );
  };
}

describe("useMessageDeepLink", () => {
  let scrollSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.useFakeTimers();
    releaseScrollLock.mockClear();
    scrollSpy = vi.spyOn(Element.prototype, "scrollIntoView").mockImplementation(() => {});
    useChatStore.setState({
      flashItemId: null,
      loadingConversation: false,
      hasMoreHistory: false,
      loadingMoreHistory: false,
      historyGeneration: 0,
    });
    document.body.innerHTML = "";
  });

  afterEach(() => {
    scrollSpy.mockRestore();
    document.body.innerHTML = "";
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it("scrolls to and flashes the message from ?message=", () => {
    document.body.innerHTML = `<div data-message-id="msg_1">hello</div>`;
    renderHook(() => useMessageDeepLink("conv_1"), {
      wrapper: wrapperFor("/c/conv_1?message=msg_1"),
    });

    expect(scrollSpy).toHaveBeenCalledOnce();
    expect(releaseScrollLock).toHaveBeenCalledOnce();
    const target = scrollSpy.mock.contexts[0] as Element;
    expect(target.getAttribute("data-message-id")).toBe("msg_1");
    act(() => vi.advanceTimersByTime(SETTLE_MS));
    expect(useChatStore.getState().flashItemId).toBe("msg_1");
  });

  it("releases the stick-to-bottom lock before scrolling a tall transcript", () => {
    // Multi-message DOM: target is not the last bubble (the open lands at bottom).
    document.body.innerHTML = `
      <div data-message-id="msg_old">older</div>
      <div data-message-id="msg_mid">middle</div>
      <div data-message-id="msg_new">newest</div>
    `;
    renderHook(() => useMessageDeepLink("conv_1"), {
      wrapper: wrapperFor("/c/conv_1?message=msg_old"),
    });

    expect(releaseScrollLock).toHaveBeenCalledOnce();
    const target = scrollSpy.mock.contexts[0] as Element;
    expect(target.getAttribute("data-message-id")).toBe("msg_old");
    act(() => vi.advanceTimersByTime(SETTLE_MS));
    expect(useChatStore.getState().flashItemId).toBe("msg_old");
  });

  it("pages older history when the target is not yet in the DOM", async () => {
    const loadMoreHistory = vi.fn(async () => {
      useChatStore.setState({ loadingMoreHistory: false, hasMoreHistory: false });
      document.body.innerHTML = `<div data-message-id="old_msg">older</div>`;
      useChatStore.setState((s) => ({ historyGeneration: s.historyGeneration + 1 }));
    });
    useChatStore.setState({
      hasMoreHistory: true,
      loadMoreHistory,
    });

    const { rerender } = renderHook(() => useMessageDeepLink("conv_1"), {
      wrapper: wrapperFor("/c/conv_1?message=old_msg"),
    });

    expect(loadMoreHistory).toHaveBeenCalledOnce();
    await act(async () => {
      await loadMoreHistory.mock.results[0]?.value;
    });
    // Re-run after history lands.
    rerender();
    expect(scrollSpy).toHaveBeenCalled();
    expect(releaseScrollLock).toHaveBeenCalled();
    const target = scrollSpy.mock.contexts.at(-1) as Element;
    expect(target.getAttribute("data-message-id")).toBe("old_msg");
  });

  it("does nothing when ?message= is absent", () => {
    document.body.innerHTML = `<div data-message-id="msg_1">hello</div>`;
    renderHook(() => useMessageDeepLink("conv_1"), {
      wrapper: wrapperFor("/c/conv_1"),
    });
    expect(scrollSpy).not.toHaveBeenCalled();
    expect(releaseScrollLock).not.toHaveBeenCalled();
  });
});
