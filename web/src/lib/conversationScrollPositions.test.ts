import { describe, expect, it } from "vitest";
import {
  captureActiveConversationScroll,
  getConversationScrollPosition,
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
});
