import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, useLocation } from "react-router-dom";
import { writeOpenSubagentsInPanel } from "@/lib/subagentPanelPreferences";
import { SubagentPanelOpenerContext } from "@/shell/subagentPanelOpener";
import { BubbleView } from "@/pages/ChatPage";
import { useChatStore } from "@/store/chatStore";
import { buildBubbles } from "@/lib/renderItems";
import { BlockStream } from "@/lib/blockStream";
import { itemsToBlocks } from "@/lib/itemsToBlocks";
import { parseEvent } from "@/lib/sse";

afterEach(() => {
  cleanup();
  localStorage.clear();
});
beforeEach(() => useChatStore.setState({ sessionStatus: "idle" }));

describe("sub-agent activity", () => {
  it.each([
    ["delegated", undefined, "Started"],
    ["returned", undefined, "Completed"],
    ["returned", "failed", "Failed"],
    ["returned", "cancelled", "Stopped"],
  ] as const)("renders %s/%s live and after reload as %s", (phase, status, label) => {
    const item = {
      id: "activity",
      type: "resource_event",
      status: "completed",
      response_id: "activity_turn",
      event_type: `session.subagent.${phase}`,
      resource_type: "session",
      resource_id: "conv_child",
      resource: { title: "Research public positioning", status },
    };
    const event = parseEvent("response.output_item.done", { item });
    expect(event).not.toBeNull();
    const live = buildBubbles(new BlockStream().reduceSync([event!]), null);
    expect(buildBubbles(itemsToBlocks([item]), null)).toEqual(live);
    expect(live).toHaveLength(1);
    render(
      <MemoryRouter
        initialEntries={["/c/parent?file=README.md&view=terminal&message=msg_parent&debug=1"]}
      >
        <BubbleView bubble={live[0]!} />
      </MemoryRouter>,
    );
    expect(screen.getByTestId("subagent-activity")).toHaveTextContent(
      `${label} Research public positioning`,
    );
    expect(screen.getByRole("link")).toHaveAttribute("href", "/c/conv_child?debug=1&panel=agents");
    expect(screen.queryByTestId("message-bubble")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copy" })).not.toBeInTheDocument();
  });

  it.each([
    { event_type: "session.resource.created", resource_type: "terminal", resource_id: "term_1" },
    { event_type: "session.subagent.delegated", resource_type: "session", resource_id: "" },
  ])("ignores unrelated or malformed resource events: %j", (fields) => {
    const item = {
      id: "resource",
      type: "resource_event",
      response_id: "activity",
      status: "completed",
      ...fields,
    };
    expect(itemsToBlocks([item])).toEqual([]);
    expect(parseEvent("response.output_item.done", { item })).toBeNull();
  });

  it.each([
    [true, "/c/parent"],
    [false, "/c/conv_child"],
  ])("with the panel setting %s, a click lands on %s", (enabled, pathname) => {
    writeOpenSubagentsInPanel(enabled);
    const opener = vi.fn(() => true);
    const item = {
      id: "activity",
      type: "resource_event",
      status: "completed",
      response_id: "activity_turn",
      event_type: "session.subagent.delegated",
      resource_type: "session",
      resource_id: "conv_child",
      resource: { title: "Research public positioning" },
    };
    const [bubble] = buildBubbles(itemsToBlocks([item]), null);
    function CurrentPath() {
      return <output data-testid="current-path">{useLocation().pathname}</output>;
    }
    render(
      <MemoryRouter initialEntries={["/c/parent"]}>
        <SubagentPanelOpenerContext.Provider value={opener}>
          <BubbleView bubble={bubble!} />
        </SubagentPanelOpenerContext.Provider>
        <CurrentPath />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("link"));

    expect(screen.getByTestId("current-path")).toHaveTextContent(pathname);
    if (enabled) expect(opener).toHaveBeenCalledWith("conv_child");
    else expect(opener).not.toHaveBeenCalled();
  });
});
