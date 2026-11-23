import { cleanup, render, renderHook, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { useChatPaneViewToggleHost } from "../chatPaneToolbarHost";
import { ChatPaneStrip } from "./ChatPaneStrip";

afterEach(cleanup);

describe("ChatPaneStrip", () => {
  it("names the chat pane for screen readers only, with no actions", () => {
    render(<ChatPaneStrip conversationId="session-1" />);
    const strip = screen.getByRole("toolbar", { name: "Chat pane" });
    expect(screen.getByTestId("pane-strip-name")).toHaveClass("sr-only");
    expect(strip.querySelectorAll("button")).toHaveLength(0);
  });

  it("offers its row's end as the Chat/Terminal switch's host while it shows", () => {
    const { unmount } = render(<ChatPaneStrip conversationId="session-1" />);
    const host = renderHook(() => useChatPaneViewToggleHost("session-1"));
    expect(host.result.current).toHaveAttribute("data-pane-strip-view-toggle-host");
    expect(renderHook(() => useChatPaneViewToggleHost("session-2")).result.current).toBeNull();
    unmount();
    host.rerender();
    expect(host.result.current).toBeNull();
  });
});
