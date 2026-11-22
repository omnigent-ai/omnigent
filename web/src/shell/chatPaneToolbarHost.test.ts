import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  claimChatPaneViewToggleHost,
  releaseChatPaneViewToggleHost,
  useChatPaneViewToggleHost,
  useChatPaneViewToggleHostRef,
} from "./chatPaneToolbarHost";

const hostOf = (conversationId: string) =>
  renderHook(() => useChatPaneViewToggleHost(conversationId)).result.current;

describe("the chat pane's Chat/Terminal switch host", () => {
  it("keeps the newer strip's spot when the older strip lets go after it", () => {
    // Back to Chat: the lone chat strip mounts, then the Terminal view's strip releases.
    const terminalStrip = document.createElement("span");
    const chatStrip = document.createElement("span");
    claimChatPaneViewToggleHost("session-a", terminalStrip);
    claimChatPaneViewToggleHost("session-a", chatStrip);
    releaseChatPaneViewToggleHost("session-a", terminalStrip);
    expect(hostOf("session-a")).toBe(chatStrip);
    releaseChatPaneViewToggleHost("session-a", chatStrip);
    expect(hostOf("session-a")).toBeNull();
  });

  it("hands the spot back to an earlier claim still held", () => {
    const first = document.createElement("span");
    const second = document.createElement("span");
    claimChatPaneViewToggleHost("session-b", first);
    claimChatPaneViewToggleHost("session-b", second);
    releaseChatPaneViewToggleHost("session-b", second);
    expect(hostOf("session-b")).toBe(first);
    releaseChatPaneViewToggleHost("session-b", first);
  });

  it("releases from a ref callback only the element that callback claimed", () => {
    const { result: terminalRef } = renderHook(() => useChatPaneViewToggleHostRef("session-c"));
    const { result: chatRef } = renderHook(() => useChatPaneViewToggleHostRef("session-c"));
    const terminalStrip = document.createElement("span");
    const chatStrip = document.createElement("span");
    terminalRef.current(terminalStrip);
    chatRef.current(chatStrip);
    terminalRef.current(null);
    expect(hostOf("session-c")).toBe(chatStrip);
    chatRef.current(null);
    expect(hostOf("session-c")).toBeNull();
  });
});
