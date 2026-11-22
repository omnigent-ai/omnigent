import { render, screen } from "@testing-library/react";
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
});
