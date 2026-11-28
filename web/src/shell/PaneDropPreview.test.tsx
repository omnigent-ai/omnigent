import { act, cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { resetAppPanes, setPendingAppPaneDrop } from "./appPanes";
import { LoneChatPaneDropPreview } from "./PaneDropPreview";

const RIGHT_HALF = {
  targetKey: "terminal:agent",
  side: "right" as const,
  area: { left: 50, top: 0, width: 50, height: 100 },
};

afterEach(() => {
  cleanup();
  resetAppPanes();
  document.body.innerHTML = "";
});

/** The session's panes view, hidden under the chat, at 100,50 and 800×600 on screen. */
function hiddenPanes(conversationId: string) {
  const root = document.createElement("div");
  root.dataset.splitPanes = conversationId;
  const box = { left: 100, top: 50, width: 800, height: 600 };
  root.getBoundingClientRect = () => ({ ...box }) as DOMRect;
  document.body.appendChild(root);
  return box;
}

describe("LoneChatPaneDropPreview", () => {
  it("draws a tab's drop over the spot the hidden panes take on screen", () => {
    hiddenPanes("c1");
    render(<LoneChatPaneDropPreview conversationId="c1" />);
    expect(document.querySelector("[data-pane-drop]")).toBeNull();
    act(() => setPendingAppPaneDrop("c1", RIGHT_HALF));
    const frame = document.querySelector<HTMLElement>("[data-lone-chat-pane-drop]")!;
    expect(frame.style).toMatchObject({
      left: "100px",
      top: "50px",
      width: "800px",
      height: "600px",
    });
    const preview = frame.querySelector<HTMLElement>("[data-pane-drop]")!;
    expect(preview.dataset.paneDrop).toBe("terminal:agent:right");
    expect(preview.style).toMatchObject({ left: "50%", width: "50%" });
    act(() => setPendingAppPaneDrop("c1", null));
    expect(document.querySelector("[data-pane-drop]")).toBeNull();
  });

  it("follows the hidden panes when the window resizes during the drag", () => {
    const box = hiddenPanes("c1");
    render(<LoneChatPaneDropPreview conversationId="c1" />);
    act(() => setPendingAppPaneDrop("c1", RIGHT_HALF));
    box.left = 300;
    box.width = 600;
    act(() => {
      window.dispatchEvent(new Event("resize"));
    });
    const frame = document.querySelector<HTMLElement>("[data-lone-chat-pane-drop]")!;
    expect(frame.style).toMatchObject({ left: "300px", width: "600px" });
  });

  it("draws nothing for another session's drop, or without the session's panes", () => {
    hiddenPanes("c2");
    render(<LoneChatPaneDropPreview conversationId="c1" />);
    act(() => setPendingAppPaneDrop("c2", RIGHT_HALF));
    expect(document.querySelector("[data-pane-drop]")).toBeNull();
    act(() => setPendingAppPaneDrop("c1", RIGHT_HALF));
    expect(document.querySelector("[data-pane-drop]")).toBeNull();
  });
});
