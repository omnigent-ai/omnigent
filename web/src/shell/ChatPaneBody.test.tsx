import { render, screen } from "@testing-library/react";
import { useEffect, useRef } from "react";
import { describe, expect, it } from "vitest";

import { CHAT_PANE_BODY } from "@/lib/paneSurfaces";

import { ChatPaneBody } from "./ChatPaneBody";

describe("ChatPaneBody", () => {
  it("marks the chat in its column where the panes view shows it, so pane keys find it", () => {
    const { container } = render(
      <ChatPaneBody sessionId="session-a" host={null} marked>
        <p>transcript</p>
      </ChatPaneBody>,
    );
    const transcript = screen.getByText("transcript");
    expect(container).toContainElement(transcript);
    expect(transcript.closest(CHAT_PANE_BODY)).toHaveClass("contents");
    expect(transcript.closest(CHAT_PANE_BODY)).toHaveAttribute("data-chat-pane-body", "session-a");
  });

  it("renders the chat in its column without a pane to render into", () => {
    const { container } = render(
      <ChatPaneBody sessionId="session-a" host={null}>
        <p>transcript</p>
      </ChatPaneBody>,
    );
    expect(container).toContainElement(screen.getByText("transcript"));
    expect(document.querySelector(CHAT_PANE_BODY)).toBeNull();
  });

  it("renders the chat in the chat pane's body, marked as the chat pane", () => {
    const host = document.body.appendChild(document.createElement("div"));
    const { container } = render(
      <ChatPaneBody sessionId="session-a" host={host}>
        <p>transcript</p>
      </ChatPaneBody>,
    );
    const transcript = screen.getByText("transcript");
    expect(container).not.toContainElement(transcript);
    expect(transcript.closest(CHAT_PANE_BODY)?.parentElement).toBe(host);
    host.remove();
  });

  it("keeps the composer, its focus and the transcript's scroll as a pane opens and closes", () => {
    const host = document.body.appendChild(document.createElement("div"));
    let mounts = 0;
    function Chat() {
      const scroller = useRef<HTMLDivElement>(null);
      useEffect(() => {
        mounts += 1;
        // Found in the page as it mounts, as the transcript measures itself.
        expect(scroller.current?.isConnected).toBe(true);
      }, []);
      return (
        <>
          <div ref={scroller} data-testid="scroller" style={{ overflow: "auto" }} />
          <textarea aria-label="Message" />
        </>
      );
    }
    const chat = (paneHost: HTMLElement | null) => (
      <ChatPaneBody sessionId="session-a" host={paneHost} marked>
        <Chat />
      </ChatPaneBody>
    );
    const view = render(chat(null));
    const composer = screen.getByRole("textbox", { name: "Message" });
    composer.focus();
    screen.getByTestId("scroller").scrollTop = 120;

    view.rerender(chat(host));
    expect(host).toContainElement(composer);
    expect(composer).toHaveFocus();
    expect(screen.getByTestId("scroller").scrollTop).toBe(120);

    view.rerender(chat(null));
    expect(view.container).toContainElement(composer);
    expect(screen.getByRole("textbox", { name: "Message" })).toBe(composer);
    expect(composer).toHaveFocus();
    expect(mounts).toBe(1);
    view.unmount();
    expect(host).toBeEmptyDOMElement();
    host.remove();
  });
});
