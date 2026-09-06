import { describe, expect, it } from "vitest";
import {
  captureActiveConversationScroll,
  getConversationScrollPosition,
  isConversationScrollPositionRestored,
  registerActiveConversationScroller,
  restoreConversationScrollPosition,
  saveConversationScrollPosition,
} from "./conversationScrollPositions";

function scroller(scrollTop: number): HTMLElement {
  const element = document.createElement("div");
  Object.defineProperties(element, {
    scrollTop: { configurable: true, value: scrollTop, writable: true },
    scrollHeight: { configurable: true, value: 2400 },
    clientHeight: { configurable: true, value: 800 },
  });
  return element;
}

describe("conversation scroll positions", () => {
  it("captures the rendered session at the switch boundary", () => {
    const element = scroller(640);
    const unregister = registerActiveConversationScroller("conv-outgoing", element);

    captureActiveConversationScroll("conv-outgoing");

    expect(getConversationScrollPosition("conv-outgoing")).toEqual({
      scrollTop: 640,
      wasAtBottom: false,
    });
    unregister();
  });

  it("does not associate shared DOM with a store session that has not rendered", () => {
    const element = scroller(640);
    const unregister = registerActiveConversationScroller("conv-rendered", element);

    captureActiveConversationScroll("conv-store-leading");

    expect(getConversationScrollPosition("conv-store-leading")).toBeUndefined();
    unregister();
  });

  it("restores a user-message anchor after transcript height changes", () => {
    const element = scroller(640);
    const message = document.createElement("div");
    message.dataset.role = "user";
    message.dataset.userMessageId = "message-1";
    element.append(message);
    Object.defineProperty(message, "offsetTop", { configurable: true, value: 560 });
    saveConversationScrollPosition("conv-anchored", element);

    element.scrollTop = 1600;
    Object.defineProperty(message, "offsetTop", { configurable: true, value: 1060 });
    const saved = getConversationScrollPosition("conv-anchored");
    expect(saved).toBeDefined();

    restoreConversationScrollPosition(element, saved!);

    expect(element.scrollTop).toBe(1140);
  });

  it("does not write a temporary fallback while the saved anchor is missing", () => {
    const element = scroller(0);
    const position = {
      scrollTop: 5000,
      anchorMessageId: "message-delayed",
      anchorOffset: -80,
    };

    expect(restoreConversationScrollPosition(element, position)).toBe(false);

    expect(element.scrollTop).toBe(0);
  });

  it("reports that restoration must retry until its anchor renders", () => {
    const element = scroller(1600);
    const position = {
      scrollTop: 640,
      anchorMessageId: "message-delayed",
      anchorOffset: -80,
    };

    expect(restoreConversationScrollPosition(element, position)).toBe(false);

    const message = document.createElement("div");
    message.dataset.role = "user";
    message.dataset.userMessageId = "message-delayed";
    Object.defineProperty(message, "offsetTop", { configurable: true, value: 1060 });
    element.append(message);

    expect(restoreConversationScrollPosition(element, position)).toBe(true);
    expect(element.scrollTop).toBe(1140);
  });

  it("does not settle when the saved position is beyond the current scroll range", () => {
    // Content hasn't fully rendered: scrollHeight is small, so maxScrollTop = 0.
    const element = document.createElement("div");
    Object.defineProperties(element, {
      scrollTop: { configurable: true, value: 0, writable: true },
      scrollHeight: { configurable: true, value: 400 },
      clientHeight: { configurable: true, value: 800 },
    });
    // Saved position was at the bottom of a fully-rendered transcript.
    const position = { scrollTop: 5000 };

    // The target is clamped to 0 (maxScrollTop = 0), scrollTop matches, but
    // the restore must NOT consider itself settled — content will grow.
    expect(isConversationScrollPositionRestored(element, position)).toBe(false);
  });

  it("does not settle when an anchored position is beyond the current scroll range", () => {
    const element = document.createElement("div");
    const message = document.createElement("div");
    message.dataset.role = "user";
    message.dataset.userMessageId = "msg-bottom";
    Object.defineProperty(message, "offsetTop", { configurable: true, value: 4800 });
    element.append(message);
    Object.defineProperties(element, {
      scrollTop: { configurable: true, value: 0, writable: true },
      scrollHeight: { configurable: true, value: 400 },
      clientHeight: { configurable: true, value: 800 },
    });
    // Saved at scrollTop=5000 with an anchor near the bottom.
    const position = { scrollTop: 5000, anchorMessageId: "msg-bottom", anchorOffset: -200 };

    // Anchor is found, but anchorTarget (5000) > maxScrollTop (0).
    expect(isConversationScrollPositionRestored(element, position)).toBe(false);
  });

  it("settles once content grows enough to hold the saved position", () => {
    const element = document.createElement("div");
    Object.defineProperties(element, {
      scrollTop: { configurable: true, value: 5000, writable: true },
      scrollHeight: { configurable: true, value: 5800 },
      clientHeight: { configurable: true, value: 800 },
    });
    const position = { scrollTop: 5000 };

    // maxScrollTop = 5000, saved scrollTop = 5000, scrollTop matches.
    expect(isConversationScrollPositionRestored(element, position)).toBe(true);
  });
});
